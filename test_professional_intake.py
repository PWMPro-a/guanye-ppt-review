"""Unit tests for the launcher's private coworker-intake queue."""
from __future__ import annotations

import base64
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from cloud.ai_gateway import HTTPResponse
from cloud.professional_import import ImportErrorResponse
from cloud.professional_intake import (
    CLAIM_RPC,
    COMPLETE_RPC,
    FAIL_RPC,
    STORAGE_PREFIX,
    IntakeCompletionError,
    IntakeTask,
    IntakeTransient,
    ProfessionalIntakeWorker,
)
from cloud.professional_launcher import start_workers
from cloud.professional_review_worker import Credentials

WORKSPACE = "937c868a-8e42-4043-b3e4-c7cfa05076b5"
TASK_ID = "7cd9e64b-72db-49b6-8e81-92a39f3b16fd"
LEASE = "51b7b1ad-e5ae-4b53-94bc-3dcb5e9c8d72"
ACTOR = "299454c0-a536-4f51-8c21-0034cb2495a3"
DECK_ID = "import-123e4567-e89b-12d3-a456-426614174000"
CONFIG = {
    "url": "https://fixture.supabase.co",
    "anonKey": "sb_publishable_fixturetesting",
    "workspaceId": WORKSPACE,
    "singleJudge": True,
}
PDF = b"%PDF-1.7\nfixture presentation\n"


class FakeSecrets:
    email = "judge@example.com"
    password = "dpapi-password-never-log"


class FakeTokens:
    def __init__(self):
        self.access_calls = 0
        self.refresh_calls = 0
        self.token = "access-secret-token"

    def access_token(self):
        self.access_calls += 1
        return self.token

    def refresh_after_unauthorized(self, rejected):
        self.refresh_calls += 1
        self.token = "refreshed-secret-token"
        return self.token


class FakeImporter:
    def __init__(self, *, result=None, error=None):
        self.result = result or {"ok": True, "deckId": DECK_ID}
        self.error = error
        self.authorize_calls = []
        self.import_calls = []

    def authorize(self, bearer):
        self.authorize_calls.append(bearer)
        return ACTOR

    def import_one(self, actor, bearer, value):
        self.import_calls.append((actor, bearer, value))
        if self.error is not None:
            raise self.error
        return self.result


