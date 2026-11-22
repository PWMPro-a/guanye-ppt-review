"""Contract tests for the unprivileged professional image queue client."""
import hashlib
import json
from urllib.parse import parse_qs, urlsplit
import unittest

from cloud.ai_gateway import HTTPResponse
from cloud.professional_image_client import ProfessionalImageClient
from cloud.windows_image_worker import WorkerError

ORIGIN = "https://project.example.invalid"
WORKSPACE = "70100000-0000-4000-8000-000000000001"
ACTOR = "70100000-0000-4000-8000-000000000004"
JOB = "70100000-0000-4000-8000-000000000010"
PAGE = "70100000-0000-4000-8000-000000000011"
LEASE = "70100000-0000-4000-8000-000000000012"
DECK = "generated-abcdefabcdefabcdefabcdef"
TOKEN = "a.b.c"
REFRESHED = "d.e.f"
WORKER_TOKEN = "w" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image"


class Tokens:
    def __init__(self):
        self.current = TOKEN
        self.refreshes = 0

    def access_token(self):
        return self.current

    def refresh_after_unauthorized(self, rejected):
        self.refreshes += 1
        self.current = REFRESHED
        return self.current


def page(status="running", digest=None, content_type="image/png"):
    return {
        "jobId": JOB, "deckId": DECK, "pageId": PAGE, "leaseToken": LEASE,
        "position": 1, "contentType": content_type,
        "storagePath": f"{WORKSPACE}/{DECK}/{PAGE}.png", "hasReferenceImage": True,
    }


def job(status="running", digest=None):
    return {
        "id": JOB, "workspaceId": WORKSPACE, "userId": ACTOR, "deckId": DECK,
        "pages": [{
            "id": PAGE, "position": 1, "status": status,
            "storagePath": f"{WORKSPACE}/{DECK}/{PAGE}.png", "contentType": "image/png",
            "contentSha256": digest,
        }],
    }


class Transport:
    def __init__(self):
        self.calls = []
        self.uploaded = None
        self.complete_calls = 0
        self.reference_calls = 0
        self.actor = ACTOR
        self.redirect = False

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlsplit(url)
        self.calls.append((method, parsed.path, parsed.query, headers.copy(), body))
        if self.redirect:
            return HTTPResponse(302, {"Location": "https://trap.invalid"}, b"")
        assert parsed.scheme == "https" and parsed.netloc == "project.example.invalid"
        assert headers["apikey"] == "sb_publishable_fixture_api_key"
        if parsed.path == "/auth/v1/user":
            return HTTPResponse(200, {}, json.dumps({"id": self.actor}).encode())
        if parsed.path == "/rest/v1/rpc/single_judge_access":
            assert json.loads(body) == {"w": WORKSPACE}
            return HTTPResponse(200, {}, b'{"mode":"single-judge","role":"judge"}')
        if parsed.path == "/rest/v1/rpc/ai_generation_get":
            value = json.loads(body)
            assert value == {"p_workspace": WORKSPACE, "p_actor": self.actor, "p_job": JOB}
            return HTTPResponse(200, {}, json.dumps(job()).encode())
        if parsed.path.startswith("/storage/v1/object/ppt-library/"):
            assert method == "GET"
            return HTTPResponse(200, {"Content-Length": str(len(PNG)), "Content-Type": "image/png"}, PNG)
        if parsed.path.startswith("/storage/v1/object/upload/sign/ppt-library/"):
            assert method == "PUT" and headers["x-upsert"] == "false"
            assert parse_qs(parsed.query) == {"token": ["a.b.c"]}
            self.uploaded = body
            self.uploaded_sha = hashlib.sha256(body).hexdigest()
            return HTTPResponse(200, {}, b"{}")
        assert parsed.path == "/functions/v1/single-judge-image-worker"
        assert headers["X-Guanye-Worker-Token"] == WORKER_TOKEN
        command = json.loads(body)
        action = command["action"]
        if action == "claim":
            return HTTPResponse(200, {}, json.dumps({"pages": [page()]}).encode())
        if action == "reference":
            self.reference_calls += 1
            return HTTPResponse(200, {}, json.dumps({
                "storagePath": f"{WORKSPACE}/{DECK}/preview/1.png", "contentType": "image/png",
            }).encode())
        if action == "issue-upload":
            return HTTPResponse(200, {}, json.dumps({
                "storagePath": page()["storagePath"], "contentType": "image/png", "uploadToken": TOKEN,
            }).encode())
        if action == "complete":
            self.complete_calls += 1
            if self.uploaded is None:
                return HTTPResponse(409, {}, b'{"ok":false,"code":"UPLOAD_NOT_FOUND"}')
            assert command["content_sha256"] == self.uploaded_sha
            return HTTPResponse(200, {}, json.dumps(job("succeeded", self.uploaded_sha)).encode())
        if action == "fail":
            assert command["error"] == "页面生成失败"
            return HTTPResponse(200, {}, json.dumps(job("failed")).encode())
        raise AssertionError(action)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tokens = Tokens()
        self.transport = Transport()
        self.client = ProfessionalImageClient(
            ORIGIN, WORKSPACE, "sb_publishable_fixture_api_key", self.tokens, WORKER_TOKEN,
            transport=self.transport,
        )

    def test_claim_and_reference_use_only_fixed_worker_surface(self):
        self.assertEqual(self.client.authorize(), ACTOR)
        pages = self.client.claim("worker-1", 1, 1800)
        self.assertEqual(pages[0]["pageId"], PAGE)
        self.assertEqual(self.tokens.refreshes, 0)
        raw, mime = self.client.reference_image(page())
        self.assertEqual(raw, PNG)
        self.assertEqual(mime, "image/png")
        self.assertEqual(self.transport.reference_calls, 2)  # Reauthorize after the private GET.
        claim_call = next(call for call in self.transport.calls if call[4] and b'"action":"claim"' in call[4])
        claim = json.loads(claim_call[4])
        self.assertEqual(claim, {"action": "claim", "worker_id": "worker-1", "limit": 1, "lease_seconds": 1800})

    def test_complete_recovers_from_object_not_yet_visible_without_upsert(self):
        result = self.client.complete(page(), PNG, "image/png", 1, 1)
        self.assertEqual(result["pages"][0]["status"], "succeeded")
        self.assertEqual(self.transport.uploaded, PNG)
        self.assertEqual(self.transport.complete_calls, 2)
        upload_call = next(call for call in self.transport.calls if "/upload/sign/" in call[1])
        self.assertEqual(upload_call[3]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(upload_call[2], "token=a.b.c")

    def test_fail_sanitizes_raw_exe_diagnostics(self):
        self.client.fail(page(), "secret local provider response https://signed.invalid/?token=x")
        edge_call = next(call for call in self.transport.calls if call[1].startswith("/functions/"))
        self.assertEqual(json.loads(edge_call[4])["error"], "页面生成失败")
        self.assertNotIn(b"secret local", edge_call[4])

    def test_actor_boundary_refresh_and_errors_are_bounded(self):
        self.transport.actor = JOB
        with self.assertRaises(WorkerError) as error:
            self.client.get_job(JOB)
        self.assertEqual(error.exception.status, 403)
        self.assertNotIn(ACTOR, str(error.exception))
        self.transport.actor = ACTOR
        self.transport.redirect = True
        with self.assertRaises(WorkerError) as redirected:
            self.client.get_job(JOB)
        self.assertEqual(redirected.exception.status, 502)
        self.assertNotIn("trap.invalid", str(redirected.exception))


if __name__ == "__main__":
    unittest.main()
