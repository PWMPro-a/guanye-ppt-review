"""End-to-end coverage for the Windows image-generation deployment path.

Real production surfaces in this test:
- ``WindowsWorker.run`` with its shared ThreadPoolExecutor cap;
- ``GatewayClient`` over the real loopback ``GatewayHTTPServer`` and router;
- planning/save/confirm/create/retry/artifact HTTP routes;
- ``review_worker_cycle`` plus review prepare/submit/confirm/admit/library HTTP routes;
- generation, planning-version and AI-review SQL RPCs in PGlite;
- all migrations in the ordered ``cloud.manage._migration_catalog()``;
- ``ExeClient`` against a deterministic HTTP service shaped like Image2PPT.

Deliberate boundaries (no network credentials, no real model spend):
- Supabase Auth/member lookup and REST/storage are an in-process fixture
  transport backed by PGlite and an in-memory byte store;
- planning and review model calls return deterministic local results;
- the Image2PPT executable process is replaced by a loopback HTTP service.
The test never reads ``.test-output/*/local.env`` or any credential store.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from cloud import ai_gateway, manage
from cloud.windows_image_worker import GatewayClient, WindowsWorker, WorkerConfig

ROOT = Path(__file__).resolve().parent
NODE = shutil.which("node")
PGLITE = ROOT / ".test-output" / "sql" / "node_modules" / "@electric-sql" / "pglite"

WORKSPACE = "11111111-1111-4111-8111-111111111111"
USER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MEMBER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
USER_TOKEN = "fixture-user-token"
MEMBER_TOKEN = "fixture-member-token"
SERVICE_KEY = "fixture-service-role-key"
WORKER_TOKEN = "fixture-worker-token"
EXE_TOKEN = "fixture-exe-token"
REVIEW_LABEL = "AI 自我学习生成"

CONTENT_TITLE = "两页内容验证"
TEMPLATE_TITLE = "一页模板验证"
CONTENT_PAGE_TITLES = ("内容封面", "内容结论")
TEMPLATE_PAGE_TITLE = "模板封面"
TEMPLATE_FILE_NAME = "fixture-template.pdf"

PGLITE_BRIDGE = r"""
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const root = process.env.GUANYE_ROOT;
const pglitePath = process.env.GUANYE_PGLITE;
const {PGlite} = require(pglitePath);
const {pgcrypto} = require(path.join(pglitePath, 'dist', 'contrib', 'pgcrypto.cjs'));

function ident(value) {
  if (!/^[a-z_][a-z0-9_]*$/.test(value)) throw new Error('invalid SQL identifier');
  return '"' + value + '"';
}

async function bootstrap() {
  const db = new PGlite({extensions: {pgcrypto}});
  await db.exec(`
    create schema auth;
    create schema storage;
    create schema extensions;
    create role anon;
    create role authenticated;
    create role service_role bypassrls;
    create extension if not exists pgcrypto with schema extensions;
    create table auth.users(id uuid primary key);
    create function auth.uid() returns uuid language sql stable as $$
      select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
    $$;
    create function auth.jwt() returns jsonb language sql stable as $$
      select jsonb_build_object('email', current_setting('request.jwt.claim.email', true))
    $$;
    create table storage.buckets(id text primary key, name text, public boolean, file_size_limit bigint);
    create table storage.objects(id uuid, name text, bucket_id text);
    alter table storage.objects enable row level security;
    create function storage.foldername(text) returns text[] language sql as $$
      select string_to_array($1, '/')
    $$;
  `);
  const schema = fs.readFileSync(path.join(root, 'cloud', 'schema.sql'), 'utf8');
  const marker = '-- AI_REVIEW_FOUNDATION_BODY_START';
  const markerAt = schema.indexOf(marker);
  if (markerAt < 1000) throw new Error('legacy schema marker is missing');
  await db.exec(schema.slice(0, markerAt));
  await db.exec(`
    grant usage on schema public, auth, storage, extensions to authenticated, anon, service_role;
    grant all on all tables in schema public, auth, storage to service_role;
    grant execute on all functions in schema public to service_role;
  `);
  const filenames = JSON.parse(process.env.GUANYE_MIGRATION_FILES);
  const names = [];
  for (const name of filenames) {
    // Keep the SQL file's own transaction boundaries; don't wrap it in db.transaction().
    try {
      await db.exec(fs.readFileSync(path.join(root, 'cloud', 'migrations', name), 'utf8'));
    } catch (error) {
      throw new Error(`Failed to apply migration ${name}: ${error.message}`);
    }
    names.push(name);
  }
  return {db, names};
}

async function applyRole(db, request) {
  await db.exec('reset role');
  await db.query(
    "select set_config('request.jwt.claim.sub', $1, false), set_config('request.jwt.claim.email', $2, false)",
    ['', '']
  );
  if (request.role === 'authenticated') {
    await db.query(
      "select set_config('request.jwt.claim.sub', $1, false), set_config('request.jwt.claim.email', $2, false)",
      [request.actor || '', 'fixture@example.test']
    );
    await db.exec('set role authenticated');
  } else if (request.role === 'service') {
    await db.exec('set role service_role');
  } else if (request.role === 'anon') {
    await db.exec('set role anon');
  }
}

