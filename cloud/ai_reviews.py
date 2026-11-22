"""Server-side AI review gateway for Guanye.

The module is intentionally self-contained: it registers fixed gateway routes, keeps
AI predictions private until a matching human submission exists, and never accepts a
client-provided user, judge, or workspace identity.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import mimetypes
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

REVIEW_PATH = "/api/ai/reviews"
IMPORT_PATH = "/api/ai/reviews/import"
KINDS = {"uploaded", "generated"}
QUEUE_ORDER = ("uploaded", "generated")
QUEUE_STATUSES = ("queued", "running", "completed", "failed")
QUEUE_PAGE_SIZE = 50
MAX_QUEUE_CURSOR_LENGTH = 4096
MAX_DECK_ID_LENGTH = 100
MAX_WORKSPACE_DETAIL_SUBMISSIONS = 1000
SCOPES = {"deck", "page"}
VARIANTS = {"baseline", "assisted"}
DOCUMENT_SUFFIXES = {".ppt", ".pptx"}
MAX_IMPORT_FILES = 10
MAX_IMPORT_FILE_BYTES = 25 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 100 * 1024 * 1024
MAX_IMPORT_PAGES = 600
MAX_TARGET_IMAGES = 12
MAX_REFERENCE_IMAGES = 3
MAX_CALIBRATION_IMAGES = 2
MAX_CALIBRATION_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PROMPT_JSON = 7000
PRE_EVALUATION_MAX_ATTEMPTS = 3
BACKFILL_DEFAULT_LIMIT = 8
BACKFILL_MAX_LIMIT = 20
SUPABASE_INT4_MIN = -(2**31)
SUPABASE_INT4_MAX = (2**31) - 1
BASELINE_SYSTEM_PROMPT = (
    "你是观页的专职视觉评委，按 GPT-6.2 Sol 的严谨口径进行单粒度预评。"
    "返回纯 JSON，不要 Markdown，不要解释图表之外的内容。"
)
ASSISTED_REVIEW_METHODS = (
    "\n\nassisted 预评补充（只依据图中可见证据和原评分尺度；不推测缺失内容）：\n"
    "1. 先看视觉入口与主次：字号、粗细、色彩和留白是否共同引导视线；"
    "同类元素的间距与边缘对齐是否一致。\n"
    "2. 再看形式是否服务内容：图表是否表达内容真正的关系；"
    "装饰是否服务信息，信息密度是否适合 PPT。\n"
    "3. 最后看选择是否有理由：设计是否服务当前内容、目标和受众；"
    "非对称（不对称）、密集或偏离常规若有图中可见的表达理由，应视为有效选择，"
    "不因个人偏好直接判差。"
)
ASSISTED_SCOPE_RULES = {
    "deck": "\n整套（scope=deck）：只看可观察的页间叙事角色、节奏和一致性，不把一页的证据当作另一页。",
    "page": "\n逐页（scope=page）：立足当前页及其生成计划，不假设未提供的页面。",
}


def configure(router):
    """Register the module's fixed routes on a gateway router."""
    router.add("GET", REVIEW_PATH, handle_get)
    router.add("POST", REVIEW_PATH, handle_post)
    router.add("POST", IMPORT_PATH, handle_import)


def _workspace(ctx) -> str:
    value = str(getattr(ctx, "workspace_id", "") or "").strip()
    if not value:
        raise PermissionError("缺少工作区")
    return value


def _user(ctx) -> str:
    value = str(getattr(ctx, "user_id", "") or "").strip()
    if not value:
        raise PermissionError("请先登录")
    return value


def _query(ctx) -> dict[str, str]:
    value = getattr(ctx, "query", {}) or {}
    return {str(k): str(v) for k, v in dict(value).items()}


