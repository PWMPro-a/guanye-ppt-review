"""Background intake worker for the professional judge launcher.

The worker claims coworker submissions from the private Supabase queue, reads
the original object with the judge's own user token, verifies the object, and
then delegates preview generation and publication to the existing
``ProfessionalImporter``.  It intentionally has no service-role credential and
no alternate preview or publish path.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
from typing import Any, Callable
from urllib.parse import quote
from uuid import UUID

from cloud.ai_gateway import DirectTransport, GatewayError, HTTPResponse
from cloud.professional_import import ImportErrorResponse, ProfessionalImporter
from cloud.windows_image_worker import SupabaseAuthConfig, SupabaseTokenProvider

MIB = 1024 * 1024
MAX_FILE = 25 * MIB
MAX_RESPONSE = 1 * MIB
DEFAULT_LEASE_SECONDS = 1800
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_BACKOFF_SECONDS = 3.0
MAX_BACKOFF_SECONDS = 60.0
MAX_ERROR_CHARS = 500
STORAGE_PREFIX = "/storage/v1/object/ppt-library/"
CLAIM_RPC = "/rest/v1/rpc/single_judge_intake_claim"
COMPLETE_RPC = "/rest/v1/rpc/single_judge_intake_complete"
FAIL_RPC = "/rest/v1/rpc/single_judge_intake_fail"

PDF_MAGICS = (b"%PDF-", b"%%PDF-")
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
ZIP_MAGIC = b"PK"
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
SHA256_TEXT = re.compile(r"[0-9a-f]{64}\Z")
DECK_ID_TEXT = re.compile(
    r"import-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
CONTROL_TEXT = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")


class IntakeWorkerError(RuntimeError):
    """A safe, bounded diagnostic suitable for the local launcher log."""


class IntakeTransient(IntakeWorkerError):
    """A retryable claim or lease operation failed without a task outcome."""


class IntakeTaskError(IntakeWorkerError):
    """One claimed task could not be verified, converted, or published."""


class IntakeCompletionError(IntakeWorkerError):
    """Publication succeeded, but the completion RPC did not confirm it."""


def _safe_error(value: Any, fallback: str = "素材处理失败", secrets: tuple[str, ...] = ()) -> str:
    text = str(value) if isinstance(value, str) else fallback
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[redacted]")
    text = CONTROL_TEXT.sub(" ", text)
    text = re.sub(r"(?i)\b(Bearer|Authorization|apikey|password|token|secret)\b\s*[:=]\s*\S+",
                  r"\1=[redacted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or fallback)[:MAX_ERROR_CHARS]


def _uuid_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise IntakeTaskError(f"{label}无效")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise IntakeTaskError(f"{label}无效") from None


@dataclass(frozen=True)
class IntakeTask:
    id: str
    workspace_id: str
    title: str
    original_name: str
    object_path: str
    content_type: str
    size_bytes: int
    sha256: str
    ai_allowed: bool
    lease_token: str

    @property
    def suffix(self) -> str:
        return Path(self.original_name).suffix.lower()

    @classmethod
    def from_payload(cls, value: Any, workspace_id: str) -> "IntakeTask":
        if not isinstance(value, dict):
            raise IntakeTaskError("投递任务数据无效")
        task_id = _uuid_text(value.get("id"), "投递编号")
        workspace = _uuid_text(value.get("workspaceId"), "投递工作区")
        if workspace != workspace_id:
            raise IntakeTaskError("投递工作区不匹配")
        title = value.get("title")
        original_name = value.get("originalName")
        object_path = value.get("objectPath")
        content_type = value.get("contentType")
        size_bytes = value.get("sizeBytes")
        digest = value.get("sha256")
        ai_allowed = value.get("aiAllowed")
        lease_token = _uuid_text(value.get("leaseToken"), "投递租约")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or CONTROL_TEXT.search(title):
            raise IntakeTaskError("投递标题无效")
        if (not isinstance(original_name, str) or not 1 <= len(original_name) <= 255
                or CONTROL_TEXT.search(original_name) or "/" in original_name or "\\" in original_name):
            raise IntakeTaskError("投递文件名无效")
        suffix = Path(original_name).suffix.lower()
        if Path(original_name).name != original_name or suffix not in CONTENT_TYPES:
            raise IntakeTaskError("投递文件格式不受支持")
        if content_type != CONTENT_TYPES[suffix]:
            raise IntakeTaskError("投递文件类型不匹配")
        if not isinstance(object_path, str):
            raise IntakeTaskError("投递对象路径无效")
        expected_path = f"{workspace}/intake/{task_id}/{original_name}"
        if object_path != expected_path:
            raise IntakeTaskError("投递对象路径无效")
        if type(size_bytes) is not int or not 1 <= size_bytes <= MAX_FILE:
            raise IntakeTaskError("投递文件大小无效")
        if not isinstance(digest, str) or not SHA256_TEXT.fullmatch(digest):
            raise IntakeTaskError("投递文件摘要无效")
        if not isinstance(ai_allowed, bool):
            raise IntakeTaskError("投递 AI 授权无效")
        return cls(
            id=task_id,
            workspace_id=workspace,
            title=title.strip(),
            original_name=original_name,
            object_path=object_path,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=digest,
            ai_allowed=ai_allowed,
            lease_token=lease_token,
        )


class ProfessionalIntakeWorker:
    """Claim and process one queue item at a time.

    Intake is deliberately single-threaded.  ``start`` creates one daemon
    thread and ``stop`` sets its event and joins it, so a launcher shutdown
    cannot continue claiming new submissions.
    """

    def __init__(self, config: dict[str, Any], secrets: Any = None, *, tokens: Any = None,
                 transport: Callable[..., HTTPResponse] | None = None,
                 importer: Any = None, worker_name: str | None = None,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 log: Callable[[str], None] | None = None,
                 poll_seconds: float = DEFAULT_POLL_SECONDS,
                 backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
                 max_backoff_seconds: float = MAX_BACKOFF_SECONDS):
        self.origin = str(config["url"]).rstrip("/")
        self.apikey = str(config["anonKey"])
        self.workspace_id = str(UUID(str(config["workspaceId"])))
        self.transport = transport or DirectTransport(MAX_RESPONSE, MAX_FILE + 1)
        self.importer = importer or ProfessionalImporter(config)
        if tokens is None:
            if secrets is None:
                raise ValueError("intake Worker 缺少本机 DPAPI 凭据")
            auth = SupabaseAuthConfig(self.origin, self.apikey, secrets.email, secrets.password)
            tokens = SupabaseTokenProvider(auth)
        self.tokens = tokens
        self.worker_name = worker_name or f"guanye-intake-{os.getpid()}"
        if (not isinstance(self.worker_name, str) or not 1 <= len(self.worker_name.strip()) <= 200
                or CONTROL_TEXT.search(self.worker_name)):
            raise ValueError("intake Worker 编号无效")
        self.worker_name = self.worker_name.strip()
        if type(lease_seconds) is not int or not 60 <= lease_seconds <= 7200:
            raise ValueError("intake 租约时间无效")
        self.lease_seconds = lease_seconds
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.backoff_seconds = max(0.05, float(backoff_seconds))
        self.max_backoff_seconds = max(self.backoff_seconds, float(max_backoff_seconds))
        self._log = log or (lambda _message: None)
        self._sensitive = {
            item for item in (
                getattr(secrets, "email", ""), getattr(secrets, "password", ""),
                getattr(secrets, "worker_token", ""), getattr(secrets, "model_key", ""),
            ) if isinstance(item, str) and item
        }
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    def _emit(self, message: str) -> None:
        try:
            self._log(message)
        except Exception:
            # Logging must never stop queue processing.
            pass

    def _safe(self, value: Any, fallback: str = "素材处理失败") -> str:
        if isinstance(value, (IntakeWorkerError, ImportErrorResponse)):
            value = str(value)
        return _safe_error(value, fallback, tuple(self._sensitive))

    def _token(self) -> str:
        try:
            token = self.tokens.access_token()
        except Exception:
            raise IntakeTransient("登录评分师账号失败") from None
        if not isinstance(token, str) or not token:
            raise IntakeTransient("评分师登录凭据无效")
        self._sensitive.add(token)
        return token

    def _refresh(self, rejected: str) -> str:
        try:
            token = self.tokens.refresh_after_unauthorized(rejected)
        except Exception:
            raise IntakeTransient("登录已失效，请重新登录") from None
        if not isinstance(token, str) or not token:
            raise IntakeTransient("登录凭据刷新失败")
        self._sensitive.add(token)
        return token

    def _send(self, method: str, path: str, token: str, payload: Any = None) -> HTTPResponse:
        headers = {
            "apikey": self.apikey,
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            headers["Content-Type"] = "application/json"
        try:
            return self.transport(method, self.origin + path, headers, data, 120.0)
        except (GatewayError, OSError, TimeoutError, socket.timeout):
            raise IntakeTransient("连接私有投递服务失败") from None

    def _request(self, method: str, path: str, payload: Any = None) -> HTTPResponse:
        token = self._token()
        response = self._send(method, path, token, payload)
        if response.status == 401:
            token = self._refresh(token)
            response = self._send(method, path, token, payload)
        if response.status == 401:
            raise IntakeTransient("登录已失效，请重新登录")
        return response

    @staticmethod
    def _json_object(response: HTTPResponse, operation: str) -> dict[str, Any]:
        if not 200 <= response.status < 300:
            if response.status == 403:
                raise IntakeTransient(f"{operation}没有评分师权限")
            if response.status in (400, 409, 422):
                raise IntakeTransient(f"{operation}请求已失效")
            raise IntakeTransient(f"{operation}暂时不可用")
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise IntakeTransient(f"{operation}返回格式无效") from None
        if not isinstance(value, dict):
            raise IntakeTransient(f"{operation}返回格式无效")
        return value

    def claim(self) -> IntakeTask | None:
        response = self._request("POST", CLAIM_RPC, {
            "p_workspace_id": self.workspace_id,
            "p_worker": self.worker_name,
            "p_lease_seconds": self.lease_seconds,
        })
        value = self._json_object(response, "投递领取")
        if value.get("ok") is not True or "task" not in value:
            raise IntakeTransient("投递领取响应无效")
        task = value["task"]
        return None if task is None else IntakeTask.from_payload(task, self.workspace_id)

    @staticmethod
    def _storage_path(task: IntakeTask) -> str:
        return STORAGE_PREFIX + "/".join(quote(part, safe="") for part in task.object_path.split("/"))

    def _download(self, task: IntakeTask) -> bytes:
        response = self._request("GET", self._storage_path(task))
        if response.status == 404:
            raise IntakeTaskError("原始文件已不可用")
        if not 200 <= response.status < 300:
            raise IntakeTaskError("无法读取原始文件")
        raw = response.body
        if not raw:
            raise IntakeTaskError("原始文件为空")
        if len(raw) != task.size_bytes:
            raise IntakeTaskError("原始文件大小与投递任务不一致")
        if sha256(raw).hexdigest() != task.sha256:
            raise IntakeTaskError("原始文件 SHA256 与投递任务不一致")
        if task.suffix == ".pdf":
            valid = any(raw.startswith(magic) for magic in PDF_MAGICS)
        elif task.suffix == ".ppt":
            valid = raw.startswith(OLE_MAGIC)
        else:
            valid = raw.startswith(ZIP_MAGIC)
        if not valid:
            raise IntakeTaskError("原始文件内容与声明的格式不符")
        return raw

    def _validated_source(self, task: IntakeTask) -> bytes:
        try:
            raw = self._download(task)
        except IntakeTaskError:
            raise
        except Exception:
            raise IntakeTaskError("原始文件下载失败") from None
        # Keep the user-controlled name out of the local filesystem.  The
        # importer creates its own isolated temporary directory as well.
        try:
            with tempfile.TemporaryDirectory(prefix="guanye-intake-") as temporary:
                source = Path(temporary) / ("source" + task.suffix)
                source.write_bytes(raw)
                if source.stat().st_size != len(raw):
                    raise IntakeTaskError("原始文件临时副本校验失败")
        except IntakeTaskError:
            raise
        except OSError:
            raise IntakeTaskError("原始文件临时副本写入失败") from None
        return raw

    def _import_value(self, task: IntakeTask, raw: bytes) -> dict[str, Any]:
        return {
            "title": task.title,
            "aiAllowed": task.ai_allowed,
            "file": {
                "name": task.original_name,
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }

    def _import_task(self, task: IntakeTask, raw: bytes) -> str:
        token = self._token()
        for attempt in (0, 1):
            try:
                actor = self.importer.authorize(token)
                result = self.importer.import_one(actor, token, self._import_value(task, raw))
            except ImportErrorResponse as exc:
                if exc.status == 401 and attempt == 0:
                    token = self._refresh(token)
                    continue
                raise IntakeTaskError(self._safe(exc)) from None
            except Exception:
                raise IntakeTaskError("本机转换或发布失败") from None
            deck_id = result.get("deckId") if isinstance(result, dict) else None
            if (not isinstance(deck_id, str) or not DECK_ID_TEXT.fullmatch(deck_id)
                    or result.get("ok") is not True):
                raise IntakeTaskError("导入服务没有确认发布结果")
            return deck_id
        raise IntakeTaskError("导入服务没有确认发布结果")

    def complete(self, task: IntakeTask, deck_id: str) -> None:
        if not isinstance(deck_id, str) or not DECK_ID_TEXT.fullmatch(deck_id):
            raise IntakeCompletionError("作品编号无效")
        try:
            response = self._request("POST", COMPLETE_RPC, {
                "p_workspace_id": self.workspace_id,
                "p_id": task.id,
                "p_lease": task.lease_token,
                "p_deck_id": deck_id,
            })
            value = self._json_object(response, "投递完成")
        except Exception as exc:
            raise IntakeCompletionError(self._safe(exc, "投递完成回执失败")) from None
        if (value.get("ok") is not True or value.get("intakeId") != task.id
                or value.get("status") != "completed" or value.get("deckId") != deck_id):
            raise IntakeCompletionError("投递完成回执无效")

    def fail(self, task: IntakeTask, message: str) -> None:
        bounded = self._safe(message, "素材处理失败")
        response = self._request("POST", FAIL_RPC, {
            "p_workspace_id": self.workspace_id,
            "p_id": task.id,
            "p_lease": task.lease_token,
            "p_error": bounded,
        })
        value = self._json_object(response, "投递失败回写")
        if value.get("ok") is not True or value.get("intakeId") != task.id or value.get("status") != "failed":
            raise IntakeTransient("投递失败回写响应无效")

    def process_task(self, task: IntakeTask) -> str:
        try:
            raw = self._validated_source(task)
        except IntakeTaskError:
            raise
        except Exception:
            raise IntakeTaskError("原始文件校验失败") from None
        deck_id = self._import_task(task, raw)
        self.complete(task, deck_id)
        return deck_id

    def _run_one(self, task: IntakeTask) -> bool:
        try:
            deck_id = self.process_task(task)
        except IntakeTaskError as exc:
            error = self._safe(exc)
            self._emit(f"intake task {task.id} failed: {error}")
            try:
                self.fail(task, error)
            except Exception as fail_error:
                self._emit(f"intake task {task.id} fail writeback deferred: {self._safe(fail_error)}")
            return False
        except IntakeCompletionError as exc:
            # The import may already be published.  Never mark it failed after
            # a lost completion response; the lease will expire and the
            # idempotent import can be retried by a later claim.
            self._emit(f"intake task {task.id} completion pending: {self._safe(exc)}")
            return False
        except Exception:
            error = "素材处理失败"
            self._emit(f"intake task {task.id} failed: {error}")
            try:
                self.fail(task, error)
            except Exception as fail_error:
                self._emit(f"intake task {task.id} fail writeback deferred: {self._safe(fail_error)}")
            return False
        self._emit(f"intake task {task.id} completed")
        return True

    def run(self, stop_event: threading.Event | None = None, *, once: bool = False) -> int:
        stop = stop_event if stop_event is not None else threading.Event()
        backoff = self.backoff_seconds
        idle_delay = self.poll_seconds
        while not stop.is_set():
            try:
                task = self.claim()
            except Exception as exc:
                self._emit("intake claim deferred: " + self._safe(exc, "投递领取失败"))
                if once:
                    return 1
                if stop.wait(backoff):
                    return 0
                backoff = min(self.max_backoff_seconds, backoff * 2)
                continue
            if task is None:
                if once:
                    return 0
                if stop.wait(idle_delay):
                    return 0
                idle_delay = min(self.max_backoff_seconds, idle_delay * 2)
                continue
            ok = self._run_one(task)
            if once:
                return 0 if ok else 1
            backoff = self.backoff_seconds
            idle_delay = self.poll_seconds
            if stop.is_set():
                return 0
        return 0

    def start(self, *, once: bool = False) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("intake Worker 已在运行")
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self.run,
            args=(self._stop_event,),
            kwargs={"once": once},
            name="guanye-intake",
            daemon=True,
        )
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 10.0) -> bool:
        if self._stop_event is not None:
            self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()


def build_intake_worker(config: dict[str, Any], secrets: Any, **kwargs: Any) -> ProfessionalIntakeWorker:
    """Build the DPAPI-authenticated worker used by the launcher."""
    return ProfessionalIntakeWorker(config, secrets, **kwargs)
