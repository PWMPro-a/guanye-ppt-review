"""User-authorized adapter for professional planning and image jobs."""
from __future__ import annotations

import json
from contextlib import contextmanager
from threading import BoundedSemaphore
from urllib.parse import quote
from uuid import UUID

from cloud import ai_generation, ai_planning
from cloud.ai_gateway import DirectTransport, Gateway, GatewayConfig, GatewayError, Router
from cloud.professional_import import ImportErrorResponse

ANALYZE = "/api/ai/planning/analyze"
ROUTES = frozenset({
    ("POST", ANALYZE), ("POST", "/api/ai/planning/save"),
    ("POST", "/api/ai/planning/confirm"), ("GET", "/api/ai/planning"),
    ("GET", "/api/ai/planning/history"), ("POST", "/api/ai/generation"),
    ("GET", "/api/ai/generation"), ("POST", "/api/ai/generation/retry"),
    ("GET", "/api/ai/generation/artifact"),
})
MAX_ANALYZE_BODY = 46 * 1024 * 1024
MAX_EDIT_BODY = 8 * 1024 * 1024

# Only the adapter can choose a cloud procedure. Legacy trusted-service arguments
# are translated to narrow authenticated procedures, never to service-role calls.
RPC = {
    "ai_planning_reference_candidates": ("single_judge_planning_references", {"p_limit": "max_items"}),
    "ai_planning_list_my_plans": ("single_judge_planning_history", {"p_after": "p_after", "p_limit": "p_limit"}),
    "ai_planning_get_plan": ("single_judge_planning_get", {"p_plan_id": "plan_id"}),
    "ai_planning_read_confirmed_plan": ("single_judge_planning_confirmed", {"p_plan_id": "p_plan_id"}),
    "ai_planning_save_draft": ("single_judge_planning_save", {"p_plan_id": "plan_id", "p_revision": "expected_revision", "p_draft": "draft"}),
    "ai_planning_confirm": ("single_judge_planning_confirm", {"p_plan_id": "plan_id", "p_revision": "expected_revision", "p_draft": "draft"}),
    "ai_generation_create": ("single_judge_generation_create", {name: name for name in (
        "p_plan_id", "p_deck_id", "p_plan", "p_title", "p_style", "p_outline", "p_references", "p_pages",
    )}),
}
GENERATION_READS = {
    "ai_generation_recent": (), "ai_generation_get": ("p_job",),
    "ai_generation_get_by_plan": ("p_plan_id",),
    "ai_generation_retry_pages": ("p_job", "p_page_ids"),
}


