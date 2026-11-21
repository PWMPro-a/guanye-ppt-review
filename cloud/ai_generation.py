"""Public AI generation gateway.

The gateway is deliberately transport-neutral: the hosting layer supplies a
router and a request context.  Cloud writes stay behind privileged RPCs; the
Windows worker only speaks HTTP to this gateway and to an already-running
Image2PPT service.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from typing import Any

MAX_PAGES = 600
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_BASE64_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4096
DEFAULT_WORKERS = 4
# The Windows Image2PPT Flask app enforces MAX_CONTENT_LENGTH=200 MiB.
EXE_MAX_REQUEST_BYTES = 200 * 1024 * 1024
def configure(router) -> None:
    """Register exact public routes.  The host router owns auth and JSON I/O."""
    router.add("POST", "/api/ai/generation", _create_generation)
    router.add("GET", "/api/ai/generation", _get_generation)
    router.add("POST", "/api/ai/generation/retry", _retry_generation)
    router.add("GET", "/api/ai/generation/settings", _get_settings)
    router.add("POST", "/api/ai/generation/settings", _set_settings)
    router.add("POST", "/api/ai/generation/worker/claim", _worker_claim)
    router.add("POST", "/api/ai/generation/worker/heartbeat", _worker_heartbeat)
    router.add("POST", "/api/ai/generation/worker/complete", _worker_complete)
    router.add("POST", "/api/ai/generation/worker/fail", _worker_fail)
    router.add("POST", "/api/ai/generation/worker/reference", _worker_reference)
    router.add("GET", "/api/ai/generation/artifact", _artifact)
    router.add("GET", "/api/ai/generation/health", _health)


_NOT_FOUND_HINTS = ("not found", "does not exist", "no such", "404", "不存在")


def _normalize_storage_bytes(raw: Any) -> bytes:
    if isinstance(raw, dict):
        if isinstance(raw.get("bytes"), (bytes, bytearray, memoryview)):
            raw = bytes(raw["bytes"])
        elif isinstance(raw.get("dataBase64"), str):
            raw = _decode_image(raw["dataBase64"])
        else:
            raw = None
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ValueError("图片存储内容无效")
    raw = bytes(raw)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片存储大小无效")
    return raw


def _read_storage_bytes(ctx, path: str) -> bytes:
    return _normalize_storage_bytes(ctx.storage_get(path))


def _try_read_storage(ctx, path: str) -> bytes | None:
    try:
        raw = ctx.storage_get(path)
        if raw is None:
            return None
        return _normalize_storage_bytes(raw)
    except PermissionError:
        raise
    except Exception as exc:
        text = _error_text(exc).lower()
        if any(hint in text for hint in _NOT_FOUND_HINTS):
            return None
        raise

def _store_image_idempotent(ctx, path: str, raw: bytes, content_type: str) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    existing = _try_read_storage(ctx, path)
    if existing is not None:
        if hashlib.sha256(existing).hexdigest() != digest:
            raise ValueError("存储路径已有不同图片，拒绝覆盖")
        return
    try:
        ctx.storage_put(path, raw, content_type)
    except Exception as exc:
        # A timeout after a successful object write leaves the object behind.
        # Re-read and compare before reporting failure so the same lease can
        # retry only the DB write-back.
        existing = _try_read_storage(ctx, path)
        if existing is None:
            raise
        if hashlib.sha256(existing).hexdigest() != digest:
            raise ValueError("存储路径已有不同图片，拒绝覆盖") from exc

def _artifact(ctx):
    query = _query(ctx)
    job_id = _uuid_text(query.get("job_id"), "job_id")
    page_id = _uuid_text(query.get("page_id"), "page_id")
    job = _rpc_job(ctx, "ai_generation_get", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_job": job_id,
    }, privileged=False)
    page = next((row for row in job.get("pages", []) if str(row.get("id")) == page_id), None)
    if not isinstance(page, dict) or page.get("status") != "succeeded":
        raise ValueError("页面图片尚未就绪")
    image = page.get("image") if isinstance(page.get("image"), dict) else {}
    path = image.get("path") or page.get("storagePath")
    if not path:
        raise ValueError("页面图片路径不存在")
    raw = ctx.storage_get(path)
    if isinstance(raw, dict):
        if isinstance(raw.get("bytes"), (bytes, bytearray, memoryview)):
            raw = bytes(raw["bytes"])
        elif isinstance(raw.get("dataBase64"), str):
            raw = _decode_image(raw["dataBase64"])
        else:
            raw = None
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise ValueError("页面图片读取失败")
    raw = bytes(raw)
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("页面图片大小无效")
    content_type = page.get("contentType") or _detect_image_type(raw)
    return {
        "jobId": job.get("id"),
        "pageId": page_id,
        "path": path,
        "contentType": content_type,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "dataBase64": base64.b64encode(raw).decode("ascii"),
    }

def _health(ctx) -> dict[str, Any]:
    return {"ok": True, "service": "ai-generation", "queueWorkers": DEFAULT_WORKERS}


def _create_generation(ctx):
    body = _body(ctx)
    plan_id = _uuid_text(body.get("plan_id"), "plan_id")
    plan = _load_plan(ctx, plan_id)
    normalized = _normalize_plan(ctx, plan_id, plan)
    workspace = _required(ctx, "workspace_id")
    user = _required(ctx, "user_id")
    deck_id = "generated-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"guanye-ai-generation:{workspace}:{user}:{plan_id}",
    ).hex[:24]
    params = {
        "p_workspace": workspace,
        "p_user": user,
        "p_plan_id": plan_id,
        "p_deck_id": deck_id,
        "p_plan": normalized["plan"],
        "p_title": normalized["title"],
        "p_style": normalized["style"],
        "p_outline": normalized["outline"],
        "p_references": normalized["references"],
        "p_pages": normalized["pages"],
        "p_config": {"workers": DEFAULT_WORKERS},
    }
    job = _rpc_job(ctx, "ai_generation_create", params, privileged=True)
    return 202, job


def _get_generation(ctx):
    query = _query(ctx)
    action = _optional_text(query.get("action"))
    if action == "recent":
        result = _rpc_value(ctx, "ai_generation_recent", {
            "p_workspace": _required(ctx, "workspace_id"),
            "p_actor": _required(ctx, "user_id"),
        }, privileged=False)
        if not isinstance(result, dict) or not isinstance(result.get("jobs"), list):
            raise ValueError("云端返回的最近生成任务无效")
        return result
    if action:
        raise ValueError("不支持的 action")
    job_id = _optional_text(query.get("job_id"))
    plan_id = _optional_text(query.get("plan_id"))
    if not job_id and not plan_id:
        raise ValueError("job_id 或 plan_id 至少提供一个")
    if job_id:
        job = _rpc_job(ctx, "ai_generation_get", {
            "p_workspace": _required(ctx, "workspace_id"),
            "p_actor": _required(ctx, "user_id"),
            "p_job": _uuid_text(job_id, "job_id"),
        }, privileged=False)
    else:
        job = _rpc_job(ctx, "ai_generation_get_by_plan", {
            "p_workspace": _required(ctx, "workspace_id"),
            "p_actor": _required(ctx, "user_id"),
            "p_plan_id": plan_id,
        }, privileged=False)
    return job


def _get_settings(ctx):
    result = _rpc_value(ctx, "ai_generation_get_settings", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
    }, privileged=False)
    if not isinstance(result, dict):
        raise ValueError("工作区生图配置无效")
    return result


def _set_settings(ctx):
    body = _body(ctx)
    max_workers = _int_between(body.get("max_workers", body.get("maxWorkers")), "max_workers", 1, 16)
    result = _rpc_value(ctx, "ai_generation_set_workers", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_max_workers": max_workers,
    }, privileged=False)
    if not isinstance(result, dict):
        raise ValueError("工作区生图配置无效")
    return result

def _retry_generation(ctx):
    body = _body(ctx)
    job_id = _uuid_text(body.get("job_id"), "job_id")
    page_ids = body.get("page_ids")
    if not isinstance(page_ids, list) or not page_ids:
        raise ValueError("page_ids 必须是非空数组")
    if len(page_ids) > MAX_PAGES:
        raise ValueError("单次重试页面过多")
    clean_ids = [_uuid_text(value, "page_id") for value in page_ids]
    if len(clean_ids) != len(set(clean_ids)):
        raise ValueError("page_ids 不能重复")
    job = _rpc_job(ctx, "ai_generation_retry_pages", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_job": job_id,
        "p_page_ids": clean_ids,
    }, privileged=False)
    return 202, job


def _worker_claim(ctx):
    body = _body(ctx)
    worker_id = _text(body.get("worker_id"), "worker_id", 200)
    limit = _int_between(body.get("limit", DEFAULT_WORKERS), "limit", 1, 32)
    lease_seconds = _int_between(body.get("lease_seconds", 1800), "lease_seconds", 60, 7200)
    pages = _rpc_value(ctx, "ai_generation_claim_pages", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_worker_id": worker_id,
        "p_limit": limit,
        "p_lease_seconds": lease_seconds,
    })
    if not isinstance(pages, list):
        raise ValueError("工作节点领取结果无效")
    return {"pages": pages}


def _worker_heartbeat(ctx):
    body = _body(ctx)
    lease_seconds = _int_between(body.get("lease_seconds", 1800), "lease_seconds", 60, 7200)
    result = _rpc_value(ctx, "ai_generation_heartbeat", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_page": _uuid_text(body.get("page_id"), "page_id"),
        "p_lease_token": _uuid_text(body.get("lease_token"), "lease_token"),
        "p_lease_seconds": lease_seconds,
    })
    return result if isinstance(result, dict) else {"ok": True}


def _worker_complete(ctx):
    body = _body(ctx)
    page_id = _uuid_text(body.get("page_id"), "page_id")
    lease_token = _uuid_text(body.get("lease_token"), "lease_token")
    raw = _decode_image(body.get("image_base64"))
    content_type = _detect_image_type(raw)
    claimed_type = _optional_text(body.get("content_type"))
    if claimed_type and claimed_type != content_type:
        raise ValueError("image_base64 内容与 content_type 不一致")
    digest = hashlib.sha256(raw).hexdigest()
    supplied_digest = _optional_text(body.get("content_sha256"))
    if supplied_digest and supplied_digest.lower() != digest:
        raise ValueError("图片哈希不匹配")

    target = _rpc_value(ctx, "ai_generation_page_upload_target", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_page": page_id,
        "p_lease_token": lease_token,
    })
    if not isinstance(target, dict) or not target.get("storagePath"):
        raise ValueError("图片存储目标无效")
    expected_type = target.get("contentType") or "image/png"
    if content_type != expected_type:
        raise ValueError("图片格式与任务配置不一致")
    storage_path = _safe_storage_path(target["storagePath"], ctx)
    _store_image_idempotent(ctx, storage_path, raw, content_type)
    width = _optional_positive_int(body.get("width"), "width")
    height = _optional_positive_int(body.get("height"), "height")
    job = _rpc_job(ctx, "ai_generation_page_succeed", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_page": page_id,
        "p_lease_token": lease_token,
        "p_storage_path": storage_path,
        "p_content_type": content_type,
        "p_content_sha256": digest,
        "p_width": width,
        "p_height": height,
    })
    return job


def _worker_fail(ctx):
    body = _body(ctx)
    error = _optional_text(body.get("error")) or "页面生成失败"
    if len(error) > 2000:
        error = error[:2000]
    job = _rpc_job(ctx, "ai_generation_page_fail", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_page": _uuid_text(body.get("page_id"), "page_id"),
        "p_lease_token": _uuid_text(body.get("lease_token"), "lease_token"),
        "p_error": error,
    })
    return job


def _worker_reference(ctx):
    body = _body(ctx)
    page_id = _uuid_text(body.get("page_id"), "page_id")
    lease_token = _uuid_text(body.get("lease_token"), "lease_token")
    path = _rpc_value(ctx, "ai_generation_page_reference_path", {
        "p_workspace": _required(ctx, "workspace_id"),
        "p_actor": _required(ctx, "user_id"),
        "p_page": page_id,
        "p_lease_token": lease_token,
    }, privileged=True)
    if not isinstance(path, str) or not path:
        raise ValueError("该页没有可用的确认参考图")
    raw = _read_storage_bytes(ctx, path)
    content_type = _detect_image_type(raw)
    return {
        "image_base64": base64.b64encode(raw).decode("ascii"),
        "content_type": content_type,
    }

def _load_plan(ctx, plan_id: str) -> dict[str, Any]:
    """Authorize with the user token, then read the immutable service-only copy."""
    workspace = _required(ctx, "workspace_id")
    try:
        authorized = _unwrap(ctx.rpc(
            "ai_planning_get_plan",
            {"w": workspace, "plan_id": plan_id},
            privileged=False,
        ))
    except PermissionError:
        raise
    except Exception as exc:
        raise ValueError(f"读取已确认计划失败：{_error_text(exc)}") from exc
    if isinstance(authorized, list) and len(authorized) == 1:
        authorized = authorized[0]
    if not isinstance(authorized, dict):
        raise ValueError("策划读取返回格式无效")
    if authorized.get("status") != "confirmed":
        raise ValueError("只能生成已确认的策划")
    authorized_workspace = authorized.get("workspaceId") or authorized.get("workspace_id")
    if authorized_workspace is not None and str(authorized_workspace) != str(workspace):
        raise PermissionError("生成计划不属于当前工作区")
    authorized_plan_id = authorized.get("planId") or authorized.get("plan_id")
    if authorized_plan_id is not None and str(authorized_plan_id) != plan_id:
        raise ValueError("计划编号不匹配")
    try:
        plan = _unwrap(ctx.rpc(
            "ai_planning_read_confirmed_plan",
            {"p_plan_id": plan_id},
            privileged=True,
        ))
    except PermissionError:
        raise
    except Exception as exc:
        raise ValueError(f"读取不可变策划版本失败：{_error_text(exc)}") from exc
    if isinstance(plan, list) and len(plan) == 1:
        plan = plan[0]
    if not isinstance(plan, dict) or plan.get("status") != "confirmed":
        raise ValueError("不可变策划版本无效")
    if str(plan.get("planId") or "") != plan_id:
        raise ValueError("不可变策划编号不匹配")
    if str(plan.get("workspaceId") or "") != str(workspace):
        raise PermissionError("不可变策划不属于当前工作区")
    authorized_confirmed = authorized.get("confirmed") if isinstance(authorized.get("confirmed"), dict) else {}
    expected_hash = authorized_confirmed.get("contentHash")
    actual_hash = plan.get("contentHash")
    if expected_hash and actual_hash and str(expected_hash) != str(actual_hash):
        raise ValueError("策划版本内容哈希不一致")
    return plan

def _normalize_plan(ctx, plan_id: str, value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("生成计划格式无效")
    workspace = _required(ctx, "workspace_id")
    plan_workspace = _first(value, "workspaceId", "workspace_id", "w")
    if plan_workspace is not None and str(plan_workspace) != str(workspace):
        raise PermissionError("生成计划不属于当前工作区")
    actual_plan_id = _first(value, "planId", "plan_id", "id")
    if actual_plan_id is not None and str(actual_plan_id) != plan_id:
        raise ValueError("计划编号不匹配")

    outline_raw, style, references, raw_pages, generation_brief = _split_plan(value)
    style_text = _style_text(style)
    refs = _array(references, "references")
    title = _clean_text(_first(value, "title", "name"), 200)
    if not title and isinstance(style, dict):
        title = _clean_text(_first(style, "title", "name"), 200)
    title = title or "AI 生成作品"
    structured_plan = _is_schema_v1(value)
    pages = _normalize_pages(
        raw_pages, style_text, generation_brief, structured_plan=structured_plan,
    )
    if not pages:
        raise ValueError("生成计划没有页面")
    if len(pages) > MAX_PAGES:
        raise ValueError("生成计划页面过多")

    if structured_plan:
        # schemaVersion=1 canonical pages also define the job outline. Any
        # top-level legacy outline/outline_content is ignored entirely.
        outline = {
            "title": title,
            "brief": generation_brief,
            "pages": [page["outline"] for page in pages],
        }
        if value.get("sourceManifest") is not None:
            outline["sourceManifest"] = value.get("sourceManifest")
    else:
        outline = _outline_object(outline_raw)
        if not outline:
            outline = {"title": title, "brief": generation_brief, "pages": [page["outline"] for page in pages]}
        else:
            outline.setdefault("title", title)
            if generation_brief:
                outline.setdefault("brief", generation_brief)
            outline.setdefault("pages", [page["outline"] for page in pages])
            if value.get("sourceManifest") is not None:
                outline.setdefault("sourceManifest", value.get("sourceManifest"))
    return {
        "plan": value,
        "title": title,
        "style": style_text or "AI 生成",
        "outline": outline,
        "references": refs,
        "pages": pages,
    }


def _split_plan(value: dict[str, Any]):
    outline = _first(value, "outline", "outline_json", "outlineJson", "outline_content")
    style = _first(value, "style", "style_spec", "styleSpec", "template_style")
    references = _first(value, "references", "reference_images", "referenceImages", "refs")
    pages = _first(value, "pages", "page_specs", "pageSpecs")
    generation_brief = _clean_text(_first(value, "generationBrief", "generation_brief"), 8000)
    return outline, style, references, pages, generation_brief


def _is_schema_v1(value: dict[str, Any]) -> bool:
    version = value.get("schemaVersion", value.get("schema_version"))
    return not isinstance(version, bool) and (version == 1 or version == "1")


def _normalize_pages(
    raw_pages: Any,
    style_text: str,
    generation_brief: str,
    *,
    structured_plan: bool = False,
) -> list[dict[str, Any]]:
    if isinstance(raw_pages, dict):
        raw_pages = raw_pages.get("pages") or raw_pages.get("items") or []
    if not isinstance(raw_pages, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_pages, 1):
        if isinstance(raw, str):
            raw = {"title": raw, "copy": [{"kind": "body", "text": raw}]}
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 页格式无效")
        title = _clean_text(_first(raw, "title", "page_title", "pageTitle", "headline"), 200)
        title = title or f"第 {index} 页"
        narrative_role = _clean_text(_first(raw, "narrativeRole", "narrative_role"), 40)
        copy = raw.get("copy") if isinstance(raw.get("copy"), list) else []
        copy_clean = []
        for item in copy:
            if isinstance(item, str):
                item = {"kind": "body", "text": item}
            if isinstance(item, dict):
                text_value = _full_text(item.get("text", item.get("value")))
                if text_value:
                    copy_clean.append({
                        "kind": _clean_text(item.get("kind"), 40) or "body",
                        "text": text_value,
                    })
        layout = _clean_text(_first(raw, "layout", "layout_instruction", "layoutInstruction"), 3000)
        visual = _clean_text(_first(raw, "visualBrief", "visual_brief", "visualDirection", "visual_direction"), 5000)
        sources = _array(_first(raw, "contentSources", "content_sources"), "contentSources")
        source_texts = [_clean_text(item, 500) for item in sources if _clean_text(item, 500)]
        refs = _array(_first(raw, "styleReferenceDeckIds", "style_reference_deck_ids", "references", "refs"), "page references")
        if structured_plan:
            # schemaVersion=1 has one source of truth: confirmed canonical page
            # fields. Ignore legacy outline/outline_content overrides entirely.
            outline = {
                "index": index,
                "title": title,
                "copy": copy_clean,
                "layout": layout,
                "visualBrief": visual,
                "contentSources": source_texts,
                "styleReferenceDeckIds": refs,
            }
            if narrative_role:
                outline["narrativeRole"] = narrative_role
        else:
            outline_raw = _first(raw, "outline", "outline_content", "outlineContent")
            outline = _outline_object(outline_raw)
            if not outline:
                outline = {
                    "index": index,
                    "title": title,
                    "copy": copy_clean,
                    "layout": layout,
                    "visualBrief": visual,
                    "contentSources": source_texts,
                    "styleReferenceDeckIds": refs,
                }
            else:
                outline.setdefault("index", index)
                outline.setdefault("title", title)
                if copy_clean:
                    outline.setdefault("copy", copy_clean)
                if layout:
                    outline.setdefault("layout", layout)
                if visual:
                    outline.setdefault("visualBrief", visual)
                if refs:
                    outline.setdefault("styleReferenceDeckIds", refs)
            if narrative_role:
                outline.setdefault("narrativeRole", narrative_role)
        structured_text = _structured_page_text(
            title,
            copy_clean,
            narrative_role,
            layout,
            visual,
            style_text,
            generation_brief,
            allow_title_fallback=not structured_plan,
        )
        if structured_plan:
            # schemaVersion=1 pages are the canonical planning contract. Legacy
            # description/prompt keys must not override confirmed copy, layout,
            # visualBrief, narrativeRole, style or generationBrief.
            description = {"text": structured_text}
            prompt = structured_text
        else:
            description_raw = _first(raw, "description", "description_content", "descriptionContent")
            prompt = _full_text(_first(raw, "prompt", "image_prompt", "imagePrompt"))
            description_text = _description_text(description_raw)
            if isinstance(description_raw, dict):
                description = dict(description_raw)
                if not description_text:
                    description_text = prompt or structured_text
                description["text"] = description_text
            else:
                legacy_description = _full_text(description_raw)
                if legacy_description:
                    description = {"text": legacy_description}
                elif prompt:
                    # The worker sends description_content to the EXE. A legacy
                    # prompt-only page must therefore carry that prompt as text.
                    description = {"text": prompt}
                else:
                    description = {"text": structured_text}
            if not prompt:
                prompt = _description_text(description) or structured_text
        if narrative_role:
            description["narrativeRole"] = narrative_role
        page_id = _clean_text(_first(raw, "planPageId", "page_id", "pageId", "id"), 200) or f"page-{index:04d}"
        content_type = _clean_text(_first(raw, "contentType", "content_type"), 40) or "image/png"
        if content_type not in ("image/png", "image/jpeg", "image/webp"):
            raise ValueError(f"第 {index} 页图片格式无效")
        if _exe_page_payload_bytes(index, outline, description) > EXE_MAX_REQUEST_BYTES:
            raise ValueError(
                f"第 {index} 页内容超过 Windows Image2PPT EXE 单次请求上限"
                f"（{EXE_MAX_REQUEST_BYTES} 字节），请拆分页面后再生成"
            )
        normalized.append({
            "planPageId": page_id,
            "position": index,
            "title": title,
            "outline": outline,
            "description": description,
            "prompt": prompt,
            "references": refs,
            "contentType": content_type,
        })
    return normalized


def _full_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).replace("\x00", "").strip()


def _description_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("text", "description", "summary", "content"):
        text_value = _full_text(value.get(key))
        if text_value:
            return text_value
    for key in ("text_content", "textContent"):
        items = value.get(key)
        if isinstance(items, list):
            text_value = "\n".join(part for part in (_full_text(item) for item in items) if part)
            if text_value:
                return text_value
        else:
            text_value = _full_text(items)
            if text_value:
                return text_value
    return ""


def _structured_page_text(
    title: str,
    copy_clean: list[dict[str, str]],
    narrative_role: str,
    layout: str,
    visual: str,
    style_text: str,
    generation_brief: str,
    *,
    allow_title_fallback: bool,
) -> str:
    visible = [item["text"] for item in copy_clean]
    if not visible and allow_title_fallback:
        visible.append(title)
    lines = [
        "画面文字只允许来自“可见逐字文案”；“只供生图的页面说明”及其标签一律不得出现在画面中。",
        "",
        "可见逐字文案（必须原样出现在画面中，不得增删改）：",
        *[item for item in visible if item],
        "",
        "只供生图的页面说明（仅用于版式、视觉与风格判断；不得把本段文字或任何技术标签画进画面）：",
    ]
    if narrative_role:
        lines.append("叙事角色：" + narrative_role)
    if layout:
        lines.append("版式：" + layout)
    if visual:
        lines.append("视觉与图表：" + visual)
    if style_text:
        lines.append("统一风格：" + style_text)
    if generation_brief:
        lines.append("生成约束：" + generation_brief)
    return "\n".join(lines)


def _exe_page_payload_bytes(position: int, outline: dict[str, Any], description: dict[str, Any]) -> int:
    body = {
        "order_index": max(0, position - 1),
        "outline_content": outline,
        "description_content": description,
    }
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

def _outline_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"pages": value}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"value": value}


def _style_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:12000]
    if isinstance(value, dict):
        parts: list[str] = []
        name = _clean_text(value.get("name"), 200)
        summary = _clean_text(value.get("summary", value.get("description")), 3000)
        layout = _clean_text(value.get("layoutLanguage", value.get("layout_language")), 3000)
        decoration = _clean_text(value.get("decoration"), 1500)
        palette = value.get("palette")
        typography = value.get("typography") if isinstance(value.get("typography"), dict) else {}
        avoid = value.get("avoid")
        if name:
            parts.append("风格：" + name)
        if summary:
            parts.append(summary)
        if isinstance(palette, list) and palette:
            parts.append("配色：" + "、".join(str(item) for item in palette[:8]))
        typography_text = " / ".join(
            f"{key} {_clean_text(typography.get(key), 300)}"
            for key in ("heading", "body", "numbers")
            if _clean_text(typography.get(key), 300)
        )
        if typography_text:
            parts.append("字体层级：" + typography_text)
        if layout:
            parts.append("版式语言：" + layout)
        if decoration:
            parts.append("装饰语言：" + decoration)
        if isinstance(avoid, list) and avoid:
            parts.append("避免：" + "；".join(_clean_text(item, 300) for item in avoid[:12] if _clean_text(item, 300)))
        if parts:
            return "\n".join(parts)[:12000]
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:12000]
    return str(value)[:12000]


def _outline_text(value: dict[str, Any]) -> str:
    for key in ("text", "description", "summary", "content"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    points = value.get("points")
    if isinstance(points, list):
        return "\n".join(str(point).strip() for point in points if str(point).strip())
    return ""

def _rpc_job(ctx, name: str, params: dict[str, Any], privileged: bool = True) -> dict[str, Any]:
    value = _rpc_value(ctx, name, params, privileged=privileged)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError("云端返回的生成任务无效") from exc
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict) and isinstance(value.get("job"), dict):
        value = value["job"]
    if not isinstance(value, dict) or not value.get("id"):
        raise ValueError("云端返回的生成任务无效")
    return _decorate_job(ctx, value)


def _decorate_job(ctx, job: dict[str, Any]) -> dict[str, Any]:
    """Attach signed URLs when the host exposes storage_sign, plus a JSON artifact fallback."""
    result = dict(job)
    pages = []
    signer = getattr(ctx, "storage_sign", None)
    for raw_page in job.get("pages", []) if isinstance(job.get("pages"), list) else []:
        if not isinstance(raw_page, dict):
            continue
        page = dict(raw_page)
        image = dict(page.get("image")) if isinstance(page.get("image"), dict) else None
        path = (image or {}).get("path") or page.get("storagePath")
        if path and page.get("status") == "succeeded":
            fallback = "/api/ai/generation/artifact?job_id=" + str(job.get("id")) + "&page_id=" + str(page.get("id"))
            page["artifactPath"] = fallback
            page["previewPath"] = fallback
            page["downloadPath"] = fallback + "&download=1"
            if callable(signer):
                signed = None
                try:
                    signed = signer(path, 3600)
                except TypeError:
                    try:
                        signed = signer(path)
                    except Exception:
                        signed = None
                except Exception:
                    signed = None
                if isinstance(signed, dict):
                    signed = signed.get("signedUrl") or signed.get("signedURL") or signed.get("url")
                if isinstance(signed, str) and signed:
                    page["previewUrl"] = signed
                    page["downloadUrl"] = signed
            if image is not None:
                image["previewPath"] = page["previewPath"]
                image["downloadPath"] = page["downloadPath"]
                if page.get("previewUrl"):
                    image["previewUrl"] = page["previewUrl"]
                    image["downloadUrl"] = page["downloadUrl"]
                page["image"] = image
        pages.append(page)
    result["pages"] = pages
    return result

def _rpc_value(ctx, name: str, params: dict[str, Any], privileged: bool = True) -> Any:
    return _unwrap(ctx.rpc(name, params, privileged=privileged))


def _unwrap(value: Any) -> Any:
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
        value = value[1]
    if isinstance(value, dict) and set(value.keys()) == {"data"}:
        return value["data"]
    return value


def _body(ctx) -> dict[str, Any]:
    value = getattr(ctx, "body", None)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError("请求体不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return value


def _query(ctx) -> dict[str, str]:
    value = getattr(ctx, "query", None)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("查询参数无效")
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _required(ctx, name: str) -> str:
    value = getattr(ctx, name, None)
    text = _optional_text(value)
    if not text:
        raise PermissionError("登录状态或工作区信息缺失")
    return text


def _text(value: Any, name: str, limit: int) -> str:
    text = _optional_text(value)
    if not text:
        raise ValueError(f"{name} 不能为空")
    if len(text) > limit:
        raise ValueError(f"{name} 过长")
    return text


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, uuid.UUID)):
        raise ValueError("文本字段格式无效")
    return str(value).strip()


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()[:limit]


def _uuid_text(value: Any, name: str) -> str:
    text = _optional_text(value)
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{name} 必须是 UUID") from exc


def _int_between(value: Any, name: str, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if number < lower or number > upper:
        raise ValueError(f"{name} 必须在 {lower} 到 {upper} 之间")
    return number


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    number = _int_between(value, name, 1, 100000)
    return number


def _array(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return value
    raise ValueError(f"{name} 必须是数组")


def _first(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return None


def _decode_image(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("image_base64 不能为空")
    text = value.strip()
    if text.startswith("data:"):
        try:
            text = text.split(",", 1)[1]
        except IndexError as exc:
            raise ValueError("image_base64 数据 URL 无效") from exc
    if len(text) > MAX_BASE64_CHARS:
        raise ValueError("图片超过 25MB")
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("image_base64 不是有效 Base64") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("图片超过 25MB")
    return raw


def _detect_image_type(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("只接受 PNG、JPEG 或 WebP 图片")


def _safe_storage_path(path: str, ctx) -> str:
    text = _optional_text(path)
    if not text or len(text) > 500 or text.startswith("/") or "\\" in text:
        raise ValueError("图片存储路径无效")
    parts = text.split("/")
    workspace = _required(ctx, "workspace_id")
    if len(parts) < 3 or parts[0] != workspace or any(part in ("", ".", "..") for part in parts):
        raise ValueError("图片存储路径不在当前工作区")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", text):
        raise ValueError("图片存储路径包含非法字符")
    return text


def _error_text(exc: Exception | None) -> str:
    if exc is None:
        return "未知错误"
    return str(exc).strip() or exc.__class__.__name__