def _body(ctx) -> dict[str, Any]:
    value = getattr(ctx, "body", {}) or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("请求内容不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("请求内容必须是 JSON 对象")
    return value


def _valid_deck_id(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= MAX_DECK_ID_LENGTH:
        return value
    return None


def _require_deck_id(value: Any) -> str:
    deck_id = _valid_deck_id(value)
    if deck_id is None:
        raise ValueError("需要 deckId")
    return deck_id


def _query_deck_id(query: dict[str, Any]) -> str:
    for key in ("deckId", "targetId", "deck_id"):
        if key in query:
            return _require_deck_id(query[key])
    return _require_deck_id(None)


def _rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        if isinstance(value.get("data"), list):
            return [dict(row) for row in value["data"]]
        return [dict(value)]
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    return []


def _select(ctx, table: str, query: dict[str, Any], privileged: bool = False) -> list[dict[str, Any]]:
    return _rows(ctx.select(table, query, privileged=privileged))


def _select_one(ctx, table: str, query: dict[str, Any], privileged: bool = False) -> dict[str, Any] | None:
    rows = _select(ctx, table, query, privileged)
    return rows[0] if rows else None


def _rpc(ctx, name: str, params: dict[str, Any] | None = None, privileged: bool = True) -> Any:
    return ctx.rpc(name, params or {}, privileged=privileged)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _member(ctx) -> dict[str, Any]:
    workspace, user = _workspace(ctx), _user(ctx)
    member = _select_one(ctx, "members", {"workspace_id": workspace, "user_id": user})
    if not member:
        raise PermissionError("请先加入团队")
    return member


def _workspace_row(ctx) -> dict[str, Any]:
    row = _select_one(ctx, "workspaces", {"id": _workspace(ctx)}, privileged=True)
    if not row:
        raise ValueError("工作区不存在")
    return row


def _is_judge(ctx) -> bool:
    row = _workspace_row(ctx)
    return bool(row.get("dedicated_judge_id")) and str(row.get("dedicated_judge_id")) == _user(ctx)


def _require_judge(ctx) -> None:
    if not _is_judge(ctx):
        raise PermissionError("仅专职评委可以执行此操作")


def _settings(ctx) -> dict[str, Any]:
    workspace = _workspace(ctx)
    row = _select_one(ctx, "ai_review_settings", {"workspace_id": workspace}, privileged=True)
    if not row:
        raise ValueError("AI 评审配置尚未初始化")
    return row


def _rubric(ctx) -> dict[str, Any]:
    value = _json_value(_rpc(ctx, "ai_review_rubric", {"w": _workspace(ctx)}))
    return value if isinstance(value, dict) else {}


def _rubric_version(ctx) -> str:
    value = _rpc(ctx, "ai_review_rubric_version", {"w": _workspace(ctx)})
    return str(value or "")


def _deck_version(ctx, deck_id: str) -> str:
    value = _rpc(ctx, "ai_review_deck_version", {"w": _workspace(ctx), "target_id": deck_id})
    return str(value or "")


def _deck(ctx, deck_id: str) -> dict[str, Any] | None:
    if not deck_id:
        return None
    return _select_one(ctx, "decks", {"workspace_id": _workspace(ctx), "id": str(deck_id)}, privileged=True)


def _deck_images(deck: dict[str, Any]) -> list[dict[str, Any]]:
    data = deck.get("data") or {}
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        generation = data.get("generation") if isinstance(data, dict) else None
        pages = generation.get("pages") if isinstance(generation, dict) else None
        images = pages if isinstance(pages, list) else []
    result = []
    for index, image in enumerate(images, 1):
        if not isinstance(image, dict):
            continue
        path = image.get("path") or image.get("imagePath") or image.get("storagePath")
        if not path:
            continue
        result.append({
            "id": str(image.get("id") or image.get("pageId") or f"page-{index}"),
            "name": str(image.get("name") or image.get("label") or f"page-{index}"),
            "path": str(path),
            "position": int(image.get("position") or index),
        })
    return result


def _has_private_preview(deck: dict[str, Any], workspace: str) -> bool:
    prefix = str(workspace).rstrip("/") + "/"
    return any(str(image.get("path") or "").startswith(prefix) for image in _deck_images(deck))


def _generated_warnings(deck: dict[str, Any]) -> list[str]:
    if str(deck.get("source_kind")) != "generated":
        return []
    data = deck.get("data") or {}
    generation = data.get("generation") if isinstance(data, dict) else None
    warnings = []
    if not isinstance(generation, dict):
        warnings.append("generated 作品缺少 decks.data.generation，已按常规 images 合同继续")
        return warnings
    for key in ("outline", "style", "pages", "references"):
        if key not in generation:
            warnings.append(f"decks.data.generation 缺少 {key}")
    return warnings


def _pick_images(images: list[dict[str, Any]], limit: int = MAX_TARGET_IMAGES) -> list[dict[str, Any]]:
    if len(images) <= limit:
        return images
    if limit <= 1:
        return images[:1]
    indexes = sorted({round(i * (len(images) - 1) / (limit - 1)) for i in range(limit)})
    return [images[i] for i in indexes]


def _image_content(ctx, path: str, label: str) -> dict[str, Any] | None:
    payload = ctx.storage_get(path)
    if payload is None:
        return None
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError(f"图片存储内容无效：{path}")
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    encoded = base64.b64encode(bytes(payload)).decode("ascii")
    return {"type": "text", "text": label}, {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
def _action(ctx, default: str = "") -> str:
    return (_query(ctx).get("action") or str(_body_safe(ctx).get("action") or default)).strip()


def _body_safe(ctx) -> dict[str, Any]:
    value = getattr(ctx, "body", {}) or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def handle_get(ctx):
    _member(ctx)
    action = _action(ctx, "workspace")
    if action in {"workspace", "state"}:
        return _workspace_state(ctx)
    if action == "pre-evaluation-status":
        return _pre_evaluation_status_response(ctx)
    if action == "queue":
        return _queue_state(ctx)
    if action == "queue-decks":
        return _queue_decks(ctx)
    if action == "library":
        return _as_dict(_rpc(ctx, "get_review_library", {"w": _workspace(ctx)}, privileged=False))
    if action in {"comparison", "stats"}:
        return _comparison(ctx)
    if action in {"samples", "training"}:
        _require_judge(ctx)
        query = _query(ctx)
        kind = str(query.get("kind") or query.get("track") or "").strip()
        deck_id = _query_deck_id(dict(getattr(ctx, "query", {}) or {}))
        scope = str(query.get("scope") or "").strip()
        if kind not in KINDS:
            raise ValueError("需要 kind（uploaded 或 generated）")
        if not deck_id:
            raise ValueError("需要 deckId")
        if scope not in SCOPES:
            raise ValueError("需要 scope（deck 或 page）")
        limit = max(1, min(12, int(query.get("limit") or 8)))
        raw = _rpc(ctx, "ai_review_calibration", {
            "w": _workspace(ctx), "actor": _user(ctx), "p_kind": kind,
            "p_limit": limit, "p_deck_id": deck_id, "p_scope": scope,
        })
        return _prepare_calibration(ctx, raw, kind=kind, deck_id=deck_id, scope=scope)["bundle"]
    if action == "settings":
        return {"settings": _settings(ctx), "isJudge": _is_judge(ctx), "role": _member(ctx).get("role")}
    raise ValueError(f"未知操作：{action}")


def handle_post(ctx):
    _member(ctx)
    action = _action(ctx, "")
    if action == "prepare":
        return _prepare(ctx)
    if action == "run":
        return _run_queue(ctx)
    if action == "submit":
        return _submit_human(ctx)
    if action == "submit-bundle":
        _require_judge(ctx)
        return _as_dict(_rpc(ctx, "single_judge_submit_bundle", {
            "w": _workspace(ctx), "payload": _body(ctx),
        }, privileged=False))
    if action == "formal":
        return _formal(ctx)
    if action == "admit":
        return _admit(ctx)
    if action == "settings":
        return _update_settings(ctx)
    if action == "backfill":
        return _backfill(ctx)
    raise ValueError(f"未知操作：{action}")


def _job_attempt_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _available_at_is_finite(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    text = str(value or "").strip().lower()
    if not text or "infinity" in text:
        return False
    try:
        datetime.fromisoformat(text.replace("z", "+00:00"))
    except ValueError:
        return False
    return True


def _pre_evaluation_status_for_deck(
    ctx,
    deck: dict[str, Any] | None,
    kind: str,
    *,
    deck_version: str | None = None,
    rubric_version: str | None = None,
) -> str:
    if not deck or str(deck.get("source_kind") or "uploaded") != kind:
        return "not_queued"
    workspace = _workspace(ctx)
    deck_version = _deck_version(ctx, str(deck.get("id") or "")) if deck_version is None else deck_version
    rubric_version = _rubric_version(ctx) if rubric_version is None else rubric_version
    if not deck_version or not rubric_version:
        return "not_queued"
    filters = {
        "workspace_id": workspace,
        "deck_id": deck["id"],
        "source_kind": kind,
        "scope": "deck",
        "deck_version": deck_version,
        "rubric_version": rubric_version,
        "select": "variant,status,attempts,available_at,created_at,updated_at",
    }
    if kind == "generated":
        filters["variant"] = "assisted"
    jobs = _select(ctx, "ai_review_jobs", filters, privileged=True)
    if not jobs:
        return "not_queued"
    # Migration 16 makes baseline the upload incumbent. Historical assisted
    # jobs still supply status for current snapshots without a baseline job.
    jobs.sort(key=lambda row: (
        kind == "uploaded" and row.get("variant") == "baseline",
        str(row.get("updated_at") or row.get("created_at") or ""),
        str(row.get("created_at") or ""),
    ), reverse=True)
    status = str(jobs[0].get("status") or "").strip().lower()
    if status in {"queued", "running", "completed"}:
        return status
    if status == "failed":
        attempts = _job_attempt_count(jobs[0].get("attempts"))
        if attempts < PRE_EVALUATION_MAX_ATTEMPTS and _available_at_is_finite(jobs[0].get("available_at")):
            return "retry_scheduled"
        return "failed"
    return "failed"


def _pre_evaluation_status_response(ctx) -> dict[str, str]:
    query = _query(ctx)
    kind = str(query.get("kind") or "").strip()
    if kind not in KINDS:
        raise ValueError("队列类型无效")
    raw_deck_id = query.get("deckId")
    deck_id = "" if raw_deck_id is None else str(raw_deck_id)
    deck = _deck(ctx, deck_id) if deck_id else None
    return {"preEvaluationStatus": _pre_evaluation_status_for_deck(ctx, deck, kind)}


def _queue_kind(value: Any, *, required: bool) -> str:
    kind = str(value or "").strip()
    if not kind and not required:
        return ""
    if kind not in KINDS:
        raise ValueError("队列类型无效")
    return kind


def _queue_cursor(value: Any) -> str:
    cursor = "" if value is None else str(value)
    if len(cursor) > MAX_QUEUE_CURSOR_LENGTH:
        raise ValueError("游标过长")
    return cursor


def _queue_deck_item(value: Any, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("作品队列返回格式无效")
    deck_id = value.get("id")
    title = value.get("title")
    name = value.get("name")
    source_kind = value.get("sourceKind")
    if (not isinstance(deck_id, str) or not deck_id
            or not isinstance(title, str) or not title
            or not isinstance(name, str) or not name
            or source_kind != kind):
        raise ValueError("作品队列返回格式无效")
    return {"id": deck_id, "title": title, "name": name, "sourceKind": source_kind}


def _queue_decks_page(ctx, kind: str, cursor: str) -> tuple[list[dict[str, Any]], str | None]:
    value = _json_value(_rpc(ctx, "ai_review_queue_decks", {
        "w": _workspace(ctx), "p_kind": kind, "p_after": cursor or None, "p_limit": QUEUE_PAGE_SIZE,
    }, privileged=True))
    if not isinstance(value, dict):
        raise ValueError("作品队列返回格式无效")
    raw_items = value.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("作品队列返回格式无效")
    items = [_queue_deck_item(raw_item, kind) for raw_item in raw_items]
    next_value = value.get("nextCursor")
    next_cursor = None if next_value is None or str(next_value) == "" else str(next_value)
    return items, next_cursor


def _queue_decks(ctx) -> dict[str, Any]:
    query = _query(ctx)
    kind = _queue_kind(query.get("kind"), required=True)
    cursor = _queue_cursor(query.get("cursor"))
    items, next_cursor = _queue_decks_page(ctx, kind, cursor)
    return {"kind": kind, "items": items, "nextCursor": next_cursor}


def _workspace_batch_rpc_missing(exc: ValueError) -> bool:
    message = str(exc).casefold()
    return (
        "pgrst202" in message
        or (
            "could not find the function public.ai_review_workspace_batch" in message
            and "schema cache" in message
        )
    )


def _workspace_batch_rows(value: Any, submission_ids: list[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    value = _json_value(value)
    if not isinstance(value, dict) or not isinstance(value.get("records"), list) or not isinstance(value.get("decisions"), list):
        raise ValueError("AI 工作区批次返回格式无效")
    allowed = set(submission_ids)
    records: dict[str, dict[str, Any]] = {}
    for raw in value["records"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise ValueError("AI 工作区批次返回格式无效")
        records.setdefault(raw["id"], dict(raw))
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value["decisions"]:
        if not isinstance(raw, dict):
            raise ValueError("AI 工作区批次返回格式无效")
        decision_id = raw.get("id")
        submission_id = raw.get("submission_id")
        if not isinstance(decision_id, str) or not decision_id or not isinstance(submission_id, str) or not submission_id:
            raise ValueError("AI 工作区批次返回格式无效")
        if submission_id not in allowed:
            raise ValueError("AI 工作区批次返回了未授权提交")
        if decision_id in seen:
            continue
        seen.add(decision_id)
        decisions.append(dict(raw))
    return records, decisions


def _workspace_details(
    ctx, workspace: str, user: str, deck: dict[str, Any], submissions: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]] | None:
    submission_ids = list(dict.fromkeys(
        str(row["id"]) for row in submissions if isinstance(row.get("id"), str) and row["id"]
    ))
    if len(submission_ids) > MAX_WORKSPACE_DETAIL_SUBMISSIONS:
        raise ValueError("单次工作区提交范围过大")
    try:
        raw = _rpc(ctx, "ai_review_workspace_batch", {
            "w": workspace,
            "actor": user,
            "p_deck_id": deck["id"],
            "p_submission_ids": submission_ids,
        }, privileged=True)
    except ValueError as exc:
        if _workspace_batch_rpc_missing(exc):
            return None
        raise
    return _workspace_batch_rows(raw, submission_ids)


def _legacy_workspace_details(
    ctx, workspace: str, submissions: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    records: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    for submission in submissions:
        if submission.get("_scores_complete"):
            matches = _select(ctx, "ai_review_matches", {"submission_id": submission["id"]}, privileged=True)
            for match in matches:
                record = _select_one(ctx, "ai_review_records", {"id": match["record_id"], "workspace_id": workspace}, privileged=True)
                if not record:
                    continue
                if record.get("scope") != submission.get("scope") or str(record.get("unit_id") or "") != str(submission.get("unit_id") or ""):
                    continue
                if record.get("deck_version") != submission.get("deck_version") or record.get("rubric_version") != submission.get("rubric_version"):
                    continue
                records.setdefault(str(record["id"]), record)
        for decision in _select(ctx, "ai_review_decisions", {"submission_id": submission["id"], "workspace_id": workspace}, privileged=True):
            entry = _select_one(ctx, "ai_generated_entries", {"decision_id": decision["id"], "workspace_id": workspace}, privileged=True)
            decision["_library_visible"] = bool(entry and entry.get("library_visible"))
            decision["_entry_id"] = entry.get("id") if entry else None
            decisions.append(decision)
    return records, decisions


def _workspace_state(ctx) -> dict[str, Any]:
    workspace, user = _workspace(ctx), _user(ctx)
    query = _query(ctx)
    kind = _queue_kind(query.get("kind") or "uploaded", required=True)
    raw_deck_id = query.get("deckId")
    deck_id = "" if raw_deck_id is None else str(raw_deck_id)
    deck = _deck(ctx, deck_id) if deck_id else None
    if deck_id and not deck:
        raise ValueError("作品不存在")
    if deck and str(deck.get("source_kind") or "uploaded") != kind:
        raise ValueError("作品与队列类型不匹配")
    queues: dict[str, list[dict[str, Any]]] = {}
    queue_cursors: dict[str, str | None] = {}
    for queue_kind in QUEUE_ORDER:
        queues[queue_kind], queue_cursors[queue_kind] = _queue_decks_page(ctx, queue_kind, "")
    if deck is None and not deck_id:
        first = queues[kind][0] if queues[kind] else None
        if first:
            deck = _deck(ctx, str(first["id"]))
            if deck and str(deck.get("source_kind") or "uploaded") != kind:
                raise ValueError("作品与队列类型不匹配")
    review = None
    submissions: list[dict[str, Any]] = []
    records: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []

    workspace_row = _workspace_row(ctx)
    workspace_settings = workspace_row.get("settings") or {}

    def scores_complete(submission: dict[str, Any]) -> bool:
        entry = _json_value(submission.get("entry")) or {}
        scores = entry.get("scores") if isinstance(entry, dict) else None
        if not isinstance(scores, dict):
            return False
        if str(submission.get("source_kind") or "uploaded") == "generated":
            expected = ["visual", "layout", "match"]
        else:
            expected = [str(dim.get("id")) for dim in workspace_settings.get("dimensions", []) if isinstance(dim, dict) and dim.get("id")]
        if not expected:
            return False
        maximum = float(submission.get("max_score") or workspace_settings.get("maxScore") or 1)
        for dimension in expected:
            value = scores.get(dimension)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            number = float(value)
            if not math.isfinite(number) or number < 0 or number > maximum:
                return False
        return True

    if deck:
        review = _select_one(ctx, "reviews", {"workspace_id": workspace, "user_id": user, "deck_id": deck["id"]}, privileged=True)
        submissions = _select(ctx, "ai_review_submissions", {"workspace_id": workspace, "deck_id": deck["id"], "human_user_id": user}, privileged=True)
        submissions.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        for submission in submissions:
            submission["_scores_complete"] = scores_complete(submission)
        details = _workspace_details(ctx, workspace, user, deck, submissions)
        if details is None:
            records, decisions = _legacy_workspace_details(ctx, workspace, submissions)
        else:
            records, decisions = details
    decisions.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    settings = _settings(ctx)
    judge = _is_judge(ctx)
    queue = _queue_counts(ctx) if judge else None
    deck_version = _deck_version(ctx, deck["id"]) if deck else ""
    rubric_version = _rubric_version(ctx)
    pre_evaluation_status = _pre_evaluation_status_for_deck(
        ctx, deck, kind, deck_version=deck_version, rubric_version=rubric_version,
    )
    return {
        "kind": kind,
        "deck": deck,
        "review": review,
        "submissions": [_public_submission(row) for row in submissions],
        "ai": [_public_record(row) for row in records.values()],
        "decisions": [_public_decision(row) for row in decisions],
        "settings": _public_settings(settings),
        "workspaceSettings": workspace_settings,
        "role": _member(ctx).get("role"),
        "isJudge": judge,
        "singleJudge": bool(workspace_row.get("single_judge_admin_id")),
        "queue": queue,
        "queues": queues,
        "queueCursors": queue_cursors,
        "preEvaluationStatus": pre_evaluation_status,
        "deckVersion": deck_version,
        "rubricVersion": rubric_version,
        "contractWarnings": _generated_warnings(deck) if deck else [],
        "blind": {"aiVisible": bool(records), "rule": "仅与已提交人工记录同版本、同评分标准、同粒度的预评解锁"},
    }


def _public_submission(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"), "deckId": row.get("deck_id"), "scope": row.get("scope"),
        "unitId": row.get("unit_id") or "", "position": row.get("position"),
        "deckVersion": row.get("deck_version"), "rubricVersion": row.get("rubric_version"),
        "humanVersion": row.get("human_version"), "entry": row.get("entry"),
        "scoresComplete": bool(row.get("_scores_complete")),
        "createdAt": row.get("created_at"),
    }


def _public_record(row: dict[str, Any]) -> dict[str, Any]:
    result = _json_value(row.get("ai_result")) or {}
    return {
        "recordId": row.get("id"), "submissionId": row.get("submission_id"),
        "jobId": row.get("job_id"), "deckId": row.get("deck_id"), "scope": row.get("scope"),
        "unitId": row.get("unit_id") or "", "variant": row.get("variant"),
        "deckVersion": row.get("deck_version"), "rubricVersion": row.get("rubric_version"),
        "modelHint": row.get("model_hint"), "result": result,
    }


def _public_decision(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "decisionId": row.get("id"), "submissionId": row.get("submission_id"),
        "aiRecordId": row.get("ai_record_id"), "status": row.get("status"),
        "classification": row.get("classification"), "reasons": row.get("reasons") or [],
        "reasonOther": row.get("reason_other") or "", "formalScores": row.get("formal_scores"),
        "formalTotal": row.get("formal_total"), "scoreRatio": row.get("score_ratio"),
        "expiresAt": row.get("expires_at"), "libraryVisible": bool(row.get("_library_visible")),
        "entryId": row.get("_entry_id"), "summary": row.get("summary") or "",
    }


def _public_settings(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "visionWeight": float(row.get("vision_weight") or 0), "layoutWeight": float(row.get("layout_weight") or 0),
        "matchWeight": float(row.get("match_weight") or 0), "humanThreshold": float(row.get("human_threshold") or 0),
        "modelHint": row.get("model_hint") or "gpt-6.2-sol", "reasonCatalog": _json_value(row.get("reason_catalog")) or [],
    }


def _queue_counts(ctx) -> dict[str, Any]:
    value = _json_value(_rpc(ctx, "ai_review_queue_counts", {"w": _workspace(ctx)}, privileged=True))
    if not isinstance(value, dict):
        raise ValueError("队列统计返回格式无效")
    result: dict[str, dict[str, int]] = {}
    for kind in QUEUE_ORDER:
        track = value.get(kind)
        if not isinstance(track, dict):
            raise ValueError("队列统计返回格式无效")
        result[kind] = {}
        for status in QUEUE_STATUSES:
            raw_count = track.get(status)
            if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
                raise ValueError("队列统计返回格式无效")
            result[kind][status] = raw_count
    return result


def _queue_job_item(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("id"),
        "deck_id": job.get("deck_id") or job.get("deckId"),
        "source_kind": job.get("source_kind") or job.get("sourceKind") or job.get("track"),
        "scope": job.get("scope"),
        "unit_id": job.get("unit_id") if "unit_id" in job else job.get("unitId"),
        "position": job.get("position"),
        "variant": job.get("variant"),
        "status": job.get("status"),
        "attempts": job.get("attempts"),
        "bucket": job.get("bucket"),
        "split": job.get("split"),
        "deck_version": job.get("deck_version") or job.get("deckVersion"),
        "rubric_version": job.get("rubric_version") or job.get("rubricVersion"),
        "error": job.get("error"),
    }


def _queue_jobs(ctx, kind: str) -> list[dict[str, Any]]:
    value = _json_value(_rpc(ctx, "ai_review_queue_jobs", {
        "w": _workspace(ctx), "p_kind": kind or None,
    }, privileged=True))
    if not isinstance(value, list):
        raise ValueError("预评队列返回格式无效")
    return [_queue_job_item(job) for job in value if isinstance(job, dict)]


def _queue_state(ctx) -> dict[str, Any]:
    _require_judge(ctx)
    kind = _queue_kind(_query(ctx).get("kind"), required=False)
    items = _queue_jobs(ctx, kind)
    return {"counts": _queue_counts(ctx), "items": items, "aiVisible": False}


def _comparison(ctx) -> dict[str, Any]:
    value = _rpc(ctx, "ai_review_comparison", {"w": _workspace(ctx)}, privileged=True)
    if isinstance(value, str):
        value = _json_value(value)
    required = {"holdoutUnits", "baseline", "assisted", "improvement", "byKind", "splitAlgorithm", "holdoutBucket"}
    kind_fields = {"holdoutUnits", "baseline", "assisted", "improvement"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("AI comparison 返回格式无效")
    by_kind = value.get("byKind")
    if not isinstance(by_kind, dict):
        raise ValueError("AI comparison 返回格式无效")
    for kind in ("uploaded", "generated"):
        if not isinstance(by_kind.get(kind), dict) or not kind_fields.issubset(by_kind[kind]):
            raise ValueError("AI comparison 返回格式无效")
    return value


def _prepare(ctx) -> dict[str, Any]:
    _require_judge(ctx)
    body = _body(ctx)
    deck_id = _require_deck_id(body.get("deckId"))
    scope = str(body.get("scope") or "deck")
    if scope not in SCOPES:
        raise ValueError("预评粒度无效")
    payload = {"deckId": deck_id, "scope": scope}
    if scope == "page":
        payload["unitIds"] = body.get("unitIds") or body.get("pages") or []
    return _as_dict(_rpc(ctx, "ai_review_prepare", {"w": _workspace(ctx), "actor": _user(ctx), "payload": payload}))


def _submit_human(ctx) -> dict[str, Any]:
    body = _body(ctx)
    deck_id = _require_deck_id(body.get("deckId"))
    scope = str(body.get("scope") or "deck")
    if scope not in SCOPES:
        raise ValueError("评分粒度无效")
    raw_units = body.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("请选择至少一个评分单元")
    units = []
    for unit in raw_units:
        if not isinstance(unit, dict):
            raise ValueError("评分单元无效")
        clean = {"scores": unit.get("scores") if isinstance(unit.get("scores"), dict) else {}}
        if scope == "page":
            clean["unitId"] = str(unit.get("unitId") or "")
            clean["position"] = int(unit.get("position") or 0)
        for key in ("note", "favorite", "classificationSuggestion", "classificationCorrection", "reasons", "reasonOther"):
            if key in unit:
                clean[key] = unit[key]
        if "reasons" in clean and "other" in clean["reasons"] and not str(clean.get("reasonOther") or "").strip():
            raise ValueError("选择“其他”时请填写原因")
        units.append(clean)
    payload = {
        "deckId": deck_id, "scope": scope, "units": units,
        "deckVersion": body.get("deckVersion"), "rubricVersion": body.get("rubricVersion"),
    }
    return _as_dict(_rpc(ctx, "ai_review_submit", {"w": _workspace(ctx), "actor": _user(ctx), "payload": payload}))


def _update_settings(ctx) -> dict[str, Any]:
    _require_judge(ctx)
    result = _rpc(ctx, "ai_review_update_settings", {"w": _workspace(ctx), "actor": _user(ctx), "payload": _body(ctx)})
    return {"settings": _as_dict(result)}


def _formal(ctx) -> dict[str, Any]:
    _require_judge(ctx)
    return _as_dict(_rpc(ctx, "ai_review_confirm", {"w": _workspace(ctx), "actor": _user(ctx), "payload": _body(ctx)}))


def _admit(ctx) -> dict[str, Any]:
    _require_judge(ctx)
    return _as_dict(_rpc(ctx, "ai_review_admit", {"w": _workspace(ctx), "actor": _user(ctx), "payload": _body(ctx)}))


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = _json_value(value)
        if isinstance(parsed, dict):
            return parsed
    return {"value": value}


class ObsoleteReview(ValueError):
    """A queued attempt no longer targets the current work or rubric."""


def _prepare_calibration(ctx, payload: Any, *, kind: str, deck_id: str, scope: str) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    bundle = {
        "samples": [], "corrections": [],
        "selectedSamples": 0, "selectedCorrections": 0,
        "kind": kind, "requestedScope": scope,
    }
    images: dict[str, str] = {}
    prefix = _workspace(ctx).rstrip("/") + "/"
    for list_name in ("corrections", "samples"):
        entries = raw.get(list_name)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            item_track = str(item.get("track") or "").strip()
            item_deck = _valid_deck_id(item.get("deckId"))
            item_scope = str(item.get("scope") or "").strip()
            if (
                item_track != kind or str(item.get("split") or "").strip() != "calibration"
                or not item_deck or item_deck == deck_id or item_scope not in SCOPES
            ):
                continue
            ref_id = f"cal-{list_name[:-1]}-{len(bundle[list_name]) + 1}"
            if list_name == "samples":
                clean = {
                    "refId": ref_id,
                    "sampleType": str(item.get("sampleType") or "human_submission"),
                    "track": kind, "split": "calibration", "scope": item_scope,
                    "scopeFallback": bool(item.get("scopeFallback")),
                    "scores": item.get("scores") if isinstance(item.get("scores"), dict) else {},
                    "maxScore": item.get("maxScore"),
                    "classification": item.get("classification"),
                    "style": item.get("style"),
                    "sameClassification": bool(item.get("sameClassification")),
                    "sameStyle": bool(item.get("sameStyle")),
                }
            else:
                clean = {
                    "refId": ref_id,
                    "sampleType": "judge_formal_decision",
                    "track": kind, "split": "calibration", "scope": item_scope,
                    "scopeFallback": bool(item.get("scopeFallback")),
                    "status": str(item.get("status") or ""),
                    "formalScores": item.get("formalScores") if isinstance(item.get("formalScores"), dict) else {},
                    "maxScore": item.get("maxScore"),
                    "formalTotal": item.get("formalTotal"),
                    "scoreRatio": item.get("scoreRatio"),
                    "classification": item.get("classification"),
                    "style": item.get("style"),
                    "reasons": item.get("reasons") if isinstance(item.get("reasons"), list) else [],
                    "reasonOther": str(item.get("reasonOther") or ""),
                    "summary": str(item.get("summary") or ""),
                    "sameClassification": bool(item.get("sameClassification")),
                    "sameStyle": bool(item.get("sameStyle")),
                }
            bundle[list_name].append(clean)
            path = str(item.get("previewPath") or "")
            if path.startswith(prefix) and ".." not in path and "\\" not in path:
                images[ref_id] = path
    bundle["selectedSamples"] = len(bundle["samples"])
    bundle["selectedCorrections"] = len(bundle["corrections"])
    bundle["scopeFallback"] = any(item.get("scopeFallback") for item in bundle["samples"])
    bundle["correctionScopeFallback"] = any(item.get("scopeFallback") for item in bundle["corrections"])
    return {"bundle": bundle, "images": images}


def _retained_calibration_entries(calibration: Any) -> list[dict[str, Any]]:
    if not isinstance(calibration, dict):
        return []
    result = []
    for name in ("corrections", "samples"):
        items = calibration.get(name)
        if isinstance(items, list):
            result.extend(item for item in items if isinstance(item, dict) and item.get("refId"))
    return result


def _calibration_image_content(ctx, ref_id: str, path: str, remaining: int) -> tuple[list[dict[str, Any]] | None, int]:
    prefix = _workspace(ctx).rstrip("/") + "/"
    if not path.startswith(prefix) or ".." in path or "\\" in path:
        return None, 0
    try:
        payload = ctx.storage_get(path)
    except Exception:
        return None, 0
    if not isinstance(payload, (bytes, bytearray)):
        return None, 0
    raw = bytes(payload)
    if not raw or len(raw) > remaining:
        return None, 0
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    label = f"人工评分示例 {ref_id}（仅用于校准，不是当前页，不计入当前页）"
    return [
        {"type": "text", "text": label},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}},
    ], len(raw)


def run_review_attempt(ctx, kind: str) -> dict[str, Any] | None:
    jobs = _as_list(_rpc(ctx, "ai_review_claim_jobs", {
        "w": _workspace(ctx), "actor": _user(ctx), "p_kind": kind, "p_limit": 1,
    }))
    if not jobs:
        return None
    job = jobs[0]
    try:
        training = None
        if job.get("variant") == "assisted":
            track = str(job.get("track") or job.get("sourceKind") or job.get("source_kind") or "")
            deck_id = str(job.get("deckId") or job.get("deck_id") or "")
            scope = str(job.get("scope") or "")
            if track != kind:
                raise ValueError("预评任务轨道不一致")
            if not deck_id:
                raise ValueError("预评任务缺少目标作品")
            if scope not in SCOPES:
                raise ValueError("预评任务粒度无效")
            raw_calibration = _rpc(ctx, "ai_review_calibration", {
                "w": _workspace(ctx), "actor": _user(ctx), "p_kind": track,
                "p_limit": 8, "p_deck_id": deck_id, "p_scope": scope,
            })
            training = _prepare_calibration(ctx, raw_calibration, kind=track, deck_id=deck_id, scope=scope)
        return _run_job(ctx, job, training)
    except Exception as exc:
        return _finish_attempt(ctx, job, "failed", error=_safe_error(exc), terminal=isinstance(exc, ObsoleteReview))


def _run_queue(ctx) -> dict[str, Any]:
    _require_judge(ctx)
    body = _body(ctx)
    kind = str(body.get("kind") or _query(ctx).get("kind") or "uploaded")
    if kind not in KINDS:
        raise ValueError("队列类型无效")
    outcome = run_review_attempt(ctx, kind) or {}
    counts = _queue_counts(ctx)
    return {"ok": True, "kind": kind, "completed": int(outcome.get("status") == "completed"),
            "failed": int(outcome.get("status") == "failed"), "errors": [], "counts": counts, "aiVisible": False,
            "next": {"action": "run", "kind": kind} if counts.get(kind, {}).get("queued", 0) else None}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, str):
        parsed = _json_value(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _safe_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    text = re.sub(r"(?i)(bearer|token|authorization|service[_-]?role)\s*[:=]?\s*\S+", r"\1 [redacted]", text)
    return text[:1000]


def _run_job(ctx, job: dict[str, Any], training: Any) -> dict[str, Any]:
    deck = _deck(ctx, str(job.get("deckId") or job.get("deck_id") or ""))
    if not deck:
        raise ValueError("作品不存在")
    deck_hash = _deck_version(ctx, deck["id"])
    rubric_hash = _rubric_version(ctx)
    if str(job.get("deckVersion") or job.get("deck_version")) != deck_hash:
        raise ObsoleteReview("作品版本已变化，预评作废")
    if str(job.get("rubricVersion") or job.get("rubric_version")) != rubric_hash:
        raise ObsoleteReview("评分标准已变化，预评作废")
    settings = _settings(ctx)
    workspace_settings = _workspace_row(ctx).get("settings") or {}
    messages = _build_messages(ctx, job, deck, settings, workspace_settings, training if job.get("variant") == "assisted" else None)
    response = ctx.chat_json(messages)
    result = _normalise_result(_json_value(response), job, settings, workspace_settings)
    return _finish_attempt(ctx, job, "completed", result=result)


def _finish_attempt(ctx, job, status, *, result=None, error=None, terminal=False) -> dict[str, Any]:
    return _as_dict(_rpc(ctx, "ai_review_finish_attempt", {
        "w": _workspace(ctx), "actor": _user(ctx), "target_job": job.get("id"),
        "p_attempt": job.get("attempts"), "p_status": status, "p_result": result,
        "p_error": error, "p_terminal": terminal,
    }))


def _generation_page_plan(generation: Any, unit_id: str, position: Any = None) -> Any:
    if not isinstance(generation, dict):
        return None
    pages = generation.get("pages")
    candidates: list[Any] = []
    if isinstance(pages, list):
        candidates = pages
    elif isinstance(pages, dict):
        if unit_id and unit_id in pages:
            return pages[unit_id]
        candidates = list(pages.values())
    unit_text = str(unit_id or "")
    for page in candidates:
        if not isinstance(page, dict):
            continue
        page_ids = {str(page.get(key) or "") for key in ("unitId", "id", "pageId", "imageId", "blockId")}
        page_ids.discard("")
        if unit_text and unit_text in page_ids:
            return page
    if position is not None:
        for index, page in enumerate(candidates, 1):
            if not isinstance(page, dict):
                continue
            page_position = page.get("position") or page.get("page") or page.get("pageNumber") or page.get("index")
            try:
                if int(page_position) == int(position):
                    return page
            except (TypeError, ValueError):
                if index == int(position):
                    return page
    return None


_PAGE_ID_FIELDS = (
    "unitId", "id", "pageId", "imageId", "blockId",
    "position", "page", "pageNumber", "index",
)
_SUMMARY_FIELDS = ("outline", "style", "contentStrategy", "generationBrief")


def _prompt_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _page_copy_text(page: Any) -> str:
    copy = page.get("copy") if isinstance(page, dict) else page
    if isinstance(copy, list):
        parts = []
        for item in copy:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    kind = item.get("kind") or item.get("role")
                    parts.append(f"[{kind}] {text}" if kind else str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(page, dict):
        return str(page.get("text") or page.get("body") or page.get("content") or "")
    return str(copy or "")


def _fit_text_fragment(payload: dict[str, Any], limit: int, text_key: str, source_length: int) -> dict[str, Any] | None:
    if len(_prompt_json(payload)) <= limit:
        return payload
    payload = {**payload, "truncated": True}
    budget = dict(payload.get("budget") or {})
    budget.update({
        "truncated": True, "maxPromptJson": MAX_PROMPT_JSON,
        "pieceLimit": limit, "originalLength": source_length,
    })
    payload["budget"] = budget
    original = str(payload.get(text_key) or "")
    low, high, best = 0, len(original), None
    while low <= high:
        middle = (low + high) // 2
        candidate = {**payload, text_key: original[:middle]}
        if len(_prompt_json(candidate)) <= limit:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


def _fit_calibration(value: Any, limit: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if len(_prompt_json(value)) <= limit:
        return value
    samples = list(value.get("samples")) if isinstance(value.get("samples"), list) else []
    corrections = list(value.get("corrections")) if isinstance(value.get("corrections"), list) else []
    result = {
        "selectedSamples": value.get("selectedSamples", len(samples)),
        "selectedCorrections": value.get("selectedCorrections", len(corrections)),
        "samples": [], "corrections": [],
        "budget": {"truncated": True, "omittedSamples": len(samples), "omittedCorrections": len(corrections)},
    }
    for index in range(max(len(samples), len(corrections))):
        for kind, items in (("corrections", corrections), ("samples", samples)):
            if index >= len(items):
                continue
            trial = {**result, "samples": list(result["samples"]), "corrections": list(result["corrections"])}
            trial[kind].append(items[index])
            if len(_prompt_json(trial)) <= limit:
                result = trial
    result["budget"]["omittedSamples"] = len(samples) - len(result["samples"])
    result["budget"]["omittedCorrections"] = len(corrections) - len(result["corrections"])
    return result if len(_prompt_json(result)) <= limit else None


def _attach_prompt_piece(task: dict[str, Any], key: str, value: Any) -> None:
    if value is None or len(_prompt_json({**task, key: value})) > MAX_PROMPT_JSON:
        return
    task[key] = value
    marker = task.get("promptBudget")
    if isinstance(marker, dict) and isinstance(marker.get("omittedFields"), list):
        marker["omittedFields"] = [field for field in marker["omittedFields"] if field != key]


def _budget_prompt_task(task: dict[str, Any]) -> dict[str, Any]:
    full_length = len(_prompt_json(task))
    if full_length <= MAX_PROMPT_JSON:
        return task
    generation = task.get("generation")
    page_plan = task.get("generationPagePlan")
    calibration = task.get("calibration")
    has_calibration = isinstance(calibration, dict) and bool(calibration.get("samples") or calibration.get("corrections"))
    applicable = [name for name in ("generation", "generationPagePlan", "calibration") if task.get(name) is not None]
    core = {
        key: task.get(key) for key in (
            "modelRole", "sourceKind", "scope", "unitId", "deckId",
            "maxScore", "requiredDimensions", "generatedWeights",
        )
    }
    core.update({
        "generation": None, "generationPagePlan": None,
        "matchChecklist": task.get("matchChecklist"), "rules": task.get("rules"),
        "calibration": None, "promptBudget": {
            "truncated": True, "reason": "MAX_PROMPT_JSON", "maxChars": MAX_PROMPT_JSON,
            "originalLength": full_length, "omittedFields": applicable,
        },
    })
    if len(_prompt_json(core)) > MAX_PROMPT_JSON:
        raise ValueError("基础评审契约超过 MAX_PROMPT_JSON")

    def available(key: str) -> int:
        return MAX_PROMPT_JSON - len(_prompt_json({**core, key: None})) + len("null")

    if page_plan is not None:
        limit = available("generationPagePlan")
        limit = min(limit, max(1, int(limit * (0.55 if (generation is not None or has_calibration) else 0.8))))
        page = page_plan if isinstance(page_plan, dict) else {"text": str(page_plan)}
        payload = {key: page[key] for key in _PAGE_ID_FIELDS if key in page}
        if page.get("title"):
            payload["title"] = page["title"]
        payload.update({"copy": _page_copy_text(page), "truncated": False})
        _attach_prompt_piece(
            core, "generationPagePlan",
            _fit_text_fragment(payload, limit, "copy", len(_prompt_json(page_plan))),
        )

    if generation is not None:
        limit = available("generation")
        share = 0.45 if (page_plan is not None and has_calibration) else (0.55 if has_calibration else 1.0)
        limit = min(limit, max(1, int(limit * share)))
        if isinstance(generation, dict):
            summary = {}
            if task.get("scope") == "deck":
                pages = generation.get("pages")
                pages = pages if isinstance(pages, list) else list(pages.values()) if isinstance(pages, dict) else []
                summary["pages"] = [
                    {
                        "unitId": page.get("unitId") or page.get("id"),
                        "title": page.get("title"), "copy": _page_copy_text(page),
                    }
                    for page in pages if isinstance(page, dict)
                ]
            summary.update({key: generation[key] for key in _SUMMARY_FIELDS if key in generation})
            summary_text = _prompt_json(summary)
        else:
            summary_text = str(generation)
        payload = {
            "summary": summary_text, "summarized": True, "truncated": False,
            "budget": {"summarized": True, "truncated": False, "originalLength": len(_prompt_json(generation))},
        }
        _attach_prompt_piece(
            core, "generation",
            _fit_text_fragment(payload, limit, "summary", len(_prompt_json(generation))),
        )

    if calibration is not None:
        _attach_prompt_piece(core, "calibration", _fit_calibration(calibration, available("calibration")))

    if len(_prompt_json(core)) > MAX_PROMPT_JSON:
        raise ValueError("结构化提示词无法控制在 MAX_PROMPT_JSON 内")
    return core


def _build_messages(ctx, job, deck, settings, workspace_settings, training) -> list[dict[str, Any]]:
    source_kind = str(job.get("sourceKind") or job.get("source_kind") or deck.get("source_kind") or "uploaded")
    scope = str(job.get("scope") or "deck")
    unit_id = str(job.get("unitId") or job.get("unit_id") or "")
    calibration_bundle = training
    calibration_images: dict[str, str] = {}
    if isinstance(training, dict) and isinstance(training.get("bundle"), dict):
        calibration_bundle = training["bundle"]
        if isinstance(training.get("images"), dict):
            calibration_images = training["images"]
    maximum = float((workspace_settings or {}).get("maxScore") or 5)
    dimensions = ["visual", "layout", "match"] if source_kind == "generated" else [
        str(dim.get("id")) for dim in (workspace_settings or {}).get("dimensions", []) if isinstance(dim, dict) and dim.get("id")
    ]
    images = _deck_images(deck)
    if scope == "page":
        images = [image for image in images if image["id"] == unit_id]
    else:
        images = _pick_images(images, MAX_TARGET_IMAGES)
    if not images:
        raise ValueError("预评目标没有可读取图片")
    task = {
        "modelRole": "GPT-6.2 Sol",
        "sourceKind": source_kind,
        "scope": scope,
        "unitId": unit_id or None,
        "deckId": deck["id"],
        "maxScore": maximum,
        "requiredDimensions": dimensions,
        "generatedWeights": {
            "visual": float(settings.get("vision_weight") or 40),
            "layout": float(settings.get("layout_weight") or 30),
            "match": float(settings.get("match_weight") or 30),
        },
        "generation": (deck.get("data") or {}).get("generation") if source_kind == "generated" else None,
        "generationPagePlan": _generation_page_plan(
            (deck.get("data") or {}).get("generation") if source_kind == "generated" else None,
            unit_id,
            job.get("position"),
        ),
        "matchChecklist": {
            "outline": "核对生成大纲与结构",
            "style": "核对生成风格与视觉语言",
            "text": "核对文字；优先以当前页对应的生成计划文案为依据",
        } if source_kind == "generated" else None,
        "rules": [
            "当前是预评，不包含、也不得推测当前作品的人工评分或人工备注。",
            "只返回 JSON。scores 必须在 0 到 maxScore 之间。",
            "generated 作品必须给出 visual/layout/match 三项；match 必须逐项校对大纲/结构、风格和文字，文字以当前页对应的生成计划文案为优先依据。",
            "历史校准样本只用于风格与尺度校准，不得复制其文案。",
        ],
        "calibration": calibration_bundle or {"samples": [], "corrections": [], "selectedSamples": 0, "selectedCorrections": 0},
    }
    prompt_task = _budget_prompt_task(task)
    text = _prompt_json(prompt_task)
    if len(text) > MAX_PROMPT_JSON:
        raise ValueError("结构化提示词超过 MAX_PROMPT_JSON")
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for index, image in enumerate(images, 1):
        pair = _image_content(ctx, image["path"], f"目标{scope}图 {index}/{len(images)}：{image['name']}")
        if pair:
            content.extend(pair)
    if source_kind == "generated":
        references = _reference_images(deck)
        for index, reference in enumerate(references[:MAX_REFERENCE_IMAGES], 1):
            pair = _image_content(ctx, reference, f"参考图 {index}（只用于匹配度，不是待评作品）")
            if pair:
                content.extend(pair)
    remaining_calibration_bytes = MAX_CALIBRATION_IMAGE_BYTES
    attached_calibration_images = 0
    for entry in _retained_calibration_entries(prompt_task.get("calibration")):
        if attached_calibration_images >= MAX_CALIBRATION_IMAGES or remaining_calibration_bytes <= 0:
            break
        ref_id = str(entry.get("refId") or "")
        path = calibration_images.get(ref_id)
        if not path:
            continue
        pair, size = _calibration_image_content(ctx, ref_id, path, remaining_calibration_bytes)
        if pair:
            content.extend(pair)
            remaining_calibration_bytes -= size
            attached_calibration_images += 1
    system_prompt = BASELINE_SYSTEM_PROMPT
    if str(job.get("variant") or "baseline") == "assisted":
        system_prompt += ASSISTED_REVIEW_METHODS
        system_prompt += ASSISTED_SCOPE_RULES.get(scope, "")
        system_prompt += (
            "\n返回一个 JSON 对象，字段为 scores、classification、reasons、summary。"
            "scores 按 requiredDimensions 给数值，classification 使用 {\"label\":\"分类\"}。"
            "reasons 是非空的中文短句数组，每句指出具体位置、元素或可见问题，以及它如何影响得分。"
            "summary 用一句中文概括。只使用实际可见细节，区分目标页和参考图。"
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def _reference_images(deck) -> list[str]:
    data = deck.get("data") or {}
    generation = data.get("generation") if isinstance(data, dict) else None
    refs = generation.get("references") if isinstance(generation, dict) else None
    result = []
    def add(path: Any) -> None:
        text = str(path or "").strip()
        if text and text not in result:
            result.append(text)
    for ref in refs or []:
        if isinstance(ref, str):
            add(ref)
        elif isinstance(ref, dict):
            image_paths = ref.get("imagePaths")
            if isinstance(image_paths, str):
                add(image_paths)
            elif isinstance(image_paths, list):
                for path in image_paths:
                    add(path)
            add(ref.get("path") or ref.get("imagePath") or ref.get("storagePath"))
    return result


def _normalise_result(value: Any, job, settings, workspace_settings) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("模型没有返回 JSON 对象")
    payload = value.get("result") if isinstance(value.get("result"), dict) else value
    scores = payload.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("模型结果缺少 scores")
    maximum = float((workspace_settings or {}).get("maxScore") or 5)
    source_kind = str(job.get("sourceKind") or job.get("source_kind") or "uploaded")
    required = ["visual", "layout", "match"] if source_kind == "generated" else [
        str(dim.get("id")) for dim in (workspace_settings or {}).get("dimensions", []) if isinstance(dim, dict) and dim.get("id")
    ]
    clean_scores: dict[str, float] = {}
    for dimension in required:
        number = scores.get(dimension)
        if not isinstance(number, (int, float)) or not math.isfinite(float(number)):
            raise ValueError(f"模型结果缺少有效分数：{dimension}")
        if float(number) < 0 or float(number) > maximum:
            raise ValueError(f"模型分数超出范围：{dimension}")
        clean_scores[dimension] = round(float(number), 4)
    weights = {"visual": float(settings.get("vision_weight") or 40), "layout": float(settings.get("layout_weight") or 30), "match": float(settings.get("match_weight") or 30)}
    if source_kind == "generated":
        weighted = sum(clean_scores.get(key, 0.0) * weight for key, weight in weights.items()) / 100
    else:
        weighted = sum(clean_scores.values()) / max(1, len(clean_scores))
    reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else []
    if job.get("variant") == "assisted":
        reasons = [reason.strip() for reason in reasons if isinstance(reason, str) and reason.strip()]
        if not reasons:
            raise ValueError("辅助预评缺少可核对的评分理由")
    classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {"label": str(payload.get("classification") or "")}
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)):
        confidence = None
    else:
        confidence = max(0.0, min(1.0, float(confidence)))
    return {
        "scores": clean_scores,
        "weightedScore": round(weighted, 4),
        "normalizedScore": round(weighted / maximum, 6),
        "weights": weights if source_kind == "generated" else None,
        "classification": classification,
        "reasons": [reason if isinstance(reason, (str, dict)) else str(reason) for reason in reasons[:20]],
        "summary": str(payload.get("summary") or "")[:2000],
        "confidence": confidence,
        "modelHint": "gpt-6.2-sol",
        "sourceKind": source_kind,
        "scope": job.get("scope"),
        "unitId": job.get("unitId") or job.get("unit_id") or "",
    }


def _backfill_integer(body: dict[str, Any], name: str, default: int) -> int:
    raw = body.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{name} 必须是整数")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError(f"{name} 必须是整数")
        value = int(text)
    else:
        raise ValueError(f"{name} 必须是整数")
    if not SUPABASE_INT4_MIN <= value <= SUPABASE_INT4_MAX:
        raise ValueError(f"{name} 超出数据库整数范围")
    return value


def _backfill_count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"AI 回填返回的 {name} 无效")
    return value


def _backfill_rpc(ctx, params: dict[str, Any]) -> Any:
    try:
        return _rpc(ctx, "ai_review_backfill", params, privileged=True)
    except KeyError as exc:
        raise ValueError("AI 回填数据库函数不可用，请先执行最新数据库迁移") from exc
    except ValueError as exc:
        message = str(exc).lower()
        if any(marker in message for marker in (
            "pgrst202", "function does not exist", "could not find the function", "schema cache",
        )):
            raise ValueError("AI 回填数据库函数不可用，请先执行最新数据库迁移") from exc
        raise


def _backfill(ctx) -> dict[str, Any]:
    _require_judge(ctx)
    body = _body(ctx)
    kind = str(body.get("kind") or "uploaded").strip()
    if kind not in KINDS:
        raise ValueError("队列类型无效")
    offset = max(0, _backfill_integer(body, "offset", 0))
    limit = max(1, min(BACKFILL_MAX_LIMIT, _backfill_integer(body, "limit", BACKFILL_DEFAULT_LIMIT)))
    raw = _backfill_rpc(ctx, {
        "w": _workspace(ctx),
        "actor": _user(ctx),
        "p_kind": kind,
        "p_offset": offset,
        "p_limit": limit,
    })
    data = _json_value(raw)
    if not isinstance(data, dict):
        raise ValueError("AI 回填返回格式无效")
    if data.get("ok") is False:
        raise ValueError(str(data.get("error") or "AI 回填失败"))
    next_offset = data.get("nextOffset")
    if next_offset is not None:
        next_offset = _backfill_count(next_offset, "nextOffset")
    return {
        "ok": True,
        "kind": kind,
        "offset": offset,
        "nextOffset": next_offset,
        "decks": _backfill_count(data.get("decks"), "decks"),
        "preparedUnits": _backfill_count(data.get("preparedUnits"), "preparedUnits"),
        "submittedUnits": 0,
        "totalDecks": _backfill_count(data.get("totalDecks"), "totalDecks"),
    }


def handle_import(ctx):
    _member(ctx)
    body = _body(ctx)
    name = str(body.get("name") or "").strip()
    files = body.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_IMPORT_FILES:
        raise ValueError("请选择 1–10 份 PPT/PPTX")
    decoded: list[tuple[str, bytes]] = []
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("文件内容无效")
        filename = Path(str(item.get("name") or "")).name
        suffix = Path(filename).suffix.lower()
        if suffix not in DOCUMENT_SUFFIXES:
            raise ValueError("此入口仅接收 PPT 或 PPTX；PDF/图片请使用普通导入")
        encoded = _decode_base64(item.get("data"))
        if not encoded:
            raise ValueError(f"文件内容为空：{filename}")
        if len(encoded) > MAX_IMPORT_FILE_BYTES:
            raise ValueError(f"单个文件不得超过 25 MB：{filename}")
        total += len(encoded)
        if total > MAX_IMPORT_TOTAL_BYTES:
            raise ValueError("单批文件总大小不得超过 100 MB")
        decoded.append((filename, encoded))
    workspace = _workspace(ctx)
    deck_id = "import-" + str(uuid4())
    title = name or Path(decoded[0][0]).stem
    if len(title) > 100:
        title = title[:100]
    deck = {"id": deck_id, "name": title, "title": title, "origin": "import", "images": []}
    with tempfile.TemporaryDirectory(prefix="guanye-ai-import-") as temporary:
        root = Path(temporary)
        pages: list[Path] = []
        for index, (filename, raw) in enumerate(decoded, 1):
            source = root / f"source-{index}{Path(filename).suffix.lower()}"
            output = root / f"converted-{index}"
            source.write_bytes(raw)
            _convert_document(source, output)
            generated = sorted(output.glob("page-*.png"), key=_natural_page_key)
            if not generated:
                raise ValueError(f"转换未生成页面：{filename}")
            pages.extend(generated)
        if not 1 <= len(pages) <= MAX_IMPORT_PAGES:
            raise ValueError("每套作品需包含 1–600 页，请拆分后导入")
        for index, page_file in enumerate(pages, 1):
            image_path = f"{workspace}/{deck_id}/{uuid4()}.png"
            ctx.storage_put(image_path, page_file.read_bytes(), "image/png")
            deck["images"].append({
                "id": str(uuid4()), "name": f"{index:04d}.png", "path": image_path, "thumbPath": image_path
            })
    if len(decoded) == 1:
        # The digest is measured from the decoded server-received bytes. Client
        # supplied hashes are ignored, and the service-role-only RPC writes the
        # provenance row before the deck in the same transaction.
        source_group_sha256 = hashlib.sha256(decoded[0][1]).hexdigest()
        try:
            _rpc(ctx, "ai_import_review_deck", {
                "w": workspace,
                "actor": _user(ctx),
                "deck": deck,
                "p_source_group_sha256": source_group_sha256,
            }, privileged=True)
        except (KeyError, ValueError) as exc:
            message = str(exc).lower()
            if any(marker in message for marker in (
                "pgrst202", "function does not exist", "could not find the function", "schema cache",
            )):
                raise ValueError("上传来源指纹数据库函数不可用，请先执行最新数据库迁移") from exc
            raise
    else:
        # Multi-file requests are still a merged deck. Do not infer a source
        # group from old data or claim that the merged output has one source.
        _rpc(ctx, "add_review_deck", {"w": workspace, "deck": deck}, privileged=False)
    return {"ok": True, "deckId": deck_id, "deck": deck, "sourceKind": "uploaded", "pageCount": len(deck["images"])}


def _decode_base64(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        return b""
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode("".join(text.split()), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("文件 data 不是有效 base64") from exc


def _natural_page_key(path: Path) -> tuple[Any, ...]:
    value = path.name
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value))


def _convert_document(source: Path, output: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from document_import import convert as convert_document
    convert_document(source, output)
