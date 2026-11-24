import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import assert from "node:assert/strict";
import { test } from "node:test";
import { createHandler } from "./index.ts";

const PROJECT = "https://project.example.invalid";
const ROUTE = "/functions/v1/single-judge-image-worker";
const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const PAGE = "33333333-3333-4333-8333-333333333333";
const LEASE = "44444444-4444-4444-8444-444444444444";
const JOB = "55555555-5555-4555-8555-555555555555";
const ACTOR = "66666666-6666-4666-8666-666666666666";
const DECK = `generated-${JOB}`;
const WORKER = "a".repeat(32);
const JWT = "fixture.user.jwt";
const SIGNED = "fixture.upload.token";
const PRIVATE_ERROR = "fixture-upstream-secret-that-must-not-be-returned";
const SERVICE_KEY = "fixture-service-key";
const PATH = `${WORKSPACE}/${DECK}/${PAGE}.png`;
const TYPE = "image/png";
const IMAGE = Uint8Array.from(Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6i8AAAAASUVORK5CYII=", "base64",
));
const hash = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");
const TARGET = { storagePath: PATH, contentType: TYPE, alreadySucceeded: false };
const JOB_ROW = { id: JOB, pages: [{ id: PAGE, status: "succeeded", error: null }], status: "completed" };
const CLAIM = { action: "claim", worker_id: "本地-worker-01", limit: 16, lease_seconds: 1800 };
const BASE = { page_id: PAGE, lease_token: LEASE };
const COMPLETE = { ...BASE, action: "complete", content_type: TYPE, content_sha256: hash(IMAGE), width: 1, height: 1 };
const FAILURE = { ...BASE, action: "fail", error: PRIVATE_ERROR };
const ISSUE = { ...BASE, action: "issue-upload" };
const REFERENCE = { ...BASE, action: "reference" };
const LEASE_RPC = { p_workspace: WORKSPACE, p_actor: ACTOR, p_page: PAGE, p_lease_token: LEASE };
const ok = (data: unknown) => ({ data, error: null });
const problem = (code: string, message: string, status?: number) => ({ data: null, error: { code, message, status } });

function imageResponse(bytes = IMAGE, headers?: HeadersInit) {
  return new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(bytes.subarray(0, 2));
      controller.enqueue(bytes.subarray(2, 9));
      controller.enqueue(bytes.subarray(9));
      controller.close();
    },
  }), { headers });
}

function harness() {
  const calls: Array<[string, ...unknown[]]> = [];
  const rpc = new Map<string, ReturnType<typeof ok> | ReturnType<typeof problem> | Error>();
  rpc.set("ai_generation_claim_pages", ok([]));
  rpc.set("ai_generation_heartbeat", ok({ ok: true, leaseExpiresAt: "fixture-expiration" }));
  rpc.set("ai_generation_page_reference_path", ok(null));
  rpc.set("ai_generation_page_upload_target", ok(TARGET));
  rpc.set("ai_generation_page_succeed", ok(JOB_ROW));
  rpc.set("ai_generation_page_fail", ok(JOB_ROW));
  const state = {
    auth: ok({ user: { id: ACTOR } }) as ReturnType<typeof ok> | ReturnType<typeof problem>,
    access: ok({ mode: "single-judge", role: "judge" }) as ReturnType<typeof ok> | ReturnType<typeof problem>,
    storage: () => imageResponse(IMAGE, { "content-length": `${IMAGE.length}`, "content-type": TYPE }),
    sign: ok({ path: PATH, token: SIGNED, signedUrl: "https://never-return.invalid" }) as ReturnType<typeof ok> | ReturnType<typeof problem>,
    config: {
      GUANYE_WORKSPACE_ID: WORKSPACE, SUPABASE_URL: PROJECT, GUANYE_IMAGE_WORKER_TOKEN: WORKER,
      SUPABASE_ANON_KEY: "fixture-anon-key", SUPABASE_SERVICE_ROLE_KEY: SERVICE_KEY,
    } as Record<string, string>,
  };
  const handle = createHandler({
    env: (name) => state.config[name],
    async user(config, jwt) {
      calls.push(["user", config, jwt]);
      return {
        auth: { async getUser(arg) { calls.push(["auth", arg]); return state.auth; } },
        async rpc(name, args) { calls.push([name, args]); return state.access; },
      };
    },
    async service(config) {
      calls.push(["service", config]);
      return {
        async rpc(name, args) {
          calls.push([name, args]);
          const answer = rpc.get(name);
          assert.ok(answer, `unexpected RPC: ${name}`);
          if (answer instanceof Error) throw answer;
          return answer;
        },
        storage: { from(bucket) { return {
          async createSignedUploadUrl(path, options) {
            calls.push(["sign", bucket, path, options]);
            return state.sign;
          },
        }; } },
      };
    },
    async fetch(url, options) {
      calls.push(["storage", String(url), options]);
      return state.storage();
    },
  });
  const headers = { "content-type": "application/json", authorization: `Bearer ${JWT}`, "X-Guanye-Worker-Token": WORKER };
  function request(command: unknown, init: RequestInit = {}, route = ROUTE) {
    const body = typeof command === "string" ? command : JSON.stringify(command);
    return handle(new Request(`${PROJECT}${route}`, { method: "POST", headers, body, ...init }));
  }
  return { calls, rpc, state, request, headers };
}

