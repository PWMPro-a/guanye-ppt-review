"""Browser smoke test for a real Pages build and the cross-origin AI gateway.

The test keeps every dependency local: a temporary build of the real ``public``
assets is served under a Pages-style subpath, while a separate loopback gateway
server exercises the production request-entry and CORS path.  Supabase Auth and
Storage are replaced at the SDK boundary by a small fixture; the gateway's own
auth/CORS/RPC code remains the production implementation.
"""

from __future__ import annotations

import contextlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
import posixpath
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.parse import parse_qs, unquote, urlsplit
import unittest

from cloud import ai_gateway, ai_generation, ai_planning, ai_reviews
from cloud import build as site_build

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None


ROOT = Path(__file__).resolve().parent
EDGE = Path(
    os.environ.get(
        "GUANYE_TEST_EDGE",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    )
)
PAGES_PREFIX = "/guanye-ppt-review/"
WORKSPACE = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
VALID_TOKEN = "pages-gateway-smoke-valid"
EXPIRED_TOKEN = "pages-gateway-smoke-expired"
GENERATION_JOB = "77777777-7777-4777-8777-777777777777"
GENERATION_DECK = "generated-lazy-deck"

STATE_FIXTURE = {
    "revision": 1,
    "configRevision": 1,
    "maxScore": 5,
    "dimensions": [{"id": "visual", "name": "视觉", "description": ""}],
    "reviews": {},
}

SUPABASE_SDK_FIXTURE = r"""(() => {
  const workspaceId = __WORKSPACE__;
  const userId = __USER__;
  const session = {
    access_token: __TOKEN__,
    token_type: "bearer",
    expires_in: 3600,
    user: {id: userId, email: "fixture@guanye.invalid"}
  };
  const member = {
    id: "fixture-member",
    workspace_id: workspaceId,
    user_id: userId,
    role: "admin",
    name: "Fixture Admin",
    display_name: "Fixture Admin"
  };
  const state = __STATE__;
  const result = data => new Promise(resolve => setTimeout(() => resolve({data, error: null}), 25));
  function from(table) {
    return {
      select() { return this; },
      eq() { return this; },
      order() { return this; },
      range() { return result([]); },
      maybeSingle() { return result(table === "members" ? member : null); },
      single() { return result(table === "members" ? member : null); },
      then(resolve, reject) { return result([]).then(resolve, reject); }
    };
  }
  function createClient() {
    return {
      auth: {
        getSession: () => result({session}),
        refreshSession: () => result({session}),
        signInWithPassword: () => result({session}),
        signOut: () => result(null),
        updateUser: () => result({user: session.user})
      },
      from,
      rpc(name) {
        if (name === "get_review_library") return result({decks: [], entries: []});
        if (name === "get_review_state") return result(state);
        if (name === "review_score_summary") return result({});
        return result(null);
      }
    };
  }
  window.supabase = {createClient};
})();"""


def gateway_response(status: int = 200, payload=None) -> ai_gateway.HTTPResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return ai_gateway.HTTPResponse(status=status, headers={}, body=body)


def header_value(headers, name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


class FixtureGatewayTransport:
    """Minimal Supabase boundary for the production gateway's auth and RPC path."""

    def __init__(self, recent_jobs: list[dict] | None = None) -> None:
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.recent_jobs = list(recent_jobs or [])

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlsplit(url)
        token = header_value(headers, "Authorization").removeprefix("Bearer ").strip()
        with self.lock:
            self.calls.append(
                {
                    "method": method,
                    "path": parsed.path,
                    "query": parsed.query,
                    "token": token,
                    "body": body,
                }
            )
        if parsed.path == "/auth/v1/user":
            if token == VALID_TOKEN:
                return gateway_response(200, {"id": USER})
            return gateway_response(401, {"message": "fixture token rejected"})
        if parsed.path == "/rest/v1/members":
            return gateway_response(200, [{"role": "admin"}])
        if parsed.path == "/rest/v1/rpc/ai_generation_recent":
            return gateway_response(200, {"jobs": list(self.recent_jobs)})
        if parsed.path == "/rest/v1/rpc/ai_generation_get":
            return gateway_response(200, {
                "id": GENERATION_JOB,
                "title": "生成任务一",
                "status": "completed",
                "deckId": GENERATION_DECK,
                "pages": [],
            })
        if parsed.path == "/rest/v1/rpc/ai_planning_list_my_plans":
            return gateway_response(200, {"items": [], "nextCursor": None})
        return gateway_response(404, {"message": "fixture route missing"})

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.calls)


