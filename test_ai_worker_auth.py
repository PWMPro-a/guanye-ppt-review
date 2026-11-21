from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from cloud.windows_image_worker import (
    ExeClient,
    GatewayClient,
    LocalState,
    PreflightError,
    SupabaseAuthConfig,
    SupabaseTokenProvider,
    WindowsWorker,
    WorkerConfig,
    WorkerError,
)


def unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class JsonLoopbackServer:
    def __init__(self, handler_factory):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                owner.handle(self, "GET")

            def do_POST(self):
                owner.handle(self, "POST")

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._closed = False

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def handle(self, handler, method):
        raise NotImplementedError

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class FakeSupabaseAuth(JsonLoopbackServer):
    def __init__(self, *, expires_in=3600, refresh_error=None, refresh_gate=None):
        self.expires_in = expires_in
        self.refresh_error = refresh_error
        self.refresh_gate = refresh_gate
        self.lock = threading.Lock()
        self.requests = []
        self.logins = 0
        self.refreshes = 0
        self.refresh_started = threading.Event()
        super().__init__(self)

    def handle(self, handler, method):
        length = int(handler.headers.get("Content-Length", "0") or "0")
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            body = {}
        parsed = urlsplit(handler.path)
        grant_type = parse_qs(parsed.query).get("grant_type", [""])[0]
        with self.lock:
            self.requests.append({
                "method": method,
                "path": parsed.path,
                "grant_type": grant_type,
                "headers": {key.lower(): value for key, value in handler.headers.items()},
                "body": body,
            })
        if method != "POST" or parsed.path != "/auth/v1/token":
            self.send_json(handler, 404, {"error_code": "not_found"})
            return
        if grant_type == "password":
            self._login(handler)
            return
        if grant_type == "refresh_token":
            self._refresh(handler, body)
            return
        self.send_json(handler, 400, {"error_code": "unsupported_grant"})

    def _login(self, handler):
        with self.lock:
            self.logins += 1
            sequence = self.logins
        self.send_json(handler, 200, {
            "access_token": f"access-login-{sequence}",
            "refresh_token": f"refresh-login-{sequence}",
            "token_type": "bearer",
            "expires_in": self.expires_in,
            "user": {"id": "worker"},
        })

    def _refresh(self, handler, body):
        with self.lock:
            self.refreshes += 1
            sequence = self.refreshes
        self.refresh_started.set()
        if self.refresh_gate is not None and not self.refresh_gate.wait(timeout=3):
            self.send_json(handler, 500, {"error_code": "gate_timeout"})
            return
        if self.refresh_error is not None:
            status, payload = self.refresh_error
            self.send_json(handler, status, payload)
            return
        self.send_json(handler, 200, {
            "access_token": f"access-refresh-{sequence}",
            "refresh_token": f"refresh-refresh-{sequence}",
            "token_type": "bearer",
            "expires_in": self.expires_in,
            "user": {"id": "worker"},
        })

    @staticmethod
    def send_json(handler, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)


class RedirectAuth(JsonLoopbackServer):
    def __init__(self, target_url):
        self.target_url = target_url
        super().__init__(self)

    def handle(self, handler, method):
        handler.send_response(302)
        handler.send_header("Location", self.target_url + "/captured")
        handler.send_header("Content-Length", "0")
        handler.end_headers()


class RedirectTrap(JsonLoopbackServer):
    def __init__(self):
        self.hits = 0
        self.requests = []
        self.lock = threading.Lock()
        super().__init__(self)

    def handle(self, handler, method):
        with self.lock:
            self.hits += 1
            self.requests.append({
                "method": method,
                "path": handler.path,
                "headers": {key.lower(): value for key, value in handler.headers.items()},
            })
        self.send_json(handler, 200, {"access_token": "trap"})

    @staticmethod
    def send_json(handler, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)