async function failure(res: Response, status: number, code: string) {
  assert.equal(res.status, status);
  assert.equal(res.headers.get("cache-control"), "no-store");
  const body = await res.json();
  assert.equal(body.ok, false);
  assert.equal(body.code, code);
  for (const secret of [PRIVATE_ERROR, SERVICE_KEY, WORKER, JWT, SIGNED]) {
    assert.ok(!JSON.stringify(body).includes(secret));
  }
  return body;
}

function names(calls: ReturnType<typeof harness>["calls"]) {
  return calls.map(([name]) => name);
}

test("only the two fixed POST routes, no CORS and no query extension", async () => {
  const h = harness();
  for (const route of [ROUTE, "/single-judge-image-worker"]) {
    const res = await h.request(CLAIM, {}, route);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("access-control-allow-origin"), null);
    assert.deepEqual(await res.json(), { pages: [] });
  }
  for (const path of [`${ROUTE}/claim`, `${ROUTE}?workspace=${OTHER}`, `/v2${ROUTE}`, `${ROUTE}/`]) {
    await failure(await h.request(CLAIM, {}, path), 404, "NOT_FOUND");
  }
  const preflight = await h.request(CLAIM, { method: "OPTIONS", body: null });
  assert.equal(preflight.headers.get("allow"), "POST");
  await failure(preflight, 405, "METHOD_NOT_ALLOWED");
});

test("missing/malformed JWT is refreshable; wrong or missing worker token never reaches Auth", async () => {
  const h = harness();
  for (const authorization of ["", "not-bearer", "Bearer invalid"]) {
    const body = await failure(await h.request(CLAIM, { headers: { ...h.headers, authorization } }), 401, "AUTH_INVALID");
    assert.equal(body.refreshable, true);
  }
  for (const token of ["", "b".repeat(32), "a".repeat(31), "a".repeat(33)]) {
    await failure(await h.request(CLAIM, { headers: { ...h.headers, "X-Guanye-Worker-Token": token } }), 403, "WORKER_TOKEN_INVALID");
  }
  assert.deepEqual(h.calls, []);
});

test("every request checks Auth and fixed-workspace membership; service is never used for other roles", async () => {
  const h = harness();
  for (const role of [null, { role: "judge" }, { mode: "single-judge", role: "admin" }, { mode: "single-judge", role: "submitter" }]) {
    h.state.access = ok(role);
    await failure(await h.request(CLAIM), 403, "ACCESS_DENIED");
  }
  h.state.auth = problem("bad_jwt", PRIVATE_ERROR, 401);
  await failure(await h.request(CLAIM), 401, "AUTH_INVALID");
  h.state.auth = problem("unexpected", PRIVATE_ERROR, 503);
  await failure(await h.request(CLAIM), 503, "UPSTREAM_UNAVAILABLE");
  assert.equal(h.calls.filter(([name]) => name === "auth").length, 6);
  assert.equal(h.calls.filter(([name]) => name === "single_judge_access").length, 4);
  assert.ok(!names(h.calls).includes("service"));
  assert.deepEqual(h.calls[0], ["user", { url: PROJECT, key: "fixture-anon-key" }, JWT]);
  assert.deepEqual(h.calls[1], ["auth", JWT]);
  assert.deepEqual(h.calls[2], ["single_judge_access", { w: WORKSPACE }]);
});