class RunningHTTPServer:
    def __init__(self, server: ThreadingHTTPServer, label: str) -> None:
        self.server = server
        self.label = label
        self.thread = threading.Thread(target=self.server.serve_forever, name=label, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "RunningHTTPServer":
        self.server.daemon_threads = True
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError(f"{self.label} did not stop")
        return False


def make_pages_server(dist_root: Path, request_paths: list[str]) -> ThreadingHTTPServer:
    resolved_root = dist_root.resolve()

    class PagesHandler(SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(resolved_root), **kwargs)

        def translate_path(self, path):
            raw_path = unquote(urlsplit(path).path)
            if not raw_path.startswith(PAGES_PREFIX):
                return str(resolved_root / "__outside_pages_prefix__")
            relative = posixpath.normpath(raw_path[len(PAGES_PREFIX):])
            if relative in {"", "."}:
                relative = "index.html"
            if relative == ".." or relative.startswith("../") or relative.startswith("/"):
                return str(resolved_root / "__path_escape__")
            candidate = (resolved_root / relative).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                return str(resolved_root / "__path_escape__")
            return str(candidate)

        def do_GET(self):
            request_paths.append(urlsplit(self.path).path)
            return super().do_GET()

        def do_HEAD(self):
            request_paths.append(urlsplit(self.path).path)
            return super().do_HEAD()

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, format, *args):
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), PagesHandler)


def make_sdk_fixture(token: str) -> str:
    return (
        SUPABASE_SDK_FIXTURE.replace("__WORKSPACE__", json.dumps(WORKSPACE))
        .replace("__USER__", json.dumps(USER))
        .replace("__TOKEN__", json.dumps(token))
        .replace("__STATE__", json.dumps(STATE_FIXTURE, ensure_ascii=False))
    )


MOCK_EMAIL = "reviewer@guanye.invalid"
MOCK_WRONG_PASSWORD = "local-wrong-password"
MOCK_PASSWORD = "local-correct-password"
MOCK_ACCESS_TOKEN = "mock-gotrue-access-token-pages-real-sdk"
MOCK_REFRESH_TOKEN = "mock-gotrue-refresh-token-pages-real-sdk"
MOCK_ANON_KEY = "sb_publishable_pages_real_sdk_fixture"
MOCK_SERVICE_KEY = "service-role-pages-real-sdk-fixture"
MOCK_DECK_ID = "material-library-deck"
MOCK_DECK_TITLE = "原素材库作品"
MOCK_TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
MOCK_USER = {"id": USER, "email": MOCK_EMAIL, "role": "authenticated"}
MOCK_MEMBER = {
    "id": "mock-member",
    "workspace_id": WORKSPACE,
    "user_id": USER,
    "role": "reviewer",
    "name": "Mock Reviewer",
    "display_name": "Mock Reviewer",
}
MOCK_LIBRARY = {
    "decks": [{
        "id": MOCK_DECK_ID,
        "data": {
            "id": MOCK_DECK_ID,
            "name": MOCK_DECK_TITLE,
            "title": MOCK_DECK_TITLE,
            "style": "视觉叙事",
            "images": [{
                "id": "page-1",
                "name": "01.png",
                "thumb": MOCK_TINY_PNG,
                "url": MOCK_TINY_PNG,
            }],
        },
    }],
    "entries": [],
}
MOCK_AI_SETTINGS = {
    "id": "mock-ai-settings",
    "workspace_id": WORKSPACE,
    "vision_weight": 40,
    "layout_weight": 30,
    "match_weight": 30,
    "human_threshold": 0.85,
    "model_hint": "mock-model",
    "reason_catalog": [],
}
MOCK_WORKSPACE = {
    "id": WORKSPACE,
    "settings": STATE_FIXTURE,
    "dedicated_judge_id": None,
}


