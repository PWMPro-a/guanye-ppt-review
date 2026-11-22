"""Unprivileged S11/M30 adapter for WindowsWorker's existing gateway interface."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

from .ai_gateway import DirectTransport, HTTPResponse, Transport
from .windows_image_worker import WorkerError, _detect_image_type

_EDGE = "/functions/v1/single-judge-image-worker"
_AUTH = "/auth/v1/user"
_JOB = "/rest/v1/rpc/ai_generation_get"
_ACCESS = "/rest/v1/rpc/single_judge_access"
_STORAGE = "/storage/v1/object/ppt-library/"
_SIGNED = "/storage/v1/object/upload/sign/ppt-library/"
_MAX_IMAGE = 25 * 1024 * 1024
_MAX_JSON = 8 * 1024 * 1024
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_DECK = re.compile(r"[A-Za-z0-9_-]{1,100}")
_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_OUTPUT_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_REFERENCE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}

# Exact Edge error/status pairs. Never forward an upstream message or arbitrary code.
_EDGE_ERRORS = {
    (401, "AUTH_INVALID"): "用户凭证无效或已过期",
    (403, "ACCESS_DENIED"): "当前账号无专业图片工作权限",
    (403, "WORKER_TOKEN_INVALID"): "图片工作节点凭证无效",
    (404, "PAGE_NOT_FOUND"): "页面租约对应的页面不存在",
    (404, "REFERENCE_NOT_FOUND"): "当前租约没有获准的引用图",
    (409, "LEASE_LOST"): "页面租约已失效或已过期",
    (409, "CONTENT_CONFLICT"): "图片存储内容与本地成品不一致",
    (409, "UPLOAD_NOT_FOUND"): "尚未读到已上传的图片",
    (409, "ALREADY_SUCCEEDED"): "页面已经完成，无需再次上传",
    (422, "IMAGE_INVALID"): "存储图片超限或格式不符",
    (503, "UPSTREAM_UNAVAILABLE"): "图片工作服务暂时不可用",
}


class TokenProvider(Protocol):
    def access_token(self) -> str: ...
    def refresh_after_unauthorized(self, rejected_token: str) -> str: ...


def _uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _int(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _jwt(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 8000 and _JWT.fullmatch(value) is not None


def _public_apikey(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 8000:
        return False
    if re.fullmatch(r"sb_publishable_[A-Za-z0-9_-]+", value):
        return True
    # Classification only, not signature verification. Refuse legacy service_role keys.
    if _jwt(value):
        middle = value.split(".")[1]
        try:
            claims = json.loads(base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4)))
            return isinstance(claims, dict) and claims.get("role") == "anon"
        except (ValueError, RecursionError):
            pass
    return False


def _project_origin(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?", value):
        raise ValueError("Supabase 项目地址必须是固定 HTTPS 源")
    try:
        source = urlsplit(value)
        if not source.hostname or source.hostname.startswith(".") or source.port == 0:
            raise ValueError
    except ValueError:
        raise ValueError("Supabase 项目地址无效") from None
    return value.rstrip("/")


def _json(body: bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError

    try:
        if len(body) <= _MAX_JSON:
            return json.loads(body.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        pass
    return None


def _protocol() -> WorkerError:
    return WorkerError("专业图片接口响应不符合契约", 502)


def _lease_params(page: dict) -> dict:
    if not isinstance(page, dict) or not _uuid(page.get("pageId")) or not _uuid(page.get("leaseToken")):
        raise WorkerError("页面租约参数无效", 409)
    return {"page_id": page["pageId"], "lease_token": page["leaseToken"]}


def _encode_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


def _error_code(error: WorkerError) -> str | None:
    return error.payload.get("code") if isinstance(error.payload, dict) else None


class ProfessionalImageClient:
    """Duck type for WindowsWorker's gateway; the launcher owns the credentials."""

    def __init__(self, supabase_url: str, workspace_id: str, apikey: str,
                 token_provider: TokenProvider, worker_token: str, *,
                 transport: Transport | None = None, timeout: float = 30.0):
        self.origin = _project_origin(supabase_url)
        if not _uuid(workspace_id):
            raise ValueError("专业图片工作区编号无效")
        if not _public_apikey(apikey):
            raise ValueError("必须提供 Supabase anon 或 publishable 公共 API key")
        if not isinstance(worker_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", worker_token):
            raise ValueError("专业图片工作节点凭证格式无效")
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError("专业图片请求超时配置无效")
        self.workspace_id = workspace_id
        self._apikey = apikey
        self._provider = token_provider
        self._worker_token = worker_token
        self._transport = transport if transport is not None else DirectTransport(_MAX_JSON, _MAX_IMAGE)
        self._timeout = timeout
        self._actor_lock = threading.Lock()
        self._actor: str | None = None
        self._actor_token: str | None = None

    def _token(self, rejected: str | None = None) -> str:
        try:
            token = (self._provider.access_token() if rejected is None else
                     self._provider.refresh_after_unauthorized(rejected))
        except Exception as exc:
            status = 401 if isinstance(exc, WorkerError) and exc.status in (400, 401, 403) else 503
            raise WorkerError("无法取得或刷新图片工作用户凭证", status) from None
        if not _jwt(token) or token == rejected:
            raise WorkerError("用户凭证无效或未能刷新", 401)
        return token

    def _request(self, method: str, path: str, token: str, *, command: dict | None = None,
                 edge: bool = False, image: bytes | None = None, mime: str | None = None) -> HTTPResponse:
        headers = {"apikey": self._apikey, "Authorization": "Bearer " + token,
                   "Cache-Control": "no-store", "Accept-Encoding": "identity"}
        data = image
        if command is not None:
            try:
                data = json.dumps(command, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (ValueError, TypeError):
                raise WorkerError("专业图片工作请求字段无效", 400) from None
            headers["Content-Type"] = "application/json"
        if edge:
            headers["X-Guanye-Worker-Token"] = self._worker_token
        if image is not None:
            headers["Content-Type"] = mime
            headers["x-upsert"] = "false"
        try:
            response = self._transport(method, self.origin + path, headers, data, self._timeout)
            status, body = response.status, response.body
        except Exception:
            # urllib exceptions can include the full signed URL. Do not let them escape.
            raise WorkerError("专业图片服务请求失败，结果未确认", 503) from None
        if not _int(status, 100, 599) or not isinstance(body, bytes):
            raise _protocol()
        if 300 <= status <= 399:
            raise WorkerError("专业图片服务拒绝重定向", 502)
        return response

    def _edge(self, action: str, fields: dict) -> dict:
        token = self._token()
        cmd = {"action": action, **fields}
        for attempt in range(2):
            response = self._request("POST", _EDGE, token, command=cmd, edge=True)
            result = _json(response.body)
            if response.status == 200:
                if not isinstance(result, dict):
                    raise _protocol()
                return result
            code = result.get("code") if isinstance(result, dict) and result.get("ok") is False else None
            if (attempt == 0 and response.status == 401 and code == "AUTH_INVALID"
                    and result.get("refreshable") is True):
                # This particular response is emitted before an action is invoked.
                token = self._token(token)
                continue
            key = (response.status, code if isinstance(code, str) else None)
            if key in _EDGE_ERRORS:
                raise WorkerError(_EDGE_ERRORS[key], response.status, {"code": code})
            raise WorkerError("专业图片工作请求失败", response.status)
        raise AssertionError("unreachable")

    def _actor_for(self, token: str) -> str:
        with self._actor_lock:
            if self._actor_token == token and self._actor is not None:
                return self._actor
            reply = self._request("GET", _AUTH, token)
            if reply.status != 200:
                raise WorkerError("无法验证图片工作用户身份", reply.status)
            data = _json(reply.body)
            actor = data.get("id") if isinstance(data, dict) else None
            if not _uuid(actor):
                raise _protocol()
            if self._actor is not None and self._actor != actor:
                raise WorkerError("图片工作期间用户身份发生变化", 403)
            self._actor = actor
            self._actor_token = token
            return actor

    def _job_shape(self, value: dict, job_id: str) -> dict:
        pages = value.get("pages") if isinstance(value, dict) else None
        if (not isinstance(pages, list) or not 1 <= len(pages) <= 600 or value.get("id") != job_id
                or value.get("workspaceId", self.workspace_id) != self.workspace_id
                or not _uuid(value.get("userId")) or not isinstance(value.get("deckId"), str)
                or not _DECK.fullmatch(value["deckId"])):
            raise _protocol()
        seen = set()
        for page in pages:
            if (not isinstance(page, dict) or not _uuid(page.get("id")) or page["id"] in seen
                    or not _int(page.get("position"), 1, 600)):
                raise _protocol()
            seen.add(page["id"])
        return value

    def authorize(self) -> str:
        """Verify the pinned user and dedicated-judge role without claiming work."""
        token = self._token()
        for attempt in range(2):
            try:
                actor = self._actor_for(token)
            except WorkerError as exc:
                if attempt == 0 and exc.status == 401:
                    token = self._token(token)
                    continue
                raise
            reply = self._request("POST", _ACCESS, token, command={"w": self.workspace_id})
            if attempt == 0 and reply.status == 401:
                token = self._token(token)
                continue
            if reply.status != 200:
                raise WorkerError("无法验证专业图片工作权限", reply.status)
            value = _json(reply.body)
            if not isinstance(value, dict) or value.get("mode") != "single-judge" or value.get("role") not in ("judge", "admin"):
                raise WorkerError("当前账号不是指定专业评分师", 403)
            return actor
        raise AssertionError("unreachable")

    def get_job(self, job_id: str) -> dict:
        if not _uuid(job_id):
            raise WorkerError("生成任务编号无效", 400)
        token = self._token()
        for attempt in range(2):
            try:
                actor = self._actor_for(token)
            except WorkerError as exc:
                if attempt == 0 and exc.status == 401:
                    token = self._token(token)
                    continue
                raise
            reply = self._request("POST", _JOB, token, command={
                "p_workspace": self.workspace_id, "p_actor": actor, "p_job": job_id,
            })
            if attempt == 0 and reply.status == 401:
                token = self._token(token)
                continue
            if reply.status != 200:
                raise WorkerError("无法读取当前评分师的生成任务", reply.status)
            result = self._job_shape(_json(reply.body), job_id)
            if result["userId"] != actor:
                raise WorkerError("生成任务不属于当前评分师", 403)
            return result
        raise AssertionError("unreachable")

    def worker_job(self, job_id: str) -> dict:
        if not _uuid(job_id):
            raise WorkerError("生成任务编号无效", 400)
        path = "/rest/v1/rpc/professional_worker_generation_job"
        token = self._token()
        for attempt in range(2):
            reply = self._request("POST", path, token, command={"w": self.workspace_id, "job_id": job_id})
            if reply.status == 401 and attempt == 0:
                token = self._token(token)
                continue
            if reply.status != 200:
                raise WorkerError("无法读取后台生图任务", reply.status)
            return self._job_shape(_json(reply.body), job_id)
        raise AssertionError("unreachable")

    def _expected_target(self, page: dict) -> tuple[str, str]:
        _lease_params(page)
        mime = page.get("contentType")
        deck = page.get("deckId")
        if (not isinstance(mime, str) or mime not in _OUTPUT_EXT or not isinstance(deck, str)
                or not _DECK.fullmatch(deck) or deck == "ai-planning" or not _uuid(page.get("jobId"))):
            raise _protocol()
        path = f"{self.workspace_id}/{deck}/{page['pageId']}{_OUTPUT_EXT[mime]}"
        if page.get("storagePath") != path:
            raise _protocol()
        return path, mime

    def claim(self, worker_id: str, limit: int, lease_seconds: int) -> list[dict]:
        if (not isinstance(worker_id, str) or not 1 <= len(worker_id.strip()) <= 200
                or any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in worker_id)
                or not _int(limit, 1, 32) or not _int(lease_seconds, 60, 7200)):
            raise WorkerError("领取任务参数无效", 400)
        value = self._edge("claim", {"worker_id": worker_id, "limit": limit, "lease_seconds": lease_seconds})
        pages = value.get("pages")
        if not isinstance(pages, list) or len(pages) > limit:
            raise _protocol()
        seen = set()
        for page in pages:
            self._expected_target(page)
            if (not isinstance(page.get("hasReferenceImage"), bool) or page["pageId"] in seen
                    or not _int(page.get("position"), 1, 600)):
                raise _protocol()
            seen.add(page["pageId"])
        return pages

    def heartbeat(self, page_id: str, lease_token: str, lease_seconds: int) -> None:
        lease = _lease_params({"pageId": page_id, "leaseToken": lease_token})
        if not _int(lease_seconds, 60, 7200):
            raise WorkerError("页面续租参数无效", 400)
        if self._edge("heartbeat", {**lease, "lease_seconds": lease_seconds}) != {"ok": True}:
            raise _protocol()

    def _reference_target(self, lease: dict) -> tuple[str, str]:
        value = self._edge("reference", lease)
        path = value.get("storagePath")
        if (not isinstance(path, str) or len(path) > 500 or ".." in path
                or re.search(r"[%?#\\:\s\x00-\x1f\x7f-\x9f]", path)):
            raise _protocol()
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != self.workspace_id or not all(parts):
            raise _protocol()
        if parts[1] == "ai-planning":
            if len(parts) != 5 or not _uuid(parts[2]) or parts[3] not in ("reference", "style-rendered"):
                raise _protocol()
        elif not _DECK.fullmatch(parts[1]) or not (len(parts) == 3 or len(parts) == 4 and parts[2] == "preview"):
            raise _protocol()
        filename, sep, suffix = parts[-1].rpartition(".")
        mime = _REFERENCE_MIME.get(suffix.lower()) if filename and sep else None
        if mime is None:
            raise WorkerError("引用图格式不受当前 Windows EXE 支持", 422)
        if "contentType" in value and value["contentType"] != mime:
            raise _protocol()
        return path, mime

    def reference_image(self, page: dict) -> tuple[bytes, str]:
        lease = _lease_params(page)
        token = self._token()
        for attempt in range(2):
            path, mime = self._reference_target(lease)
            # Resolve under the live lease again after a Bearer refresh; never reuse a prior path.
            token = self._token()
            reply = self._request("GET", _STORAGE + _encode_path(path), token)
            if attempt == 0 and reply.status == 401:
                self._token(token)
                continue
            if reply.status != 200:
                raise WorkerError("无法以当前用户权限读取引用图", reply.status)
            lowered = {key.lower(): value for key, value in reply.headers.items()}
            if ("content-range" in lowered or lowered.get("content-encoding", "identity").lower() != "identity"
                    or not 0 < len(reply.body) <= _MAX_IMAGE or _detect_image_type(reply.body) != mime):
                raise WorkerError("引用图大小或格式不符", 422)
            length = lowered.get("content-length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,9}", length) or int(length) != len(reply.body)):
                raise WorkerError("引用图下载不完整", 502)
            if self._reference_target(lease) != (path, mime):
                raise WorkerError("引用图授权在读取期间发生变化", 403)
            return reply.body, mime
        raise AssertionError("unreachable")

    def _job_page(self, result: dict, page: dict) -> dict:
        job = self._job_shape(result, page["jobId"])
        if job["deckId"] != page["deckId"]:
            raise _protocol()
        for row in job["pages"]:
            if row["id"] == page["pageId"]:
                path, mime = self._expected_target(page)
                if row.get("storagePath") != path or row.get("contentType") != mime:
                    raise _protocol()
                return row
        raise _protocol()

    def _ack(self, page: dict, fields: dict) -> dict:
        result = self._edge("complete", fields)
        row = self._job_page(result, page)
        if row.get("status") != "succeeded" or row.get("contentSha256") != fields["content_sha256"]:
            raise _protocol()
        return result

    def complete(self, page: dict, raw: bytes, content_type: str,
                 width: int | None, height: int | None) -> dict:
        path, mime = self._expected_target(page)
        if (not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_IMAGE or mime != content_type
                or _detect_image_type(raw) != mime
                or any(value is not None and not _int(value, 1, 2147483647) for value in (width, height))):
            raise WorkerError("本地成品图片大小、类型或尺寸无效", 422)
        lease = _lease_params(page)
        digest = hashlib.sha256(raw).hexdigest()
        fields = {**lease, "content_type": mime, "content_sha256": digest, "width": width, "height": height}
        # WindowsWorker can resume directly from spool without _job_pages(). Still enforce the M30 owner boundary.
        row = self._job_page(self.get_job(page["jobId"]), page)
        status = row.get("status")
        if status == "succeeded" and row.get("contentSha256") != digest:
            raise WorkerError("已完成页面的图片与本地成品不一致", 409, {"code": "CONTENT_CONFLICT"})
        if status not in ("running", "succeeded"):
            raise WorkerError("页面租约已失效", 409, {"code": "LEASE_LOST"})
        try:
            return self._ack(page, fields)
        except WorkerError as exc:
            if status == "succeeded" or _error_code(exc) not in ("UPLOAD_NOT_FOUND", "UPSTREAM_UNAVAILABLE"):
                raise
            # Some Storage failures are collapsed to UPSTREAM_UNAVAILABLE by S11 (including
            # installations that do not report absent objects as HTTP 404). A scoped, no-upsert
            # PUT is safe here; no success is inferred without a subsequent verified ACK.
        try:
            upload = self._edge("issue-upload", lease)
        except WorkerError as exc:
            if _error_code(exc) == "ALREADY_SUCCEEDED":
                return self._ack(page, fields)
            raise
        signed = upload.get("uploadToken")
        if upload.get("storagePath") != path or upload.get("contentType") != mime or not _jwt(signed):
            raise _protocol()
        signed_path = _SIGNED + _encode_path(path) + "?" + urlencode({"token": signed})
        token = self._token()
        # Probe completion after either a successful or uncertain PUT (including duplicate-object
        # replies). Do not retry PUT inside this call and never treat a duplicate as proof of bytes.
        put_error = None
        try:
            response = self._request("PUT", signed_path, token, image=raw, mime=mime)
            if response.status not in (200, 201, 204):
                put_error = WorkerError("签名图片上传未确认", response.status)
        except WorkerError as exc:
            put_error = exc
        try:
            return self._ack(page, fields)
        except WorkerError as exc:
            if put_error is not None and _error_code(exc) == "UPLOAD_NOT_FOUND":
                raise put_error from None
            raise

    def fail(self, page: dict, error: str) -> None:
        # The legacy caller can pass raw EXE diagnostics here. Do not send them to the Edge.
        lease = _lease_params(page)
        self._expected_target(page)
        self._job_page(self.get_job(page["jobId"]), page)
        result = self._edge("fail", {**lease, "error": "页面生成失败"})
        if self._job_page(result, page).get("status") not in ("failed", "succeeded"):
            raise _protocol()