class FakeCloud:
    def __init__(self, task, payload):
        self.task = task
        self.payload = payload
        self.requests = []
        self.claim_failures = 0
        self.complete_status = 200
        self.complete_calls = []
        self.fail_calls = []

    @staticmethod
    def _response(status, value):
        return HTTPResponse(status, {"Content-Type": "application/json"},
                            json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def __call__(self, method, url, headers, body, _timeout):
        parsed = urlsplit(url)
        self.requests.append((method, parsed.path, dict(headers), body))
        if parsed.path == CLAIM_RPC:
            if self.claim_failures:
                self.claim_failures -= 1
                raise OSError("fixture network interruption")
            return self._response(200, {"ok": True, "task": self.task})
        if parsed.path.startswith(STORAGE_PREFIX):
            return HTTPResponse(200, {}, self.payload)
        value = json.loads(body.decode("utf-8")) if body else {}
        if parsed.path == COMPLETE_RPC:
            self.complete_calls.append(value)
            if self.complete_status != 200:
                return self._response(self.complete_status, {"ok": False, "error": "lease expired"})
            return self._response(200, {"ok": True, "intakeId": value.get("p_id"),
                                       "deckId": value.get("p_deck_id"), "status": "completed"})
        if parsed.path == FAIL_RPC:
            self.fail_calls.append(value)
            return self._response(200, {"ok": True, "intakeId": value.get("p_id"), "status": "failed"})
        raise AssertionError(f"unexpected fixture path: {parsed.path}")


def make_task(payload=PDF, *, size=None, digest=None, original_name="原始 资料.pdf",
              content_type="application/pdf", title="原始资料"):
    return {
        "id": TASK_ID,
        "workspaceId": WORKSPACE,
        "title": title,
        "originalName": original_name,
        "objectPath": f"{WORKSPACE}/intake/{TASK_ID}/{original_name}",
        "contentType": content_type,
        "sizeBytes": len(payload) if size is None else size,
        "sha256": sha256(payload).hexdigest() if digest is None else digest,
        "aiAllowed": False,
        "leaseToken": LEASE,
    }


def make_worker(cloud, importer=None, tokens=None):
    return ProfessionalIntakeWorker(
        CONFIG,
        FakeSecrets(),
        tokens=tokens or FakeTokens(),
        transport=cloud,
        importer=importer or FakeImporter(),
        worker_name="fixture-intake",
        poll_seconds=0.05,
        backoff_seconds=0.05,
        max_backoff_seconds=0.1,
    )


class IntakeWorkerTests(unittest.TestCase):
    def test_dpapi_launcher_uses_loaded_secrets_and_stops_intake(self):
        with tempfile.TemporaryDirectory(prefix="guanye-intake-launcher-") as temporary:
            root = Path(temporary)
            secrets = Credentials("judge@example.com", "dpapi-password-never-log", "w" * 32, "m" * 32)
            worker = Mock()
            runtime = {"workers": {"review": False, "image": False, "intake": True}}
            with patch("cloud.professional_launcher.load_secrets", return_value=secrets) as loaded, \
                    patch("cloud.professional_launcher.build_intake_worker", return_value=worker) as built:
                with ExitStack() as stack:
                    start_workers(runtime, CONFIG, root, stack)
                loaded.assert_called_once_with(CONFIG)
                worker.start.assert_called_once_with()
                worker.stop.assert_called_once_with()
                self.assertIs(built.call_args.args[1], secrets)
                self.assertTrue(callable(built.call_args.kwargs["log"]))
            log_text = (root / "data" / "professional.log").read_text(encoding="utf-8")
            self.assertNotIn("dpapi-password-never-log", log_text)
            self.assertNotIn("w" * 32, log_text)

    def test_worker_builds_supabase_token_provider_from_loaded_credentials(self):
        with patch("cloud.professional_intake.SupabaseTokenProvider") as provider, \
                patch("cloud.professional_intake.ProfessionalImporter"):
            provider.return_value = FakeTokens()
            worker = ProfessionalIntakeWorker(CONFIG, FakeSecrets(), worker_name="fixture-intake")
        auth = provider.call_args.args[0]
        self.assertEqual(auth.supabase_url, CONFIG["url"])
        self.assertEqual(auth.anon_key, CONFIG["anonKey"])
        self.assertEqual(auth.email, FakeSecrets.email)
        self.assertEqual(auth.password, FakeSecrets.password)
        self.assertIsInstance(worker.tokens, FakeTokens)

    def test_claim_success_uses_authenticated_workspace_rpc(self):
        cloud = FakeCloud(make_task(), PDF)
        worker = make_worker(cloud)
        task = worker.claim()
        self.assertIsInstance(task, IntakeTask)
        self.assertEqual(task.id, TASK_ID)
        self.assertEqual(task.original_name, "原始 资料.pdf")
        method, path, headers, body = cloud.requests[0]
        self.assertEqual((method, path), ("POST", CLAIM_RPC))
        self.assertEqual(headers["apikey"], CONFIG["anonKey"])
        self.assertEqual(headers["Authorization"], "Bearer access-secret-token")
        self.assertEqual(json.loads(body), {
            "p_workspace_id": WORKSPACE,
            "p_worker": "fixture-intake",
            "p_lease_seconds": 1800,
        })

    def test_size_hash_and_magic_mismatches_fail_without_import(self):
        malformed = b"not a valid presentation"
        cases = (
            ("size", make_task(PDF, size=len(PDF) + 1), PDF),
            ("sha256", make_task(PDF, digest=sha256(b"different").hexdigest()), PDF),
            ("magic", make_task(malformed), malformed),
        )
        for kind, task_payload, payload in cases:
            with self.subTest(kind=kind):
                cloud = FakeCloud(task_payload, payload)
                importer = FakeImporter()
                worker = make_worker(cloud, importer)
                self.assertEqual(worker.run(threading.Event(), once=True), 1)
                self.assertEqual(importer.import_calls, [])
                self.assertEqual(len(cloud.fail_calls), 1)
                self.assertEqual(cloud.complete_calls, [])
                self.assertLessEqual(len(cloud.fail_calls[0]["p_error"]), 500)
                self.assertNotIn("access-secret-token", cloud.fail_calls[0]["p_error"])
                self.assertNotIn("dpapi-password-never-log", cloud.fail_calls[0]["p_error"])

    def test_ppt_and_pptx_magic_are_accepted(self):
        cases = (
            ("旧版.ppt", "application/vnd.ms-powerpoint", bytes.fromhex("D0CF11E0A1B11AE1") + b"fixture"),
            ("新版.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", b"PKfixture"),
        )
        for name, content_type, payload in cases:
            with self.subTest(name=name):
                cloud = FakeCloud(make_task(payload, original_name=name, content_type=content_type), payload)
                importer = FakeImporter()
                worker = make_worker(cloud, importer)
                self.assertEqual(worker.run(threading.Event(), once=True), 0)
                self.assertEqual(len(importer.import_calls), 1)
                self.assertEqual(len(cloud.complete_calls), 1)

    def test_import_success_completes_exactly_the_returned_deck_id(self):
        cloud = FakeCloud(make_task(), PDF)
        importer = FakeImporter()
        worker = make_worker(cloud, importer)
        self.assertEqual(worker.run(threading.Event(), once=True), 0)
        self.assertEqual(len(importer.import_calls), 1)
        actor, bearer, value = importer.import_calls[0]
        self.assertEqual((actor, bearer), (ACTOR, "access-secret-token"))
        self.assertEqual(value["title"], "原始资料")
        self.assertFalse(value["aiAllowed"])
        self.assertEqual(value["file"]["name"], "原始 资料.pdf")
        self.assertEqual(base64.b64decode(value["file"]["data"]), PDF)
        self.assertEqual(len(cloud.complete_calls), 1)
        self.assertEqual(cloud.complete_calls[0], {
            "p_workspace_id": WORKSPACE,
            "p_id": TASK_ID,
            "p_lease": LEASE,
            "p_deck_id": DECK_ID,
        })

    def test_import_exception_calls_fail_without_complete(self):
        cloud = FakeCloud(make_task(), PDF)
        importer = FakeImporter(error=ImportErrorResponse(422, "本机无法转换该文件"))
        worker = make_worker(cloud, importer)
        self.assertEqual(worker.run(threading.Event(), once=True), 1)
        self.assertEqual(cloud.complete_calls, [])
        self.assertEqual(len(cloud.fail_calls), 1)
        self.assertEqual(cloud.fail_calls[0]["p_id"], TASK_ID)
        self.assertIn("本机无法转换该文件", cloud.fail_calls[0]["p_error"])
        self.assertNotIn("access-secret-token", cloud.fail_calls[0]["p_error"])

    def test_claim_network_error_and_expired_lease_do_not_end_main_loop(self):
        cloud = FakeCloud(make_task(), PDF)
        worker = make_worker(cloud)
        stop = threading.Event()
        calls = []

        def claim():
            calls.append("claim")
            if len(calls) == 1:
                raise IntakeTransient("fixture network interruption")
            if len(calls) == 2:
                return IntakeTask.from_payload(make_task(), WORKSPACE)
            stop.set()
            return None

        def process_task(_task):
            calls.append("process")
            raise IntakeCompletionError("投递租约已失效")

        worker.claim = claim
        worker.process_task = process_task
        self.assertEqual(worker.run(stop), 0)
        self.assertEqual(calls, ["claim", "claim", "process", "claim"])

    def test_stop_event_ends_started_thread(self):
        cloud = FakeCloud(None, b"")
        worker = make_worker(cloud)
        worker.claim = lambda: None
        thread = worker.start()
        self.assertIs(thread, worker._thread)
        self.assertTrue(worker.stop(1.0))
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