class FakeGateway(JsonLoopbackServer):
    def __init__(self, behavior):
        self.behavior = behavior
        self.requests = []
        self.lock = threading.Lock()
        super().__init__(self)

    def handle(self, handler, method):
        length = int(handler.headers.get("Content-Length", "0") or "0")
        raw = handler.rfile.read(length) if length else b""
        headers = {key.lower(): value for key, value in handler.headers.items()}
        with self.lock:
            self.requests.append({
                "method": method,
                "path": handler.path,
                "headers": headers,
                "body": raw.decode("utf-8", "replace"),
            })
        authorization = headers.get("authorization", "")
        if self.behavior == "old_401_then_ok":
            if authorization == "Bearer access-login-1":
                status, payload = 401, {"error": "请先登录", "code": "unauthorized"}
            else:
                status, payload = 200, {"pages": [{"pageId": "page-1", "leaseToken": "lease-1"}]}
        elif self.behavior == "always_401":
            status, payload = 401, {"error": "请先登录", "code": "unauthorized"}
        elif self.behavior == "other_401":
            status, payload = 401, {"error": "上游拒绝", "code": "upstream_unauthorized"}
        elif self.behavior == "forbidden":
            status, payload = 403, {"error": "worker secret 无效", "code": "forbidden"}
        else:
            status, payload = 500, {"error": "bad fixture", "code": "fixture"}
        self.send_json(handler, status, payload)

    @staticmethod
    def send_json(handler, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)


class FakeExe(JsonLoopbackServer):
    def __init__(self):
        self.requests = []
        self.calls = []
        self.lock = threading.Lock()
        super().__init__(self)

    def handle(self, handler, method):
        with self.lock:
            self.requests.append({key.lower(): value for key, value in handler.headers.items()})
            self.calls.append((method, handler.path))
        if handler.path == "/health":
            payload = {"status": "ok"}
        elif handler.path == "/api/image2ppt/config":
            payload = {"data": {"worker_exists": True, "max_files": 40, "modes": ["original"]}}
        elif handler.path == "/api/image2ppt/settings":
            payload = {"data": {"vision_model": "fake-vision"}}
        else:
            payload = {"error": "not found"}
        raw = json.dumps(payload).encode("utf-8")
        handler.send_response(200 if handler.path != "/missing" else 404)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)


class FakeDesktopProxy(JsonLoopbackServer):
    def __init__(self, *, jobs_payload=None, jobs_status=200, jobs_content_type="application/json"):
        self.jobs_payload = {"data": {"jobs": []}} if jobs_payload is None else jobs_payload
        self.jobs_status = jobs_status
        self.jobs_content_type = jobs_content_type
        self.calls = []
        self.requests = []
        self.lock = threading.Lock()
        super().__init__(self)

    def handle(self, handler, method):
        with self.lock:
            self.calls.append((method, handler.path))
            self.requests.append({key.lower(): value for key, value in handler.headers.items()})
        if handler.path == "/health":
            self.send(handler, 200, "text/html; charset=utf-8",
                      b"<!doctype html><html><body>desktop spa</body></html>")
        elif handler.path == "/api/image2ppt/jobs?limit=1":
            self.send_json(handler, self.jobs_status, self.jobs_payload, self.jobs_content_type)
        elif handler.path == "/api/image2ppt/config":
            self.send_json(handler, 200, {
                "data": {"worker_exists": True, "max_files": 40, "modes": ["original"]},
            })
        elif handler.path == "/api/image2ppt/settings":
            self.send_json(handler, 200, {"data": {"vision_model": "fake-desktop-vision"}})
        else:
            self.send_json(handler, 404, {"error": "not found"})

    @staticmethod
    def send(handler, status, content_type, raw):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    @classmethod
    def send_json(cls, handler, status, payload, content_type="application/json"):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cls.send(handler, status, content_type, raw)


