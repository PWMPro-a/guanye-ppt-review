from base64 import b64encode
from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.parse import parse_qs, urlsplit

from cloud.ai_gateway import HTTPResponse
from cloud.local_professional import ProfessionalServer
from cloud.professional_import import ProfessionalImporter

CONFIG = {"url": "https://fixture.supabase.co", "anonKey": "sb_publishable_fixturetesting",
          "workspaceId": "937c868a-8e42-4043-b3e4-c7cfa05076b5", "singleJudge": True}
ACTOR = "299454c0-a536-4f51-8c21-0034cb2495a3"
TOKEN = "fixture.valid.signature"


class FakeCloud:
    def __init__(self):
        self.objects = {}
        self.commands = []
        self.published = set()
        self.writes = []
        self.manifests = []
        self.role = "submitter"
        self.fail_after_first_page = False
        self.malicious_paths = False

    def __call__(self, method, url, headers, body, _timeout):
        target = urlsplit(url)
        assert target.hostname == "fixture.supabase.co"
        assert headers["Authorization"] == "Bearer " + TOKEN
        assert headers["apikey"] == CONFIG["anonKey"]
        self.commands.append((method, target.path))
        def response(status, value):
            return HTTPResponse(status, {}, json.dumps(value).encode())
        if target.path == "/auth/v1/user":
            return response(200, {"id": ACTOR})
        if target.path == "/rest/v1/rpc/single_judge_access":
            assert json.loads(body) == {"w": CONFIG["workspaceId"]}
            return response(200, {"mode": "single-judge", "role": self.role})
        if method == "PUT":
            assert headers["x-upsert"] == "false"
            assert headers["cache-control"] == "max-age=3600"
            path = target.path.removeprefix("/storage/v1/object/upload/sign/ppt-library/")
            assert path != target.path and parse_qs(target.query) == {"token": ["bounded.token.fixture"]}
            assert path not in self.objects, "resuming must not overwrite prior uploads"
            self.writes.append(path)
            self.objects[path] = body
            if self.fail_after_first_page and path.endswith("/preview/1.png"):
                self.fail_after_first_page = False
                return response(503, {"error": "private diagnostic"})
            return response(200, {})
        assert target.path == "/functions/v1/single-judge-import"
        value = json.loads(body)
        assert value["workspaceId"] == CONFIG["workspaceId"] and value["aiAllowed"] is False
        assert value["original"]["name"] == "原始 资料.pdf"
        page_sizes = value["pageSizes"]
        page_hashes = value["pageHashes"]
        assert isinstance(page_sizes, list) and isinstance(page_hashes, list)
        assert len(page_sizes) == len(page_hashes)
        assert all(isinstance(item, str) and len(item) == 64 for item in page_hashes)
        assert len(set(page_hashes)) == len(page_hashes)
        self.manifests.append(value)
        prefix = value["workspaceId"] + "/" + value["deckId"]
        original = prefix + "/original/" + value["original"]["sha256"] + ".pdf"
        images = [prefix + f"/preview/{position}.png" for position in range(1, len(page_sizes) + 1)]
        if value["action"] == "prepare":
            paths = [original, *images]
            if self.malicious_paths:
                paths[-1] = "../../tamper"
            return response(200, {"uploads": [{"path": item, "token": "bounded.token.fixture"} for item in paths if item not in self.objects]})
        assert value["action"] == "publish"
        assert sha256(self.objects[original]).hexdigest() == value["original"]["sha256"]
        assert [len(self.objects[image]) for image in images] == page_sizes
        assert [sha256(self.objects[image]).hexdigest() for image in images] == page_hashes
        assert all(self.objects[image].startswith(b"\x89PNG\r\n\x1a\n") for image in images)
        repeated = value["deckId"] in self.published
        self.published.add(value["deckId"])
        return response(200, {"ok": True, "deckId": value["deckId"], "deckVersion": "a" * 64,
                              "idempotent": repeated, "upstreamPrivate": "not-for-browser"})


