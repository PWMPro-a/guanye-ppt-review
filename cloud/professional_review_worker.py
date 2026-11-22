"""Single-judge Windows pre-rating worker; no project service key is present here."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from getpass import GetPassWarning, getpass
import json
import math
from datetime import datetime, timezone
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote
from uuid import UUID
import warnings

from cloud import ai_reviews
from cloud.ai_gateway import DirectTransport, Gateway, GatewayConfig
from cloud.local_professional import load_browser_config
from cloud.windows_image_worker import SupabaseAuthConfig, SupabaseTokenProvider

EDGE_PATH = "/functions/v1/single-judge-review-worker"
BUCKET_PATH = "/storage/v1/object/ppt-library/"
MAX_IMAGE = 8 * 1024 * 1024
MAX_IMAGES_TOTAL = 20 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
TRACKS = ("uploaded", "generated")
VERSION = re.compile(r"[0-9a-f]{64}\Z")


class WorkerProtocolError(RuntimeError):
    """A safe, static operation label; avoid logging model or cloud payloads."""


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _object(value):
    if not isinstance(value, dict):
        raise WorkerProtocolError("预评接口响应无效")
    return value


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


@dataclass(frozen=True)
class Credentials:
    email: str = field(repr=False)
    password: str = field(repr=False)
    worker_token: str = field(repr=False)
    model_key: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", self.email):
            raise ValueError("预评账号需要有效邮箱")
        if not isinstance(self.password, str) or not 1 <= len(self.password) <= 2048:
            raise ValueError("缺少有效评委密码")
        for value in (self.worker_token, self.model_key):
            if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,512}", value):
                raise ValueError("缺少有效的 Worker 令牌或模型密钥")


class ReviewBroker:
    def __init__(self, config, tokens, worker_token, transport=None):
        self.origin = config["url"]
        self.workspace_id = config["workspaceId"]
        self.apikey = config["anonKey"]
        self.tokens = tokens
        self.worker_token = worker_token
        self.transport = transport if transport is not None else DirectTransport(MAX_RESPONSE, MAX_IMAGE)

    def _request(self, method, path, payload=None):
        # Paths originate only from fixed RPCs or individually escaped Storage segments.
        token = self.tokens.access_token()
        headers = {"apikey": self.apikey, "Authorization": "Bearer " + token}
        data = None
        if payload is not None:
            data = _json_bytes(payload)
            headers.update({"Content-Type": "application/json", "X-Guanye-Worker-Token": self.worker_token})
        response = self.transport(method, self.origin + path, headers, data, 30.0)
        if response.status == 401:
            headers["Authorization"] = "Bearer " + self.tokens.refresh_after_unauthorized(token)
            response = self.transport(method, self.origin + path, headers, data, 30.0)
        if response.status != 200:
            operation = "预评队列" if payload is not None else "预览读取"
            raise WorkerProtocolError(f"{operation}请求失败（HTTP {int(response.status)}）")
        return response.body

    def _broker(self, payload):
        raw = self._request("POST", EDGE_PATH, payload)
        if len(raw) > MAX_RESPONSE:
            raise WorkerProtocolError("预评接口响应超过限制")
        return _object(json.loads(raw))

    def claim(self, kind):
        return self._broker({"action": "claim", "kind": kind})

    def finish(self, job, status, *, result=None, error=None, terminal=False):
        outcome = self._broker({"action": "finish", "jobId": job["id"], "attempt": job["attempts"],
                                "status": status, "result": result, "error": error, "terminal": terminal})
        if (outcome.get("jobId") != job["id"] or outcome.get("aiVisible") is not False
                or not (outcome.get("status") == status and outcome.get("ok") is True
                        or outcome.get("status") in ("lease_lost", "version_changed") and outcome.get("ok") is False)):
            raise WorkerProtocolError("预评保存响应无效")

    def image(self, path: str) -> bytes:
        if (not isinstance(path, str) or len(path) > 1024 or "%" in path or "\\" in path
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
            raise WorkerProtocolError("预览路径无效")
        parts = path.split("/")
        ext = Path(parts[-1]).suffix.lower()
        if (len(parts) < 2 or parts[0] != self.workspace_id or any(p in ("", ".", "..") for p in parts)
                or ext not in (".png", ".jpg", ".jpeg", ".webp")):
            raise WorkerProtocolError("预览不在本工作区或格式不受支持")
        payload = self._request("GET", BUCKET_PATH + "/".join(quote(p, safe="") for p in parts))
        if not 0 < len(payload) <= MAX_IMAGE:
            raise WorkerProtocolError("预览图片大小无效")
        valid = (payload.startswith(b"\x89PNG\r\n\x1a\n") if ext == ".png" else
                 payload.startswith(b"\xff\xd8\xff") if ext in (".jpg", ".jpeg") else
                 payload.startswith(b"RIFF") and payload[8:12] == b"WEBP")
        if not valid:
            raise WorkerProtocolError("预览文件内容与图片格式不符")
        return payload


class VisionContext:
    def __init__(self, broker):
        self.workspace_id = broker.workspace_id
        self.broker = broker
        self.remaining = MAX_IMAGES_TOTAL
        self.cache: dict[str, bytes] = {}

    def storage_get(self, path):
        if path not in self.cache:
            payload = self.broker.image(path)
            self.remaining -= len(payload)
            if self.remaining < 0:
                raise WorkerProtocolError("单次预评的所有预览图片超过大小限制")
            self.cache[path] = payload
        return self.cache[path]


def _valid_review_details(bundle, job, kind, workspace):
    deck = _object(bundle.get("deck"))
    settings = _object(bundle.get("reviewSettings"))
    workspace_settings = _object(bundle.get("workspaceSettings"))
    if (deck.get("workspace_id") != workspace or deck.get("ai_allowed") is not True
            or job.get("sourceKind") != kind or deck.get("source_kind") != kind
            or not ai_reviews._valid_deck_id(job.get("deckId")) or job["deckId"] != deck.get("id")
            or job.get("variant") not in ("baseline", "assisted") or job.get("scope") not in ("deck", "page")
            or not isinstance(job.get("unitId"), str) or (job["scope"] == "page" and not job["unitId"])):
        raise WorkerProtocolError("预评目标或 AI 授权不匹配")
    for key in ("deckVersion", "rubricVersion"):
        version = bundle.get(key)
        if not isinstance(version, str) or not VERSION.fullmatch(version) or job.get(key) != version:
            raise WorkerProtocolError("预评版本不匹配")
    max_score = workspace_settings.get("maxScore", 5)
    if type(max_score) not in (int, float) or not math.isfinite(max_score) or max_score <= 0:
        raise WorkerProtocolError("评分上限无效")
    if kind == "uploaded":
        dimensions = workspace_settings.get("dimensions")
        if (not isinstance(dimensions, list) or not dimensions or any(
            not isinstance(dim, dict) or not isinstance(dim.get("id"), str) or not dim["id"] for dim in dimensions
        )):
            raise WorkerProtocolError("原库评分维度不可用")
    return deck, settings, workspace_settings


def review_one(broker: ReviewBroker, model: Gateway, kind: str) -> bool:
    if kind not in TRACKS:
        raise ValueError("未知预评轨道")
    bundle = broker.claim(kind)
    if "job" in bundle and bundle["job"] is None:
        return False
    job = _object(bundle.get("job"))
    if not _uuid(job.get("id")) or type(job.get("attempts")) is not int or not 1 <= job["attempts"] <= 3:
        raise WorkerProtocolError("预评领取凭据无效")
    try:
        deck, settings, workspace_settings = _valid_review_details(bundle, job, kind, broker.workspace_id)
    except WorkerProtocolError:
        broker.finish(job, "failed", error="作品授权、评分规则或版本已变化，未发送模型", terminal=True)
        return True
    try:
        ctx = VisionContext(broker)
        training = None
        if job["variant"] == "assisted":
            training = ai_reviews._prepare_calibration(ctx, bundle.get("calibration"), kind=kind,
                                                      deck_id=job["deckId"], scope=job["scope"])
        messages = ai_reviews._build_messages(ctx, job, deck, settings, workspace_settings, training)
        if not ctx.cache:
            raise WorkerProtocolError("预评未取得目标图片")
        raw = _object(ai_reviews._json_value(model.chat_json(messages)))
        model_scores = _object(raw.get("scores"))
        if any(type(value) not in (int, float) for value in model_scores.values()):
            raise WorkerProtocolError("模型评分包含非数值")
        result = ai_reviews._normalise_result(raw, job, settings, workspace_settings)
        # Check JSON limits before POST; malformed model output must not leave a live lease.
        if len(_json_bytes(result)) > 64 * 1024:
            raise WorkerProtocolError("模型评分结果超过大小限制")
    except Exception:
        broker.finish(job, "failed", error="未能读取预览、完成推理或校验评分，请重试")
    else:
        # Do not convert an ambiguous finish response into a second, conflicting terminal action.
        broker.finish(job, "completed", result=result)
    return True


def build_worker(config, secrets):
    auth = SupabaseAuthConfig(config["url"], config["anonKey"], secrets.email, secrets.password)
    broker = ReviewBroker(config, SupabaseTokenProvider(auth), secrets.worker_token)
    model = Gateway(GatewayConfig(config["url"], config["anonKey"], "", config["workspaceId"],
                                  model_api_key=secrets.model_key))
    return broker, model


def serve(broker, model, *, once=False, log=None):
    delay = 3.0
    while True:
        try:
            did_work = [review_one(broker, model, kind) for kind in TRACKS]
        except Exception as exc:
            label = str(exc) if isinstance(exc, WorkerProtocolError) else "预评连接暂时失败"
            _report(label, log=log, stream=sys.stderr)
            if once:
                return 1
            time.sleep(delay + random.random())
            delay = min(delay * 2, 60.0)
        else:
            if once:
                _report("预评扫描已完成" if any(did_work) else "暂无待预评作品", log=log, stream=sys.stdout)
                return 0
            delay = 3.0
            if not any(did_work):
                time.sleep(4.0)


def _report(message, *, log, stream):
    if stream is not None:
        print(message, file=stream, flush=True)
    if log is not None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # This file records only the latest fixed diagnostic, not user content.
        log.write_text(f"{stamp} {message}\n", encoding="utf-8")


def _read_credential(prompt):
    # getpass otherwise falls back to echoed stdin, including noninteractive launches.
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        return getpass(prompt)


def main(argv=None):
    from cloud.professional_worker_secrets import load_secrets, save_secrets, singleton, status_path

    parser = argparse.ArgumentParser(description="观页专业版后台预评")
    parser.add_argument("action", choices=("configure", "doctor", "run", "once"))
    parser.add_argument("--config", type=Path, required=True, help="专业版公开配置的本机路径")
    args = parser.parse_args(argv)
    log = None
    try:
        config = load_browser_config(args.config)
        if args.action == "doctor":
            load_secrets(config)
            print("本机凭据可由当前 Windows 用户解密；尚未验证云端身份或模型推理。")
            return 0
        with singleton(config):
            if args.action == "configure":
                secrets = Credentials(*(_read_credential(prompt) for prompt in (
                    "专业评分师邮箱：", "专业评分师密码：", "预评 Worker 令牌：", "6.2 Sol 接入密钥：")))
                save_secrets(config, secrets)
                print("已用当前 Windows 用户保护预评配置。")
                return 0
            log = status_path(config)
            worker = build_worker(config, load_secrets(config))
            _report("专业预评 Worker 进程已启动；尚未验证推理。", log=log, stream=sys.stdout)
            return serve(*worker, once=args.action == "once", log=log)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError, GetPassWarning, EOFError):
        try:
            _report("无法初始化观页预评 Worker：请检查 Windows 用户、工作区公开配置和本机凭据，或确认是否已有实例。",
                    log=log, stream=sys.stderr)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