def make_mock_upstream_server(pages_origin: str, calls: list[dict]) -> ThreadingHTTPServer:
    """Serve loopback-only GoTrue/PostgREST fixtures to the SDK and gateway."""

    lock = threading.Lock()

    class MockUpstreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _record(self) -> None:
            parsed = urlsplit(self.path)
            with lock:
                calls.append({
                    "method": self.command,
                    "path": parsed.path,
                    "query": parsed.query,
                    "authorization": self.headers.get("Authorization") or "",
                    "apikey": self.headers.get("apikey") or "",
                    "accept": self.headers.get("Accept") or "",
                })

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return {}
            if length <= 0 or length > 1024 * 1024:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        def _send_json(self, status: int, payload=None) -> None:
            body = b"" if status == 204 else json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", pages_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                self.headers.get("Access-Control-Request-Headers")
                or "authorization, apikey, content-type, accept, x-client-info, x-supabase-api-version",
            )
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            if status != 204:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self._record()
            self._send_json(204)

        def do_GET(self) -> None:
            self._record()
            parsed = urlsplit(self.path)
            if parsed.path == "/auth/v1/user":
                authorized = self.headers.get("Authorization") == "Bearer " + MOCK_ACCESS_TOKEN
                self._send_json(200, MOCK_USER) if authorized else self._send_json(
                    401, {"message": "mock token rejected"}
                )
                return
            if parsed.path == "/rest/v1/members":
                single = "application/vnd.pgrst.object+json" in (self.headers.get("Accept") or "")
                self._send_json(200, MOCK_MEMBER if single else [MOCK_MEMBER])
                return
            if parsed.path == "/rest/v1/ai_review_settings":
                self._send_json(200, [MOCK_AI_SETTINGS])
                return
            if parsed.path == "/rest/v1/workspaces":
                self._send_json(200, [MOCK_WORKSPACE])
                return
            self._send_json(404, {"message": "mock GET route missing"})

        def do_POST(self) -> None:
            self._record()
            parsed = urlsplit(self.path)
            body = self._read_json()
            if parsed.path == "/auth/v1/token":
                grant_type = parse_qs(parsed.query).get("grant_type", [""])[0]
                if (grant_type == "password" and body.get("email") == MOCK_EMAIL
                        and body.get("password") == MOCK_PASSWORD):
                    self._send_json(200, {
                        "access_token": MOCK_ACCESS_TOKEN,
                        "refresh_token": MOCK_REFRESH_TOKEN,
                        "token_type": "bearer",
                        "expires_in": 3600,
                        "user": MOCK_USER,
                    })
                else:
                    self._send_json(400, {
                        "code": 400,
                        "error_code": "invalid_credentials",
                        "msg": "Invalid login credentials",
                    })
                return
            if parsed.path == "/auth/v1/logout":
                self._send_json(204)
                return
            if parsed.path.startswith("/rest/v1/rpc/"):
                name = parsed.path.rsplit("/", 1)[-1]
                if name == "get_review_library":
                    self._send_json(200, MOCK_LIBRARY)
                elif name == "get_review_state":
                    self._send_json(200, STATE_FIXTURE)
                elif name == "review_score_summary":
                    self._send_json(200, {})
                elif name == "ai_review_queue_decks":
                    self._send_json(200, {"items": [], "nextCursor": None})
                elif name == "ai_review_rubric_version":
                    self._send_json(200, "mock-rubric-v1")
                else:
                    self._send_json(404, {"message": "mock RPC route missing"})
                return
            self._send_json(404, {"message": "mock POST route missing"})

        def log_message(self, format, *args) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamHandler)


def is_local_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme in {"about", "blob", "data"}:
        return True
    if parsed.scheme not in {"http", "https"}:
        return False
    return (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


class BrowserObservation:
    def __init__(self, gateway_origin: str) -> None:
        self.gateway_origin = gateway_origin
        self.requests: list[dict] = []
        self.responses: list[dict] = []
        self.failed_requests: list[dict] = []
        self.external_requests: list[str] = []
        self.page_errors: list[str] = []

    def attach(self, page) -> None:
        def on_request(request):
            if request.url.startswith(self.gateway_origin):
                self.requests.append(
                    {
                        "method": request.method,
                        "url": request.url,
                        "headers": dict(request.headers),
                    }
                )

        def on_response(response):
            if response.url.startswith(self.gateway_origin):
                self.responses.append(
                    {
                        "status": response.status,
                        "url": response.url,
                        "headers": dict(response.headers),
                    }
                )

        def on_failed(request):
            if request.url.startswith(self.gateway_origin):
                self.failed_requests.append(
                    {"url": request.url, "failure": request.failure or ""}
                )

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))


