import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from cloud.ai_gateway import DirectTransport, Gateway, GatewayConfig
from cloud.professional_review_worker import Credentials, ReviewBroker, TRACKS, review_one
from cloud.professional_worker_secrets import _path, load_secrets, save_secrets, singleton
from cloud.windows_image_worker import SupabaseAuthConfig, SupabaseTokenProvider

W = "29b87242-4fd7-44e0-a622-2ec4ad9679be"
J = "3aa71fd5-27d1-4d2a-9925-9cd7ac25a135"
K = "sb_publishable_fixture_public_value"
V = "b" * 64
PNG = b"\x89PNG\r\n\x1a\npreview"
ITEM = f"{W}/deck-7/preview/1.png"


def claim(kind="uploaded", *, consent=True, variant="assisted"):
    return {"job": {"id": J, "deckId": "deck-7", "sourceKind": kind, "scope": "deck", "unitId": "",
                    "variant": variant, "attempts": 1, "deckVersion": V, "rubricVersion": V, "maxScore": 5},
            "workspaceSettings": {"maxScore": 5, "dimensions": [{"id": "visual"}, {"id": "layout"}]},
            "reviewSettings": {"vision_weight": 40, "layout_weight": 30, "match_weight": 30},
            "deckVersion": V, "rubricVersion": V,
            "deck": {"id": "deck-7", "workspace_id": W, "source_kind": kind, "ai_allowed": consent,
                     "data": {"images": [{"id": "page-1", "path": ITEM}]}},
            "calibration": {"samples": [], "corrections": []}}


class TestWorker(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.bundle = claim()
        self.image_payload = PNG
        self.fail_storage = False
        self.reject_one = False
        self.invalid_model = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw) if raw else None
                headers = {k.lower(): v for k, v in self.headers.items()}
                path = self.path
                fixture.requests.append((path, headers, body))
                status = 200
                content_type = "application/json"
                if path.startswith("/auth/v1/token?"):
                    value = {"access_token": "refresh-token-used" if "refresh_token" in path else "access-fixture",
                             "refresh_token": "refresh-fixture", "expires_in": 3600}
                elif path == "/functions/v1/single-judge-review-worker":
                    if fixture.reject_one and headers.get("authorization") == "Bearer access-fixture":
                        status, value = 401, {"message": "response-without-credentials"}
                    elif body["action"] == "claim":
                        value = fixture.bundle
                    else:
                        value = {"ok": True, "status": body["status"], "jobId": J, "aiVisible": False}
                elif path == "/storage/v1/object/ppt-library/" + ITEM:
                    value = fixture.image_payload
                    content_type = "image/png"
                    if fixture.fail_storage:
                        status, value = 404, {"error": "untrusted-payload"}
                        content_type = "application/json"
                elif path == "/v1/chat/completions":
                    scores = {"visual": 5, "layout": 3, "match": 2}
                    if fixture.invalid_model:
                        scores["visual"] = "private-response-sentinel"
                    result = {"scores": scores, "classification": {"label": "教育"}, "summary": "清楚",
                              "reasons": ["标题突出，正文行间距可再增大"]}
                    value = {"choices": [{"message": {"content": json.dumps(result)}}]}
                else:
                    status, value = 500, {"message": "unexpected endpoint"}
                payload = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"
        self.broker = ReviewBroker({"url": origin, "anonKey": K, "workspaceId": W},
                                   SupabaseTokenProvider(SupabaseAuthConfig(origin, K, "judge@test.local", "test")),
                                   "worker-fixture", DirectTransport(1024 * 1024))
        self.model = Gateway(GatewayConfig(origin, K, "", W, model_gateway_url=origin + "/v1",
                                           model_api_key="model-fixture"))

    def test_real_http_each_track_and_credentials_are_isolated(self):
        for kind in TRACKS:
            with self.subTest(kind=kind):
                self.requests.clear()
                self.bundle = claim(kind)
                self.assertTrue(review_one(self.broker, self.model, kind))
                if kind == "uploaded":
                    _, headers, _ = self.requests.pop(0)
                    self.assertEqual(headers["apikey"], K)
                    self.assertNotIn("authorization", headers)
                self.assertEqual([r[0] for r in self.requests], [
                    "/functions/v1/single-judge-review-worker", "/storage/v1/object/ppt-library/" + ITEM,
                    "/v1/chat/completions", "/functions/v1/single-judge-review-worker"])
                claim_call, storage, model, finish = self.requests
                self.assertEqual(claim_call[2], {"action": "claim", "kind": kind})
                self.assertEqual(claim_call[1]["x-guanye-worker-token"], "worker-fixture")
                self.assertEqual(storage[1]["authorization"], "Bearer access-fixture")
                self.assertNotIn("x-guanye-worker-token", storage[1])
                self.assertEqual(model[1]["authorization"], "Bearer model-fixture")
                self.assertNotIn("x-guanye-worker-token", model[1])
                self.assertNotIn("apikey", model[1])
                self.assertIn("data:image/png;base64,", json.dumps(model[2]))
                self.assertEqual(model[2]["model"], "gpt-6.2-sol")
                self.assertEqual(finish[2]["status"], "completed")
                self.assertEqual(finish[2]["result"]["classification"], {"label": "教育"})
                self.assertEqual(finish[2]["result"]["weightedScore"], 4 if kind == "uploaded" else 3.5)
                self.assertEqual(finish[2]["jobId"], J)

    def test_refresh_once_without_copying_service_credentials(self):
        self.reject_one = True
        self.bundle = {"job": None}
        self.assertFalse(review_one(self.broker, self.model, "uploaded"))
        self.assertEqual([req[2].get("action") for req in self.requests], [None, "claim", None, "claim"])
        self.assertEqual(self.requests[-1][1]["authorization"], "Bearer refresh-token-used")

    def test_manual_only_wrong_scope_and_versions_stop_before_storage_and_model(self):
        scenarios = [claim(consent=False), claim("generated"), claim(), claim(), claim()]
        scenarios[2]["deckVersion"] = "c" * 64
        scenarios[3]["deck"]["workspace_id"] = J
        scenarios[4]["job"]["scope"] = "page"
        for self.bundle in scenarios:
            self.requests.clear()
            self.assertTrue(review_one(self.broker, self.model, "uploaded"))
            self.assertTrue(self.requests[-1][2]["terminal"])
            self.assertIsNone(self.requests[-1][2]["result"])
            self.assertFalse(any("/chat/" in r[0] or "/storage/" in r[0] for r in self.requests))

    def test_untrusted_or_mismatched_images_and_model_errors_fail_without_detail_leak(self):
        original = copy.deepcopy(self.bundle)
        for path in ("https://untrusted.invalid/a.png", f"{W}/../archive/a.png", f"{W}/%2e%2e/a.png",
                     f"{W}/document.pdf", f"{J}/deck-7/preview/1.png"):
            self.bundle["deck"]["data"]["images"][0]["path"] = path
            self.requests.clear()
            self.assertTrue(review_one(self.broker, self.model, "uploaded"))
            self.assertFalse(any("/chat/" in r[0] or "/storage/" in r[0] for r in self.requests))
            self.assertNotIn(path, json.dumps(self.requests[-1][2]))
        self.bundle = original
        self.image_payload = b"not an image"
        self.requests.clear()
        self.assertTrue(review_one(self.broker, self.model, "uploaded"))
        self.assertFalse(any("/chat/" in r[0] for r in self.requests))
        self.image_payload = PNG
        self.invalid_model = True
        self.assertTrue(review_one(self.broker, self.model, "uploaded"))
        finish = self.requests[-1][2]
        self.assertEqual(finish["status"], "failed")
        self.assertNotIn("private-response-sentinel", json.dumps(finish))

    def test_baseline_ignores_calibration_from_broker(self):
        self.bundle = claim(variant="baseline")
        self.bundle["calibration"]["samples"] = [{"track": "uploaded", "deckId": "prior", "scope": "deck",
                                                   "split": "calibration", "classification": "never-visible"}]
        self.assertTrue(review_one(self.broker, self.model, "uploaded"))
        self.assertNotIn("never-visible", json.dumps(self.requests[-2][2]))


