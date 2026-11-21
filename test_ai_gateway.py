"""Unit and local-loopback tests for the Guanye AI gateway.

All Supabase and model calls use an injected fake transport. The only socket
used is a loopback HTTP server hosting the gateway under test.
"""
from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import dataclasses
from email.message import Message
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import uuid
from pathlib import Path
from unittest import mock

from cloud import ai_gateway, ai_generation
from cloud import build as site_build

WORKSPACE = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
OUTSIDER = "33333333-3333-4333-8333-333333333333"
ADMIN = "44444444-4444-4444-8444-444444444444"
MATCH = "66666666-6666-4666-8666-666666666666"
SUBMISSION = "77777777-7777-4777-8777-777777777777"
RECORD = "88888888-8888-4888-8888-888888888888"


def response(status=200, payload=None, raw=None):
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    return ai_gateway.HTTPResponse(status=status, headers={}, body=raw)


class RecordingEvent(threading.Event):
    """Threading event that records which thread acknowledged a stop request."""

    def __init__(self):
        super().__init__()
        self.set_threads = []

    def set(self):
        self.set_threads.append(threading.get_ident())
        return super().set()


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.roles = {USER: "reviewer", ADMIN: "admin"}
        self.lock = threading.Lock()
        self.active_model = 0
        self.max_active_model = 0

    def __call__(self, method, url, headers, body, timeout):
        parsed = urllib.parse.urlsplit(url)
        with self.lock:
            self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if parsed.path == "/auth/v1/user":
            token = headers.get("Authorization", "").removeprefix("Bearer ")
            user_id = {f"token-{USER}": USER, f"token-{OUTSIDER}": OUTSIDER, f"token-{ADMIN}": ADMIN}.get(token)
            return response(200, {"id": user_id}) if user_id else response(401, {"message": "invalid"})
        if parsed.path == "/rest/v1/members":
            query = urllib.parse.parse_qs(parsed.query)
            user_id = query.get("user_id", [""])[0].removeprefix("eq.")
            role = self.roles.get(user_id)
            return response(200, [{"role": role}] if role else [])
        if parsed.path.startswith("/rest/v1/rpc/"):
            return response(200, {"ok": True})
        if parsed.path.startswith("/rest/v1/"):
            return response(200, [{"id": "row-1"}])
        if parsed.path.startswith("/storage/v1/object/"):
            if method == "GET":
                return response(200, raw=b"stored-bytes")
            return response(200, {"path": parsed.path.rsplit("/", 1)[-1]})
        if parsed.path.endswith("/chat/completions"):
            with self.lock:
                self.active_model += 1
                self.max_active_model = max(self.max_active_model, self.active_model)
            try:
                time.sleep(0.03)
                request = json.loads(body.decode("utf-8"))
                self.last_model_request = request
                return response(
                    200,
                    {"choices": [{"message": {"content": json.dumps({"summary": "ok", "model": request["model"]})}}]},
                )
            finally:
                with self.lock:
                    self.active_model -= 1
        return response(404, {"message": "fake route missing"})


class StrictAiReviewMatchesTransport:
    """Schema-aware transport for the workspace-less match table."""

    TABLE_COLUMNS = {
        "ai_review_matches": frozenset({"id", "submission_id", "record_id", "variant", "created_at"}),
        "ai_review_submissions": frozenset({"id", "workspace_id"}),
        "ai_review_records": frozenset({"id", "workspace_id"}),
    }
    RELATED_WORKSPACE_FILTERS = (
        "ai_review_submissions.workspace_id",
        "ai_review_records.workspace_id",
    )

    def __init__(self):
        self.calls = []
        self.queries = []

    def __call__(self, method, url, headers, body, timeout):
        parsed = urllib.parse.urlsplit(url)
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
            }
        )
        if method != "GET" or parsed.path != "/rest/v1/ai_review_matches":
            return response(404, {"message": "unexpected fake route"})
        if (
            headers.get("apikey") != "service-secret"
            or headers.get("Authorization") != "Bearer service-secret"
        ):
            return response(403, {"message": "service role required"})

        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = {}
        for key, value in pairs:
            if key in query:
                return response(400, {"message": f"duplicate query parameter: {key}"})
            query[key] = value

        if query.get("select") != ai_gateway.AI_REVIEW_MATCHES_EMBED_SELECT:
            return response(400, {"message": "match projection is not the fixed inner embedding"})
        if "workspace_id" in query:
            return response(400, {"message": "ai_review_matches has no workspace_id column"})

        for key in self.RELATED_WORKSPACE_FILTERS:
            if query.get(key) != f"eq.{WORKSPACE}":
                return response(400, {"message": f"missing workspace constraint: {key}"})

        for key, value in query.items():
            if key == "select" or key in self.RELATED_WORKSPACE_FILTERS:
                continue
            if "." in key:
                relation, column = key.split(".", 1)
                if column not in self.TABLE_COLUMNS.get(relation, frozenset()):
                    return response(400, {"message": f"unknown embedded column: {key}"})
            elif key not in self.TABLE_COLUMNS["ai_review_matches"]:
                return response(400, {"message": f"unknown match column: {key}"})
            if not value.startswith("eq."):
                return response(400, {"message": f"unsupported filter operator: {key}"})

        self.queries.append(dict(query))
        return response(
            200,
            [
                {
                    "id": MATCH,
                    "submission_id": SUBMISSION,
                    "record_id": RECORD,
                    "variant": "baseline",
                    "ai_review_submissions": [],
                    "ai_review_records": [],
                }
            ],
        )