class ImportBrowserRouteTests(unittest.TestCase):
    def setUp(self):
        self.cloud = FakeCloud()
        self.importer = ProfessionalImporter(CONFIG, transport=self.cloud)
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.server = ProfessionalServer(0, Path(self.root.name), CONFIG, importer=self.importer)
        self.addCleanup(self.server.server_close)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def upload(self, data, headers=None):
        connection = HTTPConnection(*self.server.server_address, timeout=10)
        try:
            connection.request("POST", "/api/professional/import", body=json.dumps(data).encode(), headers={
                "Origin": self.origin, "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN,
                **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def document(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("PyMuPDF is required to exercise the real converter")
        with pymupdf.open() as pdf:
            for position in range(2):
                page = pdf.new_page(width=256, height=144)
                page.insert_text((12, 30), f"Slide {position + 1}")
            content = pdf.tobytes()
        return {"title": "原始 资料", "aiAllowed": False,
                "file": {"name": "原始 资料.pdf", "data": b64encode(content).decode()}}

    def test_manifest_page_hashes_match_generated_png_bytes(self):
        preview_bytes = [
            b"\x89PNG\r\n\x1a\nfirst-preview",
            b"\x89PNG\r\n\x1a\nsecond-preview",
        ]

        def converter(_source, output):
            output.mkdir()
            pages = []
            for position, content in enumerate(preview_bytes, 1):
                name = f"page-{position:04d}.png"
                (output / name).write_bytes(content)
                pages.append({"file": name, "page": position})
            (output / "result.json").write_text(json.dumps({"pages": pages}), encoding="utf-8")

        importer = ProfessionalImporter(CONFIG, transport=self.cloud, converter=converter)
        result = importer.import_one(ACTOR, TOKEN, {
            "title": "原始 资料",
            "aiAllowed": False,
            "file": {"name": "原始 资料.pdf", "data": b64encode(b"fixture-source").decode()},
        })

        expected_hashes = [sha256(content).hexdigest() for content in preview_bytes]
        self.assertEqual(result["pageCount"], 2)
        self.assertEqual(len(self.cloud.manifests), 2)
        self.assertTrue(all(item["pageHashes"] == expected_hashes for item in self.cloud.manifests))
        prefix = CONFIG["workspaceId"] + "/" + result["deckId"]
        stored_hashes = [
            sha256(self.cloud.objects[prefix + f"/preview/{position}.png"]).hexdigest()
            for position in range(1, 3)
        ]
        self.assertEqual(self.cloud.manifests[-1]["pageHashes"], stored_hashes)

    def test_submitter_can_resume_real_pdf_conversion_and_publish_only_after_all_uploads(self):
        data = self.document()
        self.cloud.fail_after_first_page = True
        status, error = self.upload(data)
        self.assertEqual(status, 502)
        self.assertNotIn("private diagnostic", str(error))
        self.assertEqual(len(self.cloud.writes), 2)
        self.assertFalse(self.cloud.published)
        status, result = self.upload(data)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.cloud.writes), 3)
        self.assertEqual(result["pageCount"], 2)
        self.assertFalse(result["idempotent"])
        self.assertNotIn("upstreamPrivate", result)
        status, repeated = self.upload(data)
        self.assertEqual(status, 200)
        self.assertEqual(repeated["deckId"], result["deckId"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(len(self.cloud.writes), 3)
        self.assertEqual(len(self.cloud.manifests), 5)
        publish = [item for item in self.cloud.manifests if item["action"] == "publish"]
        self.assertEqual(len(publish), 2)
        self.assertEqual(len(publish[-1]["pageHashes"]), 2)
        prefix = CONFIG["workspaceId"] + "/" + result["deckId"]
        stored_hashes = [
            sha256(self.cloud.objects[prefix + f"/preview/{position}.png"]).hexdigest()
            for position in range(1, 3)
        ]
        self.assertEqual(publish[-1]["pageHashes"], stored_hashes)
        for manifest in self.cloud.manifests:
            self.assertEqual(len(manifest["pageHashes"]), len(manifest["pageSizes"]))

    def test_forbidden_origin_or_missing_bearer_never_reaches_cloud(self):
        self.assertEqual(self.upload({}, {"Origin": "https://outside.invalid"})[0], 403)
        self.assertEqual(self.upload({}, {"Authorization": ""})[0], 401)
        self.assertEqual(self.cloud.commands, [])

    def test_unknown_role_and_missing_consent_fail_before_conversion(self):
        self.cloud.role = "unexpected"
        self.assertEqual(self.upload({})[0], 403)
        self.cloud.role = "admin"
        self.assertEqual(self.upload({"title": "fixture", "file": {"name": "x.pdf", "data": "YQ=="}})[0], 400)
        self.assertTrue(all("/functions" not in path for _, path in self.cloud.commands))

    def test_rejects_any_token_for_an_unrelated_path_before_first_upload(self):
        self.cloud.malicious_paths = True
        self.assertEqual(self.upload(self.document())[0], 502)
        self.assertEqual(self.cloud.writes, [])
        self.assertEqual(self.cloud.published, set())


if __name__ == '__main__':
    unittest.main()
