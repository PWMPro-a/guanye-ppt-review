"""Guanye AI HTTP gateway.

The gateway is intentionally framework-free so it can run on a small Python
host without adding deployment-only dependencies. Business routes live in
``cloud/ai_reviews.py``, ``cloud/ai_planning.py`` and
``cloud/ai_generation.py`` and register themselves through ``configure(router)``.

Security boundaries:
* bearer tokens are validated by Supabase Auth on every request;
* workspace and role always come from the configured workspace and member row;
* privileged data access is limited to the workspace administrator;
* storage paths must stay under the configured workspace prefix;
* outbound model traffic is direct, bounded by timeout and concurrency limits;
* request headers and bodies are never logged.
"""
from __future__ import annotations

import argparse
import hmac
import importlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping, Sequence

# File execution puts ``cloud/`` on sys.path instead of the repository root.
# Keep the documented file and ``-m cloud.ai_gateway`` entrypoints equivalent
# without changing the package import contract used by the route modules.
if __package__ in {None, ""}:
    _PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _PACKAGE_ROOT not in sys.path:
        sys.path.insert(0, _PACKAGE_ROOT)

LOGGER = logging.getLogger("guanye.ai_gateway")

DEFAULT_MODEL_GATEWAY_URL = "https://internal.api.aiwelink.cc/v1"
DEFAULT_MODEL_ID = "gpt-6.2-sol"
DEFAULT_ALLOWED_ROLES = ("admin", "reviewer")
DEFAULT_MAX_BODY_BYTES = 160 * 1024 * 1024
STORAGE_BUCKET = "ppt-library"
WORKER_ROUTE_PREFIX = "/api/ai/generation/worker/"
WORKER_TOKEN_HEADER = "X-Guanye-Worker-Token"
WORKER_HEADER_ALIASES = (WORKER_TOKEN_HEADER, "X-Access-Code")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_TABLE = re.compile(r"^[a-z_][a-z0-9_]*$")
AI_REVIEW_MATCHES_TABLE = "ai_review_matches"
AI_REVIEW_MATCHES_EMBED_SELECT = "*,ai_review_submissions!inner(),ai_review_records!inner()"
AI_REVIEW_MATCHES_UUID_FILTERS = frozenset({"id", "submission_id", "record_id"})
AI_REVIEW_MATCHES_RESERVED_FILTERS = frozenset(
    {
        "workspace_id",
        "ai_review_submissions.workspace_id",
        "ai_review_records.workspace_id",
    }
)
AI_REVIEW_MATCHES_UUID_VALUE = re.compile(
    r"^eq\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_BEARER_LENGTH = 8192


class GatewayError(Exception):
    """Base class for errors safe to map onto an HTTP response."""

    status = 500
    code = "internal_error"
    public_message = "服务暂时不可用"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.public_message)
        self.detail = message or self.public_message


class GatewayConfigError(GatewayError):
    status = 503
    code = "configuration_error"
    public_message = "AI 网关配置不完整"


class AuthenticationError(GatewayError):
    status = 401
    code = "unauthorized"
    public_message = "请先登录"


class RouteNotFound(GatewayError):
    status = 404
    code = "not_found"
    public_message = "接口不存在"


class MethodNotAllowed(GatewayError):
    status = 405
    code = "method_not_allowed"
    public_message = "接口不支持此请求方法"


class BodyTooLarge(GatewayError):
    status = 413
    code = "body_too_large"
    public_message = "请求内容过大"


class UpstreamError(GatewayError):
    status = 502
    code = "upstream_error"
    public_message = "上游服务暂时不可用"


class GatewayTimeout(GatewayError):
    status = 504
    code = "timeout"
    public_message = "处理超时，请稍后重试"


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], HTTPResponse]