class TestRoutes:
    @staticmethod
    def configure(router):
        def echo(ctx):
            return 201, {
                "user_id": ctx.user_id,
                "workspace_id": ctx.workspace_id,
                "role": ctx.role,
                "body": ctx.body,
                "query": ctx.query,
            }

        def invalid(ctx):
            raise ValueError("字段无效")

        def forbidden(ctx):
            raise PermissionError("禁止访问")

        def privileged(ctx):
            return ctx.rpc("admin_only", {"w": ctx.workspace_id}, privileged=True)

        def storage(ctx):
            if ctx.query.get("outside") == "1":
                return {"data": ctx.storage_get("55555555-5555-4555-8555-555555555555/outside.bin").decode("utf-8")}
            if ctx.query.get("put") == "1":
                return ctx.storage_put(ctx.workspace_id + "/ai/result.txt", b"abc", "text/plain")
            return {"data": ctx.storage_get(ctx.workspace_id + "/ai/source.txt").decode("utf-8")}

        def model(ctx):
            return ctx.chat_json([
                {"role": "user", "content": [
                    {"type": "text", "text": "review this page"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]}
            ])

        router.add("POST", "/api/ai/echo", echo)
        router.add("POST", "/api/ai/invalid", invalid)
        router.add("POST", "/api/ai/forbidden", forbidden)
        router.add("POST", "/api/ai/privileged", privileged)
        router.add("GET", "/api/ai/storage", storage)
        router.add("POST", "/api/ai/model", model)

        def worker(ctx):
            return 202, {"worker": ctx.is_worker, "rpc": ctx.rpc("worker_complete_test", {"w": ctx.workspace_id}, privileged=True)}

        router.add("POST", "/api/ai/generation/worker/complete", worker)


class SelectContractRoutes:
    @staticmethod
    def configure(router):
        from cloud import ai_reviews

        def select_contract(ctx):
            member = ai_reviews._member(ctx)
            deck = ai_reviews._deck(ctx, "deck-1")
            boundary = ctx.select(
                "decks",
                {"workspace_id": OUTSIDER, "id": "boundary-deck", "select": "id"},
            )
            records = ctx.select(
                "ai_review_records",
                {"workspace_id": ctx.workspace_id, "bucket": 0, "select": "id"},
            )
            return {"member": member, "deck": deck, "boundary": boundary, "records": records}

        router.add("GET", "/api/ai/select-contract", select_contract)


def make_config(**changes):
    values = dict(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-public",
        supabase_service_role_key="service-secret",
        workspace_id=WORKSPACE,
        allowed_origins=("https://pwmpro-a.github.io",),
        max_body_bytes=4096,
        model_gateway_url="https://model.example/v1",
        model_id="custom-model",
        model_api_key="model-secret",
        model_max_concurrency=2,
        worker_token='worker-secret',
    )
    values.update(changes)
    return ai_gateway.GatewayConfig(**values)


@contextlib.contextmanager
def running_gateway(config=None, transport=None, modules=None):
    config = config or make_config()
    transport = transport or FakeTransport()
    gateway = ai_gateway.Gateway(config, transport=transport)
    router = ai_gateway.build_router(modules or [TestRoutes])
    server = ai_gateway.GatewayHTTPServer(("127.0.0.1", 0), gateway, router)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, transport
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def http_request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    request_headers = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=payload, headers=request_headers)
    result = connection.getresponse()
    raw = result.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else None
    result_headers = {key.lower(): value for key, value in result.getheaders()}
    connection.close()
    return result.status, result_headers, parsed


def free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_request_port(port, method, path, timeout=2.0):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path)
        result = connection.getresponse()
        raw = result.read()
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return result.status, dict(result.getheaders()), payload
    finally:
        connection.close()