class ProfessionalCreation:
    def __init__(self, config, model_key, *, transport=None, chat=None, require_cutover=False):
        self.config = config
        self.workspace = config["workspaceId"]
        self.require_cutover = require_cutover
        self.router = Router()
        ai_planning.configure(self.router)
        ai_generation.configure(self.router)
        self.transport = transport or DirectTransport(8 * 1024 * 1024, ai_generation.MAX_IMAGE_BYTES)
        self.analyze_slot = BoundedSemaphore(1)
        self.artifact_slots = BoundedSemaphore(2)
        self.chat = chat or Gateway(GatewayConfig(
            supabase_url=config["url"], supabase_anon_key=config["anonKey"],
            supabase_service_role_key="", workspace_id=self.workspace,
            model_api_key=model_key, model_max_concurrency=1,
        )).chat_json

    @contextmanager
    def slot(self, path):
        lock = (self.analyze_slot if path == ANALYZE else
                self.artifact_slots if path.endswith('/artifact') else None)
        if lock is not None and not lock.acquire(blocking=False):
            raise ImportErrorResponse(429, "创作任务繁忙，请稍后重试")
        try:
            yield
        finally:
            if lock is not None:
                lock.release()

    @classmethod
    def from_local_secrets(cls, config):
        # Lazy import: the scheduled worker also imports the browser config loader.
        from cloud.professional_worker_secrets import load_secrets
        try:
            key = load_secrets(config).model_key
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return cls(config, key) if key else None

    def _request(self, bearer, method, endpoint, *, value=None, data=None, content_type=None):
        headers = {"apikey": self.config["anonKey"], "Authorization": "Bearer " + bearer}
        if value is not None:
            data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if method == "POST" and endpoint.startswith("/storage/"):
            headers["x-upsert"] = "false"
        try:
            response = self.transport(method, self.config["url"] + endpoint, headers, data, 30.0)
        except (GatewayError, OSError):
            raise ImportErrorResponse(502, "暂时无法连接观页素材库") from None
        if response.status == 401:
            raise ImportErrorResponse(401, "登录已失效，请重新登录")
        if response.status == 403:
            raise PermissionError("没有访问当前作品或策划的权限")
        if response.status == 409:
            raise ImportErrorResponse(409, "记录已变化，请刷新后再试")
        if response.status == 404:
            raise ImportErrorResponse(404, "记录或图片不存在")
        if response.status == 400:
            raise ValueError("云端未接受该请求，请核对输入和当前版本")
        if not 200 <= response.status < 300:
            raise ImportErrorResponse(502, "观页素材库暂时不可用")
        return response.body

    def _json(self, *args, **kwargs):
        try:
            return json.loads(self._request(*args, **kwargs).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ImportErrorResponse(502, "观页素材库返回格式无效") from None

    def authorize(self, bearer):
        identity = self._json(bearer, "GET", "/auth/v1/user")
        try:
            actor = str(UUID(identity["id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ImportErrorResponse(502, "观页账号响应无效") from None
        access = self._json(bearer, "POST", "/rest/v1/rpc/single_judge_access", value={"w": self.workspace})
        if not isinstance(access, dict) or access.get("mode") != "single-judge" or access.get("role") not in ("judge", "admin"):
            raise PermissionError("只有当前专业版评分师可以策划和生图")
        if self.require_cutover and access.get("professionalWeb") is not True:
            raise PermissionError("当前工作区尚未启用正式网页")
        return actor

    def handle(self, method, path, actor, token, body, query):
        if (method, path) not in ROUTES:
            raise ImportErrorResponse(404, "未知创作接口")
        if path == "/api/ai/generation/retry" and (
            not isinstance(body.get("page_ids"), list) or len(body["page_ids"]) != 1
        ):
            raise ValueError("每次只能重试一页")
        result = self.router.resolve(method, path)(CreationContext(self, actor, token, body, query))
        return result if isinstance(result, tuple) else (200, result)


class CreationContext:
    def __init__(self, service, actor, bearer, body, query):
        self.service = service
        self.user_id = actor
        self.workspace_id = service.workspace
        self.role = "judge"
        self._bearer = bearer
        self.body = body
        self.query = query

    def rpc(self, name, params, privileged=False):
        if name in RPC:
            remote, keys = RPC[name]
            value = {"w": self.workspace_id, **{dest: params[src] for dest, src in keys.items()}}
        elif name in GENERATION_READS:
            remote = name
            value = {"p_workspace": self.workspace_id, "p_actor": self.user_id,
                     **{key: params[key] for key in GENERATION_READS[name]}}
        else:
            raise PermissionError("创作接口不允许访问该云端操作")
        return self.service._json(self._bearer, "POST", "/rest/v1/rpc/" + remote, value=value)

    def _storage_endpoint(self, path):
        if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path:
            raise ValueError("图片存储路径无效")
        parts = path.split("/")
        if (parts[0] != self.workspace_id or any(p in {"", ".", ".."} for p in parts)
                or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in path)):
            raise ValueError("图片存储路径不属于当前工作区")
        return "/storage/v1/object/ppt-library/" + "/".join(quote(p, safe="") for p in parts)

    def storage_get(self, path):
        return self.service._request(self._bearer, "GET", self._storage_endpoint(path))

    def storage_get_bounded(self, path, max_bytes):
        raw = self.storage_get(path)
        return raw if len(raw) <= max_bytes else None

    def storage_put(self, path, data, content_type):
        parts = path.split("/")
        if (len(parts) != 5 or parts[1] != "ai-planning"
                or parts[3] not in {"content", "style", "reference", "style-rendered"}
                or not 1 <= len(data) <= ai_planning.MAX_FILE_BYTES):
            raise ValueError("策划上传目标无效")
        return self.service._request(self._bearer, "POST", self._storage_endpoint(path),
                                     data=data, content_type=content_type)

    def chat_json(self, messages):
        return self.service.chat(messages)
