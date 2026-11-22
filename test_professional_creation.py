"""Exercise the production loopback HTTP adapter with controlled cloud/model fixtures."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
from threading import Thread
import unittest

from cloud.ai_gateway import HTTPResponse
from cloud.local_professional import ProfessionalServer
from cloud.professional_creation import CreationContext, ProfessionalCreation

WORKSPACE = "70100000-0000-4000-8000-000000000001"
JUDGE = "70100000-0000-4000-8000-000000000004"
JOB = "70100000-0000-4000-8000-000000000010"
PAGE = "70100000-0000-4000-8000-000000000011"
ORIGIN = "https://guanye-test.example.invalid"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jg9sAAAAASUVORK5CYII=")
CONFIG = {"url": ORIGIN, "workspaceId": WORKSPACE, "anonKey": "sb_publishable_fixture_api_key", "singleJudge": True}


class CloudFixture:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.drafts = {}
        self.artifact_path = WORKSPACE + "/generated-aaaaaaaaaaaaaaaaaaaaaaaa/1.png"

    def chat(self, messages):
        self.messages.append(messages)
        return {"title": "公司概况", "pages": [{"title": "概况", "copy": [{"kind": "body", "text": "中文说明"}]}]}

    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith(ORIGIN + "/")
        assert headers["apikey"] == CONFIG["anonKey"]
        path = url[len(ORIGIN):]
        self.calls.append((method, path, headers.copy(), body))
        bearer = headers["Authorization"]
        if bearer == "Bearer expired.jwt.fixture":
            return HTTPResponse(401, {}, b'{"error":"sensitive upstream detail never returned"}')
        is_judge = bearer == "Bearer judge.jwt.fixture"
        assert is_judge or bearer == "Bearer admin.jwt.fixture"
        args = json.loads(body) if headers.get("Content-Type") == "application/json" else None
        result = None
        if path == "/auth/v1/user":
            result = {"id": JUDGE if is_judge else JOB}
        elif path.endswith("/single_judge_access"):
            assert args == {"w": WORKSPACE}
            result = {"mode": "single-judge", "role": "judge" if is_judge else "admin"}
        else:
            assert is_judge
            if path.endswith("/single_judge_planning_references"):
                assert set(args) == {"w", "p_limit"}
                result = []
            elif path.endswith("/single_judge_planning_save"):
                assert set(args) == {"w", "p_plan_id", "p_revision", "p_draft"}
                result = {"status": "draft", "planId": args["p_plan_id"], "revision": 1, "draft": args["p_draft"]}
                self.drafts[args["p_plan_id"]] = result
            elif path.endswith("/single_judge_planning_get"):
                assert set(args) == {"w", "p_plan_id"}
                result = self.drafts[args["p_plan_id"]]
            elif path.endswith("/single_judge_planning_confirm"):
                assert args["p_revision"] == 1
                assert set(args) == {"w", "p_plan_id", "p_revision", "p_draft"}
                result = {"planId": args["p_plan_id"], "status": "confirmed", "revision": 2, "immutable": True}
            elif path.endswith("/ai_generation_get"):
                assert args == {"p_workspace": WORKSPACE, "p_actor": JUDGE, "p_job": JOB}
                result = {"id": JOB, "pages": [{"id": PAGE, "status": "succeeded",
                           "image": {"path": self.artifact_path}, "contentType": "image/png"}]}
            elif path == "/storage/v1/object/ppt-library/" + self.artifact_path:
                assert method == "GET"
                return HTTPResponse(200, {"Content-Type": "image/png"}, PNG)
            elif path.startswith(f"/storage/v1/object/ppt-library/{WORKSPACE}/ai-planning/"):
                assert method == "POST" and headers["x-upsert"] == "false"
                assert "/content/" in path and path.endswith(".txt")
                assert body.decode() == "资料内容"
                result = {"stored": True}
            else:
                raise AssertionError(f"unexpected cloud request: {path}")
        return HTTPResponse(200, {}, json.dumps(result).encode())


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.cloud = CloudFixture()
        self.service = ProfessionalCreation(CONFIG, "secret-fixture", transport=self.cloud, chat=self.cloud.chat)
        self.server = ProfessionalServer(0, Path(__file__).parent / "public", CONFIG, creation=self.service)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, *, token="judge.jwt.fixture", body=None, headers=None):
        values = {"Authorization": "Bearer " + token} if token else {}
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode()
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        try:
            connection.request(method, path, body, values)
            rsp = connection.getresponse()
            return rsp.status, rsp.read()
        finally:
            connection.close()

    def test_analyze_save_confirm_and_download(self):
        status, config = self.request("GET", "/cloud-config.js", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b'"localCreation": true', config)
        self.assertNotIn(b"secret-fixture", config)
        status, raw = self.request("POST", "/api/ai/planning/analyze", body={
            "outline": "测试大纲", "pageCount": 1, "contentFiles": [{
                "name": "带 空格.txt", "data": base64.b64encode("资料内容".encode()).decode(),
            }],
        })
        self.assertEqual(status, 200, raw)
        draft = json.loads(raw)
        self.assertEqual(draft["plannerModel"], "gpt-6.2-sol")
        self.assertEqual(draft["plan"]["title"], "公司概况")
        self.assertEqual(len(self.cloud.messages), 1)
        self.assertIn("资料内容", json.dumps(self.cloud.messages, ensure_ascii=False))
        body = {"plan_id": draft["planId"], "expected_revision": 1, "draft": draft["plan"]}
        status, raw = self.request("POST", "/api/ai/planning/confirm", body=body)
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["status"], "confirmed")
        status, raw = self.request("GET", f"/api/ai/generation/artifact?job_id={JOB}&page_id={PAGE}&download=1")
        self.assertEqual(status, 200, raw)
        artifact = json.loads(raw)
        self.assertEqual(base64.b64decode(artifact["dataBase64"]), PNG)
        self.assertEqual(artifact["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_admission_roles_and_fixed_surface(self):
        path = "/api/ai/planning/analyze"
        # Rejected by cloud authorization rather than trying to parse malformed input.
        status, raw = self.request("POST", path, token="admin.jwt.fixture", body=b"{",
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400, raw)
        self.assertEqual(len(self.cloud.calls), 2)
        self.assertEqual(self.request("POST", path, headers={"Origin": "https://outside.invalid"}, body={})[0], 403)
        self.assertEqual(self.request("GET", "/api/ai/generation?action=recent", headers={"Host": "outside.invalid"})[0], 421)
        self.assertEqual(self.request("POST", "/api/ai/generation/worker/claim", body={})[0], 404)
        self.assertEqual(self.request("POST", "/api/ai/generation/settings", body={})[0], 404)
        self.assertEqual(self.request("GET", "/api/ai/generation?action=recent&action=other")[0], 400)
        self.assertEqual(len(self.cloud.calls), 2)
        status, error = self.request("POST", path, token="expired.jwt.fixture", body={})
        self.assertEqual(status, 401)
        self.assertNotIn(b"sensitive upstream", error)
        self.assertEqual(self.request("POST", "/api/ai/generation/retry", body={"job_id": JOB, "page_ids": [PAGE, JOB]})[0], 400)
        self.assertFalse(self.cloud.messages)

    def test_legacy_snapshot_and_outside_storage_cannot_be_used(self):
        ctx = CreationContext(self.service, JUDGE, "judge.jwt.fixture", {}, {})
        with self.assertRaises(PermissionError):
            ctx.rpc("get_ai_review_sample_snapshot", {"w": WORKSPACE}, privileged=True)
        for path in ["/storage/a", WORKSPACE + "/../x", "foreign/preview.png", WORKSPACE + "\\x"]:
            with self.assertRaises(ValueError):
                ctx.storage_get(path)
        self.assertFalse(self.cloud.calls)


if __name__ == "__main__":
    unittest.main()