test("commands reject extra fields, unknown actions, duplicates, incorrect types and content limits", async () => {
  const h = harness();
  for (const action of [CLAIM, ISSUE, COMPLETE, REFERENCE, FAILURE, { ...BASE, action: "heartbeat", lease_seconds: 60 }]) {
    for (const key of ["workspace", "workspace_id", "actor", "rpc", "path", "storagePath", "url", "image_base64"]) {
      await failure(await h.request({ ...action, [key]: OTHER }), 400, "INVALID_REQUEST");
    }
  }
  for (const input of [
    { ...REFERENCE, action: "list" }, { ...REFERENCE, page_id: "not-uuid" }, { ...ISSUE, lease_token: "not-uuid" },
    { ...CLAIM, worker_id: "\nnode" }, { ...CLAIM, worker_id: " " }, { ...CLAIM, limit: 33 },
    { ...CLAIM, limit: "1" }, { ...CLAIM, lease_seconds: 59 }, { ...CLAIM, lease_seconds: 7201 },
    { ...COMPLETE, content_type: "image/svg+xml" }, { ...COMPLETE, content_sha256: "z".repeat(64) },
    { ...COMPLETE, width: 1.5 }, { ...COMPLETE, height: 2147483648 }, { ...COMPLETE, width: undefined },
    { ...FAILURE, error: {} }, { ...FAILURE, error: "字".repeat(2001) },
    [], "{bad-json", '{"action":"other","act\\u0069on":"claim","worker_id":"a","limit":1,"lease_seconds":60}',
  ]) await failure(await h.request(input), 400, "INVALID_REQUEST");
  await failure(await h.request(CLAIM, { headers: { ...h.headers, "content-type": "text/plain" } }), 415, "JSON_REQUIRED");
  await failure(await h.request(" ".repeat(40000)), 413, "REQUEST_TOO_LARGE");
  await failure(await h.request(CLAIM, { body: Uint8Array.of(0xff) }), 400, "INVALID_REQUEST");
  assert.deepEqual(h.calls, []);
});

test("claim keeps M25 rows unchanged and uses fixed workspace/actor, no retry on transport failure", async () => {
  const h = harness();
  const row = { jobId: JOB, pageId: PAGE, leaseToken: LEASE, hasReferenceImage: false, references: [{ deckId: DECK }], storagePath: PATH };
  h.rpc.set("ai_generation_claim_pages", ok([row]));
  const before = await h.request(CLAIM);
  assert.deepEqual(await before.json(), { pages: [row] });
  assert.deepEqual(h.calls.at(-1), ["ai_generation_claim_pages", {
    p_workspace: WORKSPACE, p_actor: ACTOR, p_worker_id: CLAIM.worker_id, p_limit: 16, p_lease_seconds: 1800,
  }]);
  h.calls.length = 0;
  h.rpc.set("ai_generation_claim_pages", new Error(PRIVATE_ERROR));
  const body = await failure(await h.request(CLAIM), 503, "UPSTREAM_UNAVAILABLE");
  assert.equal(body.retryable, true);
  assert.equal(h.calls.filter(([name]) => name === "ai_generation_claim_pages").length, 1);
});

test("cover claim is explicit, limited to twenty and never falls back to the standard queue", async () => {
  const h = harness();
  const cover = { ...CLAIM, cover_training: true, limit: 20 };
  for (const body of [{ ...cover, cover_training: false }, { ...cover, cover_training: "true" }, { ...cover, limit: 21 }]) {
    await failure(await h.request(body), 400, "INVALID_REQUEST");
  }
  assert.deepEqual(h.calls, []);
  const row = { jobId: JOB, pageId: PAGE, leaseToken: LEASE, coverTraining: true };
  h.rpc.set("single_judge_cover_claim_pages", ok([row]));
  assert.deepEqual(await (await h.request(cover)).json(), { pages: [row] });
  assert.deepEqual(h.calls.at(-1), ["single_judge_cover_claim_pages", {
    p_workspace: WORKSPACE, p_actor: ACTOR, p_worker_id: CLAIM.worker_id, p_limit: 20, p_lease_seconds: 1800,
  }]);
  h.calls.length = 0;
  h.rpc.set("single_judge_cover_claim_pages", problem("PGRST202", PRIVATE_ERROR));
  await failure(await h.request(cover), 503, "UPSTREAM_UNAVAILABLE");
  assert.equal(h.calls.filter(([name]) => name === "single_judge_cover_claim_pages").length, 1);
  assert.equal(h.calls.filter(([name]) => name === "ai_generation_claim_pages").length, 0);
});