def _env_text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env_text(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise GatewayConfigError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise GatewayConfigError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = _env_text(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise GatewayConfigError(f"{name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise GatewayConfigError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _is_local_host(host: str | None) -> bool:
    return (host or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _validated_url(value: str, name: str, *, origin_only: bool = False) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password:
        raise GatewayConfigError(f"{name} 不得包含账号或密码")
    if parsed.fragment or parsed.query:
        raise GatewayConfigError(f"{name} 不得包含查询参数或片段")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and _is_local_host(parsed.hostname)
    ):
        raise GatewayConfigError(f"{name} 必须使用 HTTPS")
    if not parsed.hostname:
        raise GatewayConfigError(f"{name} 缺少有效主机名")
    if origin_only and parsed.path not in {"", "/"}:
        raise GatewayConfigError(f"{name} 必须是纯来源，例如 https://example.com")
    return value.rstrip("/")


def _parse_origins(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        _validated_url(origin, "GUANYE_ALLOWED_ORIGINS", origin_only=True)
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


def _parse_roles(raw: str) -> tuple[str, ...]:
    roles = tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))
    if not roles:
        roles = DEFAULT_ALLOWED_ROLES
    return roles


@dataclass(frozen=True)
class GatewayConfig:
    supabase_url: str
    supabase_anon_key: str = field(repr=False)
    supabase_service_role_key: str = field(repr=False)
    workspace_id: str
    allowed_origins: tuple[str, ...] = ()
    allowed_roles: tuple[str, ...] = DEFAULT_ALLOWED_ROLES
    storage_bucket: str = "ppt-library"
    host: str = "127.0.0.1"
    port: int = 8787
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_storage_bytes: int = 25 * 1024 * 1024
    max_upstream_bytes: int = 12 * 1024 * 1024
    request_timeout_seconds: float = 30.0
    model_timeout_seconds: float = 180.0
    model_max_concurrency: int = 8
    model_gateway_url: str = DEFAULT_MODEL_GATEWAY_URL
    model_id: str = DEFAULT_MODEL_ID
    model_api_key: str = field(default="", repr=False)
    worker_token: str = field(default="", repr=False)

    def __post_init__(self):
        if self.storage_bucket != STORAGE_BUCKET:
            raise GatewayConfigError("GUANYE_STORAGE_BUCKET 必须为 ppt-library，与 storage policy 保持一致")

    @classmethod
    def from_env(cls, *, require_service_role: bool = True) -> "GatewayConfig":
        required = {
            "SUPABASE_URL": _env_text("GUANYE_SUPABASE_URL") or _env_text("SUPABASE_URL"),
            "SUPABASE_ANON_KEY": _env_text("GUANYE_SUPABASE_ANON_KEY")
            or _env_text("SUPABASE_ANON_KEY"),
            "SUPABASE_SERVICE_ROLE_KEY": _env_text("GUANYE_SUPABASE_SERVICE_ROLE_KEY")
            or _env_text("SUPABASE_SERVICE_ROLE_KEY"),
            "WORKSPACE_ID": _env_text("GUANYE_WORKSPACE_ID") or _env_text("WORKSPACE_ID"),
        }
        missing = [name for name, value in required.items()
                   if not value and (require_service_role or name != "SUPABASE_SERVICE_ROLE_KEY")]
        if not require_service_role:
            required["SUPABASE_SERVICE_ROLE_KEY"] = ""
        if missing:
            raise GatewayConfigError("缺少环境变量: " + ", ".join(missing))
        workspace_id = str(uuid.UUID(required["WORKSPACE_ID"]))
        supabase_url = _validated_url(required["SUPABASE_URL"], "SUPABASE_URL")
        model_gateway_url = _validated_url(
            _env_text("GUANYE_MODEL_GATEWAY_URL")
            or _env_text("GUANYE_MODEL_BASE_URL")
            or DEFAULT_MODEL_GATEWAY_URL,
            "GUANYE_MODEL_GATEWAY_URL",
        )
        storage_bucket = _env_text("GUANYE_STORAGE_BUCKET", STORAGE_BUCKET) or STORAGE_BUCKET
        if storage_bucket != STORAGE_BUCKET:
            raise GatewayConfigError("GUANYE_STORAGE_BUCKET 必须为 ppt-library，与 storage policy 保持一致")
        host = _env_text("GUANYE_HOST", "127.0.0.1")
        if any(ord(char) < 32 for char in host):
            raise GatewayConfigError("GUANYE_HOST 格式无效")
        return cls(
            supabase_url=supabase_url,
            supabase_anon_key=required["SUPABASE_ANON_KEY"],
            supabase_service_role_key=required["SUPABASE_SERVICE_ROLE_KEY"],
            workspace_id=workspace_id,
            allowed_origins=_parse_origins(_env_text("GUANYE_ALLOWED_ORIGINS")),
            allowed_roles=_parse_roles(_env_text("GUANYE_ALLOWED_ROLES", "admin,reviewer")),
            storage_bucket=storage_bucket,
            host=host,
            port=_env_int("GUANYE_PORT", 8787, 1, 65535),
            max_body_bytes=_env_int("GUANYE_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, 1024, 512 * 1024 * 1024),
            max_storage_bytes=_env_int(
                "GUANYE_MAX_STORAGE_BYTES", 25 * 1024 * 1024, 1024, 100 * 1024 * 1024
            ),
            max_upstream_bytes=_env_int(
                "GUANYE_MAX_UPSTREAM_BYTES", 12 * 1024 * 1024, 4096, 64 * 1024 * 1024
            ),
            request_timeout_seconds=_env_float("GUANYE_REQUEST_TIMEOUT_SECONDS", 30.0, 1.0, 300.0),
            model_timeout_seconds=_env_float("GUANYE_MODEL_TIMEOUT_SECONDS", 180.0, 5.0, 900.0),
            model_max_concurrency=_env_int("GUANYE_MODEL_MAX_CONCURRENCY", 8, 1, 64),
            model_gateway_url=model_gateway_url,
            model_id=_env_text("GUANYE_MODEL_ID", DEFAULT_MODEL_ID),
            model_api_key=_env_text("GUANYE_MODEL_API_KEY") or _env_text("GUANYE_AI_API_KEY"),
            worker_token=_env_text("GUANYE_WORKER_TOKEN"),
        )

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _StorageReadLimitExceeded(Exception):
    """A successful download exceeded the requested storage read budget."""


class DirectTransport:
    """Small direct HTTP transport with proxies and redirects disabled."""

    def __init__(self, max_response_bytes: int, max_storage_bytes: int | None = None):
        self.max_response_bytes = max_response_bytes
        self.max_storage_bytes = (
            max_response_bytes if max_storage_bytes is None else max_storage_bytes
        )
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _success_read_budget(self, method: str, url: str, status: int) -> tuple[int, str]:
        storage_prefix = f"/storage/v1/object/{STORAGE_BUCKET}/"
        if (
            status == 200
            and method == "GET"
            and urllib.parse.urlsplit(url).path.startswith(storage_prefix)
        ):
            return self.max_storage_bytes, "存储响应超过大小限制"
        return self.max_response_bytes, "上游响应超过大小限制"

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        return self._request(method, url, headers, body, timeout)

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        *,
        storage_read_limit: int | None = None,
    ) -> HTTPResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                limit, size_error = self._success_read_budget(method, url, status)
                bounded_limit = storage_read_limit if status == 200 else None
                if bounded_limit is not None:
                    limit = min(limit, bounded_limit)
                    lengths = response.headers.get_all("Content-Length", [])
                    if len(lengths) == 1 and "Transfer-Encoding" not in response.headers:
                        declared = lengths[0].strip(" \t")
                        if re.fullmatch(r"[0-9]+", declared):
                            # Compare decimal strings to avoid converting unbounded header integers.
                            declared = declared.lstrip("0") or "0"
                            cap = str(limit)
                            if (len(declared), declared) > (len(cap), cap):
                                raise _StorageReadLimitExceeded
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    if bounded_limit is not None:
                        raise _StorageReadLimitExceeded
                    raise UpstreamError(size_error)
                return HTTPResponse(
                    status=status,
                    headers={key: value for key, value in response.headers.items()},
                    body=raw,
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise UpstreamError("上游响应超过大小限制") from None
            return HTTPResponse(
                status=int(exc.code),
                headers={key: value for key, value in (exc.headers or {}).items()},
                body=raw,
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
                raise GatewayTimeout("上游请求超时") from None
            raise UpstreamError("上游服务不可用") from None


def _decode_json_bytes(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamError("上游返回了无效 JSON") from exc


def _json_error_message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict):
        for key in ("message", "error_description", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _storage_object_not_found(response: HTTPResponse) -> bool:
    """Recognize the Storage v1.89.x 400 wrapper for a missing object."""
    if response.status == 404:
        return True
    if response.status != 400:
        return False
    try:
        payload = _decode_json_bytes(response.body)
    except UpstreamError:
        return False
    if not isinstance(payload, dict):
        return False
    status_code = payload.get("statusCode")
    structured_missing = (
        not isinstance(status_code, bool)
        and (
            (isinstance(status_code, int) and status_code == 404)
            or (isinstance(status_code, str) and status_code == "404")
        )
    )
    return structured_missing and payload.get("code") == "NoSuchKey"


def _extract_json_value(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise ValueError("模型未返回有效 JSON")


@dataclass(frozen=True)
class Principal:
    user_id: str
    workspace_id: str
    role: str
    token: str = field(repr=False)
    is_worker: bool = False


class Router:
    """Exact ``method + path`` router for AI route modules."""

    def __init__(self):
        self._routes: dict[tuple[str, str], Callable[["RequestContext"], Any]] = {}
        self._paths: set[str] = set()
        self._lock = threading.Lock()

    def add(self, method: str, path: str, handler: Callable[["RequestContext"], Any]) -> None:
        method = str(method).upper()
        path = str(path)
        if method not in ALLOWED_METHODS - {"OPTIONS"}:
            raise ValueError(f"不支持的 HTTP 方法: {method}")
        if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
            raise ValueError("路由路径必须以 / 开头且不能包含查询、片段或反斜杠")
        segments = path.split("/")
        if any(segment in {".", ".."} for segment in segments):
            raise ValueError("路由路径不能包含 . 或 ..")
        if not callable(handler):
            raise TypeError("路由 handler 必须可调用")
        with self._lock:
            key = (method, path)
            if key in self._routes:
                raise ValueError(f"重复路由: {method} {path}")
            self._routes[key] = handler
            self._paths.add(path)

    def resolve(self, method: str, path: str) -> Callable[["RequestContext"], Any]:
        method = method.upper()
        with self._lock:
            handler = self._routes.get((method, path))
            if handler is not None:
                return handler
            if path in self._paths:
                raise MethodNotAllowed()
        raise RouteNotFound()

    @property
    def routes(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(sorted(self._routes))


def register_ai_modules(
    router: Router, modules: Sequence[Any] | None = None
) -> tuple[str, ...]:
    """Call ``configure(router)`` on all three AI route modules.

    Passing ``modules`` is intended for tests. Production defaults to importing
    ``cloud.ai_reviews``, ``cloud.ai_planning`` and ``cloud.ai_generation`` and
    fails closed if any module is missing or malformed.
    """
    if modules is None:
        loaded: list[Any] = []
        for name in ("ai_reviews", "ai_planning", "ai_generation"):
            try:
                module = importlib.import_module(f"cloud.{name}")
            except ModuleNotFoundError as exc:
                if exc.name in {f"cloud.{name}", name}:
                    raise GatewayConfigError(f"缺少 AI 路由模块: cloud/{name}.py") from None
                raise
            loaded.append(module)
        modules = loaded
    names: list[str] = []
    for module in modules:
        configure = getattr(module, "configure", None)
        if not callable(configure):
            raise GatewayConfigError("AI 路由模块缺少 configure(router)")
        configure(router)
        names.append(getattr(module, "__name__", type(module).__name__))
    return tuple(names)


class RequestContext:
    """Per-request context handed to AI route modules."""

    def __init__(self, gateway: "Gateway", principal: Principal, body: dict[str, Any], query: dict[str, str]):
        self._gateway = gateway
        self.user_id = principal.user_id
        self.workspace_id = principal.workspace_id
        self.role = principal.role
        self.token = principal.token
        self.is_worker = principal.is_worker
        self.body = body
        self.query = query

    def rpc(self, name: str, params: dict[str, Any] | None = None, privileged: bool = False) -> Any:
        return self._gateway.rpc(self._principal(), name, params or {}, privileged=privileged)

    def select(self, table: str, query: dict[str, Any] | None = None, privileged: bool = False) -> Any:
        if query is not None and not isinstance(query, dict):
            raise ValueError("查询条件必须是字典")
        filters: dict[str, str] = {}
        for key, value in (query or {}).items():
            if not isinstance(key, str) or not SAFE_IDENTIFIER.fullmatch(key):
                raise ValueError("查询条件字段无效")
            if key == "select":
                if not isinstance(value, str):
                    raise ValueError("select 字段投影必须是字符串")
                fields = [field.strip() for field in value.split(",")]
                if fields != ["*"] and any(not SAFE_IDENTIFIER.fullmatch(field) for field in fields):
                    raise ValueError("select 字段投影无效")
                filters[key] = ",".join(fields)
                continue
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, str):
                rendered = value
            else:
                raise ValueError("查询条件值必须是字符串、整数或布尔值")
            filters[key] = f"eq.{rendered}"
        return self._gateway.select(self._principal(), table, filters, privileged=privileged)

    def storage_get(self, path: str) -> bytes:
        return self._gateway.storage_get(self._principal(), path)

    def storage_get_bounded(self, path: str, max_bytes: int) -> bytes | None:
        """Read an object with the caller's byte budget, returning None on oversize."""
        return self._gateway.storage_get_bounded(self._principal(), path, max_bytes)

    def storage_put(self, path: str, data: bytes, content_type: str) -> Any:
        return self._gateway.storage_put(self._principal(), path, data, content_type)

    def chat_json(self, messages: list[dict[str, Any]]) -> Any:
        return self._gateway.chat_json(messages)

    def _principal(self) -> Principal:
        return Principal(self.user_id, self.workspace_id, self.role, self.token, self.is_worker)


class Gateway:
    def __init__(self, config: GatewayConfig, transport: Transport | None = None):
        self.config = config
        self.transport = transport or DirectTransport(
            config.max_upstream_bytes, max_storage_bytes=config.max_storage_bytes
        )
        self._model_slots = threading.BoundedSemaphore(config.model_max_concurrency)

    def _request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        *,
        timeout: float | None = None,
    ) -> HTTPResponse:
        return self.transport(
            method,
            url,
            dict(headers),
            body,
            self.config.request_timeout_seconds if timeout is None else timeout,
        )

    def _public_headers(self) -> dict[str, str]:
        return {
            "apikey": self.config.supabase_anon_key,
            "Accept": "application/json",
        }

    def _privileged_headers(self) -> dict[str, str]:
        return {
            "apikey": self.config.supabase_service_role_key,
            "Authorization": f"Bearer {self.config.supabase_service_role_key}",
            "Accept": "application/json",
        }

    def _user_headers(self, token: str) -> dict[str, str]:
        headers = self._public_headers()
        headers["Authorization"] = f"Bearer {token}"
        return headers

    def authenticate(self, token: str) -> Principal:
        if not isinstance(token, str) or not token.strip():
            raise AuthenticationError()
        token = token.strip()
        if len(token) > MAX_BEARER_LENGTH or any(ord(char) < 32 for char in token):
            raise AuthenticationError("登录凭证格式无效")
        response = self._request(
            "GET",
            f"{self.config.supabase_url}/auth/v1/user",
            self._user_headers(token),
        )
        if response.status in {401, 403}:
            raise AuthenticationError("登录已失效，请重新登录")
        if response.status != 200:
            raise UpstreamError("Supabase Auth 校验失败")
        payload = _decode_json_bytes(response.body)
        user_id = payload.get("id") if isinstance(payload, dict) else None
        if not user_id and isinstance(payload, dict) and isinstance(payload.get("user"), dict):
            user_id = payload["user"].get("id")
        try:
            user_id = str(uuid.UUID(str(user_id)))
        except (ValueError, TypeError, AttributeError):
            raise AuthenticationError("登录身份无效") from None

        query = urllib.parse.urlencode(
            {
                "workspace_id": f"eq.{self.config.workspace_id}",
                "user_id": f"eq.{user_id}",
                "select": "role",
                "limit": "1",
            }
        )
        member_response = self._request(
            "GET",
            f"{self.config.supabase_url}/rest/v1/members?{query}",
            self._privileged_headers(),
        )
        if member_response.status != 200:
            raise UpstreamError("无法校验工作区成员身份")
        rows = _decode_json_bytes(member_response.body)
        if not isinstance(rows, list) or not rows:
            raise PermissionError("当前账号不是该工作区成员")
        role = str(rows[0].get("role", "")).strip().lower() if isinstance(rows[0], dict) else ""
        if role not in self.config.allowed_roles:
            raise PermissionError("当前成员角色无权使用 AI 功能")
        return Principal(user_id=user_id, workspace_id=self.config.workspace_id, role=role, token=token)

    def authenticate_worker(self, principal: Principal, credential: str | None) -> Principal:
        """Require the separate worker secret on generation worker routes."""
        expected = self.config.worker_token.strip()
        supplied = (credential or "").strip()
        if not expected:
            raise PermissionError("生成 worker 身份尚未配置")
        if not supplied or len(supplied) > MAX_BEARER_LENGTH or not hmac.compare_digest(supplied, expected):
            raise PermissionError("生成 worker 身份无效")
        return Principal(
            user_id=principal.user_id,
            workspace_id=principal.workspace_id,
            role=principal.role,
            token=principal.token,
            is_worker=True,
        )

    def _response_or_raise(self, response: HTTPResponse, operation: str) -> Any:
        if 200 <= response.status < 300:
            return _decode_json_bytes(response.body)
        message = _json_error_message(response.body)
        if response.status in {401, 403}:
            raise PermissionError(message or "没有权限执行此操作")
        if 400 <= response.status < 500:
            raise ValueError(message or f"{operation}请求无效")
        raise UpstreamError(f"{operation}失败 ({response.status})")

    def rpc(
        self,
        principal: Principal,
        name: str,
        params: dict[str, Any] | None = None,
        *,
        privileged: bool = False,
    ) -> Any:
        if not SAFE_IDENTIFIER.fullmatch(str(name)):
            raise ValueError("RPC 名称无效")
        if params is not None and not isinstance(params, dict):
            raise ValueError("RPC 参数必须是 JSON 对象")
        headers = self._privileged_headers() if privileged else self._user_headers(principal.token)
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=representation"
        response = self._request(
            "POST",
            f"{self.config.supabase_url}/rest/v1/rpc/{name}",
            headers,
            json.dumps(params or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        return self._response_or_raise(response, "数据库函数")

    def _ai_review_matches_query(self, query: dict[str, str] | None) -> dict[str, str]:
        """Build the only permitted query shape for the workspace-less match table.

        ``ai_review_matches`` has no ``workspace_id`` column. It must be scoped
        through both foreign-key relations so a malicious caller cannot widen
        the read across workspaces. The embedded resources are fixed here and
        the workspace predicates are always overwritten from configuration.
        """
        scoped: dict[str, str] = {}
        for key, value in (query or {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("查询条件的键和值必须是字符串")
            if key in AI_REVIEW_MATCHES_RESERVED_FILTERS:
                raise ValueError("评审匹配查询不得覆盖固定工作区约束")
            if key == "select":
                if value not in {"*", AI_REVIEW_MATCHES_EMBED_SELECT}:
                    raise ValueError("评审匹配查询投影无效")
                continue
            if key in AI_REVIEW_MATCHES_UUID_FILTERS:
                if not AI_REVIEW_MATCHES_UUID_VALUE.fullmatch(value):
                    raise ValueError("评审匹配查询标识无效")
            elif key == "variant":
                if value not in {"eq.baseline", "eq.assisted"}:
                    raise ValueError("评审匹配查询变体无效")
            else:
                raise ValueError("评审匹配查询包含不允许的字段")
            scoped[key] = value

        scoped["select"] = AI_REVIEW_MATCHES_EMBED_SELECT
        scoped["ai_review_submissions.workspace_id"] = f"eq.{self.config.workspace_id}"
        scoped["ai_review_records.workspace_id"] = f"eq.{self.config.workspace_id}"
        return scoped

    def select(
        self,
        principal: Principal,
        table: str,
        query: dict[str, str] | None = None,
        *,
        privileged: bool = False,
    ) -> Any:
        table = str(table)
        if not SAFE_TABLE.fullmatch(table):
            raise ValueError("数据表名称无效")
        if query is not None and not isinstance(query, dict):
            raise ValueError("查询条件必须是字符串字典")
        if table == AI_REVIEW_MATCHES_TABLE:
            if not privileged:
                raise PermissionError("评审匹配仅允许服务端读取")
            scoped = self._ai_review_matches_query(query)
        else:
            scoped = {}
            for key, value in (query or {}).items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError("查询条件的键和值必须是字符串")
                scoped[key] = value
            if table == "workspaces":
                scoped["id"] = f"eq.{self.config.workspace_id}"
            else:
                scoped["workspace_id"] = f"eq.{self.config.workspace_id}"
            scoped.setdefault("select", "*")
        headers = self._privileged_headers() if privileged else self._user_headers(principal.token)
        response = self._request(
            "GET",
            f"{self.config.supabase_url}/rest/v1/{table}?{urllib.parse.urlencode(scoped)}",
            headers,
        )
        return self._response_or_raise(response, "数据读取")

    def _validate_storage_path(self, path: str) -> str:
        if not isinstance(path, str) or not path or len(path) > 1024:
            raise ValueError("存储路径无效")
        if path.startswith("/") or "\\" in path or "\x00" in path:
            raise ValueError("存储路径必须是私有相对路径")
        decoded = urllib.parse.unquote(path)
        if "\\" in decoded or "\x00" in decoded:
            raise ValueError("存储路径无效")
        pure = PurePosixPath(decoded)
        segments = pure.parts
        if not segments or any(part in {"", ".", ".."} for part in segments):
            raise ValueError("存储路径不能包含 . 或 ..")
        if segments[0] != self.config.workspace_id:
            raise ValueError("存储路径不在当前工作区")
        return "/".join(urllib.parse.quote(segment, safe="") for segment in segments)

    def storage_get(self, principal: Principal, path: str, *, privileged: bool = False) -> bytes:
        return self._storage_get(principal, path, privileged=privileged)

    def storage_get_bounded(self, principal: Principal, path: str, max_bytes: int) -> bytes | None:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("存储读取预算必须是非负整数")
        try:
            return self._storage_get(principal, path, max_bytes=min(max_bytes, self.config.max_storage_bytes))
        except _StorageReadLimitExceeded:
            return None

    def _storage_get(
        self, principal: Principal, path: str, *, privileged: bool = False, max_bytes: int | None = None,
    ) -> bytes:
        encoded = self._validate_storage_path(path)
        headers = self._privileged_headers() if privileged else self._user_headers(principal.token)
        url = f"{self.config.supabase_url}/storage/v1/object/{self.config.storage_bucket}/{encoded}"
        # Custom transports retain the five-argument contract and are checked below.
        if max_bytes is not None and type(self.transport) is DirectTransport:
            response = self.transport._request(
                "GET", url, headers, None, self.config.request_timeout_seconds, storage_read_limit=max_bytes,
            )
        else:
            response = self._request("GET", url, headers)
        if response.status in {401, 403}:
            raise PermissionError("没有权限读取该文件")
        if _storage_object_not_found(response):
            raise ValueError("文件不存在")
        if response.status != 200:
            raise UpstreamError("文件读取失败")
        if max_bytes is not None and len(response.body) > max_bytes:
            raise _StorageReadLimitExceeded
        if len(response.body) > self.config.max_storage_bytes:
            raise UpstreamError("存储响应超过大小限制")
        return response.body

    def storage_put(self, principal: Principal, path: str, data: bytes, content_type: str) -> Any:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError("文件内容必须是二进制数据")
        data = bytes(data)
        if len(data) > self.config.max_storage_bytes:
            raise BodyTooLarge("文件超过存储大小限制")
        content_type = str(content_type).strip()
        if not content_type or len(content_type) > 180 or any(char in content_type for char in "\r\n"):
            raise ValueError("Content-Type 无效")
        encoded = self._validate_storage_path(path)
        headers = self._user_headers(principal.token)
        headers.update(
            {
                "Content-Type": content_type,
                "Cache-Control": "no-store",
                "x-upsert": "false",
            }
        )
        response = self._request(
            "POST",
            f"{self.config.supabase_url}/storage/v1/object/{self.config.storage_bucket}/{encoded}",
            headers,
            data,
        )
        return self._response_or_raise(response, "文件写入")

    def _chat_completions_url(self) -> str:
        base = self.config.model_gateway_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def chat_json(self, messages: list[dict[str, Any]]) -> Any:
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages 必须是非空数组")
        if not all(isinstance(message, dict) for message in messages):
            raise ValueError("messages 中每一项必须是 JSON 对象")
        if not self._model_slots.acquire(timeout=self.config.request_timeout_seconds):
            raise GatewayTimeout("模型请求排队超时")
        try:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if self.config.model_api_key:
                headers["Authorization"] = f"Bearer {self.config.model_api_key}"
            payload = {
                "model": self.config.model_id,
                "messages": messages,
                "response_format": {"type": "json_object"},
            }
            response = self._request(
                "POST",
                self._chat_completions_url(),
                headers,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                timeout=self.config.model_timeout_seconds,
            )
            if response.status in {401, 403}:
                raise UpstreamError("模型网关认证失败")
            if response.status != 200:
                raise UpstreamError(f"模型请求失败 ({response.status})")
            data = _decode_json_bytes(response.body)
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise UpstreamError("模型响应缺少 choices[0].message.content") from None
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", ""))
                    for item in content
                    if isinstance(item, dict) and item.get("type") in {"text", "output_text"}
                )
            if not isinstance(content, str):
                raise UpstreamError("模型响应内容格式无效")
            return _extract_json_value(content)
        finally:
            self._model_slots.release()

    def make_context(
        self, principal: Principal, body: dict[str, Any], query: dict[str, str]
    ) -> RequestContext:
        return RequestContext(self, principal, body, query)


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], gateway: Gateway, router: Router, *, professional=None):
        self.gateway = gateway
        self.router = router
        self.professional = professional
        super().__init__(address, GatewayRequestHandler)


class GatewayRequestHandler(BaseHTTPRequestHandler):
    server: GatewayHTTPServer
    protocol_version = "HTTP/1.1"

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        path = urllib.parse.urlsplit(self.path).path
        LOGGER.info("%s %s -> %s", self.command, path, code)

    def log_message(self, format: str, *args: Any) -> None:
        # Deliberately omit headers, query strings and bodies.
        return

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _origin(self) -> str:
        return (self.headers.get("Origin") or "").strip().rstrip("/")

    def _common_headers(self, origin: str) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        if origin and origin in self.server.gateway.config.allowed_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Methods"] = ("GET, POST, OPTIONS" if self.server.professional is not None else "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            headers["Access-Control-Max-Age"] = "600"
            headers["Vary"] = "Origin"
        return headers

    def _check_origin(self, origin: str) -> None:
        if origin and origin not in self.server.gateway.config.allowed_origins:
            raise PermissionError("请求来源不允许")

    def _send(self, status: int, payload: Any, origin: str, extra: Mapping[str, str] | None = None) -> None:
        if payload is None and status == 204:
            body = b""
        else:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        headers = self._common_headers(origin)
        if extra:
            headers.update(extra)
        if self.server.professional is not None and self.close_connection:
            headers["Connection"] = "close"
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(body))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _bearer_token(self) -> str:
        value = (self.headers.get("Authorization") or "").strip()
        scheme, separator, token = value.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError()
        return token.strip()

    def _read_body(self, *, limit: int | None = None) -> dict[str, Any]:
        if (self.headers.get("Transfer-Encoding") or "").strip():
            raise ValueError("不支持分块请求体")
        raw_length = (self.headers.get("Content-Length") or "0").strip()
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0:
            raise ValueError("Content-Length 无效")
        if length > min(self.server.gateway.config.max_body_bytes, limit or self.server.gateway.config.max_body_bytes):
            raise BodyTooLarge()
        if length == 0:
            return {}
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("请求体必须是 application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("请求体不完整")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _dispatch(self, method: str) -> None:
        origin = self._origin()
        if self.server.professional is not None:
            self.close_connection = True
        try:
            self._check_origin(origin)
            if method == "OPTIONS":
                self._send(204, None, origin)
                return
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            if method == "GET" and path == "/healthz":
                self._send(200, {"ok": True, "service": "guanye-ai-gateway"}, origin)
                return
            if self.server.professional is not None:
                self._dispatch_professional(method, path, origin)
                return
            principal = self.server.gateway.authenticate(self._bearer_token())
            handler = self.server.router.resolve(method, path)
            if path.startswith(WORKER_ROUTE_PREFIX):
                principal = self.server.gateway.authenticate_worker(
                    principal, next((self.headers.get(name) for name in WORKER_HEADER_ALIASES if self.headers.get(name)), None)
                )
            body = self._read_body() if method in BODY_METHODS else {}
            query_raw = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query, keep_blank_values=True
            )
            query = {key: values[0] for key, values in query_raw.items() if values}
            context = self.server.gateway.make_context(principal, body, query)
            result = handler(context)
            if isinstance(result, tuple):
                if len(result) != 2:
                    raise RuntimeError("handler 返回的 tuple 必须是 (status, JSON)")
                status, payload = result
                if not isinstance(status, int) or not 100 <= status <= 599:
                    raise RuntimeError("handler 返回的状态码无效")
            else:
                status, payload = 200, result
            self._send(status, payload, origin)
        except GatewayError as exc:
            self._send(
                exc.status,
                {"error": exc.public_message if exc.status >= 500 else exc.detail, "code": exc.code},
                origin,
            )
        except AuthenticationError as exc:
            self._send(401, {"error": exc.detail, "code": exc.code}, origin)
        except PermissionError as exc:
            self._send(403, {"error": str(exc) or "没有权限", "code": "forbidden"}, origin)
        except ValueError as exc:
            self._send(400, {"error": str(exc) or "请求无效", "code": "invalid_request"}, origin)
        except Exception as exc:
            LOGGER.error("Unhandled gateway error (%s)", type(exc).__name__)
            self._send(500, {"error": "服务内部错误", "code": "internal_error"}, origin)

    def _dispatch_professional(self, method: str, path: str, origin: str) -> None:
        from cloud.professional_creation import ANALYZE, MAX_ANALYZE_BODY, MAX_EDIT_BODY, ROUTES
        from cloud.professional_import import ImportErrorResponse

        # Rejected uploads may still have unread bytes on the connection.
        self.close_connection = True
        if (method, path) not in ROUTES:
            raise RouteNotFound()
        if len(self.path) > 2048:
            raise ValueError("请求地址过长")
        if len(self.headers.get_all("Content-Length", [])) > 1:
            raise ValueError("Content-Length 重复")
        token = self._bearer_token()
        pairs = urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query,
                                        keep_blank_values=True, strict_parsing=True, max_num_fields=5)
        query = dict(pairs)
        if len(query) != len(pairs) or method == "POST" and query:
            raise ValueError("请求参数无效")
        if method == "GET" and (self.headers.get("Transfer-Encoding") is not None
                                or self.headers.get_all("Content-Length", []) not in ([], ["0"])):
            raise ValueError("读取请求不能包含正文")
        service = self.server.professional
        try:
            actor = service.authorize(token)
            with service.slot(path):
                if method == "POST":
                    self.connection.settimeout(30)
                    try:
                        body = self._read_body(limit=MAX_ANALYZE_BODY if path == ANALYZE else MAX_EDIT_BODY)
                    except TimeoutError:
                        self.close_connection = True
                        raise ImportErrorResponse(408, "读取请求超时") from None
                else:
                    body = {}
                status, value = service.handle(method, path, actor, token, body, query)
        except ImportErrorResponse as exc:
            self.close_connection = True
            self._send(exc.status, {"error": str(exc)[:300]}, origin)
            return
        self._send(status, value, origin)


def build_router(modules: Sequence[Any] | None = None) -> Router:
    router = Router()
    register_ai_modules(router, modules)
    return router


class ReviewWorkerContext(RequestContext):
    """Local worker context constructed from the workspace's current judge setting."""

    def __init__(
        self,
        gateway: Gateway,
        principal: Principal,
        body: dict[str, Any],
        query: dict[str, str],
        *,
        stop_event: threading.Event | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(gateway, principal, body, query)
        self._stop_event = stop_event
        self._stop_requested = stop_requested

    def rpc(self, name: str, params: dict[str, Any] | None = None, privileged: bool = False) -> Any:
        # This is the last check before the queue's atomic claim RPC. Any claim
        # already inside that RPC may finish; no later lane claim may start.
        if name == "ai_review_claim_jobs" and _review_worker_should_stop(
            self._stop_event, self._stop_requested
        ):
            return []
        return super().rpc(name, params, privileged=privileged)

    def storage_get(self, path: str) -> bytes:
        return self._gateway.storage_get(self._principal(), path, privileged=True)


REVIEW_WORKER_LANES = ("uploaded", "generated")
REVIEW_WORKER_IDLE_SECONDS = 5.0
REVIEW_WORKER_ERROR_SECONDS = 15.0
REVIEW_WORKER_SIGNAL_POLL_SECONDS = 0.1


class _ReviewWorkerStopIntent:
    """Lock-free SIGTERM intent consumed by the worker's normal control path."""

    __slots__ = ("_requested",)

    def __init__(self) -> None:
        self._requested = False

    def request(self, _signum: int, _frame: object) -> None:
        self._requested = True

    def is_requested(self) -> bool:
        return self._requested


def _review_worker_should_stop(
    stop_event: threading.Event | None,
    stop_requested: Callable[[], bool] | None,
) -> bool:
    if stop_event is not None and stop_event.is_set():
        return True
    if stop_requested is None or not stop_requested():
        return False
    LOGGER.info("Review worker stop requested; draining in-flight attempts")
    if stop_event is not None:
        stop_event.set()
    return True


def _wait_review_worker(
    stop_event: threading.Event,
    seconds: float,
    *,
    stop_requested: Callable[[], bool] | None,
    signal_poll_seconds: float,
) -> bool:
    if _review_worker_should_stop(stop_event, stop_requested):
        return True
    if stop_requested is None:
        return stop_event.wait(seconds)
    poll_seconds = max(0.01, float(signal_poll_seconds))
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _review_worker_should_stop(stop_event, stop_requested)
        if stop_event.wait(min(remaining, poll_seconds)):
            return True
        if _review_worker_should_stop(stop_event, stop_requested):
            return True


def _review_worker_context(
    gateway: Gateway,
    *,
    stop_event: threading.Event | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> ReviewWorkerContext | None:
    if _review_worker_should_stop(stop_event, stop_requested):
        return None
    workspace = gateway.config.workspace_id
    local = Principal("", workspace, "", "")
    rows = gateway.select(local, "workspaces", {"select": "dedicated_judge_id"}, privileged=True)
    if _review_worker_should_stop(stop_event, stop_requested):
        return None
    if not isinstance(rows, list) or not rows or not rows[0].get("dedicated_judge_id"):
        return None
    judge = Principal(rows[0]["dedicated_judge_id"], workspace, "reviewer", "")
    return ReviewWorkerContext(
        gateway,
        judge,
        {},
        {},
        stop_event=stop_event,
        stop_requested=stop_requested,
    )


def _review_worker_kind_cycle(
    gateway: Gateway,
    kind: str,
    *,
    stop_event: threading.Event | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    if kind not in REVIEW_WORKER_LANES:
        raise ValueError("预评轨道无效")
    ctx = _review_worker_context(
        gateway,
        stop_event=stop_event,
        stop_requested=stop_requested,
    )
    if ctx is None:
        return False
    if _review_worker_should_stop(stop_event, stop_requested):
        return False
    reviews = importlib.import_module("cloud.ai_reviews")
    return reviews.run_review_attempt(ctx, kind) is not None


def review_worker_cycle(
    gateway: Gateway,
    *,
    generated_first: bool = False,
    stop_event: threading.Event | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> bool:
    """Process at most one queued review using the legacy alternating order.

    Existing callers keep the original ``generated_first`` contract. The optional
    stop event and intent are rechecked by the context immediately before each
    atomic claim RPC, including after workspace lookup and an empty first lane.
    """
    reviews = importlib.import_module("cloud.ai_reviews")
    ctx = _review_worker_context(
        gateway,
        stop_event=stop_event,
        stop_requested=stop_requested,
    )
    if ctx is None:
        return False
    kinds = ("generated", "uploaded") if generated_first else ("uploaded", "generated")
    for kind in kinds:
        if _review_worker_should_stop(stop_event, stop_requested):
            return False
        if reviews.run_review_attempt(ctx, kind) is not None:
            return True
    return False


def _review_worker_lane(
    gateway: Gateway,
    kind: str,
    stop_event: threading.Event,
    *,
    idle_seconds: float = REVIEW_WORKER_IDLE_SECONDS,
    error_seconds: float = REVIEW_WORKER_ERROR_SECONDS,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    while not _review_worker_should_stop(stop_event, stop_requested):
        try:
            cycle_kwargs: dict[str, Any] = {"stop_event": stop_event}
            if stop_requested is not None:
                cycle_kwargs["stop_requested"] = stop_requested
            worked = _review_worker_kind_cycle(gateway, kind, **cycle_kwargs)
            if _review_worker_should_stop(stop_event, stop_requested):
                break
            if not worked and stop_event.wait(idle_seconds):
                break
        except Exception as exc:
            LOGGER.warning("Review worker %s cycle failed (%s)", kind, type(exc).__name__)
            if stop_event.wait(error_seconds):
                break
    return 0


def _serve_review_worker_serial(
    gateway: Gateway,
    stop_event: threading.Event,
    *,
    idle_seconds: float = REVIEW_WORKER_IDLE_SECONDS,
    error_seconds: float = REVIEW_WORKER_ERROR_SECONDS,
    stop_requested: Callable[[], bool] | None = None,
    signal_poll_seconds: float = REVIEW_WORKER_SIGNAL_POLL_SECONDS,
) -> int:
    """Preserve the original single-thread alternating worker for model limit 1."""
    generated_first = False
    while not _review_worker_should_stop(stop_event, stop_requested):
        try:
            cycle_kwargs: dict[str, Any] = {
                "generated_first": generated_first,
                "stop_event": stop_event,
            }
            if stop_requested is not None:
                cycle_kwargs["stop_requested"] = stop_requested
            worked = review_worker_cycle(gateway, **cycle_kwargs)
            generated_first = not generated_first
            if not worked and _wait_review_worker(
                stop_event,
                idle_seconds,
                stop_requested=stop_requested,
                signal_poll_seconds=signal_poll_seconds,
            ):
                break
        except Exception as exc:
            LOGGER.warning("Review worker cycle failed (%s)", type(exc).__name__)
            if _wait_review_worker(
                stop_event,
                error_seconds,
                stop_requested=stop_requested,
                signal_poll_seconds=signal_poll_seconds,
            ):
                break
    return 0


def _serve_review_worker_parallel(
    gateway: Gateway,
    stop_event: threading.Event,
    *,
    idle_seconds: float = REVIEW_WORKER_IDLE_SECONDS,
    error_seconds: float = REVIEW_WORKER_ERROR_SECONDS,
    stop_requested: Callable[[], bool] | None = None,
    signal_poll_seconds: float = REVIEW_WORKER_SIGNAL_POLL_SECONDS,
) -> int:
    """Run one isolated listener per review lane, with two workers by default."""
    if _review_worker_should_stop(stop_event, stop_requested):
        return 0
    threads = [
        threading.Thread(
            target=_review_worker_lane,
            args=(gateway, kind, stop_event),
            kwargs={
                "idle_seconds": idle_seconds,
                "error_seconds": error_seconds,
                "stop_requested": stop_requested,
            },
            name=f"guanye-review-worker-{kind}",
            daemon=False,
        )
        for kind in REVIEW_WORKER_LANES
    ]
    started: list[threading.Thread] = []
    poll_seconds = max(0.01, float(signal_poll_seconds))
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
        while not stop_event.is_set():
            if _review_worker_should_stop(stop_event, stop_requested):
                break
            if stop_event.wait(poll_seconds):
                break
    finally:
        stop_event.set()
        for thread in started:
            thread.join()
    return 0


def _run_review_worker(
    gateway: Gateway,
    *,
    stop_event: threading.Event | None = None,
    stop_requested: Callable[[], bool] | None = None,
    signal_poll_seconds: float = REVIEW_WORKER_SIGNAL_POLL_SECONDS,
) -> int:
    if not gateway.config.model_api_key:
        LOGGER.error("Review worker requires GUANYE_MODEL_API_KEY")
        return 2
    stopping = stop_event if stop_event is not None else threading.Event()
    if _review_worker_should_stop(stopping, stop_requested):
        return 0
    try:
        if gateway.config.model_max_concurrency <= 1:
            return _serve_review_worker_serial(
                gateway,
                stopping,
                stop_requested=stop_requested,
                signal_poll_seconds=signal_poll_seconds,
            )
        return _serve_review_worker_parallel(
            gateway,
            stopping,
            stop_requested=stop_requested,
            signal_poll_seconds=signal_poll_seconds,
        )
    except KeyboardInterrupt:
        stopping.set()
        return 0


def serve_review_worker(
    gateway: Gateway, *, stop_event: threading.Event | None = None
) -> int:
    """Embeddable review worker entrypoint; signal policy remains caller-owned."""
    return _run_review_worker(gateway, stop_event=stop_event)


def _serve_review_worker_cli(
    gateway: Gateway,
    *,
    stop_event: threading.Event | None = None,
    signal_poll_seconds: float = REVIEW_WORKER_SIGNAL_POLL_SECONDS,
) -> int:
    """Run the CLI worker with a minimal, restorable SIGTERM intent handler."""
    stopping = stop_event if stop_event is not None else threading.Event()
    intent = _ReviewWorkerStopIntent()
    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, intent.request)
    try:
        return _run_review_worker(
            gateway,
            stop_event=stopping,
            stop_requested=intent.is_requested,
            signal_poll_seconds=signal_poll_seconds,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Guanye AI HTTP gateway")
    parser.add_argument("--host", help="override GUANYE_HOST")
    parser.add_argument("--port", type=int, help="override GUANYE_PORT")
    parser.add_argument("--review-worker", action="store_true", help="process queued AI reviews without an HTTP listener")
    parser.add_argument("--professional", action="store_true", help="serve only professional web creation routes")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = GatewayConfig.from_env(require_service_role=not args.professional)
        if args.host:
            config = GatewayConfig(**{**config.__dict__, "host": args.host})
        if args.port:
            config = GatewayConfig(**{**config.__dict__, "port": args.port})
        gateway = Gateway(config)
        if args.review_worker:
            if args.professional:
                parser.error("--professional does not use the legacy review worker")
            return _serve_review_worker_cli(gateway)
        professional = None
        if args.professional:
            from cloud.professional_creation import ProfessionalCreation
            if not config.allowed_origins or not config.model_api_key:
                raise GatewayConfigError("Professional gateway requires its allowed browser origin and model key")
            professional = ProfessionalCreation({"url": config.supabase_url, "anonKey": config.supabase_anon_key,
                                                 "workspaceId": config.workspace_id}, "", chat=gateway.chat_json, require_cutover=True)
        server = GatewayHTTPServer((config.host, config.port), gateway,
                                   Router() if professional else build_router(), professional=professional)
    except GatewayError as exc:
        LOGGER.error("Gateway startup failed: %s", exc.detail)
        return 2
    try:
        LOGGER.info("Guanye AI gateway listening on %s:%s", config.host, config.port)
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Guanye AI gateway stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    # Imports inside professional adapters must share these exception classes.
    sys.modules.setdefault("cloud.ai_gateway", sys.modules[__name__])
    raise SystemExit(main())