(async () => {
  const {db, names} = await bootstrap();
  process.stdout.write(JSON.stringify({ready: true, migrations: names.length, names}) + '\n');
  const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  for await (const line of lines) {
    try {
      const request = JSON.parse(line);
      await applyRole(db, request);
      let value;
      if (request.type === 'query') {
        value = (await db.query(request.sql, request.args || [])).rows;
      } else if (request.type === 'rpc') {
        const keys = Object.keys(request.args || {});
        const bindings = keys.map((key, index) => ident(key) + ' => $' + (index + 1));
        const args = keys.map((key) => request.args[key]);
        value = (await db.query(
          'select public.' + ident(request.f) + '(' + bindings.join(',') + ') as value',
          args
        )).rows[0].value;
      } else {
        throw new Error('unsupported bridge request');
      }
      process.stdout.write(JSON.stringify({value}) + '\n');
    } catch (error) {
      process.stdout.write(JSON.stringify({error: error.message || String(error)}) + '\n');
    }
  }
  await db.close();
})().catch((error) => {
  process.stderr.write((error.stack || error.message || String(error)) + '\n');
  process.exitCode = 1;
});
"""

def _http_response(
    status: int = 200,
    payload: Any | None = None,
    *,
    raw: bytes | None = None,
    content_type: str = "application/json; charset=utf-8",
) -> ai_gateway.HTTPResponse:
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return ai_gateway.HTTPResponse(status=status, headers={"Content-Type": content_type}, body=raw)


class PGliteBridge:
    """Line-delimited RPC/query bridge to an isolated PGlite instance."""

    def __init__(self, temp_dir: Path):
        self.temp_dir = temp_dir
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.migration_names: list[str] = []

    def __enter__(self) -> "PGliteBridge":
        expected_names = [item["filename"] for item in manage._migration_catalog()]
        if not expected_names or len(set(expected_names)) != len(expected_names):
            raise RuntimeError("Migration catalog is empty or contains duplicate filenames")
        script = self.temp_dir / "pglite_bridge.cjs"
        script.write_text(PGLITE_BRIDGE, encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "GUANYE_ROOT": str(ROOT),
            "GUANYE_PGLITE": str(PGLITE),
            "GUANYE_MIGRATION_FILES": json.dumps(expected_names),
            "NODE_NO_WARNINGS": "1",
        })
        self.process = subprocess.Popen(
            [NODE or "node", str(script)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        line = self.process.stdout.readline() if self.process.stdout else ""
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            self._stop()
            raise RuntimeError("PGlite bridge failed to start: " + stderr.strip())
        ready = json.loads(line)
        applied_names = ready.get("names")
        if (not ready.get("ready") or ready.get("migrations") != len(expected_names)
                or applied_names != expected_names):
            self._stop()
            raise RuntimeError(
                f"PGlite migration sequence mismatch: expected {expected_names!r}, got {applied_names!r}"
            )
        self.migration_names = list(applied_names)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop()
        return False

    def _stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def request(self, **request: Any) -> Any:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("PGlite bridge is not running")
        encoded = json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self.lock:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        if not line:
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise RuntimeError("PGlite bridge exited: " + stderr.strip())
        response = json.loads(line)
        if "error" in response:
            raise ValueError(str(response["error"]))
        return response.get("value")

    def rpc(self, name: str, args: dict[str, Any], *, role: str, actor: str = "") -> Any:
        return self.request(type="rpc", f=name, args=args, role=role, actor=actor)

    def query(self, sql: str, *args: Any, role: str = "service", actor: str = "") -> Any:
        return self.request(type="query", sql=sql, args=list(args), role=role, actor=actor)

    def seed(self) -> None:
        self.query("insert into auth.users(id) values($1),($2)", USER, MEMBER)
        settings = {
            "maxScore": 5,
            "dimensions": [{"id": "visual", "name": "视觉"}, {"id": "layout", "name": "版式"}],
        }
        self.query(
            "insert into workspaces(id,name,owner_email,invite_hash,settings) "
            "values($1,$2,$3,$4,$5::jsonb)",
            WORKSPACE,
            "端到端测试工作区",
            "owner@example.test",
            "fixture-invite-hash",
            json.dumps(settings, ensure_ascii=False),
        )
        self.query(
            "insert into members(workspace_id,user_id,name,role) "
            "values($1,$2,$3,$4),($1,$5,$6,$7)",
            WORKSPACE,
            USER,
            "端到端管理员",
            "admin",
            MEMBER,
            "端到端普通评审",
            "reviewer",
        )
        self.query(
            "update workspaces set dedicated_judge_id=$1 where id=$2",
            USER,
            WORKSPACE,
        )


class FixtureSupabaseTransport:
    """Auth/member, REST-RPC and object-storage transport over the PGlite bridge."""

    def __init__(self, db: PGliteBridge):
        self.db = db
        self.storage: dict[str, bytes] = {}
        self.storage_types: dict[str, str] = {}
        self.calls: list[dict[str, str]] = []
        self.model_calls = 0
        self.planning_model_calls = 0
        self.review_model_calls = 0
        self.lock = threading.RLock()

    def _rest_rows(self, table: str, query: dict[str, list[str]], *, role: str) -> list[dict[str, Any]]:
        """Project the narrow PostgREST reads exercised by the review routes."""
        allowed = {
            "ai_generated_entries",
            "ai_review_decisions",
            "ai_review_jobs",
            "ai_review_records",
            "ai_review_settings",
            "ai_review_submissions",
            "decks",
            "reviews",
            "workspaces",
        }
        if table == "ai_review_matches":
            args: list[Any] = [WORKSPACE]
            clauses = [
                "s.workspace_id=$1",
                "r.workspace_id=$1",
            ]
            for column in ("id", "submission_id", "record_id"):
                values = query.get(column) or []
                if values and values[-1].startswith("eq."):
                    args.append(values[-1][3:])
                    clauses.append(f'm."{column}"=${len(args)}')
            sql = (
                "select m.* from public.ai_review_matches m "
                "join public.ai_review_submissions s on s.id=m.submission_id "
                "join public.ai_review_records r on r.id=m.record_id "
                "where " + " and ".join(clauses)
            )
            return self.db.query(sql, *args, role=role)
        if table not in allowed:
            raise ValueError("unsupported fixture REST table: " + table)

        args = []
        clauses = []
        for key, values in query.items():
            if key in {"select", "order", "limit", "offset"} or "." in key:
                continue
            if not key.isidentifier():
                raise ValueError("invalid fixture REST column")
            value = values[-1] if values else ""
            if not value.startswith("eq."):
                continue
            args.append(value[3:])
            clauses.append(f'"{key}"=${len(args)}')
        sql = f'select * from public."{table}"'
        if clauses:
            sql += " where " + " and ".join(clauses)
        return self.db.query(sql, *args, role=role)

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> ai_gateway.HTTPResponse:
        del timeout
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        authorization = str(headers.get("Authorization") or "")
        token = authorization.removeprefix("Bearer ").strip()
        if path == "/auth/v1/user":
            user_id = {USER_TOKEN: USER, MEMBER_TOKEN: MEMBER}.get(token)
            if user_id:
                return _http_response(200, {"id": user_id})
            return _http_response(401, {"message": "fixture token rejected"})
        if path == "/rest/v1/members":
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            raw_user = (query.get("user_id") or [""])[-1]
            user_id = raw_user.removeprefix("eq.")
            role = {USER: "admin", MEMBER: "reviewer"}.get(user_id)
            return _http_response(200, [{"role": role}] if role else [])
        if path.startswith("/rest/v1/rpc/"):
            name = path.rsplit("/", 1)[-1]
            role = "service" if token == SERVICE_KEY else "authenticated"
            actor = "" if role == "service" else {USER_TOKEN: USER, MEMBER_TOKEN: MEMBER}.get(token, "")
            with self.lock:
                self.calls.append({"kind": "rpc", "name": name, "role": role})
            params = json.loads((body or b"{}").decode("utf-8"))
            try:
                value = self.db.rpc(name, params, role=role, actor=actor)
            except ValueError as exc:
                return _http_response(400, {"message": str(exc), "code": "fixture_sql_error"})
            return _http_response(200, value)
        if path.startswith("/rest/v1/"):
            table = path[len("/rest/v1/"):]
            role = "service" if token == SERVICE_KEY else "authenticated"
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                value = self._rest_rows(table, query, role=role)
            except ValueError as exc:
                return _http_response(400, {"message": str(exc)})
            return _http_response(200, value)
        storage_prefix = "/storage/v1/object/ppt-library/"
        if path.startswith(storage_prefix):
            object_path = urllib.parse.unquote(path[len(storage_prefix):])
            if not object_path.startswith(WORKSPACE + "/"):
                return _http_response(403, {"message": "outside workspace"})
            with self.lock:
                self.calls.append({"kind": "storage", "name": object_path, "role": "user"})
                if method == "GET":
                    data = self.storage.get(object_path)
                    if data is None:
                        return _http_response(404, {"message": "file not found"})
                    return _http_response(
                        200,
                        raw=data,
                        content_type=self.storage_types.get(object_path, "application/octet-stream"),
                    )
                if method in {"POST", "PUT", "PATCH"}:
                    artifact = bytes(body or b"")
                    content_type = str(headers.get("Content-Type") or "application/octet-stream")
                    self.storage[object_path] = artifact
                    self.storage_types[object_path] = content_type
                    return _http_response(200, {"path": object_path})
            return _http_response(405, {"message": "unsupported storage method"})
        if path.endswith("/chat/completions"):
            with self.lock:
                self.model_calls += 1
                call_number = self.model_calls
            request = json.loads((body or b"{}").decode("utf-8"))
            messages = request.get("messages") or []
            text = _model_prompt_text(messages)
            if "<UNTRUSTED_INPUT_DATA>" in text:
                with self.lock:
                    self.planning_model_calls += 1
                draft = _model_draft(messages, call_number)
            else:
                with self.lock:
                    self.review_model_calls += 1
                draft = _review_model_result(messages, call_number)
            return _http_response(200, {
                "choices": [{
                    "message": {
                        "content": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                    },
                }],
            })
        return _http_response(404, {"message": "fixture route missing: " + path})


def _model_prompt_text(messages: list[dict[str, Any]]) -> str:
    """Return the first text block from the user turn of a fixture model call."""
    text = ""
    if len(messages) >= 2 and isinstance(messages[1].get("content"), list):
        for item in messages[1]["content"]:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "")
                break
    return text


def _review_model_result(messages: list[dict[str, Any]], call_number: int) -> dict[str, Any]:
    """Return a deterministic review result shaped like the production model."""
    task = json.loads(_model_prompt_text(messages))
    if not isinstance(task, dict) or task.get("modelRole") != "GPT-6.2 Sol":
        raise ValueError("model fixture did not receive a review task")
    dimensions = task.get("requiredDimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValueError("review fixture task lacks required dimensions")
    scores = {str(dimension): 4.0 for dimension in dimensions}
    return {
        "scores": scores,
        "classification": {"primary": "生成视觉样张", "style": "克制深青"},
        "reasons": [
            "画面层级清楚，标题与正文形成稳定主次关系。",
            "十二栏对齐一致，留白与页边距保持克制。",
        ],
        "summary": "固定评审样例 " + str(call_number) + "，用于本地端到端联验。",
        "confidence": 0.92,
    }


def _model_draft(messages: list[dict[str, Any]], call_number: int) -> dict[str, Any]:
    text = _model_prompt_text(messages)
    start_marker = "<UNTRUSTED_INPUT_DATA>\n"
    end_marker = "\n</UNTRUSTED_INPUT_DATA>"
    if start_marker not in text or end_marker not in text:
        raise ValueError("model fixture did not receive the planning payload")
    payload = json.loads(text.split(start_marker, 1)[1].split(end_marker, 1)[0])
    mode = payload.get("mode")
    page_count = int(payload.get("pageCount") or 0)
    is_template = mode == "template_sample"
    title = TEMPLATE_TITLE if is_template else CONTENT_TITLE
    names = [TEMPLATE_PAGE_TITLE] if is_template else list(CONTENT_PAGE_TITLES)
    if len(names) != page_count:
        raise ValueError("model fixture page count mismatch")
    pages = []
    for index, page_title in enumerate(names, 1):
        pages.append({
            "index": index,
            "title": page_title,
            "narrativeRole": "cover" if index == 1 else "summary",
            "copy": [
                {"kind": "title", "text": page_title},
                {"kind": "body", "text": "固定测试正文，逐字保留。"},
            ],
            "layout": "十二栏网格，左对齐，固定字号层级。",
            "visualBrief": "克制深青配色与细线装饰，不使用渐变。",
            "contentSources": ["端到端测试大纲"],
            "styleReferenceDeckIds": [],
        })
    draft: dict[str, Any] = {
        "title": title,
        "summary": "端到端生成链路固定样例 " + str(call_number),
        "contentStrategy": "结论先行，逐页证据化。",
        "styleRecommendation": {
            "name": "克制深青",
            "summary": "白底、深青、严格网格。",
            "palette": ["#FFFFFF", "#164E5A", "#D8E2E3"],
            "typography": {"heading": "思源宋体", "body": "思源黑体", "numbers": "等宽数字"},
            "layoutLanguage": "十二栏网格与大留白。",
            "decoration": "细线与页码。",
            "avoid": ["渐变", "发光", "3D"],
            "rationale": "适合逐页核对。",
            "referenceDeckIds": [],
        },
        "pages": pages,
        "generationBrief": "严格按已确认文案、页序与风格生成，不得改写。",
        "assumptions": [],
        "warnings": [],
    }
    if is_template:
        draft["templateSample"] = {
            "templateFiles": [TEMPLATE_FILE_NAME],
            "pageAssignments": [{"page": 1, "templatePage": 1, "adaptation": "保留网格与配色"}],
            "reuseRules": ["保留版式层级", "替换全部文案"],
            "adaptationNotes": ["封面使用模板留白比例"],
        }
    return draft


def _fixture_template_pdf() -> bytes:
    try:
        import pymupdf as fitz  # PyMuPDF is the production parser.
    except ImportError:  # Older installs only expose the deprecated fitz alias.
        import fitz

    document = fitz.open()
    try:
        page = document.new_page(width=320, height=180)
        page.insert_text((24, 36), "fixture template", fontsize=12)
        return document.tobytes()
    finally:
        document.close()


def _png_1x1(rgb: tuple[int, int, int]) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + name
            + data
            + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    image_data = zlib.compress(b"\x00" + bytes(rgb), 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", image_data) + chunk(b"IEND", b"")


def _expected_image(title: str, order_index: int) -> bytes:
    if title == TEMPLATE_TITLE:
        return _png_1x1((28, 82, 112))
    colors = ((156, 58, 36), (36, 118, 76))
    return _png_1x1(colors[order_index])


class LoopbackExeService:
    """Deterministic API-compatible stand-in for the already-running EXE."""

    def __init__(self):
        self.lock = threading.RLock()
        self.projects: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.template_uploads: list[str] = []
        self.success_order: list[tuple[str, int]] = []
        self.failed_once: set[tuple[str, int]] = set()
        self.active = 0
        self.max_active = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _authorized(self) -> bool:
                return (
                    self.headers.get("Authorization") == "Bearer " + EXE_TOKEN
                    and self.headers.get("X-Access-Code") == EXE_TOKEN
                )

            def _read_raw(self) -> bytes:
                length = int(self.headers.get("Content-Length") or "0")
                return self.rfile.read(length) if length else b""

            def _read_json(self) -> dict[str, Any]:
                raw = self._read_raw()
                return json.loads(raw.decode("utf-8")) if raw else {}

            def _json(self, status: int, payload: Any) -> None:
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _raw(self, status: int, raw: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                if not self._authorized():
                    return self._json(403, {"error": {"message": "fixture EXE token rejected"}})
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                with owner.lock:
                    owner.calls.append(("GET", path))
                if path == "/health":
                    return self._json(200, {"status": "ok"})
                if path == "/api/image2ppt/config":
                    return self._json(200, {"data": {"worker_exists": True, "max_files": 40}})
                if path == "/api/image2ppt/settings":
                    return self._json(200, {"data": {"vision_model": "fixture-vision"}})
                parts = [part for part in path.split("/") if part]
                if len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3] == "tasks":
                    project_id, task_id = parts[2], parts[4]
                    with owner.lock:
                        task = owner.tasks.get(task_id)
                        if task is None or task["project_id"] != project_id:
                            return self._json(404, {"error": {"message": "task not found"}})
                        if not task["terminal"] and time.monotonic() >= task["ready_at"]:
                            task["terminal"] = True
                            owner.active = max(0, owner.active - 1)
                            page = owner.projects[project_id]["pages"][task["page_id"]]
                            if task["fail"]:
                                task["status"] = "FAILED"
                                page["status"] = "FAILED"
                                page["image_error"] = "fixture page failed once"
                            else:
                                task["status"] = "COMPLETED"
                                page["status"] = "COMPLETED"
                                owner.success_order.append((
                                    owner.projects[project_id]["title"],
                                    int(page["order_index"]),
                                ))
                        return self._json(200, {
                            "data": {
                                "task_id": task_id,
                                "status": task["status"],
                                "error_message": task.get("error_message"),
                            },
                        })
                if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                    project_id = parts[2]
                    with owner.lock:
                        project = owner.projects.get(project_id)
                        if project is None:
                            return self._json(404, {"error": {"message": "project not found"}})
                        pages = []
                        for page in sorted(project["pages"].values(), key=lambda row: int(row["order_index"])):
                            pages.append({
                                "page_id": page["page_id"],
                                "order_index": page["order_index"],
                                "generated_image_url": (
                                    f"/files/{project_id}/{page['page_id']}.png"
                                    if page["status"] == "COMPLETED" else None
                                ),
                                "status": page["status"],
                                "image_error": page.get("image_error"),
                            })
                        return self._json(200, {"data": {"project_id": project_id, "pages": pages}})
                if len(parts) == 3 and parts[0] == "files":
                    project_id, filename = parts[1], parts[2]
                    page_id = filename.removesuffix(".png")
                    with owner.lock:
                        project = owner.projects.get(project_id)
                        page = project["pages"].get(page_id) if project else None
                        if page is None or page["status"] != "COMPLETED":
                            return self._json(404, {"error": {"message": "image not found"}})
                        raw = _expected_image(project["title"], int(page["order_index"]))
                    return self._raw(200, raw, "image/png")
                return self._json(404, {"error": {"message": "route not found"}})

            def do_POST(self) -> None:
                if not self._authorized():
                    return self._json(403, {"error": {"message": "fixture EXE token rejected"}})
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                with owner.lock:
                    owner.calls.append(("POST", path))
                if path == "/api/projects":
                    body = self._read_json()
                    project_id = "project-" + uuid.uuid4().hex
                    with owner.lock:
                        owner.projects[project_id] = {
                            "title": str(body.get("title") or ""),
                            "pages": {},
                            "has_template": False,
                        }
                    return self._json(200, {"data": {"project_id": project_id}})
                parts = [part for part in path.split("/") if part]
                if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "pages":
                    project_id = parts[2]
                    body = self._read_json()
                    page_id = "page-" + uuid.uuid4().hex
                    with owner.lock:
                        project = owner.projects.get(project_id)
                        if project is None:
                            return self._json(404, {"error": {"message": "project not found"}})
                        project["pages"][page_id] = {
                            "page_id": page_id,
                            "order_index": int(body.get("order_index") or 0),
                            "outline": body.get("outline_content"),
                            "description": body.get("description_content"),
                            "status": "PENDING",
                            "image_error": None,
                        }
                    return self._json(200, {"data": {"page_id": page_id}})
                if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "template":
                    project_id = parts[2]
                    raw = self._read_raw()
                    with owner.lock:
                        project = owner.projects.get(project_id)
                        if project is None:
                            return self._json(404, {"error": {"message": "project not found"}})
                        if b'template_image' not in raw or b'\x89PNG\r\n\x1a\n' not in raw:
                            return self._json(400, {"error": {"message": "template image missing"}})
                        project["has_template"] = True
                        owner.template_uploads.append(project_id)
                    return self._json(200, {"data": {"ok": True}})
                if len(parts) == 7 and parts[:2] == ["api", "projects"] and parts[3] == "pages" and parts[5:] == ["generate", "image"]:
                    project_id, page_id = parts[2], parts[4]
                    self._read_json()
                    with owner.lock:
                        project = owner.projects.get(project_id)
                        page = project["pages"].get(page_id) if project else None
                        if page is None:
                            return self._json(404, {"error": {"message": "page not found"}})
                        key = (project["title"], int(page["order_index"]))
                        fail = key == (CONTENT_TITLE, 0) and key not in owner.failed_once
                        if fail:
                            owner.failed_once.add(key)
                        owner.active += 1
                        owner.max_active = max(owner.max_active, owner.active)
                        if fail:
                            delay = 0.05
                        elif key == (CONTENT_TITLE, 0):
                            delay = 0.10
                        elif key == (CONTENT_TITLE, 1):
                            delay = 0.04
                        else:
                            delay = 0.20
                        task_id = "task-" + uuid.uuid4().hex
                        owner.tasks[task_id] = {
                            "project_id": project_id,
                            "page_id": page_id,
                            "ready_at": time.monotonic() + delay,
                            "status": "RUNNING",
                            "terminal": False,
                            "fail": fail,
                            "error_message": "fixture page failed once" if fail else None,
                        }
                    return self._json(200, {"data": {"task_id": task_id}})
                return self._json(404, {"error": {"message": "route not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="fixture-exe", daemon=True)

    @property
    def base(self) -> str:
        return "http://127.0.0.1:" + str(self.server.server_address[1])

    def __enter__(self) -> "LoopbackExeService":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        return False


@contextlib.contextmanager
def running_gateway(transport: FixtureSupabaseTransport) -> Iterator[tuple[str, ai_gateway.GatewayHTTPServer]]:
    config = ai_gateway.GatewayConfig(
        supabase_url="http://fixture-supabase.invalid",
        supabase_anon_key="fixture-anon-key",
        supabase_service_role_key=SERVICE_KEY,
        workspace_id=WORKSPACE,
        max_body_bytes=4 * 1024 * 1024,
        max_storage_bytes=4 * 1024 * 1024,
        max_upstream_bytes=4 * 1024 * 1024,
        request_timeout_seconds=10.0,
        model_timeout_seconds=10.0,
        model_gateway_url="http://fixture-model.invalid/v1",
        model_id="fixture-planner",
        worker_token=WORKER_TOKEN,
    )
    gateway = ai_gateway.Gateway(config, transport)
    router = ai_gateway.build_router()
    server = ai_gateway.GatewayHTTPServer(("127.0.0.1", 0), gateway, router)
    thread = threading.Thread(target=server.serve_forever, name="gateway-http", daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1]), server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def http_json(
    server: ai_gateway.GatewayHTTPServer,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str = USER_TOKEN,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    headers: dict[str, str] = {}
    payload = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, parsed, response_headers
    finally:
        connection.close()


@unittest.skipUnless(NODE and PGLITE.is_dir(), "Node/PGlite dependency is not installed")
class AiGenerationEndToEndTests(unittest.TestCase):
    def test_worker_gateway_pglite_and_exe_adapter(self) -> None:
        try:
            template_pdf = _fixture_template_pdf()
        except Exception as exc:
            self.skipTest("PyMuPDF unavailable: " + str(exc))

        with tempfile.TemporaryDirectory(prefix="guanye-generation-e2e-") as temp_name:
            temp_dir = Path(temp_name)
            with PGliteBridge(temp_dir) as db:
                self.assertIn("20261121_18_review_backfill_classification.sql", db.migration_names)
                self.assertEqual(db.query("""
                    select
                      exists (select 1 from pg_proc
                              where oid = to_regprocedure('public.ai_review_effective_meta(uuid,text)')
                                and position('LIBRARY_CLASSIFICATION_BACKFILL_V18' in prosrc) > 0)
                        as effective_meta_v18,
                      exists (select 1 from pg_proc
                              where oid = to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)')
                                and position('REVIEW_BACKFILL_CLASSIFICATION_V18' in prosrc) > 0)
                        as backfill_v18
                """), [{"effective_meta_v18": True, "backfill_v18": True}])
                db.seed()
                transport = FixtureSupabaseTransport(db)
                with running_gateway(transport) as (gateway_base, gateway_server), LoopbackExeService() as exe:
                    def call(
                        method: str,
                        path: str,
                        body: dict[str, Any] | None = None,
                        *,
                        expected: int = 200,
                        token: str = USER_TOKEN,
                    ) -> dict[str, Any]:
                        status, payload, _ = http_json(
                            gateway_server,
                            method,
                            path,
                            body=body,
                            token=token,
                        )
                        self.assertEqual(status, expected, payload)
                        return payload

                    self.assertEqual(call("GET", "/api/ai/generation/health")["ok"], True)
                    status, payload, _ = http_json(gateway_server, "GET", "/api/ai/generation/health", token="")
                    self.assertEqual(status, 401, payload)

                    template_analysis = call(
                        "POST",
                        "/api/ai/planning/analyze",
                        {
                            "mode": "template_sample",
                            "pageCount": 1,
                            "outline": "一页模板样张，验证参考图进入实际生图项目。",
                            "styleBrief": "克制深青、十二栏网格、白底。",
                            "styleFiles": [{
                                "name": TEMPLATE_FILE_NAME,
                                "data_base64": base64.b64encode(template_pdf).decode("ascii"),
                            }],
                        },
                    )
                    self.assertEqual(template_analysis["plan"]["mode"], "template_sample")
                    self.assertEqual(template_analysis["plan"]["pageCount"], 1)
                    self.assertTrue(template_analysis["plan"]["generationStyleImages"])
                    self.assertIsNotNone(template_analysis["plan"]["templateSample"])
                    template_plan_id = template_analysis["planId"]
                    template_confirm = call(
                        "POST",
                        "/api/ai/planning/confirm",
                        {
                            "plan_id": template_plan_id,
                            "expected_revision": template_analysis["revision"],
                        },
                    )
                    self.assertEqual(template_confirm["status"], "confirmed")
                    self.assertTrue(template_confirm["versionId"])
                    template_version_id = template_confirm["versionId"]
                    reopened_template = call(
                        "GET",
                        "/api/ai/planning?" + urllib.parse.urlencode({"plan_id": template_plan_id}),
                    )
                    self.assertEqual(reopened_template["confirmed"]["versionId"], template_version_id)

                    template_job = call(
                        "POST",
                        "/api/ai/generation",
                        {"plan_id": template_plan_id},
                        expected=202,
                    )
                    template_job_again = call(
                        "POST",
                        "/api/ai/generation",
                        {"plan_id": template_plan_id},
                        expected=202,
                    )
                    self.assertEqual(template_job["id"], template_job_again["id"])
                    self.assertEqual(template_job["pages"], template_job_again["pages"])
                    self.assertEqual(len(template_job["pages"]), 1)
                    self.assertEqual(
                        call(
                            "GET",
                            "/api/ai/generation?" + urllib.parse.urlencode({"plan_id": template_plan_id}),
                        )["id"],
                        template_job["id"],
                    )

                    content_analysis = call(
                        "POST",
                        "/api/ai/planning/analyze",
                        {
                            "mode": "content_draft",
                            "pageCount": 2,
                            "outline": "先给出结论，再给出行动建议。",
                            "materials": "固定事实：本测试只验证生成链路，不访问真实模型额度。",
                            "styleBrief": "与模板样例一致的克制深青风格。",
                        },
                    )
                    self.assertEqual(content_analysis["plan"]["pageCount"], 2)
                    self.assertFalse(content_analysis["plan"].get("generationStyleImages"))
                    content_plan_id = content_analysis["planId"]
                    content_confirm = call(
                        "POST",
                        "/api/ai/planning/confirm",
                        {
                            "plan_id": content_plan_id,
                            "expected_revision": content_analysis["revision"],
                        },
                    )
                    content_version_id = content_confirm["versionId"]
                    self.assertTrue(content_version_id)
                    content_job = call(
                        "POST",
                        "/api/ai/generation",
                        {"plan_id": content_plan_id},
                        expected=202,
                    )
                    content_job_again = call(
                        "POST",
                        "/api/ai/generation",
                        {"plan_id": content_plan_id},
                        expected=202,
                    )
                    self.assertEqual(content_job["id"], content_job_again["id"])
                    self.assertEqual(
                        [page["id"] for page in content_job["pages"]],
                        [page["id"] for page in content_job_again["pages"]],
                    )
                    self.assertEqual([page["position"] for page in content_job["pages"]], [1, 2])
                    self.assertEqual(transport.model_calls, 2)

                    for job_id, version_id in (
                        (template_job["id"], template_version_id),
                        (content_job["id"], content_version_id),
                    ):
                        rows = db.query(
                            "select plan_snapshot,status,plan_id from public.ai_generation_jobs where id=$1",
                            job_id,
                        )
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["plan_snapshot"]["versionId"], version_id)
                        self.assertEqual(rows[0]["status"], "queued")
                    self.assertEqual(
                        db.query("select count(*)::int as n from public.ai_planning_versions")[0]["n"],
                        2,
                    )

                    settings = call(
                        "POST",
                        "/api/ai/generation/settings",
                        {"maxWorkers": 2},
                    )
                    self.assertEqual(settings["maxWorkers"], 2)
                    self.assertEqual(
                        call("GET", "/api/ai/generation/settings")["maxWorkers"],
                        2,
                    )

                    config = WorkerConfig(
                        gateway_base=gateway_base,
                        gateway_token=USER_TOKEN,
                        worker_token=WORKER_TOKEN,
                        workspace_id=WORKSPACE,
                        exe_base=exe.base,
                        exe_token=EXE_TOKEN,
                        worker_id="end-to-end-worker",
                        workers=4,
                        lease_seconds=60,
                        poll_seconds=0.01,
                        task_timeout=5.0,
                        upload_retries=2,
                        state_dir=temp_dir / "worker-state",
                    )
                    gateway_client = GatewayClient(
                        gateway_base,
                        USER_TOKEN,
                        WORKSPACE,
                        worker_token=WORKER_TOKEN,
                    )
                    worker = WindowsWorker(config, gateway=gateway_client)
                    preflight = worker.exe.preflight()
                    self.assertTrue(preflight["ok"])
                    self.assertTrue(preflight["httpProtocol"]["verified"])
                    self.assertFalse(preflight["runtimeImage"]["verified"])
                    self.assertFalse(preflight["startedProcess"])
                    self.assertEqual(preflight["paidRequests"], 0)

                    first_processed = worker.run(max_pages=3, once=True)
                    self.assertEqual(first_processed, 2)
                    self.assertEqual(worker.last_run.claimed, 2)
                    self.assertEqual(worker.last_run.failed, 1)
                    self.assertEqual(exe.max_active, 2)

                    template_after_first = call(
                        "GET",
                        "/api/ai/generation?" + urllib.parse.urlencode({"job_id": template_job["id"]}),
                    )
                    self.assertEqual(template_after_first["status"], "completed")
                    content_after_first = call(
                        "GET",
                        "/api/ai/generation?" + urllib.parse.urlencode({"job_id": content_job["id"]}),
                    )
                    self.assertEqual(
                        [page["status"] for page in content_after_first["pages"]],
                        ["failed", "queued"],
                    )

                    second_processed = worker.run(max_pages=3, once=True)
                    self.assertEqual(second_processed, 1)
                    self.assertEqual(worker.last_run.claimed, 1)
                    self.assertEqual(worker.last_run.failed, 0)
                    content_partial = call(
                        "GET",
                        "/api/ai/generation?" + urllib.parse.urlencode({"job_id": content_job["id"]}),
                    )
                    self.assertEqual(content_partial["status"], "partial")
                    self.assertEqual(
                        [page["status"] for page in content_partial["pages"]],
                        ["failed", "succeeded"],
                    )
                    self.assertEqual(
                        [page["position"] for page in content_partial["pages"]],
                        [1, 2],
                    )
                    failed_page = content_partial["pages"][0]
                    retry = call(
                        "POST",
                        "/api/ai/generation/retry",
                        {"job_id": content_job["id"], "page_ids": [failed_page["id"]]},
                        expected=202,
                    )
                    self.assertEqual(retry["pages"][0]["status"], "queued")
                    self.assertEqual(retry["pages"][1]["status"], "succeeded")

                    third_processed = worker.run(max_pages=1, once=True)
                    self.assertEqual(third_processed, 1)
                    self.assertEqual(worker.last_run.claimed, 1)
                    self.assertEqual(worker.last_run.failed, 0)

                    template_final = call(
                        "GET",
                        "/api/ai/generation?" + urllib.parse.urlencode({"job_id": template_job["id"]}),
                    )
                    content_final = call(
                        "GET",
                        "/api/ai/generation?" + urllib.parse.urlencode({"job_id": content_job["id"]}),
                    )
                    for job in (template_final, content_final):
                        self.assertEqual(job["status"], "completed")
                        self.assertEqual(
                            [page["position"] for page in job["pages"]],
                            list(range(1, len(job["pages"]) + 1)),
                        )
                        self.assertEqual(
                            [page["status"] for page in job["pages"]],
                            ["succeeded"] * len(job["pages"]),
                        )
                        self.assertEqual(job["pages"][0]["status"], "succeeded")
                        published = db.query(
                            "select count(*)::int as n from public.decks where id=$1",
                            job["deckId"],
                        )
                        self.assertEqual(published[0]["n"], 1)
                    self.assertEqual(
                        [page["title"] for page in content_final["pages"]],
                        list(CONTENT_PAGE_TITLES),
                    )
                    content_successes = [
                        order for title, order in exe.success_order if title == CONTENT_TITLE
                    ]
                    self.assertEqual(content_successes, [1, 0])
                    self.assertEqual(len(exe.template_uploads), 1)

                    for job in (template_final, content_final):
                        for page in job["pages"]:
                            download_path = page.get("downloadPath")
                            self.assertTrue(download_path, page)
                            artifact = call("GET", str(download_path))
                            self.assertEqual(artifact["contentType"], "image/png")
                            self.assertEqual(artifact["jobId"], job["id"])
                            self.assertEqual(artifact["pageId"], page["id"])
                            raw = base64.b64decode(artifact["dataBase64"], validate=True)
                            expected = _expected_image(job["title"], int(page["position"]) - 1)
                            self.assertEqual(raw, expected)
                            digest = hashlib.sha256(raw).hexdigest()
                            self.assertEqual(digest, artifact["sha256"])
                            self.assertEqual(digest, page["contentSha256"])
                            self.assertEqual(artifact["size"], len(raw))
                            self.assertEqual((page["width"], page["height"]), (1, 1))

                    content_deck_id = str(content_final["deckId"])
                    template_deck_id = str(template_final["deckId"])
                    content_page_id = str(content_final["pages"][0]["id"])
                    unapproved_page_id = str(content_final["pages"][1]["id"])

                    for action, body in (
                        ("prepare", {"deckId": content_deck_id, "scope": "page", "unitIds": [content_page_id]}),
                        ("formal", {"submissionId": "00000000-0000-4000-8000-000000000000"}),
                        ("admit", {"submissionId": "00000000-0000-4000-8000-000000000000"}),
                    ):
                        status, payload, _ = http_json(
                            gateway_server,
                            "POST",
                            "/api/ai/reviews?action=" + action,
                            body=body,
                            token=MEMBER_TOKEN,
                        )
                        self.assertEqual(status, 403, payload)
                        self.assertIn("专职评委", payload.get("error", ""))
                    status, payload, _ = http_json(
                        gateway_server,
                        "GET",
                        "/api/ai/reviews?action=library",
                        token="",
                    )
                    self.assertEqual(status, 401, payload)

                    page_prepare = call(
                        "POST",
                        "/api/ai/reviews?action=prepare",
                        {
                            "deckId": content_deck_id,
                            "scope": "page",
                            "unitIds": [content_page_id],
                        },
                    )
                    self.assertEqual(page_prepare["deckId"], content_deck_id)
                    self.assertEqual(page_prepare["scope"], "page")

                    review_cycles = 0
                    while review_cycles < 20:
                        if not ai_gateway.review_worker_cycle(
                            gateway_server.gateway,
                            generated_first=True,
                        ):
                            break
                        review_cycles += 1
                    self.assertLess(review_cycles, 20)
                    self.assertGreaterEqual(review_cycles, 2)

                    review_jobs = db.query(
                        "select deck_id,scope,unit_id,variant,status,error "
                        "from public.ai_review_jobs where deck_id in ($1,$2) "
                        "order by created_at,id",
                        content_deck_id,
                        template_deck_id,
                    )
                    self.assertGreaterEqual(len(review_jobs), 2)
                    self.assertEqual({row["status"] for row in review_jobs}, {"completed"})
                    self.assertEqual(
                        db.query(
                            "select count(*)::int as n from public.ai_review_records "
                            "where deck_id in ($1,$2)",
                            content_deck_id,
                            template_deck_id,
                        )[0]["n"],
                        len(review_jobs),
                    )
                    page_records = db.query(
                        "select ai_result from public.ai_review_records "
                        "where deck_id=$1 and scope='page' and unit_id=$2",
                        content_deck_id,
                        content_page_id,
                    )
                    self.assertTrue(page_records)
                    self.assertTrue(page_records[0]["ai_result"]["reasons"])

                    hidden_state = call(
                        "GET",
                        "/api/ai/reviews?" + urllib.parse.urlencode({
                            "action": "workspace",
                            "kind": "generated",
                            "deckId": content_deck_id,
                        }),
                    )
                    self.assertEqual(hidden_state["preEvaluationStatus"], "completed")
                    self.assertEqual(hidden_state["ai"], [])
                    self.assertFalse(hidden_state["blind"]["aiVisible"])
                    self.assertEqual(
                        db.query(
                            "select count(*)::int as n from public.ai_review_submissions where deck_id=$1",
                            content_deck_id,
                        )[0]["n"],
                        0,
                    )
                    self.assertEqual(
                        db.query(
                            "select count(*)::int as n from public.ai_review_matches m "
                            "join public.ai_review_submissions s on s.id=m.submission_id "
                            "where s.deck_id=$1",
                            content_deck_id,
                        )[0]["n"],
                        0,
                    )

                    page_submit = call(
                        "POST",
                        "/api/ai/reviews?action=submit",
                        {
                            "deckId": content_deck_id,
                            "scope": "page",
                            "units": [{
                                "unitId": content_page_id,
                                "position": 1,
                                "scores": {"visual": 3, "layout": 3, "match": 3},
                                "note": "人工提交后才能看到同单位预评。",
                            }],
                        },
                    )
                    self.assertTrue(page_submit["predictionAvailable"])
                    self.assertGreaterEqual(page_submit["matchedAssisted"], 1)
                    page_submission_id = page_submit["submissionIds"][0]

                    compared_state = call(
                        "GET",
                        "/api/ai/reviews?" + urllib.parse.urlencode({
                            "action": "workspace",
                            "kind": "generated",
                            "deckId": content_deck_id,
                        }),
                    )
                    self.assertTrue(compared_state["blind"]["aiVisible"])
                    page_submission = next(
                        row for row in compared_state["submissions"]
                        if row["id"] == page_submission_id
                    )
                    self.assertEqual(
                        page_submission["entry"]["scores"],
                        {"visual": 3, "layout": 3, "match": 3},
                    )
                    page_ai = next(
                        row for row in compared_state["ai"]
                        if row["unitId"] == content_page_id and row["variant"] == "assisted"
                    )
                    self.assertEqual(page_ai["unitId"], content_page_id)
                    self.assertEqual(page_ai["result"]["scores"]["visual"], 4.0)
                    self.assertTrue(page_ai["result"]["reasons"])

                    low_decision = call(
                        "POST",
                        "/api/ai/reviews?action=formal",
                        {
                            "submissionId": page_submission_id,
                            "formalScores": {"visual": 4, "layout": 4, "match": 4},
                            "reasons": ["正式加权分低于入库门槛。"],
                            "idempotencyKey": "e2e-page-low",
                        },
                    )
                    self.assertFalse(low_decision["thresholdMet"])
                    self.assertFalse(low_decision["admitted"])
                    self.assertAlmostEqual(float(low_decision["scoreRatio"]), 0.8)
                    status, rejection, _ = http_json(
                        gateway_server,
                        "POST",
                        "/api/ai/reviews?action=admit",
                        body={
                            "submissionId": page_submission_id,
                            "decisionId": low_decision["decisionId"],
                        },
                    )
                    self.assertEqual(status, 400, rejection)
                    self.assertIn("未达到", rejection.get("error", ""))
                    library_before_admit = call(
                        "GET",
                        "/api/ai/reviews?action=library",
                        token=MEMBER_TOKEN,
                    )
                    self.assertNotIn(
                        content_deck_id,
                        {deck["id"] for deck in library_before_admit["decks"]},
                    )

                    high_decision = call(
                        "POST",
                        "/api/ai/reviews?action=formal",
                        {
                            "submissionId": page_submission_id,
                            "formalScores": {"visual": 4.25, "layout": 4.25, "match": 4.25},
                            "reasons": ["新人工分达到门槛，但仍需明确入库确认。"],
                            "idempotencyKey": "e2e-page-high",
                        },
                    )
                    self.assertTrue(high_decision["thresholdMet"])
                    self.assertFalse(high_decision["admitted"])
                    self.assertAlmostEqual(float(high_decision["scoreRatio"]), 0.85)
                    entries_before_admit = db.query(
                        "select library_visible,library_label,ai_self_learning,unit_id "
                        "from public.ai_generated_entries where deck_id=$1",
                        content_deck_id,
                    )
                    self.assertGreaterEqual(len(entries_before_admit), 2)
                    self.assertTrue(all(not row["library_visible"] for row in entries_before_admit))
                    library_still_hidden = call(
                        "GET",
                        "/api/ai/reviews?action=library",
                        token=MEMBER_TOKEN,
                    )
                    self.assertNotIn(
                        content_deck_id,
                        {deck["id"] for deck in library_still_hidden["decks"]},
                    )

                    page_admitted = call(
                        "POST",
                        "/api/ai/reviews?action=admit",
                        {
                            "submissionId": page_submission_id,
                            "decisionId": high_decision["decisionId"],
                        },
                    )
                    self.assertTrue(page_admitted["admitted"])
                    self.assertEqual(page_admitted["libraryLabel"], REVIEW_LABEL)
                    page_library = call(
                        "GET",
                        "/api/ai/reviews?action=library",
                        token=MEMBER_TOKEN,
                    )
                    page_deck = next(
                        deck for deck in page_library["decks"] if deck["id"] == content_deck_id
                    )
                    self.assertEqual(page_deck["data"]["libraryScope"], "pages")
                    self.assertEqual(
                        [image["id"] for image in page_deck["data"]["images"]],
                        [content_page_id],
                    )
                    self.assertEqual(page_deck["data"]["approvedPageIds"], [content_page_id])
                    self.assertNotIn(
                        unapproved_page_id,
                        {image["id"] for image in page_deck["data"]["images"]},
                    )
                    self.assertEqual(page_deck["data"]["aiLabel"], REVIEW_LABEL)
                    self.assertEqual(page_deck["data"]["libraryLabel"], REVIEW_LABEL)
                    visible_page_entries = db.query(
                        "select unit_id,library_label,ai_self_learning "
                        "from public.ai_generated_entries "
                        "where deck_id=$1 and library_visible",
                        content_deck_id,
                    )
                    self.assertEqual(len(visible_page_entries), 1)
                    self.assertEqual(visible_page_entries[0]["unit_id"], content_page_id)
                    self.assertEqual(visible_page_entries[0]["library_label"], REVIEW_LABEL)
                    self.assertTrue(visible_page_entries[0]["ai_self_learning"])

                    whole_submit = call(
                        "POST",
                        "/api/ai/reviews?action=submit",
                        {
                            "deckId": template_deck_id,
                            "scope": "deck",
                            "units": [{
                                "scores": {"visual": 3, "layout": 3, "match": 3},
                                "note": "整套人工评分必须在明确确认后入库。",
                            }],
                        },
                    )
                    self.assertTrue(whole_submit["predictionAvailable"])
                    self.assertGreaterEqual(whole_submit["matchedAssisted"], 1)
                    whole_submission_id = whole_submit["submissionIds"][0]
                    whole_state = call(
                        "GET",
                        "/api/ai/reviews?" + urllib.parse.urlencode({
                            "action": "workspace",
                            "kind": "generated",
                            "deckId": template_deck_id,
                        }),
                    )
                    self.assertTrue(whole_state["blind"]["aiVisible"])
                    self.assertTrue(any(row["scope"] == "deck" for row in whole_state["ai"]))

                    whole_high = call(
                        "POST",
                        "/api/ai/reviews?action=formal",
                        {
                            "submissionId": whole_submission_id,
                            "formalScores": {"visual": 4.25, "layout": 4.25, "match": 4.25},
                            "reasons": ["整套达到门槛，仍需明确确认。"],
                            "idempotencyKey": "e2e-deck-high",
                        },
                    )
                    self.assertTrue(whole_high["thresholdMet"])
                    self.assertFalse(whole_high["admitted"])
                    library_before_whole_admit = call(
                        "GET",
                        "/api/ai/reviews?action=library",
                        token=MEMBER_TOKEN,
                    )
                    self.assertNotIn(
                        template_deck_id,
                        {deck["id"] for deck in library_before_whole_admit["decks"]},
                    )
                    whole_admitted = call(
                        "POST",
                        "/api/ai/reviews?action=admit",
                        {
                            "submissionId": whole_submission_id,
                            "decisionId": whole_high["decisionId"],
                        },
                    )
                    self.assertTrue(whole_admitted["admitted"])
                    self.assertEqual(whole_admitted["libraryLabel"], REVIEW_LABEL)

                    final_library = call(
                        "GET",
                        "/api/ai/reviews?action=library",
                        token=MEMBER_TOKEN,
                    )
                    final_decks = {deck["id"]: deck for deck in final_library["decks"]}
                    self.assertEqual(set(final_decks), {content_deck_id, template_deck_id})
                    self.assertEqual(
                        final_decks[template_deck_id]["data"]["libraryScope"],
                        "deck",
                    )
                    self.assertEqual(
                        final_decks[template_deck_id]["data"]["aiLabel"],
                        REVIEW_LABEL,
                    )
                    self.assertEqual(
                        final_decks[template_deck_id]["data"]["libraryLabel"],
                        REVIEW_LABEL,
                    )
                    visible_entries = db.query(
                        "select deck_id,scope,unit_id,library_label,ai_self_learning "
                        "from public.ai_generated_entries where library_visible "
                        "order by deck_id,scope",
                    )
                    self.assertEqual(len(visible_entries), 2)
                    self.assertEqual(
                        {(row["deck_id"], row["scope"], row["unit_id"]) for row in visible_entries},
                        {
                            (content_deck_id, "page", content_page_id),
                            (template_deck_id, "deck", ""),
                        },
                    )
                    self.assertEqual(
                        {row["library_label"] for row in visible_entries},
                        {REVIEW_LABEL},
                    )
                    self.assertTrue(all(row["ai_self_learning"] for row in visible_entries))
                    self.assertTrue(transport.storage)
                    self.assertEqual(
                        {path.split("/", 1)[0] for path in transport.storage},
                        {WORKSPACE},
                    )

                    role_calls = {
                        (item["name"], item["role"])
                        for item in transport.calls
                        if item["kind"] == "rpc"
                    }
                    self.assertIn(("ai_planning_get_plan", "authenticated"), role_calls)
                    self.assertIn(("ai_planning_read_confirmed_plan", "service"), role_calls)
                    self.assertIn(("ai_generation_create", "service"), role_calls)
                    self.assertIn(("ai_generation_get", "authenticated"), role_calls)
                    self.assertIn(("ai_generation_claim_pages", "service"), role_calls)
                    self.assertIn(("ai_generation_page_succeed", "service"), role_calls)
                    self.assertIn(("ai_generation_page_fail", "service"), role_calls)
                    self.assertIn(("ai_generation_retry_pages", "authenticated"), role_calls)
                    self.assertIn(("ai_review_prepare", "service"), role_calls)
                    self.assertIn(("ai_review_workspace_batch", "service"), role_calls)
                    self.assertIn(("ai_review_submit", "service"), role_calls)
                    self.assertIn(("ai_review_confirm", "service"), role_calls)
                    self.assertIn(("ai_review_admit", "service"), role_calls)
                    self.assertIn(("get_review_library", "authenticated"), role_calls)
                    self.assertIn(("ai_review_claim_jobs", "service"), role_calls)
                    self.assertIn(("ai_review_finish_attempt", "service"), role_calls)
                    self.assertEqual(transport.planning_model_calls, 2)
                    self.assertEqual(transport.review_model_calls, review_cycles)
                    self.assertEqual(
                        transport.model_calls,
                        transport.planning_model_calls + transport.review_model_calls,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