test("heartbeat projects only ok and maps missing page / expired lease / SQL conflicts", async () => {
  const h = harness();
  const cmd = { ...BASE, action: "heartbeat", lease_seconds: 7200 };
  assert.deepEqual(await (await h.request(cmd)).json(), { ok: true });
  assert.deepEqual(h.calls.at(-1), ["ai_generation_heartbeat", { ...LEASE_RPC, p_lease_seconds: 7200 }]);
  for (const [sqlstate, message, status, code] of [
    ["P0001", "页面租约已失效", 409, "LEASE_LOST"], ["P0001", "页面租约已过期", 409, "LEASE_LOST"],
    ["P0001", "生成页面不存在", 404, "PAGE_NOT_FOUND"], ["42501", PRIVATE_ERROR, 403, "ACCESS_DENIED"],
    ["23505", PRIVATE_ERROR, 409, "STATE_CONFLICT"], ["40001", PRIVATE_ERROR, 503, "UPSTREAM_UNAVAILABLE"],
    ["P0001", PRIVATE_ERROR, 503, "UPSTREAM_UNAVAILABLE"],
  ] as const) {
    h.rpc.set("ai_generation_heartbeat", problem(sqlstate, message));
    await failure(await h.request(cmd), status, code);
  }
});

test("reference only uses lease-checked reference RPC and returns private path and MIME hint", async () => {
  const h = harness();
  await failure(await h.request(REFERENCE), 404, "REFERENCE_NOT_FOUND");
  for (const [storagePath, contentType] of [
    [`${WORKSPACE}/${DECK}/${PAGE}.jpg`, "image/jpeg"], [`${WORKSPACE}/${DECK}/preview/001.png`, "image/png"],
    [`${WORKSPACE}/ai-planning/${JOB}/style-rendered/图一.webp`, "image/webp"],
  ]) {
    h.rpc.set("ai_generation_page_reference_path", ok(storagePath));
    assert.deepEqual(await (await h.request(REFERENCE)).json(), { storagePath, contentType });
    assert.deepEqual(h.calls.at(-1), ["ai_generation_page_reference_path", LEASE_RPC]);
  }
  for (const value of [
    `${OTHER}/${DECK}/${PAGE}.png`, `${WORKSPACE}/${DECK}/original/a.png`, `https://host/${PATH}`,
    `${WORKSPACE}/${DECK}/preview/%2f.png`, `${WORKSPACE}/${DECK}/preview/a.png?sig=x`, `${WORKSPACE}/${DECK}/../a.png`,
    `${WORKSPACE}/ai-planning/${JOB}/style-rendered/b.pdf`, `${WORKSPACE}/${DECK}/a.png\n`,
  ]) {
    h.rpc.set("ai_generation_page_reference_path", ok(value));
    await failure(await h.request(REFERENCE), 502, "UPSTREAM_RESPONSE_INVALID");
  }
  h.rpc.set("ai_generation_page_reference_path", problem("P0001", "页面租约已过期"));
  await failure(await h.request(REFERENCE), 409, "LEASE_LOST");
  assert.ok(!names(h.calls).some((name) => name === "sign" || name === "storage"));
});

test("issue-upload signs exactly one fixed object, without upsert or a URL", async () => {
  const h = harness();
  const res = await h.request(ISSUE);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { storagePath: PATH, contentType: TYPE, uploadToken: SIGNED });
  assert.deepEqual(h.calls.slice(-2), [
    ["ai_generation_page_upload_target", LEASE_RPC], ["sign", "ppt-library", PATH, { upsert: false }],
  ]);
});

