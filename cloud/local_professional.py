"""Loopback-only Windows shell for the shared single-judge library."""
from __future__ import annotations

import base64
import binascii
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from threading import BoundedSemaphore
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from cloud.professional_creation import ANALYZE, MAX_ANALYZE_BODY, MAX_EDIT_BODY, ROUTES
from cloud.professional_import import ImportErrorResponse, MAX_BODY, ProfessionalImporter

MAX_CONFIG_BYTES = 8 * 1024
PUBLIC_FIELDS = frozenset({"url", "anonKey", "workspaceId"})
PUBLISHABLE_KEY = re.compile(r"sb_publishable_[A-Za-z0-9_-]{12,225}\Z")
JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")


def _is_browser_key(value: str) -> bool:
    if PUBLISHABLE_KEY.fullmatch(value):
        return True
    if len(value) > 4096 or not JWT.fullmatch(value):
        return False
    try:
        claim = value.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(claim + "=" * (-len(claim) % 4)))
        return isinstance(payload, dict) and payload.get("role") == "anon"
    except (binascii.Error, ValueError):
        return False


def load_browser_config(path: Path) -> dict[str, str | bool]:
    with path.open("rb") as source:
        raw = source.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("专业版配置文件超过大小限制")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
        if not isinstance(data, dict) or set(data) != PUBLIC_FIELDS:
            raise ValueError
        for value in data.values():
            if not isinstance(value, str) or not value or value.strip() != value or any(ord(c) < 33 for c in value):
                raise ValueError
        url = urlsplit(data["url"])
        if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path not in ("", "/") or url.port not in (None, 443)):
            raise ValueError
        workspace = str(UUID(data["workspaceId"]))
        if not _is_browser_key(data["anonKey"]):
            raise ValueError
    except (UnicodeError, ValueError):
        raise ValueError("专业版配置只允许 HTTPS 项目地址、publishable/anon 公钥和工作区 UUID") from None
    return {"url": data["url"].rstrip("/"), "anonKey": data["anonKey"], "workspaceId": workspace, "singleJudge": True}


def _unique_fields(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("配置有重复字段")
        result[key] = value
    return result


class ProfessionalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, public_dir: Path, config: dict[str, str | bool], *, importer=None, creation=None):
        self.public_dir = public_dir.resolve(strict=True)
        self.importer = importer if importer is not None else ProfessionalImporter(config)
        self.import_slot = BoundedSemaphore(1)
        self.creation = creation
        self.analyze_slot = BoundedSemaphore(1)
        self.artifact_slots = BoundedSemaphore(4)
        exposed = {**config, "localImport": True, "localReview": True, "localCreation": creation is not None}
        self.browser_config = b"window.CLOUD_CONFIG = " + json.dumps(exposed, ensure_ascii=True).encode("ascii") + b";\n"
        super().__init__(("127.0.0.1", port), ProfessionalHandler)


class ProfessionalHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(args[2].public_dir), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _local_host(self) -> bool:
        return self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]

    def _serve(self, head: bool):
        if not self._local_host():
            self.send_error(421)
            return
        path = urlsplit(self.path).path
        if path == "/cloud-config.js":
            self._send(self.server.browser_config, "text/javascript; charset=utf-8", head)
            return
        if path == "/api/health":
            health = {"app": "ppt-review", "version": 2, "mode": "single-judge", "pid": os.getpid()}
            self._send(json.dumps(health).encode("ascii"), "application/json; charset=utf-8", head)
            return
        if path.startswith(("/api/", "/image/", "/thumb/")):
            self.send_error(404)
            return
        return super().do_HEAD() if head else super().do_GET()

    def _send(self, payload: bytes, content_type: str, head: bool, *, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def _creation(self, method):
        if not self._local_host():
            self.send_error(421)
            return
        slot = None
        acquired = False
        self.close_connection = True
        try:
            if len(self.path) > 2048:
                raise ImportErrorResponse(400, "请求地址过长")
            target = urlsplit(self.path)
            service = self.server.creation
            if target.scheme or target.netloc or target.fragment or (method, target.path) not in ROUTES or service is None:
                raise ImportErrorResponse(404, "本机创作接口不可用")
            origin = self.headers.get_all("Origin", [])
            fetch_site = self.headers.get_all("Sec-Fetch-Site", [])
            if (origin and origin != [f"http://127.0.0.1:{self.server.server_port}"]
                    or fetch_site and fetch_site != ["same-origin"]):
                raise PermissionError("只能从本机观页访问创作接口")
            authorization = self.headers.get_all("Authorization", [])
            token = authorization[0][7:] if len(authorization) == 1 and authorization[0].startswith("Bearer ") else ""
            if len(token) > 8192 or not JWT.fullmatch(token):
                raise ImportErrorResponse(401, "请先登录后再打开创作工作台")
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True,
                              max_num_fields=5, errors="strict")
            query = _unique_fields(pairs)
            size = 0
            if method == "POST":
                lengths = self.headers.get_all("Content-Length", [])
                if query or self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
                    raise ValueError("创作请求参数或长度无效")
                size = int(lengths[0])
                if not 1 <= size <= (MAX_ANALYZE_BODY if target.path == ANALYZE else MAX_EDIT_BODY):
                    raise ImportErrorResponse(413, "创作请求超过大小限制")
                if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
                    raise ImportErrorResponse(415, "创作接口仅接收 JSON")
            elif self.headers.get("Transfer-Encoding") is not None or self.headers.get_all("Content-Length", []) not in ([], ["0"]):
                raise ValueError("读取请求不能包含正文")
            actor = service.authorize(token)
            if target.path == ANALYZE:
                slot = self.server.analyze_slot
            elif target.path.endswith("/artifact"):
                slot = self.server.artifact_slots
            if slot is not None:
                acquired = slot.acquire(blocking=False)
                if not acquired:
                    raise ImportErrorResponse(429, "本机创作任务繁忙，请稍后重试")
            body = {}
            if size:
                self.connection.settimeout(30)
                try:
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError
                    body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
                    if not isinstance(body, dict):
                        raise ValueError
                except (OSError, ValueError, RecursionError):
                    raise ValueError("创作请求不完整或格式无效") from None
                self.connection.settimeout(None)
            status, result = service.handle(method, target.path, actor, token, body, query)
            payload = json.dumps(result, ensure_ascii=True, allow_nan=False).encode("ascii")
        except ImportErrorResponse as err:
            status, payload = err.status, self._creation_error(str(err))
        except PermissionError as err:
            status, payload = 403, self._creation_error(str(err))
        except ValueError as err:
            status, payload = 400, self._creation_error(str(err))
        except Exception as err:
            self.log_error("professional creation failed: %s", type(err).__name__)
            status, payload = 500, self._creation_error("本机创作服务暂不可用")
        try:
            self._send(payload, "application/json; charset=utf-8", False, status=status)
        except OSError:
            pass
        finally:
            if acquired:
                slot.release()

    @staticmethod
    def _creation_error(message):
        return json.dumps({"error": message[:300]}, ensure_ascii=True).encode("ascii")

    def do_GET(self):
        if self.path.startswith("/api/ai/"):
            return self._creation("GET")
        return self._serve(False)

    def do_HEAD(self):
        return self._serve(True)

    def do_POST(self):
        if self.path.startswith("/api/ai/"):
            return self._creation("POST")
        if not self._local_host():
            self.send_error(421)
            return
        if self.path != "/api/professional/import":
            self.send_error(404)
            return
        try:
            origin = self.headers.get_all("Origin", [])
            fetch_site = self.headers.get_all("Sec-Fetch-Site", [])
            if (origin and origin != [f"http://127.0.0.1:{self.server.server_port}"]
                    or fetch_site and fetch_site != ["same-origin"]):
                raise ImportErrorResponse(403, "只能从本机观页投递")
            authorization = self.headers.get_all("Authorization", [])
            token = authorization[0][7:] if len(authorization) == 1 and authorization[0].startswith("Bearer ") else ""
            if len(token) > 8192 or not JWT.fullmatch(token):
                raise ImportErrorResponse(401, "请先登录后再投递")
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
                raise ImportErrorResponse(400, "文档请求长度无效")
            size = int(lengths[0])
            if not 1 <= size <= MAX_BODY:
                raise ImportErrorResponse(413, "每份文档最多 25 MiB")
            if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
                raise ImportErrorResponse(415, "仅接收 JSON 投递请求")
            if not self.server.import_slot.acquire(blocking=False):
                raise ImportErrorResponse(409, "本机正在处理上一份素材，请稍后再投递")
            try:
                actor = self.server.importer.authorize(token)
                self.connection.settimeout(30)
                try:
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError
                    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
                except (OSError, ValueError, RecursionError):
                    raise ImportErrorResponse(400, "文档投递请求不完整或格式无效") from None
                self.connection.settimeout(None)
                result = self.server.importer.import_one(actor, token, value)
            finally:
                self.server.import_slot.release()
        except ImportErrorResponse as err:
            self._send(json.dumps({"error": str(err)}, ensure_ascii=True).encode("ascii"),
                       "application/json; charset=utf-8", False, status=err.status)
        except OSError:
            self._send(b'{"error":"Local import unavailable"}', "application/json; charset=utf-8", False, status=500)
        else:
            self._send(json.dumps(result, ensure_ascii=True).encode("ascii"), "application/json; charset=utf-8", False)
