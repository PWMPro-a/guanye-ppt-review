#!/usr/bin/env python3
"""Windows queue worker for Guanye AI generation.

It never starts Image2PPT and never talks to an image provider directly.  It
claims page jobs from the public gateway, drives the already-running EXE's
HTTP endpoints, and uploads only the resulting page images.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import ntpath
import os
import socket
import struct
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class WorkerError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class PreflightError(WorkerError):
    def __init__(self, message: str, stage: str):
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class WorkerRunResult:
    processed: int = 0
    claimed: int = 0
    failed: int = 0
    claim_failed: bool = False
    empty: bool = False
    once: bool = False


def _env_int(name: str, default: int, lower: int, upper: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < lower or value > upper:
        raise ValueError(f"{name} 必须在 {lower} 到 {upper} 之间")
    return value


def _env_float(name: str, default: float, lower: float, upper: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if value < lower or value > upper:
        raise ValueError(f"{name} 必须在 {lower} 到 {upper} 之间")
    return value


def _trust_env_proxy_disabled() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"


def _join(base: str, path: str) -> str:
    if not base:
        raise ValueError("服务地址不能为空")
    if not path.startswith("/"):
        path = "/" + path
    return base.rstrip("/") + path


def _safe_base_url(base: str) -> str:
    """Return a display-safe base URL without userinfo, query, or fragment."""
    parsed = urlsplit(base)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port is not None else "")
    return parsed._replace(netloc=netloc, query="", fragment="").geturl()


def _safe_same_origin_file_path(base: str, value: Any) -> str:
    """Validate an image URL as a same-origin /files/ path without guessing names."""
    if not isinstance(value, str) or not value.strip():
        raise WorkerError("Image2PPT 图片地址无效")
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.query or parsed.fragment:
        raise WorkerError("Image2PPT 图片地址不得包含查询参数或片段")
    if parsed.scheme or parsed.netloc:
        origin = urlsplit(base)
        try:
            same_origin = (
                parsed.scheme.lower() == origin.scheme.lower()
                and (parsed.hostname or "").lower() == (origin.hostname or "").lower()
                and parsed.port == origin.port
            )
        except ValueError:
            same_origin = False
        if not same_origin:
            raise WorkerError("Image2PPT 图片地址必须同源")
    path = parsed.path
    decoded = unquote(path)
    if (
        "\\" in decoded
        or not decoded.startswith("/files/")
        or decoded.startswith("//")
        or "//" in decoded[1:]
        or any(segment in (".", "..") for segment in decoded.split("/"))
    ):
        raise WorkerError("Image2PPT 图片地址必须是同源 /files/ 路径")
    return path


def _json_error(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or fallback)
        if isinstance(error, str) and error.strip():
            return error.strip()
        for key in ("message", "detail", "error_description"):
            if isinstance(payload.get(key), str) and payload[key].strip():
                return payload[key].strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


def _is_loopback_host(host: str | None) -> bool:
    return (host or "").lower() in {"localhost", "127.0.0.1", "::1"}


def _auth_error_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("error_code", "code"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


def _is_invalid_refresh_error(exc: WorkerError) -> bool:
    if exc.status not in (400, 401, 403):
        return False
    code = _auth_error_code(exc.payload)
    return code in {
        "invalid_grant",
        "refresh_token_already_used",
        "refresh_token_not_found",
        "session_expired",
        "session_not_found",
    }


def _is_gateway_auth_rejection(exc: WorkerError) -> bool:
    # The gateway authenticates the bearer token before it resolves or executes
    # any worker route. A 401 with this exact public code therefore proves the
    # request never reached claim, heartbeat, reference, completion or failure.
    return (
        exc.status == 401
        and isinstance(exc.payload, dict)
        and exc.payload.get("code") == "unauthorized"
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib copies Authorization and other non-Content headers to the
        # redirected request. Refuse instead of forwarding auth to another URL.
        raise HTTPError(req.full_url, code, msg, headers, fp)


@dataclass(repr=False)
class SupabaseAuthConfig:
    supabase_url: str
    anon_key: str
    email: str
    password: str

    def __post_init__(self) -> None:
        self.supabase_url = self.supabase_url.strip().rstrip("/")
        self.anon_key = self.anon_key.strip()
        self.email = self.email.strip()
        parsed = urlsplit(self.supabase_url)
        if parsed.username or parsed.password:
            raise ValueError("GUANYE_SUPABASE_URL 不得包含账号或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("GUANYE_SUPABASE_URL 不得包含查询参数或片段")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and _is_loopback_host(parsed.hostname)
        ):
            raise ValueError("GUANYE_SUPABASE_URL 必须使用 HTTPS；HTTP 仅允许本机回环")
        if not parsed.hostname:
            raise ValueError("GUANYE_SUPABASE_URL 无效")
        if not self.anon_key:
            raise ValueError("GUANYE_SUPABASE_ANON_KEY 不能为空")
        if not self.email:
            raise ValueError("GUANYE_WORKER_EMAIL 不能为空")
        if not self.password:
            raise ValueError("GUANYE_WORKER_PASSWORD 不能为空")


class _AuthSession:
    __slots__ = ("access_token", "refresh_token", "expires_at")

    def __init__(self, access_token: str, refresh_token: str, expires_at: float):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at


class SupabaseTokenProvider:
    """Process-local password/refresh token state shared by worker threads."""

    def __init__(self, config: SupabaseAuthConfig, timeout: float = 30.0,
                 clock: Any = None, expiry_margin: float = 10.0):
        base = config.supabase_url.rstrip("/")
        if not base.endswith("/auth/v1"):
            base += "/auth/v1"
        self.config = config
        self.base = base
        self.timeout = timeout
        self._clock = clock or time.monotonic
        self._expiry_margin = max(0.0, float(expiry_margin))
        self._lock = threading.Lock()
        self._session: _AuthSession | None = None
        self.opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    @staticmethod
    def _expires_at(clock: float, expires_in: int) -> float:
        return clock + expires_in

    def access_token(self) -> str:
        with self._lock:
            session = self._session
            if session is None:
                return self._login_locked()
            if session.expires_at <= self._clock() + self._expiry_margin:
                return self._refresh_locked()
            return session.access_token

    def refresh_after_unauthorized(self, rejected_token: str) -> str:
        with self._lock:
            session = self._session
            if session is not None and session.access_token != rejected_token:
                # Another worker thread already refreshed the rotating token.
                return session.access_token
            if session is None:
                return self._login_locked()
            return self._refresh_locked()

    def _login_locked(self) -> str:
        payload = self._token_request("password", {
            "email": self.config.email,
            "password": self.config.password,
        })
        session = self._store_session(payload)
        return session.access_token

    def _refresh_locked(self) -> str:
        session = self._session
        if session is None:
            return self._login_locked()
        try:
            payload = self._token_request("refresh_token", {
                "refresh_token": session.refresh_token,
            })
        except WorkerError as exc:
            if not _is_invalid_refresh_error(exc):
                raise
            # Only a known terminal refresh rejection may fall back to one
            # password login. Network errors, 429/5xx and unknown bodies never do.
            return self._login_locked()
        return self._store_session(payload).access_token

    def _store_session(self, payload: Any) -> _AuthSession:
        if not isinstance(payload, dict):
            raise WorkerError("Supabase Auth 响应无效")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise WorkerError("Supabase Auth 响应缺少 access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise WorkerError("Supabase Auth 响应缺少 refresh_token")
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
            raise WorkerError("Supabase Auth 响应缺少有效 expires_in")
        session = _AuthSession(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=self._expires_at(self._clock(), expires_in),
        )
        self._session = session
        return session

    def _token_request(self, grant_type: str, body: dict[str, str]) -> Any:
        url = _join(self.base, "token?grant_type=" + grant_type)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "apikey": self.config.anon_key,
            "Authorization": "Bearer " + self.config.anon_key,
        }
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        operation = "登录" if grant_type == "password" else "刷新"
        limit = 1024 * 1024
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(limit + 1)
        except HTTPError as exc:
            try:
                raw = exc.read(limit + 1)
            finally:
                exc.close()
            payload: Any = None
            if len(raw) <= limit:
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, ValueError):
                    payload = None
            error_code = _auth_error_code(payload)
            safe_payload = {"code": error_code} if error_code else None
            raise WorkerError(
                f"Supabase Auth {operation}失败 (HTTP {exc.code})",
                exc.code,
                safe_payload,
            ) from exc
        except (URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            raise WorkerError(f"连接 Supabase Auth 失败：{exc}") from exc
        if len(raw) > limit:
            raise WorkerError("Supabase Auth 响应过大")
        if not raw:
            raise WorkerError("Supabase Auth 响应为空")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkerError("Supabase Auth 响应不是有效 JSON") from exc


class JsonHttpClient:
    def __init__(self, base: str, token: str = "", workspace_id: str = "", timeout: float = 30.0,
                 extra_headers: dict[str, str] | None = None, *, no_redirect: bool = False):
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("HTTP 服务地址无效")
        self.base = base.rstrip("/")
        self.token = token
        self.workspace_id = workspace_id
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        handlers = [ProxyHandler({})]
        if no_redirect:
            handlers.append(_NoRedirectHandler())
        self.opener = build_opener(*handlers)

    def _headers(self, auth_token: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = self.token if auth_token is None else auth_token
        if token:
            headers["X-Access-Code"] = token
            headers["Authorization"] = "Bearer " + token
        if self.workspace_id:
            headers["X-Workspace-Id"] = self.workspace_id
        headers.update(self.extra_headers)
        return headers

    def request(self, method: str, path: str, body: Any = None, timeout: float | None = None,
                *, template: tuple[bytes, str] | None = None,
                auth_token: str | None = None) -> Any:
        headers = self._headers(auth_token)
        data = None
        if template is not None:
            image, content_type = template
            extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[content_type]
            boundary = uuid.uuid4().hex
            part = (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="template_image"; filename="reference.{extension}"\r\n'
                f'Content-Type: {content_type}\r\n\r\n'
            )
            data = part.encode("ascii") + image + f"\r\n--{boundary}--\r\n".encode("ascii")
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(_join(self.base, path), data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                payload = response.read()
                if not payload:
                    return {}
                return json.loads(payload.decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, ValueError):
                payload = raw.decode("utf-8", "replace")
            raise WorkerError(_json_error(payload, f"HTTP {exc.code}"), exc.code, payload) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise WorkerError(f"连接 {self.base} 失败：{exc}") from exc

    def download(self, path: str, timeout: float | None = None) -> tuple[bytes, str]:
        request = Request(_join(self.base, path), headers=self._headers(), method="GET")
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(25 * 1024 * 1024 + 1)
                if len(raw) > 25 * 1024 * 1024:
                    raise WorkerError("页面图片超过 25MB")
                content_type = (response.headers.get_content_type() or "").lower()
                return raw, content_type
        except HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, ValueError):
                payload = raw.decode("utf-8", "replace")
            raise WorkerError(_json_error(payload, f"HTTP {exc.code}"), exc.code, payload) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise WorkerError(f"下载页面图片失败：{exc}") from exc


_REQUEST_BODY_UNSET = object()


class GatewayClient:
    def __init__(self, base: str, token: str, workspace_id: str, timeout: float = 30.0,
                 worker_token: str = "", auth_provider: SupabaseTokenProvider | None = None):
        if not token and auth_provider is None:
            raise ValueError("网关访问令牌不能为空")
        if not workspace_id:
            raise ValueError("GUANYE_WORKSPACE_ID 不能为空")
        if not worker_token:
            raise ValueError("GUANYE_WORKER_TOKEN 不能为空")
        self.auth_provider = auth_provider
        self.http = JsonHttpClient(
            base, token, workspace_id, timeout,
            extra_headers={"X-Guanye-Worker-Token": worker_token},
            no_redirect=True,
        )

    def _http_request(self, method: str, path: str, body: Any,
                      timeout: float | None,
                      template: tuple[bytes, str] | None = None,
                      auth_token: str | None = None) -> Any:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if template is not None:
            kwargs["template"] = template
        if auth_token is not None:
            kwargs["auth_token"] = auth_token
        if body is _REQUEST_BODY_UNSET:
            return self.http.request(method, path, **kwargs)
        return self.http.request(method, path, body, **kwargs)

    def _request(self, method: str, path: str, body: Any = _REQUEST_BODY_UNSET,
                 timeout: float | None = None,
                 *, template: tuple[bytes, str] | None = None) -> Any:
        if self.auth_provider is None:
            return self._http_request(method, path, body, timeout, template)
        token = self.auth_provider.access_token()
        try:
            return self._http_request(
                method, path, body, timeout, template, auth_token=token,
            )
        except WorkerError as exc:
            if not _is_gateway_auth_rejection(exc):
                raise
            retry_token = self.auth_provider.refresh_after_unauthorized(token)
            return self._http_request(
                method, path, body, timeout, template, auth_token=retry_token,
            )

    def reference_image(self, page: dict[str, Any]) -> tuple[bytes, str]:
        result = self._request("POST", "/api/ai/generation/worker/reference", {
            "page_id": page["pageId"],
            "lease_token": page["leaseToken"],
        }, timeout=60.0)
        encoded = result.get("image_base64") if isinstance(result, dict) else None
        if not isinstance(encoded, str) or len(encoded) > 4 * ((25 * 1024 * 1024 + 2) // 3):
            raise WorkerError("生成参考图超限或无效")
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise WorkerError("生成参考图无效") from exc
        content_type = _detect_image_type(image)
        if len(image) > 25 * 1024 * 1024 or content_type is None or content_type != result.get("content_type"):
            raise WorkerError("生成参考图超限或格式不符")
        return image, content_type

    def get_job(self, job_id: str) -> dict[str, Any]:
        result = self._request("GET", "/api/ai/generation?job_id=" + str(job_id))
        if isinstance(result, dict) and isinstance(result.get("job"), dict):
            result = result["job"]
        if not isinstance(result, dict) or not result.get("id"):
            raise WorkerError("网关返回的生成任务无效")
        return result

    def claim(self, worker_id: str, limit: int, lease_seconds: int) -> list[dict[str, Any]]:
        result = self._request("POST", "/api/ai/generation/worker/claim", {
            "worker_id": worker_id,
            "limit": limit,
            "lease_seconds": lease_seconds,
        })
        if isinstance(result, dict) and isinstance(result.get("job"), dict):
            result = result["job"]
        pages = result.get("pages") if isinstance(result, dict) else None
        if not isinstance(pages, list):
            raise WorkerError("网关领取结果无效")
        return [page for page in pages if isinstance(page, dict)]

    def heartbeat(self, page_id: str, lease_token: str, lease_seconds: int) -> None:
        self._request("POST", "/api/ai/generation/worker/heartbeat", {
            "page_id": page_id,
            "lease_token": lease_token,
            "lease_seconds": lease_seconds,
        })

    def complete(self, page: dict[str, Any], raw: bytes, content_type: str, width: int | None, height: int | None) -> dict[str, Any]:
        result = self._request("POST", "/api/ai/generation/worker/complete", {
            "page_id": page["pageId"],
            "lease_token": page["leaseToken"],
            "image_base64": base64.b64encode(raw).decode("ascii"),
            "content_type": content_type,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "width": width,
            "height": height,
        }, timeout=120.0)
        return result if isinstance(result, dict) else {}

    def fail(self, page: dict[str, Any], error: str) -> None:
        self._request("POST", "/api/ai/generation/worker/fail", {
            "page_id": page["pageId"],
            "lease_token": page["leaseToken"],
            "error": error[:2000],
        })


def _unverified_runtime_image(release_shell: str | None = None) -> dict[str, Any]:
    """Describe what HTTP protocol probes cannot establish about the EXE."""
    result = {
        "verified": False,
        "sha256": None,
        "reason": (
            "HTTP 响应只能证明已运行服务的协议兼容性，不能识别可执行文件或进程映像；"
            "PyInstaller 程序的摘要必须对确切 EXE 文件测量。"
        ),
        "measurement": "Get-FileHash -Algorithm SHA256 -LiteralPath <确切 EXE 路径>",
    }
    if release_shell:
        result["releaseShell"] = release_shell
        result["reason"] = (
            "desktop-ready.json 标识了启动壳文件名，但 HTTP 响应不能验证该 EXE 的摘要或进程映像；"
            "程序摘要必须对确切 EXE 文件测量。"
        )
    return result


class ExeClient:
    """HTTP adapter for the existing Image2PPT desktop service."""

    def __init__(self, base: str, token: str, state: "LocalState", timeout: float = 30.0,
                 task_timeout: float = 1800.0, poll_seconds: float = 2.0, *,
                 desktop_proxy: bool = False, endpoint_source: str = "direct",
                 ready_file: str | None = None, release_shell: str | None = None):
        self.http = JsonHttpClient(base, token, "", timeout)
        self.state = state
        self.task_timeout = task_timeout
        self.poll_seconds = poll_seconds
        self.desktop_proxy = desktop_proxy
        self.endpoint_source = endpoint_source
        self.ready_file = ready_file
        self.release_shell = release_shell
        self._project_lock = threading.RLock()

    def preflight(self) -> dict[str, Any]:
        # These read-only probes verify HTTP compatibility only. They do not
        # identify the executable/process image and never start a conversion.
        def read(path: str, timeout: float, stage: str, label: str) -> Any:
            try:
                return self.http.request("GET", path, timeout=timeout)
            except WorkerError as exc:
                suffix = f" (HTTP {exc.status})" if exc.status is not None else ""
                raise PreflightError(f"{label}请求失败{suffix}", stage) from None
            except (UnicodeDecodeError, ValueError):
                raise PreflightError(f"{label}响应不是有效 JSON", stage) from None

        jobs_summary: dict[str, Any] | None = None
        health_summary: dict[str, str] | None = None
        if self.desktop_proxy:
            jobs_path = "/api/image2ppt/jobs?limit=1"
            jobs_payload = read(jobs_path, 15.0, "jobs", "任务列表检查")
            if not isinstance(jobs_payload, dict) or not isinstance(jobs_payload.get("data"), dict):
                raise PreflightError("任务列表响应缺少 data 对象", "jobs")
            jobs = jobs_payload["data"].get("jobs")
            if not isinstance(jobs, list):
                raise PreflightError("任务列表响应 data.jobs 必须是数组", "jobs")
            jobs_summary = {
                "probe": jobs_path,
                "count": len(jobs),
                "empty": len(jobs) == 0,
            }
            checks = [jobs_path, "/api/image2ppt/config", "/api/image2ppt/settings"]
        else:
            health = _unwrap_data(read("/health", 10.0, "health", "健康检查"))
            if not isinstance(health, dict):
                raise PreflightError("健康检查响应必须是 JSON 对象", "health")
            if health.get("status") != "ok":
                raise PreflightError("健康检查状态不是 ok", "health")
            health_summary = {"status": "ok"}
            checks = ["/health", "/api/image2ppt/config", "/api/image2ppt/settings"]

        config = _unwrap_data(read("/api/image2ppt/config", 15.0, "config", "能力检查"))
        if not isinstance(config, dict):
            raise PreflightError("能力检查响应必须是 JSON 对象", "config")
        if config.get("worker_exists") is not True:
            raise PreflightError("生图 worker 缺失", "config")
        if "max_files" not in config:
            raise PreflightError("能力检查响应缺少 max_files", "config")
        max_files = config["max_files"]
        if max_files is not None and (type(max_files) is not int or max_files <= 0):
            raise PreflightError("能力检查响应的 max_files 必须为 null 或正整数", "config")

        settings = _unwrap_data(read("/api/image2ppt/settings", 15.0, "settings", "设置检查"))
        if not isinstance(settings, dict):
            raise PreflightError("设置检查响应必须是 JSON 对象", "settings")
        vision_model = settings.get("vision_model")
        if not isinstance(vision_model, str) or not vision_model.strip():
            raise PreflightError("设置检查响应缺少 vision_model", "settings")
        vision_model = vision_model.strip()
        modes = config.get("modes")
        if not isinstance(modes, list) or not all(isinstance(mode, str) for mode in modes):
            modes = None

        result = {
            "ok": True,
            "base": _safe_base_url(self.http.base),
            "desktopProxy": self.desktop_proxy,
            "endpointSource": self.endpoint_source,
            "readinessFile": self.ready_file,
            "releaseShell": self.release_shell,
            "transport": {
                "kind": "desktop-proxy" if self.desktop_proxy else "direct",
                "source": self.endpoint_source,
                "readinessFile": self.ready_file,
                "releaseShell": self.release_shell,
            },
            "httpProtocol": {
                "verified": True,
                "scope": "只读 HTTP 兼容性与能力探测",
                "checks": checks,
                "capabilities": {
                    "workerExists": True,
                    "maxFiles": max_files,
                    "modes": modes,
                    "visionModel": vision_model,
                },
            },
            "runtimeImage": _unverified_runtime_image(self.release_shell),
            "config": {
                "workerExists": True,
                "maxFiles": max_files,
                "modes": modes,
            },
            "settingsHttp": 200,
            "visionModel": vision_model,
            "paidRequests": 0,
            "startedProcess": False,
        }
        if health_summary is not None:
            result["health"] = health_summary
        if jobs_summary is not None:
            result["jobs"] = jobs_summary
        return result

    def _state_key(self, job: dict[str, Any]) -> str:
        key = f"{job.get('workspaceId', '')}/{job['jobId']}"
        # The desktop template is project-wide. Different page references must
        # never overwrite each other while renders are in flight.
        return f"{key}/{job['referenceSha256']}" if job.get("referenceSha256") else key

    def ensure_project(self, job: dict[str, Any], pages: list[dict[str, Any]],
                       reference: tuple[bytes, str] | None = None) -> dict[str, Any]:
        key = self._state_key(job)
        with self._project_lock:
            cached = self.state.project(key)
            if cached and cached.get("projectId"):
                try:
                    self.http.request("GET", "/api/projects/" + cached["projectId"])
                    self._ensure_pages(cached, job, pages)
                    return cached
                except WorkerError as exc:
                    if exc.status != 404:
                        raise
                    self.state.drop_project(key)
            created = self.http.request("POST", "/api/projects", {
                "creation_type": "descriptions",
                "title": job.get("title") or "AI 生成作品",
                "description_text": "",
                "template_style": job.get("style") or "AI 生成",
                "image_aspect_ratio": job.get("imageAspectRatio") or "16:9",
            })
            created = _unwrap_data(created)
            project_id = created.get("project_id") if isinstance(created, dict) else None
            if not project_id:
                raise WorkerError("Image2PPT 未返回项目编号")
            state = {"projectId": project_id, "pages": {}, "hasTemplate": reference is not None}
            if reference is not None:
                self.http.request("POST", f"/api/projects/{project_id}/template", timeout=60.0, template=reference)
            self.state.save_project(key, state)
            self._ensure_pages(state, job, pages)
            return state

    def _ensure_pages(self, state: dict[str, Any], job: dict[str, Any], pages: list[dict[str, Any]]) -> None:
        page_map = state.setdefault("pages", {})
        for page in pages:
            cache_key = _page_cache_key(page)
            if cache_key in page_map:
                continue
            body = {
                "order_index": max(0, int(page.get("position") or 1) - 1),
                "outline_content": page.get("pageOutline") or {"title": page.get("pageTitle") or ""},
                "description_content": page.get("description") or {"text": page.get("prompt") or page.get("pageTitle") or ""},
            }
            if page.get("part"):
                body["part"] = page["part"]
            result = _unwrap_data(self.http.request("POST", f"/api/projects/{state['projectId']}/pages", body))
            exe_page_id = result.get("page_id") or result.get("id") if isinstance(result, dict) else None
            if not exe_page_id:
                raise WorkerError("Image2PPT 未返回页面编号")
            page_map[cache_key] = {
                "exePageId": exe_page_id,
                "position": int(page.get("position") or 1),
            }
            self.state.save_project(self._state_key(job), state)

    def _current_image_version_path(self, project_id: str, page_id: str) -> str | None:
        try:
            payload = self.http.request(
                "GET",
                f"/api/projects/{project_id}/pages/{page_id}/image-versions",
                timeout=30.0,
            )
        except WorkerError as exc:
            if exc.status in (404, 405):
                return None
            raise
        data = _unwrap_data(payload)
        versions = data.get("versions") if isinstance(data, dict) else None
        if not isinstance(versions, list) or not versions:
            return None
        current = [
            version for version in versions
            if isinstance(version, dict) and version.get("is_current") is True
        ]
        if len(current) != 1:
            raise WorkerError("Image2PPT 未返回唯一的当前图像版本")
        return _safe_same_origin_file_path(self.http.base, current[0].get("image_url"))

    def _legacy_generated_image_path(self, project_id: str, page_id: str) -> str:
        project = _unwrap_data(self.http.request(
            "GET", f"/api/projects/{project_id}", timeout=30.0,
        ))
        row = next(
            (item for item in project.get("pages", []) if item.get("page_id") == page_id),
            None,
        ) if isinstance(project, dict) else None
        if not row or not row.get("generated_image_url"):
            raise WorkerError(row.get("image_error") if isinstance(row, dict) else "Image2PPT 页面没有生成图片")
        return _safe_same_origin_file_path(self.http.base, row["generated_image_url"])

    def generate_page(self, job: dict[str, Any], page: dict[str, Any]) -> tuple[bytes, str, int | None, int | None]:
        key = self._state_key(job)
        with self._project_lock:
            state = self.state.project(key)
        if not state or not state.get("projectId"):
            raise WorkerError("Image2PPT 项目尚未准备")
        exe_page = state.get("pages", {}).get(_page_cache_key(page))
        if not exe_page or not exe_page.get("exePageId"):
            raise WorkerError("Image2PPT 页面尚未准备")
        project_id = state["projectId"]
        exe_page_id = exe_page["exePageId"]
        task = _unwrap_data(self.http.request(
            "POST",
            f"/api/projects/{project_id}/pages/{exe_page_id}/generate/image",
            {"use_template": bool(state.get("hasTemplate")), "force_regenerate": True, "language": "zh"},
            timeout=60.0,
        ))
        task_id = task.get("task_id") if isinstance(task, dict) else None
        if not task_id:
            raise WorkerError("Image2PPT 未返回生图任务编号")
        self._wait_task(project_id, task_id)
        image_path = self._current_image_version_path(project_id, exe_page_id)
        if image_path is None:
            if self.desktop_proxy:
                raise WorkerError("Image2PPT 未返回当前原图版本")
            # Older direct backends predate image-versions. Only that explicit
            # compatibility path may use the project display URL.
            image_path = self._legacy_generated_image_path(project_id, exe_page_id)
        raw, response_type = self.http.download(image_path, timeout=120.0)
        content_type = _detect_image_type(raw) or response_type
        if content_type not in ("image/png", "image/jpeg", "image/webp"):
            raise WorkerError("Image2PPT 返回了不支持的图片格式")
        width, height = image_dimensions(raw)
        return raw, content_type, width, height

    def _wait_task(self, project_id: str, task_id: str) -> None:
        deadline = time.monotonic() + self.task_timeout
        while time.monotonic() < deadline:
            task = _unwrap_data(self.http.request("GET", f"/api/projects/{project_id}/tasks/{task_id}", timeout=20.0))
            status = str(task.get("status") or "").upper() if isinstance(task, dict) else ""
            if status in ("COMPLETED", "SUCCESS", "SUCCEEDED"):
                return
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise WorkerError(task.get("error_message") or "Image2PPT 生图失败")
            time.sleep(self.poll_seconds)
        raise WorkerError("Image2PPT 生图超时；已完成的任务可在原任务中继续核对")


class LocalState:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "spool").mkdir(exist_ok=True)
        self.path = self.root / "state.json"
        self.lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, value: dict[str, Any]) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    def project(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            value = self._load().get("projects", {}).get(key)
            return value if isinstance(value, dict) else None

    def save_project(self, key: str, value: dict[str, Any]) -> None:
        with self.lock:
            data = self._load()
            data.setdefault("projects", {})[key] = value
            self._save(data)

    def drop_project(self, key: str) -> None:
        with self.lock:
            data = self._load()
            projects = data.get("projects", {})
            projects.pop(key, None)
            data["projects"] = projects
            self._save(data)

    def save_spool(self, page_id: str, raw: bytes, content_type: str, width: int | None, height: int | None) -> None:
        with self.lock:
            meta = {
                "contentType": content_type,
                "contentSha256": hashlib.sha256(raw).hexdigest(),
                "width": width,
                "height": height,
            }
            binary = self.root / "spool" / (page_id + ".bin")
            temp = binary.with_suffix(".tmp")
            temp.write_bytes(raw)
            os.replace(temp, binary)
            meta_path = self.root / "spool" / (page_id + ".json")
            meta_temp = meta_path.with_suffix(".tmp")
            meta_temp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(meta_temp, meta_path)

    def load_spool(self, page_id: str) -> tuple[bytes, str, int | None, int | None] | None:
        with self.lock:
            binary = self.root / "spool" / (page_id + ".bin")
            meta_path = self.root / "spool" / (page_id + ".json")
            try:
                raw = binary.read_bytes()
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            digest = hashlib.sha256(raw).hexdigest()
            if meta.get("contentSha256") != digest:
                return None
            content_type = meta.get("contentType")
            if content_type not in ("image/png", "image/jpeg", "image/webp"):
                return None
            return raw, content_type, meta.get("width"), meta.get("height")

    def clear_spool(self, page_id: str) -> None:
        with self.lock:
            (self.root / "spool" / (page_id + ".bin")).unlink(missing_ok=True)
            (self.root / "spool" / (page_id + ".json")).unlink(missing_ok=True)


@dataclass
class WorkerConfig:
    gateway_base: str
    gateway_token: str = field(repr=False)
    worker_token: str = field(repr=False)
    workspace_id: str
    exe_base: str
    exe_token: str = field(repr=False)
    worker_id: str
    workers: int = 4
    lease_seconds: int = 1800
    poll_seconds: float = 3.0
    task_timeout: float = 1800.0
    upload_retries: int = 5
    state_dir: Path = field(default_factory=lambda: Path(os.getenv("LOCALAPPDATA") or Path.home()) / "GuanyeAIWorker")
    auth: SupabaseAuthConfig | None = field(default=None, repr=False)
    exe_desktop_proxy: bool = False
    exe_endpoint_source: str = "direct"
    exe_ready_file: str | None = None
    exe_release_shell: str | None = None

    @classmethod
    def from_env(cls, require_cloud: bool = True, workers: int | None = None) -> "WorkerConfig":
        _trust_env_proxy_disabled()
        gate_base = os.environ.get("GUANYE_API_BASE", "").strip()
        gate_token = os.environ.get("GUANYE_TOKEN", "").strip()
        worker_token = os.environ.get("GUANYE_WORKER_TOKEN", "").strip()
        workspace = os.environ.get("GUANYE_WORKSPACE_ID", "").strip()
        auth_values = {
            "GUANYE_SUPABASE_URL": os.environ.get("GUANYE_SUPABASE_URL", "").strip(),
            "GUANYE_SUPABASE_ANON_KEY": os.environ.get("GUANYE_SUPABASE_ANON_KEY", "").strip(),
            "GUANYE_WORKER_EMAIL": os.environ.get("GUANYE_WORKER_EMAIL", "").strip(),
            "GUANYE_WORKER_PASSWORD": os.environ.get("GUANYE_WORKER_PASSWORD", ""),
        }
        if require_cloud and not gate_base:
            raise ValueError("GUANYE_API_BASE 不能为空")
        auth_config: SupabaseAuthConfig | None = None
        if require_cloud and not gate_token:
            present = [name for name, value in auth_values.items() if value]
            if present and len(present) != len(auth_values):
                missing = [name for name in auth_values if not auth_values[name]]
                raise ValueError("专用账号认证配置不完整，缺少：" + "、".join(missing))
            if not present:
                raise ValueError(
                    "GUANYE_TOKEN 或专用账号认证配置不能为空；"
                    "专用模式需要 GUANYE_SUPABASE_URL、GUANYE_SUPABASE_ANON_KEY、"
                    "GUANYE_WORKER_EMAIL、GUANYE_WORKER_PASSWORD"
                )
            auth_config = SupabaseAuthConfig(
                supabase_url=auth_values["GUANYE_SUPABASE_URL"],
                anon_key=auth_values["GUANYE_SUPABASE_ANON_KEY"],
                email=auth_values["GUANYE_WORKER_EMAIL"],
                password=auth_values["GUANYE_WORKER_PASSWORD"],
            )
        if require_cloud and not worker_token:
            raise ValueError("GUANYE_WORKER_TOKEN 不能为空")
        if require_cloud and not workspace:
            raise ValueError("GUANYE_WORKSPACE_ID 不能为空")
        endpoint = _resolve_exe_endpoint_details()
        worker_id = os.environ.get("GUANYE_WORKER_ID", "").strip() or f"{socket.gethostname()}-{uuid.uuid4().hex[:10]}"
        state_dir = Path(os.environ.get("GUANYE_WORKER_STATE", "").strip() or (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "GuanyeAIWorker"))
        configured_workers = workers if workers is not None else _env_int("GUANYE_GENERATION_WORKERS", 4, 1, 16)
        return cls(
            gateway_base=gate_base,
            gateway_token=gate_token,
            worker_token=worker_token,
            workspace_id=workspace,
            exe_base=endpoint.base,
            exe_token=endpoint.token,
            worker_id=worker_id,
            workers=configured_workers,
            lease_seconds=_env_int("GUANYE_LEASE_SECONDS", 1800, 60, 7200),
            poll_seconds=_env_float("GUANYE_POLL_SECONDS", 3.0, 0.2, 60.0),
            task_timeout=_env_float("GUANYE_TASK_TIMEOUT", 1800.0, 30.0, 7200.0),
            upload_retries=_env_int("GUANYE_UPLOAD_RETRIES", 5, 1, 12),
            state_dir=state_dir,
            auth=auth_config,
            exe_desktop_proxy=endpoint.desktop_proxy,
            exe_endpoint_source=endpoint.source,
            exe_ready_file=endpoint.ready_file,
            exe_release_shell=endpoint.release_shell,
        )


class LeaseHeartbeat:
    def __init__(self, gateway: GatewayClient, page: dict[str, Any], lease_seconds: int):
        self.gateway = gateway
        self.page = page
        self.lease_seconds = lease_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="lease-" + str(page.get("pageId")), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop_event.set()
        self.thread.join(timeout=2)
        return False

    def _run(self) -> None:
        interval = max(20.0, min(self.lease_seconds / 3.0, 600.0))
        while not self.stop_event.wait(interval):
            try:
                self.gateway.heartbeat(self.page["pageId"], self.page["leaseToken"], self.lease_seconds)
            except WorkerError as exc:
                if exc.status in (401, 403) or "租约" in str(exc):
                    return


class WindowsWorker:
    def __init__(self, config: WorkerConfig, gateway: GatewayClient | None = None, exe: ExeClient | None = None):
        self.config = config
        self.state = LocalState(config.state_dir)
        auth_provider = SupabaseTokenProvider(config.auth) if config.auth is not None else None
        self.gateway = gateway or GatewayClient(
            config.gateway_base,
            config.gateway_token,
            config.workspace_id,
            worker_token=config.worker_token,
            auth_provider=auth_provider,
        )
        self.exe = exe or ExeClient(
            config.exe_base,
            config.exe_token,
            self.state,
            task_timeout=config.task_timeout,
            poll_seconds=min(2.0, config.poll_seconds),
            desktop_proxy=config.exe_desktop_proxy,
            endpoint_source=config.exe_endpoint_source,
            ready_file=config.exe_ready_file,
            release_shell=config.exe_release_shell,
        )
        self.stop_event = threading.Event()
        self._active_jobs: dict[str, dict[str, Any]] = {}
        self._active_lock = threading.RLock()
        self.last_run = WorkerRunResult()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self, max_pages: int = 0, *, once: bool = False) -> int:
        """Run queue work, optionally as one claim-and-drain burst.

        ``max_pages > 0`` caps pages actually claimed, not just completed work.
        A once run attempts one claim, waits for that batch, and never polls an
        empty queue again. The existing continuous behavior is unchanged.
        """
        processed = 0
        claimed_count = 0
        failed = 0
        claim_failed = False
        empty = False
        claim_attempted = False
        budget = max_pages if max_pages > 0 else None
        pending: dict[Future[bool], dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=self.config.workers, thread_name_prefix="guanye-image") as pool:
            while not self.stop_event.is_set():
                remaining = None if budget is None else max(0, budget - claimed_count)
                slots = self.config.workers - len(pending)
                may_claim = (
                    slots > 0
                    and (remaining is None or remaining > 0)
                    and not (once and claim_attempted)
                )
                if may_claim:
                    limit = slots if remaining is None else min(slots, remaining)
                    claim_attempted = True
                    try:
                        claimed = self.gateway.claim(
                            self.config.worker_id, limit, self.config.lease_seconds,
                        )
                    except WorkerError as exc:
                        claim_failed = True
                        _log("领取任务失败：" + str(exc))
                        claimed = []
                    if not isinstance(claimed, list):
                        raise WorkerError("网关领取结果无效")
                    if len(claimed) > limit:
                        _log(f"网关返回页数超过本次领取上限，已截断 {len(claimed) - limit} 页")
                        claimed = claimed[:limit]
                    claimed_count += len(claimed)
                    for page in claimed:
                        pending[pool.submit(self.process_page, page)] = page
                if pending:
                    done, _ = wait(tuple(pending), timeout=self.config.poll_seconds, return_when=FIRST_COMPLETED)
                    for future in done:
                        page = pending.pop(future)
                        processed += 1
                        try:
                            future.result()
                        except Exception as exc:
                            failed += 1
                            _log(f"页面 {page.get('pageId')} 处理失败：{exc}")
                    if once and not pending:
                        break
                elif once:
                    empty = not claim_failed and claimed_count == 0
                    break
                elif budget is not None and claimed_count >= budget:
                    break
                else:
                    if self.stop_event.wait(self.config.poll_seconds):
                        break
            for future in pending:
                future.cancel()
        self.last_run = WorkerRunResult(
            processed=processed,
            claimed=claimed_count,
            failed=failed,
            claim_failed=claim_failed,
            empty=empty,
            once=once,
        )
        return processed

    def process_page(self, page: dict[str, Any]) -> bool:
        page_id = str(page.get("pageId") or "")
        if not page_id or not page.get("leaseToken"):
            raise WorkerError("领取页面字段不完整")
        job = {
            "workspaceId": self.config.workspace_id,
            "jobId": page.get("jobId"),
            "title": page.get("title"),
            "style": page.get("style"),
            "outline": page.get("outline"),
        }
        try:
            with LeaseHeartbeat(self.gateway, page, self.config.lease_seconds):
                artifact = self.state.load_spool(page_id)
                if artifact is None:
                    with self._active_lock:
                        self._active_jobs[job["jobId"]] = job
                    pages = self._job_pages(page)
                    if page.get("hasReferenceImage") is True:
                        reference = self.gateway.reference_image(page)
                        job["referenceSha256"] = hashlib.sha256(reference[0]).hexdigest()
                        self.exe.ensure_project(job, pages, reference=reference)
                    else:
                        self.exe.ensure_project(job, pages)
                    artifact = self.exe.generate_page(job, page)
                    raw, content_type, width, height = artifact
                    self.state.save_spool(page_id, raw, content_type, width, height)
                raw, content_type, width, height = artifact
                self._upload_with_retry(page, raw, content_type, width, height)
                self.state.clear_spool(page_id)
                return True
        except Exception as exc:
            # Lease/permission errors mean another worker owns the page; do not
            # write a competing terminal state.
            if isinstance(exc, WorkerError) and (exc.status in (401, 403) or "租约" in str(exc)):
                raise
            try:
                self.gateway.fail(page, str(exc))
            except Exception as fail_exc:
                _log(f"页面 {page_id} 失败状态回写失败：{fail_exc}")
            raise

    def _job_pages(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        # Materialize the entire job before generating so page creation is
        # ordered once inside the EXE.  The project cache makes this idempotent.
        remote = self.gateway.get_job(str(page.get("jobId") or ""))
        rows = remote.get("pages") if isinstance(remote, dict) else None
        if not isinstance(rows, list) or not rows:
            raise WorkerError("生成任务没有页面")
        normalized: list[dict[str, Any]] = []
        for row in sorted((item for item in rows if isinstance(item, dict)), key=lambda item: int(item.get("position") or 0)):
            normalized.append({
                "jobId": remote.get("id"),
                "pageId": row.get("id"),
                "planPageId": row.get("planPageId"),
                "position": row.get("position"),
                "pageTitle": row.get("title"),
                "title": row.get("title"),
                "pageOutline": row.get("outline"),
                "description": row.get("description"),
                "prompt": row.get("prompt"),
                "pageReferences": row.get("references") or [],
                "storagePath": row.get("storagePath"),
                "contentType": row.get("contentType"),
            })
        return normalized

    def _upload_with_retry(self, page: dict[str, Any], raw: bytes, content_type: str,
                           width: int | None, height: int | None) -> None:
        last: Exception | None = None
        for attempt in range(self.config.upload_retries):
            try:
                self.gateway.complete(page, raw, content_type, width, height)
                return
            except WorkerError as exc:
                last = exc
                if exc.status in (401, 403) or "租约" in str(exc):
                    raise
            except Exception as exc:
                last = exc
            if attempt + 1 < self.config.upload_retries:
                time.sleep(min(30.0, 0.8 * (2 ** attempt)))
        raise WorkerError(f"图片上传失败：{last}")


@dataclass(frozen=True)
class _ExeEndpoint:
    base: str
    token: str = field(repr=False)
    desktop_proxy: bool = False
    source: str = "direct"
    ready_file: str | None = None
    release_shell: str | None = None


def _release_shell_name(ready: Any) -> str | None:
    if not isinstance(ready, dict):
        return None
    executable = ready.get("executable")
    if not isinstance(executable, str) or not executable.strip():
        return None
    name = ntpath.basename(executable.strip())
    return name or None


def _resolve_exe_endpoint_details() -> _ExeEndpoint:
    base = os.environ.get("IMAGE2PPT_BASE_URL", "").strip()
    token = (os.environ.get("IMAGE2PPT_ACCESS_TOKEN") or os.environ.get("GUANYE_EXE_TOKEN") or os.environ.get("HN_LOCAL_TOKEN") or "").strip()
    ready_file = os.environ.get("GUANYE_EXE_READY_FILE", "").strip()
    data_dir = os.environ.get("GUANYE_EXE_DATA_DIR", "").strip() or os.environ.get("HN_LOCAL_DATA", "").strip()
    discovered = Path(ready_file) if ready_file else None
    desktop_proxy = False
    source = "explicit"
    selected_ready: str | None = None
    release_shell: str | None = None
    if not base:
        if discovered is None and data_dir:
            for name in ("desktop-ready.json", "backend-ready.json"):
                candidate = Path(data_dir) / name
                if candidate.is_file():
                    discovered = candidate
                    break
        if discovered is None or not discovered.is_file():
            raise ValueError("IMAGE2PPT_BASE_URL 未设置，且找不到 desktop-ready.json / backend-ready.json")
        try:
            ready = json.loads(discovered.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("Image2PPT readiness 文件无法读取") from None
        base = str(ready.get("url") or "").strip() if isinstance(ready, dict) else ""
        if not base:
            raise ValueError("Image2PPT readiness 文件缺少服务地址")
        selected_ready = discovered.name
        if selected_ready == "desktop-ready.json":
            desktop_proxy = True
            source = "desktop-ready"
            release_shell = _release_shell_name(ready)
        elif selected_ready == "backend-ready.json":
            source = "backend-ready"
        else:
            source = "readiness"
    if not token and data_dir:
        client_file = Path(data_dir) / "client.json"
        if client_file.is_file():
            try:
                settings = json.loads(client_file.read_text(encoding="utf-8"))
                code = settings.get("code") if isinstance(settings, dict) else None
                if isinstance(code, str):
                    token = code.strip()
            except (OSError, ValueError):
                pass
    # A desktop-ready URL points at the trusted loopback proxy, which injects
    # the backend token itself. An explicit/backend URL still uses /health and
    # requires its own token; HTTP failures remain preflight failures.
    return _ExeEndpoint(
        base=base.rstrip("/"),
        token=token,
        desktop_proxy=desktop_proxy,
        source=source,
        ready_file=selected_ready,
        release_shell=release_shell,
    )


def _resolve_exe_endpoint() -> tuple[str, str]:
    endpoint = _resolve_exe_endpoint_details()
    return endpoint.base, endpoint.token


def _unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]
    return value


def _page_cache_key(page: dict[str, Any]) -> str:
    return str(page.get("planPageId") or page.get("pageId") or page.get("position") or "")


def _detect_image_type(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_dimensions(raw: bytes) -> tuple[int | None, int | None]:
    try:
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
            width, height = struct.unpack(">II", raw[16:24])
            return (width, height) if width > 0 and height > 0 else (None, None)
        if raw.startswith(b"\xff\xd8\xff"):
            index = 2
            while index + 9 < len(raw):
                if raw[index] != 0xFF:
                    index += 1
                    continue
                marker = raw[index + 1]
                index += 2
                if marker in (0xD8, 0xD9):
                    continue
                if index + 2 > len(raw):
                    break
                length = struct.unpack(">H", raw[index:index + 2])[0]
                if length < 2 or index + length > len(raw):
                    break
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", raw[index + 3:index + 7])
                    return (width, height) if width > 0 and height > 0 else (None, None)
                index += length
        if len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            chunk = raw[12:16]
            if chunk == b"VP8X" and len(raw) >= 30:
                width = 1 + int.from_bytes(raw[24:27], "little")
                height = 1 + int.from_bytes(raw[27:30], "little")
                return width, height
            if chunk == b"VP8 " and len(raw) >= 30:
                width, height = struct.unpack("<HH", raw[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L" and len(raw) >= 25:
                bits = int.from_bytes(raw[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    except (ValueError, struct.error, IndexError):
        pass
    return None, None


def preflight(config: WorkerConfig) -> dict[str, Any]:
    state = LocalState(config.state_dir)
    client = ExeClient(
        config.exe_base,
        config.exe_token,
        state,
        task_timeout=config.task_timeout,
        poll_seconds=min(2.0, config.poll_seconds),
        desktop_proxy=config.exe_desktop_proxy,
        endpoint_source=config.exe_endpoint_source,
        ready_file=config.exe_ready_file,
        release_shell=config.exe_release_shell,
    )
    return client.preflight()


def _log(message: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + message, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guanye Windows AI image generation worker")
    parser.add_argument("--preflight", action="store_true", help="only verify the already-running EXE service; never generate an image")
    parser.add_argument("--once", action="store_true", help="process one queue cycle and exit")
    parser.add_argument("--max-pages", type=int, default=0, help="optional page cap for a controlled run")
    parser.add_argument("--workers", type=int, default=None, help="shared queue concurrency (default 4, max 16)")
    args = parser.parse_args(argv)
    config: WorkerConfig | None = None
    try:
        if args.max_pages < 0:
            raise ValueError("--max-pages 不能为负数")
        config = WorkerConfig.from_env(require_cloud=not args.preflight, workers=args.workers)
        if args.preflight:
            result = preflight(config)
            result["isolatedState"] = str(config.state_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        worker = WindowsWorker(config)
        if args.once:
            worker.run(max_pages=args.max_pages, once=True)
            outcome = worker.last_run
            if outcome.claim_failed or outcome.failed or (outcome.claimed == 0 and not outcome.empty):
                status, exit_code = "failed", 2
            elif outcome.empty:
                status, exit_code = "empty", 3
            else:
                status, exit_code = "succeeded", 0
            print(json.dumps({
                "mode": "once",
                "status": status,
                "claimed": outcome.claimed,
                "processed": outcome.processed,
                "failed": outcome.failed,
            }, ensure_ascii=False, separators=(",", ":")))
            return exit_code
        worker.run(max_pages=args.max_pages)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        safe_preflight_error = isinstance(exc, (PreflightError, ValueError))
        detail = f": {exc}" if args.preflight and safe_preflight_error else ""
        _log(f"worker failed ({type(exc).__name__}){detail}")
        if args.preflight:
            message = str(exc) if safe_preflight_error else "预检未完成（内部错误）"
            stage = exc.stage if isinstance(exc, PreflightError) else (
                "configuration" if isinstance(exc, ValueError) else "internal"
            )
            print(json.dumps({
                "ok": False,
                "desktopProxy": config.exe_desktop_proxy if config is not None else None,
                "endpointSource": config.exe_endpoint_source if config is not None else None,
                "readinessFile": config.exe_ready_file if config is not None else None,
                "releaseShell": config.exe_release_shell if config is not None else None,
                "httpProtocol": {
                    "verified": False,
                    "stage": stage,
                    "error": message,
                },
                "runtimeImage": _unverified_runtime_image(
                    config.exe_release_shell if config is not None else None
                ),
                "isolatedState": str(config.state_dir) if config is not None else None,
            }, ensure_ascii=False, indent=2))
        elif args.once:
            print(json.dumps({
                "mode": "once",
                "status": "failed",
                "stage": "startup_or_run",
            }, ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