test("issue/complete reject cross-workspace, wrong page, reserved scope, MIME extension and unsafe target", async () => {
  const h = harness();
  for (const storagePath of [
    `${OTHER}/${DECK}/${PAGE}.png`, `${WORKSPACE}/${DECK}/${OTHER}.png`, `${WORKSPACE}/${DECK}/${PAGE}.webp`,
    `${WORKSPACE}/../${PAGE}.png`, `${WORKSPACE}/ai-planning/${PAGE}.png`,
    `${WORKSPACE}/${DECK}/preview/${PAGE}.png`, `${WORKSPACE}/${DECK}/${PAGE}.png?url=x`,
  ]) {
    h.rpc.set("ai_generation_page_upload_target", ok({ ...TARGET, storagePath }));
    for (const cmd of [ISSUE, COMPLETE]) await failure(await h.request(cmd), 502, "UPSTREAM_RESPONSE_INVALID");
  }
  h.rpc.set("ai_generation_page_upload_target", problem("P0001", "页面租约已失效"));
  for (const cmd of [ISSUE, COMPLETE, FAILURE]) await failure(await h.request(cmd), 409, "LEASE_LOST");
  assert.ok(!names(h.calls).some((name) => name === "sign" || name === "storage" || name === "ai_generation_page_succeed" || name === "ai_generation_page_fail"));
});

test("issue-upload never exposes misbound, malformed or failed signer result", async () => {
  const h = harness();
  for (const value of [{ path: PATH, token: "not a token" }, { token: SIGNED }, { path: `${OTHER}/image.png`, token: SIGNED }]) {
    h.state.sign = ok(value);
    await failure(await h.request(ISSUE), 502, "UPSTREAM_RESPONSE_INVALID");
  }
  h.state.sign = problem("sign_error", PRIVATE_ERROR);
  await failure(await h.request(ISSUE), 503, "UPSTREAM_UNAVAILABLE");
});

test("complete fetches and verifies bytes before succeed, including same-content completed retry", async () => {
  const h = harness();
  assert.deepEqual(await (await h.request(COMPLETE)).json(), JOB_ROW);
  assert.deepEqual(names(h.calls).slice(-3), ["ai_generation_page_upload_target", "storage", "ai_generation_page_succeed"]);
  const [, url, options] = h.calls.at(-2)!;
  assert.equal(url, `${PROJECT}/storage/v1/object/ppt-library/${PATH}`);
  assert.equal((options as RequestInit).redirect, "error");
  assert.equal(new Headers((options as RequestInit).headers).get("authorization"), `Bearer ${SERVICE_KEY}`);
  assert.deepEqual(h.calls.at(-1), ["ai_generation_page_succeed", {
    ...LEASE_RPC, p_storage_path: PATH, p_content_type: TYPE, p_content_sha256: hash(IMAGE), p_width: 1, p_height: 1,
  }]);
  h.calls.length = 0;
  h.rpc.set("ai_generation_page_upload_target", ok({ ...TARGET, alreadySucceeded: true }));
  await failure(await h.request(ISSUE), 409, "ALREADY_SUCCEEDED");
  const retry = await h.request({ ...COMPLETE, content_sha256: hash(IMAGE).toUpperCase(), width: null, height: null });
  assert.deepEqual(await retry.json(), JOB_ROW);
  assert.ok(!names(h.calls).includes("sign"));
  assert.equal((h.calls.at(-1)![1] as { p_width: unknown }).p_width, null);
  h.rpc.set("ai_generation_page_succeed", problem("P0001", "页面已完成且内容不一致"));
  await failure(await h.request(COMPLETE), 409, "CONTENT_CONFLICT");
  h.rpc.set("ai_generation_page_succeed", problem("P0001", "页面租约已过期"));
  await failure(await h.request(COMPLETE), 409, "LEASE_LOST");
});