@unittest.skipUnless(os.name == "nt", "requires Windows DPAPI")
class TestWindowsCredentials(unittest.TestCase):
    def test_scheduler_plan_preserves_unicode_path_without_installing(self):
        pwsh = os.environ.get("GUANYE_TEST_PWSH") or shutil.which("pwsh.exe")
        python = Path(sys.executable).resolve()
        if not pwsh or not python.with_name("pythonw.exe").is_file():
            self.skipTest("requires PowerShell 7 and pythonw.exe")
        repo = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="观页 配置 ") as root:
            config = Path(root) / "工作区 公开配置.json"
            config.write_text(json.dumps({"url": "https://fixture.example", "workspaceId": W, "anonKey": K}),
                              encoding="utf-8")
            result = subprocess.run([pwsh, "-NoProfile", "-File",
                                     str(repo / "cloud/register_professional_review_worker.ps1"),
                                     "-Python", str(python), "-ProfessionalConfig", str(config)],
                                    capture_output=True, encoding="utf-8", timeout=20, cwd=repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)
            self.assertFalse(plan["installed"])
            self.assertEqual(plan["trigger"], "current-user-logon")
            self.assertEqual(plan["multipleInstances"], "ignore-new")
            self.assertEqual(plan["workingDirectory"], str(repo))
            self.assertEqual(plan["arguments"], f'-B -m cloud.professional_review_worker run --config "{config}"')
            self.assertNotIn(K, result.stdout)

    def test_dpapi_and_single_instance(self):
        config = {"url": "https://fixture.example", "workspaceId": W}
        other = {**config, "workspaceId": J}
        credentials = Credentials("judge-fixture@test.local", "中文密码", "fixture-worker", "fixture-model")
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"LOCALAPPDATA": root}):
            save_secrets(config, credentials)
            self.assertEqual(load_secrets(config), credentials)
            ciphertext = _path(config).read_bytes()
            self.assertNotIn(b"fixture-model", ciphertext)
            self.assertNotIn("中文密码".encode(), ciphertext)
            self.assertFalse(list(Path(root).rglob("*.tmp")))
            _path(other).write_bytes(ciphertext)
            with self.assertRaises(OSError):
                load_secrets(other)
            with singleton(config):
                with self.assertRaises(RuntimeError), singleton(config):
                    pass
                with singleton(other):
                    pass
            with singleton(config):
                pass


if __name__ == "__main__":
    unittest.main()