class PagesGatewaySmokeTest(unittest.TestCase):
    def _new_browser_page(self, browser, gateway_origin: str, token: str):
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        observation = BrowserObservation(gateway_origin)
        sdk = make_sdk_fixture(token)

        def handle_route(route):
            url = route.request.url
            if urlsplit(url).path.endswith("/vendor/supabase.js"):
                route.fulfill(
                    status=200,
                    content_type="application/javascript; charset=utf-8",
                    body=sdk,
                )
                return
            if is_local_url(url):
                route.continue_()
                return
            observation.external_requests.append(url)
            route.abort()

        context.route("**/*", handle_route)
        page = context.new_page()
        page.set_default_timeout(15000)
        observation.attach(page)
        return context, page, observation

    def _open_workspace(self, page, pages_url: str, gateway_origin: str):
        page.goto(pages_url, wait_until="domcontentloaded")
        page.wait_for_selector(".main-shell", timeout=15000)
        self.assertEqual(page.locator(".boot-error").count(), 0)
        page.wait_for_function(
            "(expected) => window.Cloud?.member?.user_id === expected", arg=USER
        )

        self.assertTrue(page.evaluate("window.Cloud.aiAvailable"))
        self.assertEqual(page.evaluate("window.GuanyeAI === window.GuanyeAICore"), True)
        self.assertEqual(page.evaluate("window.GUANYE_AI_BASE"), gateway_origin)

        opener = page.get_by_role("button", name="AI 工作台").first
        expect(opener).to_be_visible()
        opener.click()
        expect(page.locator("#modal")).to_be_visible()

    def _open_workspace_generation(self, page, pages_url: str, gateway_origin: str):
        self._open_workspace(page, pages_url, gateway_origin)
        tab = page.get_by_role("tab", name="生成进度")
        expect(tab).to_be_visible()
        tab.click()

        status = page.locator("#guanye-ai-generation-history-status")
        page.wait_for_function(
            """() => {
                const node = document.querySelector('#guanye-ai-generation-history-status');
                return node && node.textContent && !node.textContent.includes('正在读取');
            }"""
        )
        return status

    @staticmethod
    def _assert_request_header(request, name: str, expected: str) -> None:
        headers = request.all_headers()
        actual = header_value(headers, name)
        if actual != expected:
            raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")

    def test_pages_build_browser_gateway_origin_auth_and_failures(self) -> None:
        self.assertIsNotNone(sync_playwright, "requires the Playwright Python package")
        self.assertTrue(EDGE.is_file(), f"requires Microsoft Edge at {EDGE}")

        request_paths: list[str] = []
        transport = FixtureGatewayTransport()
        pages_a = pages_b = gateway = None

        with tempfile.TemporaryDirectory(prefix="pages-gateway-smoke-") as temp_name:
            temp_root = Path(temp_name)
            shutil.copytree(ROOT / "public", temp_root / "public")
            dist_root = temp_root / "dist"

            with contextlib.ExitStack() as stack:
                pages_a = stack.enter_context(
                    RunningHTTPServer(
                        make_pages_server(dist_root, request_paths), "pages-origin-a"
                    )
                )
                pages_b = stack.enter_context(
                    RunningHTTPServer(
                        make_pages_server(dist_root, request_paths), "pages-origin-b"
                    )
                )
                self.assertNotEqual(pages_a.origin, pages_b.origin)

                config = ai_gateway.GatewayConfig(
                    supabase_url="http://127.0.0.1:9",
                    supabase_anon_key="anon-public-fixture",
                    supabase_service_role_key="service-secret-fixture",
                    workspace_id=WORKSPACE,
                    allowed_origins=(pages_a.origin,),
                    host="127.0.0.1",
                    port=0,
                    max_body_bytes=4096,
                )
                gateway_object = ai_gateway.Gateway(config, transport=transport)
                router = ai_gateway.build_router([ai_generation, ai_planning])
                gateway = stack.enter_context(
                    RunningHTTPServer(
                        ai_gateway.GatewayHTTPServer(("127.0.0.1", 0), gateway_object, router),
                        "ai-gateway",
                    )
                )

                build_env = {
                    "SUPABASE_URL": "http://127.0.0.1:9",
                    "SUPABASE_ANON_KEY": "sb_publishable_pages_gateway_fixture",
                    "WORKSPACE_ID": WORKSPACE,
                    "AI_GATEWAY_URL": gateway.origin,
                }
                output = StringIO()
                with contextlib.redirect_stdout(output):
                    site_build.build(build_env, root=temp_root)

                self.assertIn("Built dist", output.getvalue())
                self.assertEqual(
                    (dist_root / "index.html").read_bytes(),
                    (ROOT / "public" / "index.html").read_bytes(),
                )
                for relative in (
                    "app.js",
                    "cloud.js",
                    "ai-workspace.js",
                    "styles.css",
                    "vendor/supabase.js",
                ):
                    self.assertTrue((dist_root / relative).is_file(), relative)
                config_text = (dist_root / "cloud-config.js").read_text(encoding="utf-8")
                prefix = "window.CLOUD_CONFIG = "
                self.assertTrue(config_text.startswith(prefix))
                built_config = json.loads(config_text[len(prefix):].strip().rstrip(";"))
                self.assertEqual(built_config["aiGatewayUrl"], gateway.origin)
                self.assertEqual(built_config["workspaceId"], WORKSPACE)

                pages_a_url = pages_a.origin + PAGES_PREFIX
                pages_b_url = pages_b.origin + PAGES_PREFIX

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        with self.subTest("authorized lazy workspace route"):
                            transport.recent_jobs = [{
                                "id": GENERATION_JOB,
                                "title": "生成任务一",
                                "createdAt": "2026-11-21T08:30:00Z",
                            }]
                            context, page, observation = self._new_browser_page(
                                browser, gateway.origin, VALID_TOKEN
                            )
                            try:
                                self._open_workspace(page, pages_a_url, gateway.origin)
                                page.wait_for_function(
                                    """() => ['uploaded', 'generated'].every(kind => {
                                        const root = document.querySelector(
                                            `[data-ai-root="reviews-${kind}"]`
                                        );
                                        return root && root.childElementCount > 0;
                                    })"""
                                )
                                # Let an eager mount expose its hidden requests.
                                page.wait_for_timeout(300)

                                def urls(path):
                                    return [
                                        str(request["url"])
                                        for request in observation.requests
                                        if urlsplit(request["url"]).path == path
                                    ]

                                review_urls = urls("/api/ai/reviews")
                                self.assertTrue(any("kind=uploaded" in url for url in review_urls))
                                self.assertTrue(any("kind=generated" in url for url in review_urls))
                                self.assertEqual(urls("/api/ai/planning/history"), [])
                                self.assertEqual(urls("/api/ai/generation"), [])
                                for kind in ("planning", "generation"):
                                    self.assertEqual(
                                        page.locator(f'[data-ai-root="{kind}"]').evaluate(
                                            "node => [node.childElementCount, !!node.shadowRoot]"
                                        ),
                                        [0, False],
                                    )

                                reviews_tab = page.get_by_role("tab", name="AI 评审")
                                planning_tab = page.get_by_role("tab", name="生成策划")
                                generation_tab = page.get_by_role("tab", name="生成进度")
                                reviews_tab.focus()
                                with page.expect_request(
                                    lambda request: urlsplit(request.url).path
                                    == "/api/ai/planning/history"
                                ):
                                    page.keyboard.press("ArrowRight")
                                expect(planning_tab).to_have_attribute(
                                    "aria-selected", "true"
                                )
                                page.wait_for_function(
                                    """() => !!document.querySelector(
                                        '[data-ai-root="planning"]'
                                    )?.shadowRoot"""
                                )
                                planning_root = page.locator('[data-ai-root="planning"]')
                                planning_root.evaluate(
                                    "node => node.dataset.retentionMarker = 'planning'"
                                )
                                outline = planning_root.locator('textarea[name="outline"]')
                                outline.fill("未提交草稿")
                                reviews_tab.click()
                                planning_tab.click()
                                expect(outline).to_have_value("未提交草稿")
                                expect(planning_root).to_have_attribute(
                                    "data-retention-marker", "planning"
                                )
                                self.assertEqual(len(urls("/api/ai/planning/history")), 1)

                                with page.expect_response(
                                    lambda response: response.request.method == "GET"
                                    and response.url
                                    == gateway.origin + "/api/ai/generation?action=recent"
                                ) as response_info:
                                    generation_tab.click()
                                response = response_info.value
                                self.assertEqual(response.status, 200)
                                self.assertEqual(
                                    response.headers.get("access-control-allow-origin"),
                                    pages_a.origin,
                                )
                                self._assert_request_header(
                                    response.request, "authorization", "Bearer " + VALID_TOKEN
                                )
                                self._assert_request_header(
                                    response.request, "origin", pages_a.origin
                                )
                                job_select = page.get_by_role(
                                    "combobox", name="生成任务", exact=True
                                )
                                expect(job_select).to_have_value(GENERATION_JOB)
                                expect(
                                    page.get_by_role("heading", name="生成任务一")
                                ).to_be_visible()
                                generation_root = page.locator('[data-ai-root="generation"]')
                                generation_root.evaluate(
                                    "node => node.dataset.retentionMarker = 'generation'"
                                )
                                generation_before = len(urls("/api/ai/generation"))

                                page.evaluate(
                                    """() => {
                                        const select = document.querySelector('#guanye-ai-deck');
                                        select.add(new Option('作品 A', 'deck-a'));
                                        select.add(new Option('作品 B', 'deck-b'));
                                    }"""
                                )
                                with page.expect_request(
                                    lambda request: urlsplit(request.url).path
                                    == "/api/ai/reviews"
                                    and "kind=uploaded" in request.url
                                    and "deckId=deck-b" in request.url
                                ):
                                    page.locator('#guanye-ai-deck').select_option("deck-b")
                                page.wait_for_timeout(100)
                                rebound_reviews = urls("/api/ai/reviews")
                                self.assertTrue(
                                    any(
                                        "kind=generated" in url and "deckId=deck-b" in url
                                        for url in rebound_reviews
                                    )
                                )
                                expect(generation_root).to_have_attribute(
                                    "data-retention-marker", "generation"
                                )
                                expect(planning_root).to_have_attribute(
                                    "data-retention-marker", "planning"
                                )
                                expect(outline).to_have_value("未提交草稿")
                                expect(
                                    page.get_by_role("heading", name="生成任务一")
                                ).to_be_visible()
                                self.assertEqual(len(urls("/api/ai/generation")), generation_before)

                                reviews_tab.click()
                                generation_tab.click()
                                expect(generation_root).to_have_attribute(
                                    "data-retention-marker", "generation"
                                )
                                self.assertEqual(len(urls("/api/ai/generation")), generation_before)

                                history_before_reopen = len(urls("/api/ai/planning/history"))
                                page.locator("#modal").evaluate("(dialog) => dialog.close()")
                                expect(page.locator("#modal")).not_to_be_visible()
                                page.get_by_role("button", name="AI 工作台").first.click()
                                expect(page.locator("#modal")).to_be_visible()
                                expect(reviews_tab).to_have_attribute(
                                    "aria-selected", "true"
                                )
                                page.wait_for_timeout(200)
                                self.assertEqual(
                                    len(urls("/api/ai/planning/history")),
                                    history_before_reopen,
                                )
                                self.assertEqual(len(urls("/api/ai/generation")), generation_before)

                                planning_tab.click()
                                page.evaluate(
                                    """(jobId) => window.dispatchEvent(new CustomEvent(
                                        'guanye:generation-created',
                                        {detail: {jobId, planId: 'plan-submitted'}}
                                    ))""",
                                    GENERATION_JOB,
                                )
                                expect(generation_tab).to_have_attribute(
                                    "aria-selected", "true"
                                )
                                expect(
                                    page.get_by_role("heading", name="生成任务一")
                                ).to_be_visible()
                                review_entry = page.get_by_role(
                                    "button", name="查看生成评审"
                                )
                                with page.expect_request(
                                    lambda request: urlsplit(request.url).path
                                    == "/api/ai/reviews"
                                    and "kind=generated" in request.url
                                    and "deckId=" + GENERATION_DECK in request.url
                                ):
                                    review_entry.click()
                                expect(reviews_tab).to_have_attribute(
                                    "aria-selected", "true"
                                )
                                page.wait_for_function(
                                    "document.activeElement?.getAttribute('data-review-focus-target') === 'generated'"
                                )

                                auth_calls = [
                                    call
                                    for call in transport.snapshot()
                                    if call["path"] == "/auth/v1/user"
                                    and call["token"] == VALID_TOKEN
                                ]
                                rpc_calls = [
                                    call
                                    for call in transport.snapshot()
                                    if call["path"] == "/rest/v1/rpc/ai_generation_recent"
                                ]
                                self.assertTrue(auth_calls)
                                self.assertTrue(rpc_calls)
                                self.assertEqual(observation.external_requests, [])
                                self.assertEqual(observation.page_errors, [])
                            finally:
                                transport.recent_jobs = []
                                context.close()

                        with self.subTest("gateway rejects expired identity and UI reports it"):
                            context, page, observation = self._new_browser_page(
                                browser, gateway.origin, EXPIRED_TOKEN
                            )
                            try:
                                status = self._open_workspace_generation(
                                    page, pages_a_url, gateway.origin
                                )
                                expect(status).to_have_text("登录已失效，请重新登录")
                                expired_responses = [
                                    response
                                    for response in observation.responses
                                    if response["url"]
                                    == gateway.origin + "/api/ai/generation?action=recent"
                                ]
                                self.assertGreaterEqual(len(expired_responses), 2)
                                self.assertTrue(
                                    all(response["status"] == 401 for response in expired_responses)
                                )
                                self.assertTrue(
                                    all(
                                        response["headers"].get(
                                            "access-control-allow-origin"
                                        )
                                        == pages_a.origin
                                        for response in expired_responses
                                    )
                                )
                                self.assertEqual(observation.external_requests, [])
                                self.assertEqual(observation.page_errors, [])
                            finally:
                                context.close()

                        with self.subTest("disallowed Pages origin cannot read gateway"):
                            context, page, observation = self._new_browser_page(
                                browser, gateway.origin, VALID_TOKEN
                            )
                            try:
                                status = self._open_workspace_generation(
                                    page, pages_b_url, gateway.origin
                                )
                                status_text = status.inner_text().strip()
                                self.assertTrue(status_text)
                                self.assertNotEqual(status_text, "暂无生成任务")
                                self.assertNotEqual(status_text, "正在读取生成任务…")
                                self.assertRegex(
                                    status_text,
                                    r"(Failed to fetch|NetworkError|网络|读取生成任务失败|CORS)",
                                )
                                self.assertEqual(observation.external_requests, [])
                                self.assertEqual(observation.page_errors, [])
                            finally:
                                context.close()
                    finally:
                        browser.close()
                    self.assertFalse(browser.is_connected())

                connection = HTTPConnection("127.0.0.1", gateway.port, timeout=5)
                try:
                    connection.request(
                        "OPTIONS",
                        "/api/ai/generation",
                        headers={
                            "Origin": pages_a.origin,
                            "Access-Control-Request-Method": "GET",
                            "Access-Control-Request-Headers": "authorization",
                        },
                    )
                    response = connection.getresponse()
                    response.read()
                    response_headers = {key.lower(): value for key, value in response.getheaders()}
                finally:
                    connection.close()
                self.assertEqual(response.status, 204)
                self.assertEqual(
                    response_headers.get("access-control-allow-origin"), pages_a.origin
                )
                self.assertIn(
                    "authorization",
                    {
                        value.strip().lower()
                        for value in response_headers.get(
                            "access-control-allow-headers", ""
                        ).split(",")
                    },
                )
                connection = HTTPConnection("127.0.0.1", gateway.port, timeout=5)
                try:
                    connection.request(
                        "OPTIONS",
                        "/api/ai/generation",
                        headers={
                            "Origin": pages_b.origin,
                            "Access-Control-Request-Method": "GET",
                            "Access-Control-Request-Headers": "authorization",
                        },
                    )
                    response = connection.getresponse()
                    body = response.read()
                    response_headers = {key.lower(): value for key, value in response.getheaders()}
                finally:
                    connection.close()
                self.assertEqual(response.status, 403)
                self.assertNotIn("access-control-allow-origin", response_headers)
                self.assertIn("请求来源不允许", body.decode("utf-8"))

                self.assertTrue(
                    any(path.endswith("/cloud.js") for path in request_paths), request_paths
                )
                self.assertTrue(
                    any(path.endswith("/ai-workspace.js") for path in request_paths),
                    request_paths,
                )
                self.assertTrue(
                    any(path.endswith("/app.js") for path in request_paths), request_paths
                )

        self.assertIsNotNone(pages_a)
        self.assertIsNotNone(pages_b)
        self.assertIsNotNone(gateway)
        self.assertFalse(pages_a.thread.is_alive())
        self.assertFalse(pages_b.thread.is_alive())
        self.assertFalse(gateway.thread.is_alive())

    def test_real_bundled_sdk_login_through_mock_upstream_and_real_gateway(self) -> None:
        """Exercise form login with the bundled SDK; upstream is explicitly mocked."""

        self.assertIsNotNone(sync_playwright, "requires the Playwright Python package")
        self.assertTrue(EDGE.is_file(), f"requires Microsoft Edge at {EDGE}")

        request_paths: list[str] = []
        upstream_calls: list[dict] = []
        pages = gateway = upstream = None

        with tempfile.TemporaryDirectory(prefix="pages-real-sdk-login-") as temp_name:
            temp_root = Path(temp_name)
            shutil.copytree(ROOT / "public", temp_root / "public")
            dist_root = temp_root / "dist"

            with contextlib.ExitStack() as stack:
                pages = stack.enter_context(
                    RunningHTTPServer(
                        make_pages_server(dist_root, request_paths), "pages-real-sdk"
                    )
                )
                upstream = stack.enter_context(
                    RunningHTTPServer(
                        make_mock_upstream_server(pages.origin, upstream_calls),
                        "mock-gotrue-postgrest",
                    )
                )
                self.assertNotEqual(pages.origin, upstream.origin)

                config = ai_gateway.GatewayConfig(
                    supabase_url=upstream.origin,
                    supabase_anon_key=MOCK_ANON_KEY,
                    supabase_service_role_key=MOCK_SERVICE_KEY,
                    workspace_id=WORKSPACE,
                    allowed_origins=(pages.origin,),
                    host="127.0.0.1",
                    port=0,
                    max_body_bytes=4096,
                )
                gateway_object = ai_gateway.Gateway(config)
                gateway = stack.enter_context(
                    RunningHTTPServer(
                        ai_gateway.GatewayHTTPServer(
                            ("127.0.0.1", 0),
                            gateway_object,
                            ai_gateway.build_router([ai_reviews]),
                        ),
                        "real-gateway-http",
                    )
                )
                self.assertNotEqual(pages.origin, gateway.origin)

                build_env = {
                    "SUPABASE_URL": upstream.origin,
                    "SUPABASE_ANON_KEY": MOCK_ANON_KEY,
                    "WORKSPACE_ID": WORKSPACE,
                    "AI_GATEWAY_URL": gateway.origin,
                }
                with contextlib.redirect_stdout(StringIO()):
                    site_build.build(build_env, root=temp_root)

                vendor_source = (ROOT / "public" / "vendor" / "supabase.js").read_bytes()
                vendor_built = (dist_root / "vendor" / "supabase.js").read_bytes()
                self.assertEqual(vendor_built, vendor_source)
                self.assertIn("2.89.0", vendor_built.decode("utf-8"))

                pages_url = pages.origin + PAGES_PREFIX
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        executable_path=str(EDGE), headless=True
                    )
                    context = browser.new_context(viewport={"width": 1280, "height": 900})
                    observation = BrowserObservation(gateway.origin)

                    def handle_route(route):
                        url = route.request.url
                        if is_local_url(url):
                            route.continue_()
                            return
                        observation.external_requests.append(url)
                        route.abort()

                    context.route("**/*", handle_route)
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    observation.attach(page)
                    try:
                        page.goto(pages_url, wait_until="domcontentloaded")
                        expect(page.locator("#cloud-auth")).to_be_visible()
                        expect(page.locator("#cloud-auth-message")).to_be_empty()

                        page.locator("#cloud-email").fill(MOCK_EMAIL)
                        page.locator("#cloud-password").fill(MOCK_WRONG_PASSWORD)
                        with page.expect_response(
                            lambda response: urlsplit(response.url).path
                            == "/auth/v1/token"
                            and response.request.method == "POST"
                        ) as invalid_response:
                            page.get_by_role("button", name="登录并进入").click()
                        self.assertEqual(invalid_response.value.status, 400)
                        invalid_headers = invalid_response.value.request.all_headers()
                        self.assertEqual(
                            header_value(invalid_headers, "authorization"),
                            "Bearer " + MOCK_ANON_KEY,
                        )
                        expect(page.locator("#cloud-auth-message")).to_have_text(
                            "Invalid login credentials"
                        )
                        expect(page.locator("#cloud-auth")).to_be_visible()

                        page.locator("#cloud-password").fill(MOCK_PASSWORD)
                        with page.expect_navigation(wait_until="domcontentloaded"):
                            page.get_by_role("button", name="登录并进入").click()

                        page.wait_for_selector(".main-shell")
                        self.assertEqual(page.locator(".boot-error").count(), 0)
                        expect(page.locator(".deck-card")).to_have_count(1)
                        expect(page.locator(".card-title")).to_have_text(MOCK_DECK_TITLE)
                        self.assertEqual(
                            page.evaluate("window.Cloud?.member?.role"), "reviewer"
                        )
                        self.assertTrue(
                            any(
                                urlsplit(path).path.endswith("/vendor/supabase.js")
                                for path in request_paths
                            ),
                            "the real bundled supabase.js was not requested",
                        )

                        def is_workspace_response(response):
                            parsed = urlsplit(response.url)
                            return (
                                response.request.method == "GET"
                                and parsed.path == "/api/ai/reviews"
                                and parse_qs(parsed.query).get("action") == ["workspace"]
                            )

                        with page.expect_response(is_workspace_response) as workspace_response:
                            page.get_by_role("button", name="AI 工作台").first.click()
                        self.assertEqual(workspace_response.value.status, 200)
                        workspace_headers = workspace_response.value.request.all_headers()
                        self.assertEqual(
                            header_value(workspace_headers, "authorization"),
                            "Bearer " + MOCK_ACCESS_TOKEN,
                        )
                        expect(page.locator("#modal")).to_be_visible()

                        calls = list(upstream_calls)
                        self.assertTrue(
                            any(
                                call["method"] == "GET"
                                and call["path"] == "/auth/v1/user"
                                and call["authorization"] == "Bearer " + MOCK_ACCESS_TOKEN
                                for call in calls
                            ),
                            "gateway did not authenticate the browser token upstream",
                        )
                        self.assertEqual(observation.external_requests, [])
                        self.assertEqual(observation.page_errors, [])

                        page.locator("#modal").evaluate("(dialog) => dialog.close()")
                        expect(page.locator("#modal")).not_to_be_visible()
                        with page.expect_navigation(wait_until="domcontentloaded"):
                            page.locator('[data-cloud-action="logout"]').first.click()
                        expect(page.locator("#cloud-auth")).to_be_visible()
                        self.assertEqual(
                            page.evaluate(
                                "Object.keys(localStorage).filter("
                                "key => key.includes('auth-token')).length"
                            ),
                            0,
                        )
                        self.assertTrue(
                            any(
                                call["method"] == "POST"
                                and call["path"] == "/auth/v1/logout"
                                for call in list(upstream_calls)
                            ),
                            "SDK sign-out did not reach the mock GoTrue endpoint",
                        )
                        self.assertEqual(observation.external_requests, [])
                        self.assertEqual(observation.page_errors, [])
                    finally:
                        context.close()
                        browser.close()
                    self.assertFalse(browser.is_connected())

        self.assertIsNotNone(pages)
        self.assertIsNotNone(upstream)
        self.assertIsNotNone(gateway)
        self.assertFalse(pages.thread.is_alive())
        self.assertFalse(upstream.thread.is_alive())
        self.assertFalse(gateway.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