test("complete validates PNG/JPEG/WebP by bytes, not by metadata; wrong hash / MIME never succeeds", async () => {
  const h = harness();
  const jpg = Uint8Array.of(0xff, 0xd8, 0xff, 0xe0, 0x01);
  const webp = Uint8Array.from(Buffer.from("RIFF0000WEBPVP8 ", "ascii"));
  for (const [type, ext, bytes] of [["image/jpeg", "jpg", jpg], ["image/webp", "webp", webp]] as const) {
    h.rpc.set("ai_generation_page_upload_target", ok({ ...TARGET, storagePath: `${WORKSPACE}/${DECK}/${PAGE}.${ext}`, contentType: type }));
    h.state.storage = () => imageResponse(bytes, { "content-type": "application/octet-stream", "x-meta-sha256": "garbage" });
    assert.deepEqual(await (await h.request({ ...COMPLETE, content_type: type, content_sha256: hash(bytes) })).json(), JOB_ROW);
  }
  h.calls.length = 0;
  h.rpc.set("ai_generation_page_upload_target", ok(TARGET));
  h.state.storage = () => imageResponse(jpg, { "content-type": TYPE, "x-meta-sha256": hash(jpg) });
  await failure(await h.request({ ...COMPLETE, content_sha256: hash(jpg) }), 422, "IMAGE_INVALID");
  h.state.storage = () => imageResponse(IMAGE, { "x-meta-sha256": "f".repeat(64) });
  await failure(await h.request({ ...COMPLETE, content_sha256: "f".repeat(64) }), 409, "CONTENT_CONFLICT");
  const storageCalls = names(h.calls).filter((name) => name === "storage").length;
  await failure(await h.request({ ...COMPLETE, content_type: "image/jpeg" }), 422, "IMAGE_INVALID");
  assert.equal(names(h.calls).filter((name) => name === "storage").length, storageCalls);
  assert.ok(!names(h.calls).includes("ai_generation_page_succeed"));
});

test("complete fails closed on unavailable, partial, truncated and oversize object streams", async () => {
  const h = harness();
  const limit = 25 * 1024 * 1024;
  for (const [storage, status, code] of [
    [() => new Response(PRIVATE_ERROR, { status: 404 }), 409, "UPLOAD_NOT_FOUND"],
    [() => new Response(PRIVATE_ERROR, { status: 503 }), 503, "UPSTREAM_UNAVAILABLE"],
    [() => new Response(IMAGE, { status: 206 }), 503, "UPSTREAM_UNAVAILABLE"],
    [() => imageResponse(IMAGE, { "content-encoding": "gzip" }), 503, "UPSTREAM_UNAVAILABLE"],
    [() => imageResponse(IMAGE, { "content-length": `${IMAGE.length + 1}` }), 503, "UPSTREAM_UNAVAILABLE"],
    [() => imageResponse(IMAGE, { "content-length": `${limit + 1}` }), 422, "IMAGE_INVALID"],
    [() => { throw new Error(PRIVATE_ERROR); }, 503, "UPSTREAM_UNAVAILABLE"],
  ] as const) {
    h.state.storage = storage;
    await failure(await h.request(COMPLETE), status, code);
  }
  const chunk = new Uint8Array(1024 * 1024);
  let chunks = 0;
  let cancelled = false;
  h.state.storage = () => new Response(new ReadableStream({
    pull(controller) { controller.enqueue(chunks++ ? chunk : IMAGE); },
    cancel() { cancelled = true; },
  }));
  await failure(await h.request(COMPLETE), 422, "IMAGE_INVALID");
  assert.ok(cancelled);
  assert.ok(chunks <= 27);
  assert.ok(!names(h.calls).includes("ai_generation_page_succeed"));
});

test("failure prechecks live lease, preserves job shape and does not persist or reflect free-form diagnostics", async () => {
  const h = harness();
  h.rpc.set("ai_generation_page_fail", ok({ ...JOB_ROW, status: "failed", pages: [{ id: PAGE, error: PRIVATE_ERROR }] }));
  assert.deepEqual(await (await h.request(FAILURE)).json(), {
    ...JOB_ROW, status: "failed", pages: [{ id: PAGE, error: "页面生成失败" }],
  });
  assert.deepEqual(h.calls.slice(-2), [
    ["ai_generation_page_upload_target", LEASE_RPC], ["ai_generation_page_fail", { ...LEASE_RPC, p_error: "页面生成失败" }],
  ]);
  h.calls.length = 0;
  h.rpc.set("ai_generation_page_upload_target", problem("P0001", "页面租约已过期"));
  await failure(await h.request(FAILURE), 409, "LEASE_LOST");
  assert.ok(!names(h.calls).includes("ai_generation_page_fail"));
});