ENV_KEYS = (
    "GUANYE_API_BASE",
    "GUANYE_TOKEN",
    "GUANYE_WORKER_TOKEN",
    "GUANYE_WORKSPACE_ID",
    "GUANYE_SUPABASE_URL",
    "GUANYE_SUPABASE_ANON_KEY",
    "GUANYE_WORKER_EMAIL",
    "GUANYE_WORKER_PASSWORD",
    "IMAGE2PPT_BASE_URL",
    "IMAGE2PPT_ACCESS_TOKEN",
    "GUANYE_EXE_TOKEN",
    "HN_LOCAL_TOKEN",
    "GUANYE_EXE_READY_FILE",
    "GUANYE_EXE_DATA_DIR",
    "GUANYE_WORKER_ID",
    "GUANYE_WORKER_STATE",
)


def patched_worker_env(**values):
    environment = {key: "" for key in ENV_KEYS}
    environment.update(values)
    return mock.patch.dict(os.environ, environment, clear=False)


def auth_config(base: str) -> SupabaseAuthConfig:
    return SupabaseAuthConfig(
        supabase_url=base,
        anon_key="anon-secret-sentinel",
        email="worker@example.test",
        password="password-secret-sentinel",
    )


class WorkerAuthTests(unittest.TestCase):
    def test_from_env_static_token_is_backward_compatible(self):
        with patched_worker_env(
            GUANYE_API_BASE="https://gateway.example",
            GUANYE_TOKEN="static-token-sentinel",
            GUANYE_WORKER_TOKEN="worker-secret-sentinel",
            GUANYE_WORKSPACE_ID="workspace-1",
            IMAGE2PPT_BASE_URL="http://127.0.0.1:1",
        ):
            config = WorkerConfig.from_env()
        self.assertEqual(config.gateway_token, "static-token-sentinel")
        self.assertIsNone(config.auth)
        rendered = repr(config)
        self.assertNotIn("static-token-sentinel", rendered)
        self.assertNotIn("worker-secret-sentinel", rendered)

    def test_from_env_dedicated_account_mode_keeps_worker_secret_separate(self):
        with patched_worker_env(
            GUANYE_API_BASE="https://gateway.example",
            GUANYE_WORKER_TOKEN="worker-secret-sentinel",
            GUANYE_WORKSPACE_ID="workspace-1",
            GUANYE_SUPABASE_URL="http://127.0.0.1:54321",
            GUANYE_SUPABASE_ANON_KEY="anon-secret-sentinel",
            GUANYE_WORKER_EMAIL="worker@example.test",
            GUANYE_WORKER_PASSWORD="password-secret-sentinel",
            IMAGE2PPT_BASE_URL="http://127.0.0.1:1",
        ):
            config = WorkerConfig.from_env()
        self.assertEqual(config.gateway_token, "")
        self.assertEqual(config.worker_token, "worker-secret-sentinel")
        self.assertIsNotNone(config.auth)
        rendered = repr(config)
        for secret in ("anon-secret-sentinel", "password-secret-sentinel", "worker-secret-sentinel"):
            self.assertNotIn(secret, rendered)

    def test_worker_config_positional_arguments_remain_compatible(self):
        config = WorkerConfig(
            "https://gateway.example",
            "gateway-token",
            "worker-secret",
            "workspace-1",
            "http://127.0.0.1:1",
            "exe-token",
            "worker-1",
        )
        self.assertFalse(config.exe_desktop_proxy)
        self.assertEqual(config.exe_endpoint_source, "direct")
        self.assertIsNone(config.exe_ready_file)
        self.assertIsNone(config.exe_release_shell)

    def test_explicit_exe_endpoint_wins_and_keeps_direct_health_chain(self):
        exe = FakeExe()
        secret_token = "exe-secret-sentinel"
        try:
            with tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / "desktop-ready.json"
                ready.write_text(
                    json.dumps({
                        "url": f"http://127.0.0.1:{unused_loopback_port()}",
                        "executable": r"E:\release\HN PPT2 对话体验版.exe",
                    }),
                    encoding="utf-8",
                )
                with patched_worker_env(
                    GUANYE_API_BASE="https://gateway.example",
                    GUANYE_TOKEN="gateway-token",
                    GUANYE_WORKER_TOKEN="worker-secret-sentinel",
                    GUANYE_WORKSPACE_ID="workspace-1",
                    IMAGE2PPT_BASE_URL=exe.base,
                    IMAGE2PPT_ACCESS_TOKEN=secret_token,
                    GUANYE_EXE_DATA_DIR=directory,
                ):
                    config = WorkerConfig.from_env()
                self.assertFalse(config.exe_desktop_proxy)
                self.assertEqual(config.exe_endpoint_source, "explicit")
                self.assertIsNone(config.exe_ready_file)

                client = ExeClient(
                    config.exe_base,
                    config.exe_token,
                    LocalState(Path(directory)),
                    desktop_proxy=config.exe_desktop_proxy,
                    endpoint_source=config.exe_endpoint_source,
                    ready_file=config.exe_ready_file,
                    release_shell=config.exe_release_shell,
                )
                result = client.preflight()

            self.assertEqual(exe.calls, [
                ("GET", "/health"),
                ("GET", "/api/image2ppt/config"),
                ("GET", "/api/image2ppt/settings"),
            ])
            self.assertFalse(result["desktopProxy"])
            self.assertEqual(result["endpointSource"], "explicit")
            self.assertEqual(result["health"], {"status": "ok"})
            self.assertNotIn(secret_token, json.dumps(result, ensure_ascii=False))
        finally:
            exe.close()

    def test_desktop_ready_marks_proxy_and_identifies_release_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "desktop-ready.json"
            ready.write_text(
                json.dumps({
                    "url": "http://127.0.0.1:52251",
                    "pid": 10,
                    "backend_pid": 11,
                    "executable": r"E:\release\HN PPT2 对话体验版.exe",
                    "window_title": "HN PPT2 本地体验版",
                    "instance": "-Guanye-release",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            with patched_worker_env(
                GUANYE_API_BASE="https://gateway.example",
                GUANYE_TOKEN="gateway-token",
                GUANYE_WORKER_TOKEN="worker-secret-sentinel",
                GUANYE_WORKSPACE_ID="workspace-1",
                GUANYE_EXE_DATA_DIR=directory,
            ):
                config = WorkerConfig.from_env()

        self.assertTrue(config.exe_desktop_proxy)
        self.assertEqual(config.exe_endpoint_source, "desktop-ready")
        self.assertEqual(config.exe_ready_file, "desktop-ready.json")
        self.assertEqual(config.exe_release_shell, "HN PPT2 对话体验版.exe")

    def test_desktop_proxy_preflight_uses_jobs_before_original_schema_checks(self):
        proxy = FakeDesktopProxy()
        secret_token = "desktop-secret-sentinel"
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = ExeClient(
                    proxy.base,
                    secret_token,
                    LocalState(Path(directory)),
                    desktop_proxy=True,
                    endpoint_source="desktop-ready",
                    ready_file="desktop-ready.json",
                    release_shell="HN PPT2 对话体验版.exe",
                )
                result = client.preflight()

            self.assertEqual(proxy.calls, [
                ("GET", "/api/image2ppt/jobs?limit=1"),
                ("GET", "/api/image2ppt/config"),
                ("GET", "/api/image2ppt/settings"),
            ])
            self.assertTrue(result["desktopProxy"])
            self.assertEqual(result["endpointSource"], "desktop-ready")
            self.assertEqual(result["readinessFile"], "desktop-ready.json")
            self.assertEqual(result["releaseShell"], "HN PPT2 对话体验版.exe")
            self.assertEqual(result["jobs"], {
                "probe": "/api/image2ppt/jobs?limit=1",
                "count": 0,
                "empty": True,
            })
            self.assertEqual(result["httpProtocol"]["checks"], [
                "/api/image2ppt/jobs?limit=1",
                "/api/image2ppt/config",
                "/api/image2ppt/settings",
            ])
            self.assertNotIn(secret_token, json.dumps(result, ensure_ascii=False))
        finally:
            proxy.close()

    def test_desktop_jobs_shape_error_stops_at_jobs_without_echoing_content(self):
        project_secret = "project-title-secret-sentinel"
        scenarios = (
            (200, {"data": {"jobs": {"title": project_secret}}}),
            (500, {"error": {"message": project_secret}}),
        )
        for status, payload in scenarios:
            with self.subTest(status=status):
                proxy = FakeDesktopProxy(jobs_payload=payload, jobs_status=status)
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        client = ExeClient(
                            proxy.base,
                            "desktop-secret-sentinel",
                            LocalState(Path(directory)),
                            desktop_proxy=True,
                            endpoint_source="desktop-ready",
                            ready_file="desktop-ready.json",
                        )
                        with self.assertRaises(PreflightError) as error:
                            client.preflight()
                    self.assertEqual(error.exception.stage, "jobs")
                    self.assertEqual(proxy.calls, [("GET", "/api/image2ppt/jobs?limit=1")])
                    rendered = str(error.exception) + repr(error.exception)
                    if error.exception.__cause__ is not None:
                        rendered += repr(error.exception.__cause__)
                    self.assertNotIn(project_secret, rendered)
                    self.assertNotIn("desktop-secret-sentinel", rendered)
                finally:
                    proxy.close()

    def test_desktop_jobs_nonempty_list_is_summarized_without_project_content(self):
        project_secret = "project-prompt-secret-sentinel"
        proxy = FakeDesktopProxy(jobs_payload={
            "data": {"jobs": [{"id": "job-1", "title": project_secret, "prompt": project_secret}]},
        })
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = ExeClient(
                    proxy.base,
                    "desktop-secret-sentinel",
                    LocalState(Path(directory)),
                    desktop_proxy=True,
                    endpoint_source="desktop-ready",
                )
                result = client.preflight()
            self.assertEqual(result["jobs"]["count"], 1)
            self.assertFalse(result["jobs"]["empty"])
            self.assertNotIn(project_secret, json.dumps(result, ensure_ascii=False))
        finally:
            proxy.close()

    def test_windows_worker_wires_dedicated_auth_provider(self):
        auth = FakeSupabaseAuth()
        gateway_server = FakeGateway("old_401_then_ok")
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = WorkerConfig(
                    gateway_base=gateway_server.base,
                    gateway_token="",
                    worker_token="worker-secret-sentinel",
                    workspace_id="workspace-1",
                    exe_base="http://127.0.0.1:1",
                    exe_token="exe-token",
                    worker_id="worker-1",
                    state_dir=Path(directory),
                    auth=auth_config(auth.base),
                )
                worker = WindowsWorker(config)
                pages = worker.gateway.claim("worker-1", 1, 60)
            self.assertEqual(pages[0]["pageId"], "page-1")
            self.assertIsNotNone(worker.gateway.auth_provider)
            self.assertEqual(auth.logins, 1)
            self.assertEqual(auth.refreshes, 1)
        finally:
            gateway_server.close()
            auth.close()

    def test_expiry_refresh_rotates_refresh_token_before_reuse(self):
        auth = FakeSupabaseAuth(expires_in=3600)
        now = [1000.0]
        provider = SupabaseTokenProvider(
            auth_config(auth.base), clock=lambda: now[0], expiry_margin=10,
        )
        try:
            self.assertEqual(provider.access_token(), "access-login-1")
            now[0] = 4591
            self.assertEqual(provider.access_token(), "access-refresh-1")
            self.assertEqual(auth.logins, 1)
            self.assertEqual(auth.refreshes, 1)
            refresh_requests = [item for item in auth.requests if item["grant_type"] == "refresh_token"]
            self.assertEqual(refresh_requests[0]["body"], {"refresh_token": "refresh-login-1"})
        finally:
            auth.close()

    def test_concurrent_expiry_refresh_is_single_flight(self):
        gate = threading.Event()
        auth = FakeSupabaseAuth(expires_in=3600, refresh_gate=gate)
        now = [1000.0]
        provider = SupabaseTokenProvider(
            auth_config(auth.base), clock=lambda: now[0], expiry_margin=10,
        )
        try:
            provider.access_token()
            now[0] = 4600
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(provider.access_token) for _ in range(8)]
                self.assertTrue(auth.refresh_started.wait(timeout=2))
                gate.set()
                tokens = [future.result(timeout=3) for future in futures]
            self.assertEqual(tokens, ["access-refresh-1"] * 8)
            self.assertEqual(auth.logins, 1)
            self.assertEqual(auth.refreshes, 1)
        finally:
            gate.set()
            auth.close()

    def test_concurrent_gateway_401_uses_one_shared_refresh(self):
        auth = FakeSupabaseAuth()
        gateway_server = FakeGateway("old_401_then_ok")
        provider = SupabaseTokenProvider(auth_config(auth.base))
        gateway = GatewayClient(
            gateway_server.base, "", "workspace-1",
            worker_token="worker-secret-sentinel", auth_provider=provider,
        )
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(gateway.claim, "worker-1", 1, 60) for _ in range(4)]
                results = [future.result(timeout=3) for future in futures]
            self.assertTrue(all(result[0]["pageId"] == "page-1" for result in results))
            self.assertEqual(auth.logins, 1)
            self.assertEqual(auth.refreshes, 1)
            self.assertEqual(len(gateway_server.requests), 8)
        finally:
            gateway_server.close()
            auth.close()

    def test_gateway_401_retries_once_only(self):
        auth = FakeSupabaseAuth()
        gateway_server = FakeGateway("always_401")
        provider = SupabaseTokenProvider(auth_config(auth.base))
        gateway = GatewayClient(
            gateway_server.base, "", "workspace-1",
            worker_token="worker-secret-sentinel", auth_provider=provider,
        )
        try:
            with self.assertRaises(WorkerError) as error:
                gateway.claim("worker-1", 1, 60)
            self.assertEqual(error.exception.status, 401)
            self.assertEqual(auth.logins, 1)
            self.assertEqual(auth.refreshes, 1)
            self.assertEqual(len(gateway_server.requests), 2)
        finally:
            gateway_server.close()
            auth.close()

    def test_gateway_non_auth_401_does_not_refresh(self):
        auth = FakeSupabaseAuth()
        gateway_server = FakeGateway("other_401")
        provider = SupabaseTokenProvider(auth_config(auth.base))
        gateway = GatewayClient(
            gateway_server.base, "", "workspace-1",
            worker_token="worker-secret-sentinel", auth_provider=provider,
        )
        try:
            with self.assertRaises(WorkerError) as error:
                gateway.claim("worker-1", 1, 60)
            self.assertEqual(error.exception.status, 401)
            self.assertEqual(auth.logins, 1)
            self.assertEqual(auth.refreshes, 0)
            self.assertEqual(len(gateway_server.requests), 1)
        finally:
            gateway_server.close()
            auth.close()

    def test_static_token_has_no_refresh_path(self):
        gateway_server = FakeGateway("always_401")
        gateway = GatewayClient(
            gateway_server.base, "static-old-token", "workspace-1",
            worker_token="worker-secret-sentinel",
        )
        try:
            with self.assertRaises(WorkerError) as error:
                gateway.claim("worker-1", 1, 60)
            self.assertEqual(error.exception.status, 401)
            self.assertEqual(len(gateway_server.requests), 1)
            self.assertEqual(
                gateway_server.requests[0]["headers"]["authorization"],
                "Bearer static-old-token",
            )
        finally:
            gateway_server.close()

    def test_invalid_refresh_credentials_relogin_once(self):
        auth = FakeSupabaseAuth(refresh_error=(
            400,
            {"code": 400, "error_code": "refresh_token_not_found", "msg": "missing"},
        ))
        now = [0.0]
        provider = SupabaseTokenProvider(
            auth_config(auth.base), clock=lambda: now[0], expiry_margin=10,
        )
        try:
            self.assertEqual(provider.access_token(), "access-login-1")
            now[0] = 3601
            self.assertEqual(provider.access_token(), "access-login-2")
            self.assertEqual(auth.logins, 2)
            self.assertEqual(auth.refreshes, 1)
        finally:
            auth.close()

    def test_unknown_refresh_error_does_not_relogin(self):
        auth = FakeSupabaseAuth(refresh_error=(
            400,
            {"code": 400, "error_code": "unexpected_fixture_error", "msg": "unknown"},
        ))
        now = [0.0]
        provider = SupabaseTokenProvider(
            auth_config(auth.base), clock=lambda: now[0], expiry_margin=10,
        )
        try:
            provider.access_token()
            now[0] = 3601
            with self.assertRaises(WorkerError):
                provider.access_token()
            self.assertEqual(auth.logins, 1)
            self.assertEqual(auth.refreshes, 1)
        finally:
            auth.close()

    def test_network_refresh_failure_does_not_relogin(self):
        auth = FakeSupabaseAuth()
        now = [0.0]
        provider = SupabaseTokenProvider(
            auth_config(auth.base), clock=lambda: now[0], expiry_margin=10,
        )
        provider.access_token()
        auth.close()
        now[0] = 3601
        with self.assertRaises(WorkerError):
            provider.access_token()
        self.assertEqual(auth.logins, 1)
        self.assertEqual(auth.refreshes, 0)

    def test_auth_requests_disable_proxy_and_redirect(self):
        auth = FakeSupabaseAuth()
        provider = SupabaseTokenProvider(auth_config(auth.base))
        proxy_environment = {
            "HTTP_PROXY": f"http://127.0.0.1:{unused_loopback_port()}",
            "HTTPS_PROXY": f"http://127.0.0.1:{unused_loopback_port()}",
            "ALL_PROXY": f"http://127.0.0.1:{unused_loopback_port()}",
            "http_proxy": f"http://127.0.0.1:{unused_loopback_port()}",
            "https_proxy": f"http://127.0.0.1:{unused_loopback_port()}",
            "all_proxy": f"http://127.0.0.1:{unused_loopback_port()}",
        }
        try:
            with mock.patch.dict(os.environ, proxy_environment, clear=False):
                self.assertEqual(provider.access_token(), "access-login-1")
            request = auth.requests[0]
            self.assertEqual(request["headers"]["apikey"], "anon-secret-sentinel")
            self.assertEqual(request["headers"]["authorization"], "Bearer anon-secret-sentinel")
            self.assertNotIn("x-guanye-worker-token", request["headers"])
        finally:
            auth.close()

        trap = RedirectTrap()
        redirect = RedirectAuth(trap.base)
        redirect_provider = SupabaseTokenProvider(auth_config(redirect.base))
        try:
            with self.assertRaises(WorkerError) as error:
                redirect_provider.access_token()
            self.assertEqual(error.exception.status, 302)
            self.assertEqual(trap.hits, 0)
        finally:
            redirect.close()
            trap.close()

    def test_gateway_redirect_is_not_followed_for_static_or_account_token(self):
        for mode in ("static", "account"):
            with self.subTest(mode=mode):
                trap = RedirectTrap()
                redirect = RedirectAuth(trap.base)
                auth = None
                try:
                    if mode == "static":
                        gateway = GatewayClient(
                            redirect.base,
                            "static-token-sentinel",
                            "workspace-1",
                            worker_token="worker-secret-sentinel",
                        )
                    else:
                        auth = FakeSupabaseAuth()
                        provider = SupabaseTokenProvider(auth_config(auth.base))
                        gateway = GatewayClient(
                            redirect.base,
                            "",
                            "workspace-1",
                            worker_token="worker-secret-sentinel",
                            auth_provider=provider,
                        )
                    with self.assertRaises(WorkerError) as error:
                        gateway.get_job("job-1")
                    self.assertEqual(error.exception.status, 302)
                    self.assertEqual(str(error.exception), "HTTP 302")
                    self.assertEqual(error.exception.payload, {})
                    self.assertEqual(trap.hits, 0)
                    self.assertEqual(trap.requests, [])
                    if auth is not None:
                        self.assertEqual(auth.logins, 1)
                        self.assertEqual(auth.refreshes, 0)
                finally:
                    redirect.close()
                    trap.close()
                    if auth is not None:
                        auth.close()

    def test_exe_transport_keeps_default_redirect_behavior(self):
        trap = RedirectTrap()
        redirect = RedirectAuth(trap.base)
        try:
            with tempfile.TemporaryDirectory() as directory:
                exe = ExeClient(
                    redirect.base,
                    "exe-secret-sentinel",
                    LocalState(Path(directory)),
                )
                result = exe.http.request("GET", "/health")
            self.assertEqual(result, {"access_token": "trap"})
            self.assertEqual(trap.hits, 1)
        finally:
            redirect.close()
            trap.close()

    def test_auth_sensitive_values_not_in_repr_gateway_or_exe(self):
        auth = FakeSupabaseAuth()
        gateway_server = FakeGateway("old_401_then_ok")
        exe_server = FakeExe()
        provider = SupabaseTokenProvider(auth_config(auth.base))
        gateway = GatewayClient(
            gateway_server.base, "", "workspace-1",
            worker_token="worker-secret-sentinel", auth_provider=provider,
        )
        account_secrets = (
            "anon-secret-sentinel",
            "password-secret-sentinel",
            "worker@example.test",
        )
        protected_secrets = account_secrets + ("worker-secret-sentinel",)
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = WorkerConfig(
                    gateway_base=gateway_server.base,
                    gateway_token="",
                    worker_token="worker-secret-sentinel",
                    workspace_id="workspace-1",
                    exe_base=exe_server.base,
                    exe_token="exe-secret-sentinel",
                    worker_id="worker-1",
                    state_dir=Path(directory),
                    auth=provider.config,
                )
                rendered = repr(config)
                for secret in protected_secrets + ("exe-secret-sentinel",):
                    self.assertNotIn(secret, rendered)
                gateway.claim("worker-1", 1, 60)
                exe = ExeClient(exe_server.base, "exe-secret-sentinel", LocalState(Path(directory)))
                exe.preflight()
            gateway_blob = json.dumps(gateway_server.requests, ensure_ascii=False)
            exe_blob = json.dumps(exe_server.requests, ensure_ascii=False)
            for secret in account_secrets:
                self.assertNotIn(secret, gateway_blob)
            for secret in protected_secrets:
                self.assertNotIn(secret, exe_blob)
        finally:
            exe_server.close()
            gateway_server.close()
            auth.close()

    def test_login_error_does_not_echo_credentials(self):
        auth = FakeSupabaseAuth()
        try:
            with self.assertRaises(WorkerError) as error:
                provider = SupabaseTokenProvider(auth_config(auth.base))
                provider.base = f"http://127.0.0.1:{unused_loopback_port()}/auth/v1"
                provider.access_token()
            rendered = str(error.exception) + repr(error.exception) + repr(error.exception.payload)
            self.assertNotIn("password-secret-sentinel", rendered)
            self.assertNotIn("anon-secret-sentinel", rendered)
        finally:
            auth.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
