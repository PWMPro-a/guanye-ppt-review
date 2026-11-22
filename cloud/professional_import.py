"""Convert one authorized local document and publish it to the shared private library."""
from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID, uuid5

from cloud.ai_gateway import DirectTransport, GatewayError

MIB = 1024 * 1024
MAX_FILE = 25 * MIB
MAX_BODY = 4 * ((MAX_FILE + 2) // 3) + 8192
MAX_PREVIEWS = 600
MAX_PREVIEWS_BYTES = 300 * MIB
NAMESPACE = UUID("ad2b66fe-9f39-41e9-8c1f-a149321416e8")
ROLES = frozenset({"judge", "admin", "submitter"})
TYPES = {".ppt": "application/vnd.ms-powerpoint", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pdf": "application/pdf"}
HASH = re.compile(r"[0-9a-f]{64}\Z")
INVALID_TEXT = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class ImportErrorResponse(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _workspace_actor(value: Any) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ImportErrorResponse(502, "云端未返回有效账号") from None


def convert_preview(source: Path, output: Path) -> None:
    from document_import import convert
    convert(source, output)


class ProfessionalImporter:
    def __init__(self, config: dict, *, transport=None, converter=convert_preview):
        self.origin = config["url"]
        self.key = config["anonKey"]
        self.workspace = config["workspaceId"]
        self.transport = transport or DirectTransport(1024 * 1024)
        self.converter = converter

    def _request(self, method, endpoint, bearer, *, value=None, data=None, content_type=None):
        headers = {"apikey": self.key, "Authorization": "Bearer " + bearer, "Accept": "application/json"}
        if value is not None:
            data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if method == "PUT" and endpoint.startswith("/storage/v1/object/upload/sign/ppt-library/"):
            headers.update({"x-upsert": "false", "cache-control": "max-age=3600"})
        try:
            return self.transport(method, self.origin + endpoint, headers, data, 120.0)
        except (GatewayError, OSError):
            raise ImportErrorResponse(502, "连接私有素材库失败，请重试同一份文件") from None

    def _json_request(self, method, endpoint, bearer, *, value=None):
        result = self._request(method, endpoint, bearer, value=value)
        if not 200 <= result.status < 300:
            if result.status == 401:
                raise ImportErrorResponse(401, "登录已失效，请重新登录")
            if result.status == 403:
                raise ImportErrorResponse(403, "当前账号无权向此素材库投递")
            if result.status == 409:
                raise ImportErrorResponse(409, "素材文件或预览与上次投递不一致，请核对后重试")
            if result.status in (400, 413, 422) and endpoint.startswith("/functions/"):
                raise ImportErrorResponse(422, "云端拒绝了本次投递，请检查文件和预览后重试")
            raise ImportErrorResponse(502, "私有素材库暂不可用，请稍后重试")
        try:
            value = json.loads(result.body)
        except (UnicodeError, ValueError):
            raise ImportErrorResponse(502, "私有素材库返回格式无效") from None
        if not isinstance(value, dict):
            if value is None and endpoint == "/rest/v1/rpc/single_judge_access":
                raise ImportErrorResponse(403, "当前账号无权向此素材库投递")
            raise ImportErrorResponse(502, "私有素材库返回格式无效")
        return value

    def authorize(self, bearer: str) -> str:
        identity = self._json_request("GET", "/auth/v1/user", bearer)
        actor = _workspace_actor(identity.get("id"))
        access = self._json_request("POST", "/rest/v1/rpc/single_judge_access", bearer, value={"w": self.workspace})
        role = access.get("role")
        if access.get("mode") != "single-judge" or not isinstance(role, str) or role not in ROLES:
            raise ImportErrorResponse(403, "当前账号无权向此素材库投递")
        return actor

    @staticmethod
    def _inputs(value):
        if not isinstance(value, dict) or set(value) != {"title", "aiAllowed", "file"} or not isinstance(value["aiAllowed"], bool):
            raise ImportErrorResponse(400, "请明确选择是否允许 AI 处理，并上传一份原始文档")
        title, item = value["title"], value["file"]
        if not isinstance(item, dict) or set(item) != {"name", "data"}:
            raise ImportErrorResponse(400, "每次仅支持投递一份文档")
        name, data = item["name"], item["data"]
        if not isinstance(name, str) or not 1 <= len(name) <= 255 or INVALID_TEXT.search(name) or "/" in name or "\\" in name:
            raise ImportErrorResponse(400, "文件名无效")
        suffix = Path(name).suffix.lower()
        if suffix not in TYPES:
            raise ImportErrorResponse(400, "仅支持 PPT、PPTX 或 PDF")
        if not isinstance(title, str) or INVALID_TEXT.search(title) or len(title.strip()) > 100:
            raise ImportErrorResponse(400, "作品标题无效（最多 100 字）")
        if not isinstance(data, str) or not 1 <= len(data) <= 4 * ((MAX_FILE + 2) // 3):
            raise ImportErrorResponse(413, "每份文档最多 25 MiB")
        try:
            raw = b64decode(data, validate=True)
        except (Base64Error, ValueError):
            raise ImportErrorResponse(400, "文件编码无效") from None
        if not 1 <= len(raw) <= MAX_FILE:
            raise ImportErrorResponse(413, "每份文档最多 25 MiB")
        title = title.strip() or Path(name).stem.strip()[:100] or "未命名作品"
        return title, name, suffix, raw, value["aiAllowed"]

    @staticmethod
    def _page_files(output: Path):
        try:
            metadata_file = output / "result.json"
            if metadata_file.stat().st_size > 128 * 1024:
                raise ValueError
            pages = json.loads(metadata_file.read_text(encoding="utf-8"))["pages"]
            if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PREVIEWS:
                raise ValueError
            result = []
            for number, page in enumerate(pages, 1):
                name = f"page-{number:04d}.png"
                if not isinstance(page, dict) or page.get("file") != name or page.get("page") != number:
                    raise ValueError
                path = output / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError
                size = path.stat().st_size
                if not len(PNG_HEADER) < size <= MAX_FILE:
                    raise ValueError
                digest = sha256()
                total = 0
                with path.open("rb") as file:
                    header = file.read(len(PNG_HEADER))
                    if header != PNG_HEADER:
                        raise ValueError
                    digest.update(header)
                    total += len(header)
                    while chunk := file.read(MIB):
                        digest.update(chunk)
                        total += len(chunk)
                if total != size:
                    raise ValueError
                result.append((path, size, digest.hexdigest()))
            if sum(size for _, size, _ in result) > MAX_PREVIEWS_BYTES:
                raise ImportErrorResponse(413, "预览图合计超过 300 MiB，请拆分作品后再投递")
            return result
        except (OSError, KeyError, TypeError, ValueError):
            raise ImportErrorResponse(422, "本机没有生成齐全且按页排列的预览图") from None

    def import_one(self, actor: str, bearer: str, value) -> dict:
        title, name, suffix, raw, ai_allowed = self._inputs(value)
        digest = sha256(raw).hexdigest()
        identity = json.dumps([self.workspace, actor, title, name, digest, ai_allowed], ensure_ascii=True, separators=(",", ":"))
        deck_id = "import-" + str(uuid5(NAMESPACE, identity))
        prefix = self.workspace + "/" + deck_id
        with TemporaryDirectory(prefix="guanye-submission-") as temporary:
            root = Path(temporary)
            # User-controlled names are metadata only, never local path components.
            source = root / ("source" + suffix)
            source.write_bytes(raw)
            del raw
            output = root / "preview"
            try:
                self.converter(source, output)
            except (ValueError, RuntimeError, OSError, ImportError):
                raise ImportErrorResponse(422, "本机无法转换该文件，请检查文件，或另存为 PDF 后重试") from None
            previews = self._page_files(output)
            objects = {prefix + "/original/" + digest + suffix: (source, TYPES[suffix])}
            for position, (path, _, _) in enumerate(previews, 1):
                objects[f"{prefix}/preview/{position}.png"] = (path, "image/png")
            # pageHashes is mandatory in the current importer protocol. There is no
            # pageSizes-only compatibility mode because unhashed previews cannot be
            # safely bound to the uploaded bytes at publish time.
            manifest = {"workspaceId": self.workspace, "deckId": deck_id, "title": title, "aiAllowed": ai_allowed,
                        "original": {"name": name, "sha256": digest, "sizeBytes": source.stat().st_size},
                        "pageSizes": [length for _, length, _ in previews],
                        "pageHashes": [page_hash for _, _, page_hash in previews]}
            edge = "/functions/v1/single-judge-import"
            prepared = self._json_request("POST", edge, bearer, value={"action": "prepare", **manifest})
            uploads = prepared.get("uploads")
            if not isinstance(uploads, list) or len(uploads) > len(objects):
                raise ImportErrorResponse(502, "导入服务返回了无效的文件清单")
            validated = []
            seen = set()
            for item in uploads:
                if not isinstance(item, dict):
                    raise ImportErrorResponse(502, "导入服务返回了无效的文件清单")
                path, token = item.get("path"), item.get("token")
                if (not isinstance(path, str) or path not in objects or path in seen or not isinstance(token, str)
                        or not re.fullmatch(r"[A-Za-z0-9._-]{1,8192}", token)):
                    raise ImportErrorResponse(502, "导入服务返回了无效的文件清单")
                seen.add(path)
                validated.append((path, token))
            for path, token in validated:
                file, mime = objects[path]
                endpoint = "/storage/v1/object/upload/sign/ppt-library/" + quote(path, safe="/") + "?" + urlencode({"token": token})
                response = self._request("PUT", endpoint, bearer, data=file.read_bytes(), content_type=mime)
                if not 200 <= response.status < 300:
                    raise ImportErrorResponse(502, "文件同步中断，重新投递同一份文件可补齐未上传部分")
            published = self._json_request("POST", edge, bearer, value={"action": "publish", **manifest})
            version = published.get("deckVersion")
            if (published.get("ok") is not True or published.get("deckId") != deck_id or not isinstance(version, str)
                    or not HASH.fullmatch(version) or not isinstance(published.get("idempotent"), bool)):
                raise ImportErrorResponse(502, "导入服务没有确认发布结果，可重试同一份文件")
            return {"ok": True, "deckId": deck_id, "deckVersion": version, "idempotent": published["idempotent"],
                    "pageCount": len(previews)}