class LoopbackTransportUpstream:
    def __init__(self):
        owner = self
        self.storage_status = 200
        self.storage_body = b""
        self.rest_status = 200
        self.rest_body = b""
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                owner.requests.append(
                    {
                        "path": path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                    }
                )
                if path.startswith("/storage/v1/object/"):
                    status, body, content_type = owner.storage_status, owner.storage_body, "application/octet-stream"
                elif path.startswith("/rest/v1/"):
                    status, body, content_type = owner.rest_status, owner.rest_body, "application/json"
                else:
                    status, body, content_type = 404, b"{}", "application/json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class GatewayTests(unittest.TestCase):
    def test_default_contracts_and_router(self):
        self.assertEqual(ai_gateway.DEFAULT_MAX_BODY_BYTES, 160 * 1024 * 1024)
        self.assertEqual(ai_gateway.STORAGE_BUCKET, "ppt-library")
        config = make_config(max_body_bytes=ai_gateway.DEFAULT_MAX_BODY_BYTES)
        self.assertEqual(config.max_body_bytes, 160 * 1024 * 1024)
        with self.assertRaises(ai_gateway.GatewayConfigError):
            make_config(storage_bucket="another-bucket")
        router = ai_gateway.build_router([TestRoutes])
        self.assertIn(("POST", "/api/ai/echo"), router.routes)
        self.assertIn(("POST", "/api/ai/generation/worker/complete"), router.routes)
        with self.assertRaises(ai_gateway.RouteNotFound):
            router.resolve("POST", "/api/ai/echo/")

    def test_recent_generation_uses_authenticated_rpc_and_propagates_missing_migration(self):
        class HistoryTransport(FakeTransport):
            history = response(200, {"jobs": []})

            def __call__(self, method, url, headers, body, timeout):
                if urllib.parse.urlsplit(url).path != "/rest/v1/rpc/ai_generation_recent":
                    return super().__call__(method, url, headers, body, timeout)
                self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
                return self.history

        transport = HistoryTransport()
        path = "/api/ai/generation?action=recent"
        headers = {"Authorization": f"Bearer token-{USER}"}
        with running_gateway(transport=transport, modules=[ai_generation]) as (server, _):
            self.assertEqual(http_request(server, "GET", path)[0], 401)
            self.assertEqual(http_request(server, "GET", path, headers={"Authorization": f"Bearer token-{OUTSIDER}"})[0], 403)
            status, _, payload = http_request(server, "GET", path, headers=headers)
            self.assertEqual((status, payload), (200, {"jobs": []}))
            rpc_calls = [call for call in transport.calls if call["url"].endswith("/ai_generation_recent")]
            self.assertEqual(len(rpc_calls), 1)
            self.assertEqual(rpc_calls[0]["headers"]["Authorization"], headers["Authorization"])
            self.assertEqual(rpc_calls[0]["headers"]["apikey"], "anon-public")
            self.assertEqual(json.loads(rpc_calls[0]["body"]), {"p_actor": USER, "p_workspace": WORKSPACE})
            transport.history = response(404, {"code": "PGRST202", "message": "function does not exist"})
            status, _, payload = http_request(server, "GET", path, headers=headers)
            self.assertGreaterEqual(status, 400)
            self.assertIn("error", payload)
            self.assertNotIn("jobs", payload)

    def test_http_auth_statuses_context_and_cors(self):
        with running_gateway() as (server, _transport):
            status, headers, payload = http_request(server, "GET", "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(payload["ok"], True)

            status, _headers, payload = http_request(server, "POST", "/api/ai/echo", {})
            self.assertEqual(status, 401)
            self.assertEqual(payload["code"], "unauthorized")

            outsider = {"Authorization": f"Bearer token-{OUTSIDER}"}
            status, _headers, payload = http_request(server, "POST", "/api/ai/echo", {}, outsider)
            self.assertEqual(status, 403)
            self.assertEqual(payload["code"], "forbidden")

            member = {
                "Authorization": f"Bearer token-{USER}",
                "Origin": "https://pwmpro-a.github.io",
            }
            status, headers, payload = http_request(
                server, "POST", "/api/ai/echo?deck=deck-1", {"note": "ok"}, member
            )
            self.assertEqual(status, 201)
            self.assertEqual(headers["access-control-allow-origin"], "https://pwmpro-a.github.io")
            self.assertEqual(payload["user_id"], USER)
            self.assertEqual(payload["workspace_id"], WORKSPACE)
            self.assertEqual(payload["role"], "reviewer")
            self.assertEqual(payload["query"]["deck"], "deck-1")
            self.assertEqual(payload["body"], {"note": "ok"})

            status, _headers, payload = http_request(
                server, "POST", "/api/ai/invalid", {}, {"Authorization": f"Bearer token-{USER}"}
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "字段无效")

            status, _headers, payload = http_request(
                server,
                "POST",
                "/api/ai/echo",
                {},
                {"Authorization": f"Bearer token-{USER}", "Origin": "https://evil.example"},
            )
            self.assertEqual(status, 403)

            status, headers, _payload = http_request(
                server,
                "OPTIONS",
                "/api/ai/echo",
                headers={
                    "Origin": "https://pwmpro-a.github.io",
                    "Access-Control-Request-Method": "POST",
                },
            )
            self.assertEqual(status, 204)
            self.assertEqual(headers["access-control-allow-origin"], "https://pwmpro-a.github.io")

    def test_body_limit_and_exact_route(self):
        with running_gateway(make_config(max_body_bytes=128)) as (server, _transport):
            status, _headers, payload = http_request(
                server,
                "POST",
                "/api/ai/echo",
                {"value": "x" * 200},
                {"Authorization": f"Bearer token-{USER}"},
            )
            self.assertEqual(status, 413)
            self.assertEqual(payload["code"], "body_too_large")
            status, _headers, payload = http_request(
                server,
                "POST",
                "/api/ai/echo/",
                {},
                {"Authorization": f"Bearer token-{USER}"},
            )
            self.assertEqual(status, 404)

    def test_worker_identity_blocks_reviewer_forgery(self):
        member_path = "/api/ai/generation/worker/complete"
        with running_gateway() as (server, transport):
            status, _headers, payload = http_request(
                server,
                "POST",
                member_path,
                {"page_id": "p"},
                {"Authorization": f"Bearer token-{USER}"},
            )
            self.assertEqual(status, 403)
            self.assertIn("worker", payload["error"])

            status, _headers, payload = http_request(
                server,
                "POST",
                member_path,
                {"page_id": "p"},
                {"Authorization": f"Bearer token-{ADMIN}"},
            )
            self.assertEqual(status, 403)

            status, _headers, payload = http_request(
                server,
                "POST",
                member_path,
                {"page_id": "p"},
                {
                    "Authorization": f"Bearer token-{USER}",
                    "X-Guanye-Worker-Token": "wrong-secret",
                },
            )
            self.assertEqual(status, 403)

            status, _headers, payload = http_request(
                server,
                "POST",
                member_path,
                {"page_id": "p"},
                {
                    "Authorization": f"Bearer token-{USER}",
                    "X-Guanye-Worker-Token": "worker-secret",
                },
            )
            status, _headers, payload = http_request(
                server,
                "POST",
                member_path,
                {"page_id": "p"},
                {
                    "Authorization": f"Bearer token-{USER}",
                    "X-Access-Code": "worker-secret",
                },
            )
            self.assertEqual(status, 202)
            self.assertEqual(status, 202)
            self.assertEqual(payload["worker"], True)
            self.assertEqual(payload["rpc"]["ok"], True)
            rpc_calls = [call for call in transport.calls if call["url"].endswith("/rpc/worker_complete_test")]
            self.assertEqual(len(rpc_calls), 2)
            self.assertEqual(rpc_calls[0]["headers"]["Authorization"], "Bearer service-secret")

    def test_request_context_select_converts_raw_equality_and_forces_workspace(self):
        with running_gateway(modules=[SelectContractRoutes]) as (server, transport):
            status, _headers, payload = http_request(
                server,
                "GET",
                "/api/ai/select-contract",
                headers={"Authorization": f"Bearer token-{USER}"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["member"]["role"], "reviewer")

            rest_calls = [
                call for call in transport.calls
                if urllib.parse.urlsplit(call["url"]).path.startswith("/rest/v1/")
            ]

            def query(call):
                return urllib.parse.parse_qs(
                    urllib.parse.urlsplit(call["url"]).query,
                    keep_blank_values=True,
                )

            member_calls = [
                call for call in rest_calls
                if urllib.parse.urlsplit(call["url"]).path == "/rest/v1/members"
                and "limit" not in query(call)
            ]
            self.assertEqual(len(member_calls), 1)
            member_query = query(member_calls[0])
            self.assertEqual(member_query["user_id"], [f"eq.{USER}"])
            self.assertEqual(member_query["workspace_id"], [f"eq.{WORKSPACE}"])
            self.assertEqual(member_query["select"], ["*"])

            deck_calls = [
                call for call in rest_calls
                if urllib.parse.urlsplit(call["url"]).path == "/rest/v1/decks"
            ]
            self.assertEqual(len(deck_calls), 2)
            deck_query = query(deck_calls[0])
            self.assertEqual(deck_query["id"], ["eq.deck-1"])
            self.assertEqual(deck_query["workspace_id"], [f"eq.{WORKSPACE}"])

            boundary_query = query(deck_calls[1])
            self.assertEqual(boundary_query["id"], ["eq.boundary-deck"])
            self.assertEqual(boundary_query["workspace_id"], [f"eq.{WORKSPACE}"])
            self.assertNotIn("eq." + OUTSIDER, boundary_query["workspace_id"])
            self.assertEqual(boundary_query["select"], ["id"])

            records_calls = [
                call for call in rest_calls
                if urllib.parse.urlsplit(call["url"]).path == "/rest/v1/ai_review_records"
            ]
            self.assertEqual(len(records_calls), 1)
            records_query = query(records_calls[0])
            self.assertEqual(records_query["workspace_id"], [f"eq.{WORKSPACE}"])
            self.assertEqual(records_query["bucket"], ["eq.0"])
            self.assertEqual(records_query["select"], ["id"])

    def test_gateway_select_preserves_native_postgrest_query_for_workers(self):
        transport = FakeTransport()
        gateway = ai_gateway.Gateway(make_config(), transport=transport)
        principal = ai_gateway.Principal("", WORKSPACE, "", "")
        result = gateway.select(
            principal,
            "workspaces",
            {"select": "dedicated_judge_id"},
            privileged=True,
        )
        self.assertEqual(result, [{"id": "row-1"}])
        call = next(
            call for call in transport.calls
            if urllib.parse.urlsplit(call["url"]).path == "/rest/v1/workspaces"
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
        self.assertEqual(query["id"], [f"eq.{WORKSPACE}"])
        self.assertEqual(query["select"], ["dedicated_judge_id"])

    def _strict_ai_review_matches_context(self):
        transport = StrictAiReviewMatchesTransport()
        gateway = ai_gateway.Gateway(make_config(), transport=transport)
        principal = ai_gateway.Principal(USER, WORKSPACE, "reviewer", "reviewer-token")
        context = ai_gateway.RequestContext(gateway, principal, {}, {})
        return transport, gateway, principal, context

    def test_ai_review_matches_default_projection_uses_fixed_inner_embeds(self):
        transport, _gateway, _principal, context = self._strict_ai_review_matches_context()
        rows = context.select("ai_review_matches", privileged=True)
        self.assertEqual(rows[0]["id"], MATCH)
        self.assertEqual(len(transport.queries), 1)
        query = transport.queries[0]
        self.assertEqual(query["select"], ai_gateway.AI_REVIEW_MATCHES_EMBED_SELECT)
        self.assertNotIn("workspace_id", query)
        self.assertEqual(query["ai_review_submissions.workspace_id"], f"eq.{WORKSPACE}")
        self.assertEqual(query["ai_review_records.workspace_id"], f"eq.{WORKSPACE}")

    def test_ai_review_matches_config_workspace_wins_over_principal_workspace(self):
        transport = StrictAiReviewMatchesTransport()
        gateway = ai_gateway.Gateway(make_config(), transport=transport)
        forged = ai_gateway.Principal(USER, OUTSIDER, "reviewer", "reviewer-token")
        gateway.select(
            forged,
            "ai_review_matches",
            {"submission_id": f"eq.{SUBMISSION}"},
            privileged=True,
        )
        query = transport.queries[0]
        self.assertEqual(query["ai_review_submissions.workspace_id"], f"eq.{WORKSPACE}")
        self.assertEqual(query["ai_review_records.workspace_id"], f"eq.{WORKSPACE}")
        self.assertNotEqual(f"eq.{OUTSIDER}", query["ai_review_submissions.workspace_id"])
        self.assertNotEqual(f"eq.{OUTSIDER}", query["ai_review_records.workspace_id"])

    def test_ai_review_matches_original_filters_remain_scoped(self):
        transport, _gateway, _principal, context = self._strict_ai_review_matches_context()
        for key, identifier in (("submission_id", SUBMISSION), ("record_id", RECORD)):
            with self.subTest(filter=key):
                context.select("ai_review_matches", {key: identifier}, privileged=True)
                query = transport.queries[-1]
                self.assertEqual(query[key], f"eq.{identifier}")
                self.assertNotIn("workspace_id", query)
                self.assertEqual(query["ai_review_submissions.workspace_id"], f"eq.{WORKSPACE}")
                self.assertEqual(query["ai_review_records.workspace_id"], f"eq.{WORKSPACE}")
        self.assertEqual(len(transport.queries), 2)

    def test_ai_review_matches_rejects_unprivileged_and_unsafe_queries(self):
        transport, gateway, principal, context = self._strict_ai_review_matches_context()
        with self.assertRaises(PermissionError):
            context.select("ai_review_matches", {"submission_id": SUBMISSION})
        self.assertEqual(transport.calls, [])

        unsafe_queries = (
            {"workspace_id": f"eq.{OUTSIDER}"},
            {"ai_review_submissions.workspace_id": f"eq.{OUTSIDER}"},
            {"submission_id": f"neq.{SUBMISSION}"},
            {"variant": "eq.other"},
            {"select": "id"},
            {"unexpected": "eq.value"},
        )
        for query in unsafe_queries:
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    gateway.select(principal, "ai_review_matches", query, privileged=True)
        self.assertEqual(transport.calls, [])

    def test_storage_bucket_policy_and_private_prefix(self):
        with running_gateway() as (server, transport):
            auth = {"Authorization": f"Bearer token-{USER}"}
            status, _headers, payload = http_request(
                server,
                "GET",
                f"/api/ai/storage?path={urllib.parse.quote(WORKSPACE + '/ai/source.txt')}",
                headers=auth,
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"], "stored-bytes")

            status, _headers, payload = http_request(server, "GET", "/api/ai/storage?outside=1", headers=auth)
            self.assertEqual(status, 400)
            self.assertIn("工作区", payload["error"])

            status, _headers, payload = http_request(server, "GET", "/api/ai/storage?put=1", headers=auth)
            self.assertEqual(status, 200)
            storage_calls = [call for call in transport.calls if "/storage/v1/object/" in call["url"]]
            self.assertTrue(storage_calls)
            self.assertIn("/storage/v1/object/ppt-library/" + WORKSPACE + "/", storage_calls[-1]["url"])
            self.assertTrue(storage_calls[-1]["headers"]["Authorization"].startswith("Bearer "))

    def test_storage_get_maps_only_structured_missing_object_400(self):
        current = {"response": response(200, raw=b"stored-bytes")}

        def transport(method, url, headers, body, timeout):
            self.assertEqual(method, "GET")
            return current["response"]

        gateway = ai_gateway.Gateway(make_config(), transport=transport)
        principal = ai_gateway.Principal(USER, WORKSPACE, "reviewer", "user-token")
        ctx = ai_gateway.RequestContext(gateway, principal, {}, {})
        path = f"{WORKSPACE}/ai/new-page.png"

        for label, upstream_response in (
            ("string-404", response(400, {
                "statusCode": "404",
                "error": "not_found",
                "code": "NoSuchKey",
                "message": "Object not found",
            })),
            ("integer-404", response(400, {
                "statusCode": 404,
                "error": "not_found",
                "code": "NoSuchKey",
            })),
            ("plain-404", response(404, raw=b"not-json")),
        ):
            with self.subTest(case=label):
                current["response"] = upstream_response
                with self.assertRaisesRegex(ValueError, "^文件不存在$"):
                    ctx.storage_get(path)
                self.assertIsNone(ai_generation._try_read_storage(ctx, path))

        for label, upstream_response, error, message in (
            ("permission", response(403, {
                "statusCode": "404", "code": "NoSuchKey",
            }), PermissionError, "没有权限"),
            ("permission-400", response(400, {
                "statusCode": "403", "error": "Unauthorized", "code": "AccessDenied",
            }), ai_gateway.UpstreamError, "文件读取失败"),
            ("bucket", response(400, {
                "statusCode": "404", "code": "NoSuchBucket",
            }), ai_gateway.UpstreamError, "文件读取失败"),
            ("wrong-status-code", response(400, {
                "statusCode": 400, "code": "NoSuchKey",
            }), ai_gateway.UpstreamError, "文件读取失败"),
            ("missing-code", response(400, {
                "statusCode": "404",
            }), ai_gateway.UpstreamError, "文件读取失败"),
            ("boolean-status-code", response(400, {
                "statusCode": True, "code": "NoSuchKey",
            }), ai_gateway.UpstreamError, "文件读取失败"),
            ("list-body", response(400, [{
                "statusCode": "404", "code": "NoSuchKey",
            }]), ai_gateway.UpstreamError, "文件读取失败"),
            ("malformed-body", response(400, raw=b"{"), ai_gateway.UpstreamError, "文件读取失败"),
            ("server-error", response(500, {
                "statusCode": "404", "code": "NoSuchKey",
            }), ai_gateway.UpstreamError, "文件读取失败"),
        ):
            with self.subTest(case=label):
                current["response"] = upstream_response
                with self.assertRaisesRegex(error, message):
                    ctx.storage_get(path)
                with self.assertRaisesRegex(error, message):
                    ai_generation._try_read_storage(ctx, path)

    def test_direct_transport_storage_get_uses_storage_limit(self):
        with contextlib.closing(LoopbackTransportUpstream()) as upstream:
            config = make_config(
                supabase_url=upstream.base_url,
                max_upstream_bytes=64,
                max_storage_bytes=128,
            )
            gateway = ai_gateway.Gateway(config)
            principal = ai_gateway.Principal(USER, WORKSPACE, "reviewer", "user-token")
            path = f"{WORKSPACE}/ai/large.bin"

            upstream.storage_body = b"a" * 65
            ctx = ai_gateway.RequestContext(gateway, principal, {}, {})
            self.assertIsNone(ctx.storage_get_bounded(path, 64))
            self.assertEqual(ctx.storage_get_bounded(path, 65), upstream.storage_body)
            self.assertEqual(gateway.storage_get(principal, path), upstream.storage_body)
            request = upstream.requests[-1]
            self.assertEqual(
                request["path"],
                f"/storage/v1/object/ppt-library/{WORKSPACE}/ai/large.bin",
            )
            self.assertEqual(request["headers"]["authorization"], "Bearer user-token")
            self.assertEqual(request["headers"]["apikey"], "anon-public")

            upstream.storage_body = b"a" * 129
            with self.assertRaisesRegex(ai_gateway.UpstreamError, "存储响应超过大小限制"):
                gateway.storage_get(principal, path)

            upstream.rest_body = b'["' + b"x" * 60 + b'"]'
            self.assertEqual(gateway.select(principal, "decks", {"select": "*"}), ["x" * 60])
            upstream.rest_body = b'["' + b"x" * 61 + b'"]'
            with self.assertRaisesRegex(ai_gateway.UpstreamError, "上游响应超过大小限制"):
                gateway.select(principal, "decks", {"select": "*"})

            upstream.storage_status = 500
            upstream.storage_body = b"e" * 65
            with self.assertRaisesRegex(ai_gateway.UpstreamError, "上游响应超过大小限制"):
                gateway.storage_get(principal, path)

    def test_bounded_storage_direct_transport_caps_success_reads(self):
        gateway = ai_gateway.Gateway(make_config(max_storage_bytes=8, max_upstream_bytes=16))
        principal = ai_gateway.Principal(USER, WORKSPACE, "reviewer", "user-token")
        ctx = ai_gateway.RequestContext(gateway, principal, {}, {})
        path = f"{WORKSPACE}/ai/封面.png"
        for headers, body, budget, expected, read_size in (
            ((('Content-Length', '4'),), b'1234', 4, b'1234', 5),
            ((('Content-Length', '5'),), b'12345', 4, None, None),
            ((), b'12', 4, b'12', 5),
            ((), b'12345678', 4, None, 5),
            ((('Content-Length', '2'),), b'12345678', 4, None, 5),
            ((('Content-Length', '+8'),), b'12', 4, b'12', 5),
            ((('Content-Length', '8'), ('Content-Length', '2')), b'12', 4, b'12', 5),
            ((('Content-Length', '8'), ('Transfer-Encoding', 'chunked')), b'12', 4, b'12', 5),
            ((), b'1234567890', 20, None, 9),
        ):
            with self.subTest(headers=headers, body=body, budget=budget):
                response_headers = Message()
                for name, value in headers:
                    response_headers[name] = value
                response = mock.MagicMock(status=200, headers=response_headers)
                response.__enter__.return_value = response
                response.read.side_effect = lambda size: body[:size]
                with mock.patch.object(gateway.transport._opener, "open", return_value=response) as open_response:
                    self.assertEqual(ctx.storage_get_bounded(path, budget), expected)
                if read_size is None:
                    response.read.assert_not_called()
                else:
                    response.read.assert_called_once_with(read_size)
                response.__exit__.assert_called_once()
                request = open_response.call_args.args[0]
                self.assertTrue(request.full_url.endswith("/" + urllib.parse.quote(path)))
                self.assertEqual(request.get_header("Authorization"), "Bearer user-token")
                self.assertEqual(request.get_header("Apikey"), "anon-public")

    def test_bounded_storage_preserves_path_checks_and_error_budget(self):
        gateway = ai_gateway.Gateway(make_config(max_storage_bytes=8, max_upstream_bytes=16))
        principal = ai_gateway.Principal(USER, WORKSPACE, "reviewer", "user-token")
        ctx = ai_gateway.RequestContext(gateway, principal, {}, {})
        path = f"{WORKSPACE}/ai/source.png"
        with mock.patch.object(gateway.transport._opener, "open") as open_response:
            for bad_path in (f"{OUTSIDER}/ai/source.png", f"{WORKSPACE}/%2e%2e/source.png"):
                with self.subTest(bad_path=bad_path), self.assertRaises(ValueError):
                    ctx.storage_get_bounded(bad_path, 1)
            open_response.assert_not_called()

        for status, error, message, body in (
            (403, PermissionError, "没有权限", b"error"),
            (404, ValueError, "文件不存在", b"error"),
            (403, ai_gateway.UpstreamError, "上游响应超过大小限制", b"e" * 17),
        ):
            with self.subTest(status=status, error=error):
                headers = Message()
                headers["Content-Length"] = str(len(body))
                source = mock.Mock()
                source.read.side_effect = lambda size: body[:size]
                with mock.patch.object(
                    gateway.transport._opener, "open",
                    side_effect=urllib.error.HTTPError("http://example.invalid", status, "error", headers, source),
                ):
                    with self.assertRaisesRegex(error, message):
                        ctx.storage_get_bounded(path, 1)
                source.read.assert_called_once_with(17)

    def test_bounded_storage_preserves_five_argument_transport_and_normal_downloads(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(method)
            return response(raw=b"12345678")

        gateway = ai_gateway.Gateway(make_config(max_storage_bytes=8), transport=transport)
        principal = ai_gateway.Principal(USER, WORKSPACE, "reviewer", "user-token")
        ctx = ai_gateway.RequestContext(gateway, principal, {}, {})
        path = f"{WORKSPACE}/ai/source.png"
        self.assertIsNone(ctx.storage_get_bounded(path, 4))
        self.assertEqual(ctx.storage_get_bounded(path, 8), b"12345678")
        self.assertEqual(ctx.storage_get(path), b"12345678")
        self.assertEqual(calls, ["GET"] * 3)

    def test_privileged_reviewer_and_model_multimodal_override(self):
        with running_gateway() as (server, transport):
            auth = {"Authorization": f"Bearer token-{USER}"}
            status, _headers, payload = http_request(server, "POST", "/api/ai/privileged", {}, auth)
            self.assertEqual(status, 200)
            self.assertEqual(payload["ok"], True)

            status, _headers, payload = http_request(server, "POST", "/api/ai/model", {}, auth)
            self.assertEqual(status, 200)
            self.assertEqual(payload["summary"], "ok")
            self.assertEqual(payload["model"], "custom-model")
            model_calls = [call for call in transport.calls if call["url"].endswith("/chat/completions")]
            self.assertEqual(len(model_calls), 1)
            self.assertEqual(model_calls[0]["url"], "https://model.example/v1/chat/completions")
            self.assertEqual(model_calls[0]["headers"]["Authorization"], "Bearer model-secret")
            request = json.loads(model_calls[0]["body"].decode("utf-8"))
            self.assertEqual(request["model"], "custom-model")
            image = request["messages"][0]["content"][1]["image_url"]["url"]
            self.assertEqual(image, "data:image/png;base64,AAAA")

    def test_model_concurrency_is_bounded(self):
        transport = FakeTransport()
        gateway = ai_gateway.Gateway(make_config(model_max_concurrency=2), transport=transport)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(gateway.chat_json, [{"role": "user", "content": "json"}]) for _ in range(6)]
            results = [future.result(timeout=3) for future in futures]
        self.assertEqual(len(results), 6)
        self.assertLessEqual(transport.max_active_model, 2)

    def test_review_worker_stop_before_lookup_does_not_claim(self):
        gateway = mock.Mock()
        gateway.config.workspace_id = WORKSPACE
        stop_event = threading.Event()
        stop_event.set()
        self.assertFalse(ai_gateway.review_worker_cycle(gateway, stop_event=stop_event))
        gateway.select.assert_not_called()

    def test_review_worker_model_limit_one_keeps_serial_alternation(self):
        gateway = ai_gateway.Gateway(make_config(model_max_concurrency=1), transport=FakeTransport())
        stop_event = threading.Event()
        calls = []

        def cycle(_gateway, *, generated_first=False, stop_event=None):
            calls.append(generated_first)
            if len(calls) == 3:
                stop_event.set()
            return True

        with mock.patch.object(ai_gateway, "review_worker_cycle", side_effect=cycle):
            self.assertEqual(
                ai_gateway._serve_review_worker_serial(
                    gateway,
                    stop_event,
                    idle_seconds=0.01,
                    error_seconds=0.01,
                ),
                0,
            )
        self.assertEqual(calls, [False, True, False])

        selected_stop = threading.Event()
        with (
            mock.patch.object(ai_gateway, "_serve_review_worker_serial", return_value=0) as serial,
            mock.patch.object(ai_gateway, "_serve_review_worker_parallel", return_value=0) as parallel,
        ):
            self.assertEqual(
                ai_gateway.serve_review_worker(gateway, stop_event=selected_stop),
                0,
            )
        self.assertIs(serial.call_args.args[0], gateway)
        self.assertIs(serial.call_args.args[1], selected_stop)
        parallel.assert_not_called()

    def test_review_worker_lanes_overlap_and_stop_prevents_next_cycle(self):
        gateway = ai_gateway.Gateway(make_config(model_max_concurrency=2), transport=FakeTransport())
        stop_event = threading.Event()
        release_uploaded = threading.Event()
        uploaded_started = threading.Event()
        uploaded_finished = threading.Event()
        generated_finished = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        def cycle(_gateway, kind, *, stop_event=None):
            with calls_lock:
                calls.append(kind)
            if kind == "uploaded":
                uploaded_started.set()
                release_uploaded.wait(2)
                uploaded_finished.set()
                return True
            generated_finished.set()
            return False

        runner = threading.Thread(
            target=ai_gateway._serve_review_worker_parallel,
            args=(gateway, stop_event),
            kwargs={"idle_seconds": 5.0, "error_seconds": 5.0},
            daemon=True,
        )
        with mock.patch.object(ai_gateway, "_review_worker_kind_cycle", side_effect=cycle):
            runner.start()
            try:
                self.assertTrue(generated_finished.wait(2))
                self.assertTrue(uploaded_started.wait(2))
                self.assertFalse(uploaded_finished.is_set())
                stop_event.set()
            finally:
                release_uploaded.set()
                stop_event.set()
                runner.join(timeout=3)
        self.assertFalse(runner.is_alive())
        with calls_lock:
            self.assertEqual(len(calls), 2)
            self.assertCountEqual(calls, ["uploaded", "generated"])

    def test_review_worker_exception_backoff_is_lane_local(self):
        gateway = ai_gateway.Gateway(make_config(model_max_concurrency=2), transport=FakeTransport())
        stop_event = threading.Event()
        release_uploaded = threading.Event()
        uploaded_started = threading.Event()
        uploaded_finished = threading.Event()
        generated_failed = threading.Event()
        calls = []
        calls_lock = threading.Lock()

        def cycle(_gateway, kind, *, stop_event=None):
            with calls_lock:
                calls.append(kind)
            if kind == "uploaded":
                uploaded_started.set()
                release_uploaded.wait(2)
                uploaded_finished.set()
                return True
            generated_failed.set()
            raise RuntimeError("lane boom")

        runner = threading.Thread(
            target=ai_gateway._serve_review_worker_parallel,
            args=(gateway, stop_event),
            kwargs={"idle_seconds": 5.0, "error_seconds": 5.0},
            daemon=True,
        )
        with mock.patch.object(ai_gateway, "_review_worker_kind_cycle", side_effect=cycle):
            runner.start()
            try:
                self.assertTrue(generated_failed.wait(2))
                self.assertTrue(uploaded_started.wait(2))
                self.assertFalse(uploaded_finished.is_set())
                stop_event.set()
            finally:
                release_uploaded.set()
                stop_event.set()
                runner.join(timeout=3)
        self.assertFalse(runner.is_alive())
        with calls_lock:
            self.assertEqual(len(calls), 2)
            self.assertCountEqual(calls, ["uploaded", "generated"])

    def test_review_worker_sigterm_handler_only_records_intent(self):
        intent = ai_gateway._ReviewWorkerStopIntent()
        stop_event = RecordingEvent()
        with stop_event._cond:
            intent.request(signal.SIGTERM, None)
            self.assertTrue(intent.is_requested())
        self.assertFalse(stop_event.is_set())
        self.assertEqual(stop_event.set_threads, [])

    def test_review_worker_sigterm_handler_drains_both_lanes_without_new_claims(self):
        gateway = ai_gateway.Gateway(make_config(model_max_concurrency=2), transport=FakeTransport())
        stop_event = RecordingEvent()
        release_attempts = threading.Event()
        both_claimed = threading.Event()
        calls = []
        finished = []
        calls_lock = threading.Lock()
        errors = []
        previous_handler = signal.getsignal(signal.SIGTERM)

        def cycle(_gateway, kind, *, stop_event=None, stop_requested=None):
            with calls_lock:
                calls.append(kind)
                if len(calls) == 2:
                    both_claimed.set()
            if not both_claimed.wait(2):
                return False
            release_attempts.wait(2)
            with calls_lock:
                finished.append(kind)
            return True

        def trigger_sigterm():
            try:
                if not both_claimed.wait(2):
                    raise AssertionError("both review lanes did not start")
                handler = signal.getsignal(signal.SIGTERM)
                if not callable(handler):
                    raise AssertionError("SIGTERM handler was not registered")
                handler(signal.SIGTERM, None)
                if not stop_event.wait(2):
                    raise AssertionError("normal worker path did not acknowledge stop intent")
            except BaseException as exc:
                errors.append(exc)
            finally:
                release_attempts.set()
                if not stop_event.is_set():
                    stop_event.set()

        driver = threading.Thread(target=trigger_sigterm, daemon=True)
        with mock.patch.object(ai_gateway, "_review_worker_kind_cycle", side_effect=cycle):
            driver.start()
            try:
                result = ai_gateway._serve_review_worker_cli(
                    gateway,
                    stop_event=stop_event,
                    signal_poll_seconds=0.01,
                )
            finally:
                release_attempts.set()
                if not stop_event.is_set():
                    stop_event.set()
                driver.join(timeout=3)
        self.assertFalse(driver.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertCountEqual(calls, list(ai_gateway.REVIEW_WORKER_LANES))
        self.assertCountEqual(finished, list(ai_gateway.REVIEW_WORKER_LANES))
        self.assertEqual(set(stop_event.set_threads), {threading.get_ident()})
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_review_worker_serial_sigterm_drains_inflight_attempt(self):
        gateway = ai_gateway.Gateway(make_config(model_max_concurrency=1), transport=FakeTransport())
        stop_event = RecordingEvent()
        started = threading.Event()
        release_attempt = threading.Event()
        calls = []
        finished = []
        errors = []
        previous_handler = signal.getsignal(signal.SIGTERM)

        def cycle(_gateway, *, generated_first=False, stop_event=None, stop_requested=None):
            calls.append(generated_first)
            started.set()
            release_attempt.wait(2)
            finished.append(generated_first)
            return True

        def trigger_sigterm():
            try:
                if not started.wait(2):
                    raise AssertionError("serial review attempt did not start")
                handler = signal.getsignal(signal.SIGTERM)
                if not callable(handler):
                    raise AssertionError("SIGTERM handler was not registered")
                handler(signal.SIGTERM, None)
                release_attempt.set()
                if not stop_event.wait(2):
                    raise AssertionError("serial worker did not drain after its attempt")
            except BaseException as exc:
                errors.append(exc)
            finally:
                release_attempt.set()
                if not stop_event.is_set():
                    stop_event.set()

        driver = threading.Thread(target=trigger_sigterm, daemon=True)
        with mock.patch.object(ai_gateway, "review_worker_cycle", side_effect=cycle):
            driver.start()
            try:
                result = ai_gateway._serve_review_worker_cli(
                    gateway,
                    stop_event=stop_event,
                    signal_poll_seconds=0.01,
                )
            finally:
                release_attempt.set()
                if not stop_event.is_set():
                    stop_event.set()
                driver.join(timeout=3)
        self.assertFalse(driver.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, 0)
        self.assertEqual(calls, [False])
        self.assertEqual(finished, [False])
        self.assertEqual(set(stop_event.set_threads), {threading.get_ident()})
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_review_worker_sigterm_intent_gates_claim_at_pre_rpc_boundary(self):
        previous_handler = signal.getsignal(signal.SIGTERM)

        def fake_gateway(concurrency):
            gateway = mock.Mock()
            gateway.config.workspace_id = WORKSPACE
            gateway.config.model_api_key = "model-secret"
            gateway.config.model_max_concurrency = concurrency
            gateway.select.return_value = [{"dedicated_judge_id": ADMIN}]
            return gateway

        def trigger_after(entered, release, stop_event):
            errors = []

            def trigger():
                try:
                    if not entered.wait(2):
                        raise AssertionError("pre-claim gap was not reached")
                    handler = signal.getsignal(signal.SIGTERM)
                    if not callable(handler):
                        raise AssertionError("SIGTERM handler was not registered")
                    handler(signal.SIGTERM, None)
                    release.set()
                    if not stop_event.wait(2):
                        raise AssertionError("worker did not acknowledge SIGTERM intent")
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    release.set()
                    if not stop_event.is_set():
                        stop_event.set()

            driver = threading.Thread(target=trigger, daemon=True)
            driver.start()
            return driver, errors

        with self.subTest(gap="workspace lookup"):
            gateway = fake_gateway(1)
            lookup_started = threading.Event()
            lookup_release = threading.Event()
            claims = []

            def select_after_signal(*_args, **_kwargs):
                lookup_started.set()
                lookup_release.wait(2)
                return [{"dedicated_judge_id": ADMIN}]

            def claim(principal, name, params, privileged=False):
                if name == "ai_review_claim_jobs":
                    claims.append(params.get("p_kind"))
                return []

            gateway.select.side_effect = select_after_signal
            gateway.rpc.side_effect = claim
            reviews = mock.Mock()
            reviews.run_review_attempt.side_effect = lambda ctx, kind: (
                ctx.rpc("ai_review_claim_jobs", {"p_kind": kind}),
                None,
            )[1]
            stop_event = RecordingEvent()
            driver, errors = trigger_after(lookup_started, lookup_release, stop_event)
            with mock.patch.object(ai_gateway.importlib, "import_module", return_value=reviews):
                try:
                    result = ai_gateway._serve_review_worker_cli(
                        gateway,
                        stop_event=stop_event,
                        signal_poll_seconds=0.01,
                    )
                finally:
                    lookup_release.set()
                    if not stop_event.is_set():
                        stop_event.set()
                    driver.join(timeout=3)
            self.assertFalse(driver.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, 0)
            self.assertEqual(claims, [])
            self.assertEqual(set(stop_event.set_threads), {threading.get_ident()})

        with self.subTest(gap="empty first lane"):
            gateway = fake_gateway(1)
            generated_preclaim = threading.Event()
            generated_release = threading.Event()
            claims = []

            def claim(principal, name, params, privileged=False):
                if name == "ai_review_claim_jobs":
                    claims.append(params.get("p_kind"))
                return []

            def run_attempt(ctx, kind):
                if kind == "generated":
                    generated_preclaim.set()
                    generated_release.wait(2)
                ctx.rpc("ai_review_claim_jobs", {"p_kind": kind})
                return None

            gateway.rpc.side_effect = claim
            reviews = mock.Mock()
            reviews.run_review_attempt.side_effect = run_attempt
            stop_event = RecordingEvent()
            driver, errors = trigger_after(generated_preclaim, generated_release, stop_event)
            with mock.patch.object(ai_gateway.importlib, "import_module", return_value=reviews):
                try:
                    result = ai_gateway._serve_review_worker_cli(
                        gateway,
                        stop_event=stop_event,
                        signal_poll_seconds=0.01,
                    )
                finally:
                    generated_release.set()
                    if not stop_event.is_set():
                        stop_event.set()
                    driver.join(timeout=3)
            self.assertFalse(driver.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, 0)
            self.assertEqual(claims, ["uploaded"])
            self.assertEqual(set(stop_event.set_threads), {threading.get_ident()})

        with self.subTest(gap="parallel pre-claim window"):
            gateway = fake_gateway(2)
            both_preclaim = threading.Event()
            release_preclaim = threading.Event()
            claims = []
            run_count = 0
            run_lock = threading.Lock()

            def claim(principal, name, params, privileged=False):
                if name == "ai_review_claim_jobs":
                    claims.append(params.get("p_kind"))
                return []

            def run_attempt(ctx, kind):
                nonlocal run_count
                with run_lock:
                    run_count += 1
                    if run_count == 2:
                        both_preclaim.set()
                if not both_preclaim.wait(2):
                    return None
                release_preclaim.wait(2)
                ctx.rpc("ai_review_claim_jobs", {"p_kind": kind})
                return None

            gateway.rpc.side_effect = claim
            reviews = mock.Mock()
            reviews.run_review_attempt.side_effect = run_attempt
            stop_event = RecordingEvent()
            driver, errors = trigger_after(both_preclaim, release_preclaim, stop_event)
            with mock.patch.object(ai_gateway.importlib, "import_module", return_value=reviews):
                try:
                    result = ai_gateway._serve_review_worker_cli(
                        gateway,
                        stop_event=stop_event,
                        signal_poll_seconds=0.01,
                    )
                finally:
                    release_preclaim.set()
                    if not stop_event.is_set():
                        stop_event.set()
                    driver.join(timeout=3)
            self.assertFalse(driver.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, 0)
            self.assertEqual(run_count, 2)
            self.assertEqual(claims, [])
            self.assertNotIn(driver.ident, stop_event.set_threads)

        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_review_worker_cli_preserves_exit_code_and_restores_handler(self):
        gateway = ai_gateway.Gateway(make_config(), transport=FakeTransport())
        previous_handler = signal.getsignal(signal.SIGTERM)

        with mock.patch.object(ai_gateway, "_run_review_worker", return_value=7) as run:
            self.assertEqual(ai_gateway._serve_review_worker_cli(gateway), 7)
            self.assertTrue(callable(run.call_args.kwargs["stop_requested"]))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

        with mock.patch.object(ai_gateway, "_run_review_worker", side_effect=RuntimeError("worker boom")):
            with self.assertRaisesRegex(RuntimeError, "worker boom"):
                ai_gateway._serve_review_worker_cli(gateway)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

    def test_review_worker_compose_stop_grace_period_is_bounded(self):
        text = (Path(__file__).resolve().parent / "cloud" / "compose.yml").read_text(encoding="utf-8")
        common, _services = text.split("services:", 1)
        self.assertNotIn("stop_grace_period", common)
        self.assertIn(
            "  gateway:\n"
            "    <<: *guanye-service\n"
            "    stop_grace_period: 20s\n",
            text,
        )
        self.assertIn(
            "  review-worker:\n"
            "    <<: *guanye-service\n"
            "    stop_grace_period: 5m\n"
            '    command: ["--review-worker"]\n',
            text,
        )

    def test_build_config_ai_gateway_is_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / "public"
            public.mkdir()
            (public / "cloud-config.js").write_text("window.CLOUD_CONFIG = null;\n", encoding="utf-8")
            base_env = {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_ANON_KEY": "sb_publishable_test",
                "WORKSPACE_ID": WORKSPACE,
            }
            site_build.build(base_env, root)
            text = (root / "dist" / "cloud-config.js").read_text(encoding="utf-8")
            self.assertNotIn("aiGatewayUrl", text)

            with_ai = dict(base_env, AI_GATEWAY_URL="https://gateway.example.com/ai/")
            site_build.build(with_ai, root)
            text = (root / "dist" / "cloud-config.js").read_text(encoding="utf-8")
            self.assertIn('"aiGatewayUrl": "https://gateway.example.com/ai"', text)
            self.assertNotIn("SERVICE_ROLE", text)

    def test_direct_file_entrypoint_starts_with_clean_pythonpath(self):
        port = free_loopback_port()
        upstream_port = free_loopback_port()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GUANYE_")
            and key not in {"SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "WORKSPACE_ID"}
        }
        env.pop("PYTHONPATH", None)
        env.update(
            {
                "GUANYE_SUPABASE_URL": f"http://127.0.0.1:{upstream_port}",
                "GUANYE_SUPABASE_ANON_KEY": "test-anon",
                "GUANYE_SUPABASE_SERVICE_ROLE_KEY": "test-service",
                "GUANYE_WORKSPACE_ID": WORKSPACE,
                "GUANYE_ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
                "GUANYE_MODEL_GATEWAY_URL": f"http://127.0.0.1:{upstream_port}/v1",
                "GUANYE_MODEL_API_KEY": "test-model",
                "GUANYE_WORKER_TOKEN": "test-worker",
                "GUANYE_HOST": "127.0.0.1",
                "GUANYE_PORT": str(port),
            }
        )
        root = Path(__file__).resolve().parent
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "cloud/ai_gateway.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        health = None
        route_status = None
        route_payload = None
        try:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    status, _headers, payload = http_request_port(port, "GET", "/healthz")
                    if status == 200:
                        health = payload
                        break
                except (ConnectionError, OSError):
                    pass
                time.sleep(0.05)
            if health is not None:
                route_status, _headers, route_payload = http_request_port(
                    port, "GET", "/api/ai/reviews?action=workspace"
                )
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                output, _stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _stderr = process.communicate(timeout=3)
        if health is None:
            self.fail(
                "direct gateway entrypoint did not become healthy; "
                f"exit={process.returncode} log={output[-4000:]}"
            )
        self.assertEqual(health.get("ok"), True)
        self.assertEqual(route_status, 401)
        self.assertEqual(route_payload.get("code"), "unauthorized")

    def test_config_model_default_and_overrides(self):
        env = {
            "GUANYE_SUPABASE_URL": "https://example.supabase.co",
            "GUANYE_SUPABASE_ANON_KEY": "anon",
            "GUANYE_SUPABASE_SERVICE_ROLE_KEY": "service",
            "GUANYE_WORKSPACE_ID": WORKSPACE,
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = ai_gateway.GatewayConfig.from_env()
            self.assertEqual(config.model_gateway_url, ai_gateway.DEFAULT_MODEL_GATEWAY_URL)
            self.assertEqual(config.model_id, "gpt-6.2-sol")
            self.assertEqual(config.max_body_bytes, 160 * 1024 * 1024)

        override = dict(
            env,
            GUANYE_MODEL_GATEWAY_URL="https://models.example/v1",
            GUANYE_MODEL_ID="gpt-6.1-sol",
            GUANYE_WORKER_TOKEN="worker-secret",
        )
        with mock.patch.dict(os.environ, override, clear=True):
            config = ai_gateway.GatewayConfig.from_env()
            self.assertEqual(config.model_gateway_url, "https://models.example/v1")
            self.assertEqual(config.model_id, "gpt-6.1-sol")
            self.assertEqual(config.worker_token, "worker-secret")
            self.assertNotIn("worker-secret", repr(config))


if __name__ == "__main__":
    unittest.main(verbosity=2)
