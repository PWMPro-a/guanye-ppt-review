import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createHandler } from "./index.ts";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222";
const DECK_ID = "import-33333333-3333-4333-8333-333333333333";
const USER_ID = "44444444-4444-4444-8444-444444444444";
const JWT = "header.payload.signature";
const API_KEY = "sb_publishable_test";
const SERVICE_KEY = "service-role-secret-for-test";
const ORIGINAL_BYTES = new Uint8Array(512);
const SHA256 = createHash("sha256").update(ORIGINAL_BYTES).digest("hex");
const PREVIEW_BYTES = [
  new Uint8Array(1024).fill(2),
  new Uint8Array(2048).fill(3),
];
const PREVIEW_HASHES = PREVIEW_BYTES.map((bytes) =>
  createHash("sha256").update(bytes).digest("hex")
);
const MIB = 1024 * 1024;

interface HarnessOptions {
  role?: "judge" | "admin" | "submitter" | null;
  userId?: string;
  objects?: Map<string, number>;
  wrongOriginalBytes?: boolean;
  previewBytes?: Uint8Array[];
  infoShape?: "modern" | "legacy" | "invalid-modern";
  accessError?: Record<string, unknown>;
  publishResults?: unknown[];
  publishError?: Record<string, unknown>;
  signedError?: Record<string, unknown>;
  infoError?: Record<string, unknown>;
}

function syntheticPageHashes(length: number): string[] {
  return Array.from({ length }, (_, index) =>
    createHash("sha256").update(`preview-${index + 1}`).digest("hex")
  );
}

function validBody(overrides: Record<string, unknown> = {}) {
  const body: Record<string, unknown> = {
    action: "prepare",
    aiAllowed: true,
    workspaceId: WORKSPACE_ID,
    deckId: DECK_ID,
    title: "季度复盘",
    original: {
      name: "资料.pptx",
      sha256: SHA256,
      sizeBytes: 512,
    },
    pageSizes: [1024, 2048],
    pageHashes: PREVIEW_HASHES,
    ...overrides,
  };
  if (
    !Object.hasOwn(overrides, "pageHashes") &&
    Array.isArray(body.pageSizes) &&
    (body.pageSizes.length !== PREVIEW_HASHES.length ||
      body.pageSizes[0] !== 1024 ||
      body.pageSizes[1] !== 2048)
  ) {
    body.pageHashes = syntheticPageHashes(body.pageSizes.length);
  }
  return body;
}

function makeRequest(
  body: unknown,
  options: { jwt?: string | null; apiKey?: string | null; method?: string } = {},
) {
  const jwt = options.jwt === undefined ? JWT : options.jwt;
  const apiKey = options.apiKey === undefined ? API_KEY : options.apiKey;
  const headers = new Headers({ "content-type": "application/json; charset=utf-8" });
  if (jwt !== null) headers.set("authorization", `Bearer ${jwt}`);
  if (apiKey !== null) headers.set("apikey", apiKey);
  return new Request("https://example.supabase.co/functions/v1/single-judge-import", {
    method: options.method ?? "POST",
    headers,
    body: options.method === "GET" ? undefined : JSON.stringify(body),
  });
}

async function responseBody(response: Response) {
  return await response.json();
}

function makeHarness(options: HarnessOptions = {}) {
  const calls: string[] = [];
  const userOperations: string[] = [];
  const serviceOperations: string[] = [];
  const signedCalls: Array<{ bucket: string; path: string; options: unknown }> = [];
  const serviceRpcCalls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const objects = options.objects ?? new Map<string, number>();
  const role = options.role === undefined ? "judge" : options.role;
  let authJwt: string | null = null;
  let userConfig: Record<string, unknown> | null = null;
  let serviceConfig: Record<string, unknown> | null = null;
  let signedIndex = 0;
  let publishIndex = 0;

  const userClient = {
    auth: {
      async getUser(jwt?: string) {
        userOperations.push("auth.getUser");
        calls.push("auth.getUser");
        authJwt = jwt ?? null;
        return {
          data: { user: { id: options.userId ?? USER_ID } },
          error: null,
        };
      },
    },
    storage: {
      from(bucket: string) {
        userOperations.push(`storage.from:${bucket}`);
        throw new Error("用户客户端不得访问存储");
      },
    },
    async rpc(name: string, args?: Record<string, unknown>) {
      userOperations.push(`rpc:${name}`);
      calls.push(`user.rpc:${name}`);
      if (options.accessError) {
        return { data: null, error: options.accessError };
      }
      return {
        data: role === null ? null : { mode: "single-judge", role },
        error: null,
      };
    },
  };

  const serviceClient = {
    auth: {
      async getUser() {
        serviceOperations.push("auth.getUser");
        throw new Error("服务客户端不得查询用户");
      },
    },
    storage: {
      from(bucket: string) {
        serviceOperations.push(`storage.from:${bucket}`);
        return {
          async info(path: string) {
            serviceOperations.push("storage.info");
            if (options.infoError) return { data: null, error: options.infoError };
            const size = objects.get(path);
            if (size === undefined) {
              return { data: null, error: { status: 404, message: "Object not found" } };
            }
            const data = options.infoShape === "legacy" ? { metadata: { size } }
              : options.infoShape === "invalid-modern" ? { size: -1, metadata: { size } }
              : { size, id: "storage-object", bucketId: "ppt-library" };
            return { data, error: null };
          },
          async download(path: string) {
            serviceOperations.push("storage.download");
            if (path.endsWith(`/original/${SHA256}.pptx`)) {
              assert.equal(objects.get(path), 512);
              const data = options.wrongOriginalBytes ? new Uint8Array(512).fill(1) : ORIGINAL_BYTES;
              return { data: new Blob([data]), error: null };
            }
            const match = path.match(/\/preview\/(\d+)\.png$/);
            assert.ok(match);
            const previewIndex = Number(match[1]) - 1;
            const data = (options.previewBytes ?? PREVIEW_BYTES)[previewIndex];
            assert.ok(data);
            assert.equal(objects.get(path), data.byteLength);
            return { data: new Blob([data]), error: null };
          },
          async createSignedUploadUrl(path: string, uploadOptions?: unknown) {
            serviceOperations.push("storage.createSignedUploadUrl");
            signedCalls.push({ bucket, path, options: uploadOptions });
            if (options.signedError) return { data: null, error: options.signedError };
            signedIndex += 1;
            return {
              data: { path, token: `upload-token-${signedIndex}` },
              error: null,
            };
          },
        };
      },
    },
    async rpc(name: string, args?: Record<string, unknown>) {
      serviceOperations.push(`rpc:${name}`);
      serviceRpcCalls.push({ name, args: args ?? {} });
      if (options.publishError) {
        return { data: null, error: options.publishError };
      }
      const fallback = {
        ok: true,
        deckId: DECK_ID,
        deckVersion: "deck-version-1",
        idempotent: false,
        preEvaluation: { queued: true },
      };
      const results = options.publishResults ?? [fallback];
      const index = Math.min(publishIndex, results.length - 1);
      publishIndex += 1;
      return { data: results[index], error: null };
    },
  };

  const deps = {
    readSupabaseUrl() {
      calls.push("read.supabaseUrl");
      return "https://example.supabase.co";
    },
    readServiceRoleKey() {
      calls.push("read.serviceRoleKey");
      return SERVICE_KEY;
    },
    async createUserClient(config: Record<string, unknown>) {
      calls.push("createUserClient");
      userConfig = { ...config };
      return userClient;
    },
    async createServiceClient(config: Record<string, unknown>) {
      calls.push("createServiceClient");
      serviceConfig = { ...config };
      return serviceClient;
    },
  };

  return {
    deps,
    calls,
    userOperations,
    serviceOperations,
    signedCalls,
    serviceRpcCalls,
    objects,
    get authJwt() {
      return authJwt;
    },
    get userConfig() {
      return userConfig;
    },
    get serviceConfig() {
      return serviceConfig;
    },
  };
}

function seedObjects(sizes: number[]) {
  const objects = new Map<string, number>();
  const originalPath = `${WORKSPACE_ID}/${DECK_ID}/original/${SHA256}.pptx`;
  objects.set(originalPath, sizes[0]);
  sizes.slice(1).forEach((size, index) => {
    objects.set(`${WORKSPACE_ID}/${DECK_ID}/preview/${index + 1}.png`, size);
  });
  return objects;
}

test("无 JWT 拒绝且不创建任何 Supabase 客户端", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(makeRequest(validBody(), { jwt: null }));
  const body = await responseBody(response);

  assert.equal(response.status, 401);
  assert.equal(body.error.code, "AUTH_REQUIRED");
  assert.deepEqual(harness.calls, []);
});

test("管理员可以使用 prepare 提交素材", async () => {
  const harness = makeHarness({ role: "admin" });
  const response = await createHandler(harness.deps)(makeRequest(validBody()));
  const body = await responseBody(response);

  assert.equal(response.status, 200);
  assert.deepEqual(body.uploads.map((item: { path: string }) => item.path), [
    `${WORKSPACE_ID}/${DECK_ID}/original/${SHA256}.pptx`,
    `${WORKSPACE_ID}/${DECK_ID}/preview/1.png`,
    `${WORKSPACE_ID}/${DECK_ID}/preview/2.png`,
  ]);
  assert.deepEqual(harness.calls.slice(0, 6), [
    "read.supabaseUrl",
    "createUserClient",
    "auth.getUser",
    "user.rpc:single_judge_access",
    "read.serviceRoleKey",
    "createServiceClient",
  ]);
  assert.equal(harness.authJwt, JWT);
  assert.equal(harness.userConfig?.jwt, JWT);
  assert.equal(harness.userConfig?.apiKey, API_KEY);
  assert.equal(harness.serviceConfig?.apiKey, SERVICE_KEY);
});

test("陌生工作区拒绝且绝不读取 service role key", async () => {
  const harness = makeHarness({ role: null });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ workspaceId: OTHER_WORKSPACE_ID })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 403);
  assert.equal(body.error.code, "IMPORT_ACCESS_REQUIRED");
  assert.equal(harness.calls.includes("read.serviceRoleKey"), false);
  assert.equal(harness.calls.includes("createServiceClient"), false);
});

test("未知顶层字段与客户端 path 均被拒绝", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ path: "attacker/chosen/path" })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 400);
  assert.equal(body.error.code, "UNKNOWN_FIELD");
  assert.deepEqual(harness.calls, []);

  const actorHarness = makeHarness();
  const actorResponse = await createHandler(actorHarness.deps)(
    makeRequest(validBody({ actor: "attacker-supplied-actor" })),
  );
  const actorBody = await responseBody(actorResponse);
  assert.equal(actorResponse.status, 400);
  assert.equal(actorBody.error.code, "UNKNOWN_FIELD");
  assert.deepEqual(actorHarness.calls, []);

  const nestedHarness = makeHarness();
  const nestedResponse = await createHandler(nestedHarness.deps)(
    makeRequest(
      validBody({
        original: {
          name: "资料.pptx",
          sha256: SHA256,
          sizeBytes: 512,
          path: "attacker/chosen/original.pptx",
        },
      }),
    ),
  );
  const nestedBody = await responseBody(nestedResponse);
  assert.equal(nestedResponse.status, 400);
  assert.equal(nestedBody.error.code, "UNKNOWN_FIELD");
  assert.deepEqual(nestedHarness.calls, []);
});

test("aiAllowed 必须显式传入且必须是布尔值", async () => {
  const missingHarness = makeHarness();
  const bodyWithoutFlag = validBody();
  delete (bodyWithoutFlag as Record<string, unknown>).aiAllowed;
  const missingResponse = await createHandler(missingHarness.deps)(
    makeRequest(bodyWithoutFlag),
  );
  const missingBody = await responseBody(missingResponse);

  assert.equal(missingResponse.status, 400);
  assert.equal(missingBody.error.code, "INVALID_REQUEST");
  assert.deepEqual(missingHarness.calls, []);

  const wrongHarness = makeHarness();
  const wrongResponse = await createHandler(wrongHarness.deps)(
    makeRequest(validBody({ aiAllowed: "true" })),
  );
  const wrongBody = await responseBody(wrongResponse);

  assert.equal(wrongResponse.status, 400);
  assert.equal(wrongBody.error.code, "INVALID_AI_ALLOWED");
  assert.deepEqual(wrongHarness.calls, []);
});

test("旧 pageSizes-only 客户端明确拒绝，不进入鉴权", async () => {
  const harness = makeHarness();
  const body = validBody();
  delete (body as Record<string, unknown>).pageHashes;
  const response = await createHandler(harness.deps)(makeRequest(body));
  const result = await responseBody(response);

  assert.equal(response.status, 400);
  assert.equal(result.error.code, "INVALID_REQUEST");
  assert.deepEqual(harness.calls, []);
});

test("pageHashes 数组长度必须与 pageSizes 一致", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ pageHashes: [PREVIEW_HASHES[0]] })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 400);
  assert.equal(body.error.code, "INVALID_PAGE_HASH_COUNT");
  assert.deepEqual(harness.calls, []);
});

test("pageHashes 形状非法或重复时在鉴权前拒绝", async () => {
  const notArrayHarness = makeHarness();
  const notArrayResponse = await createHandler(notArrayHarness.deps)(
    makeRequest(validBody({ pageHashes: PREVIEW_HASHES[0] })),
  );
  assert.equal(notArrayResponse.status, 400);
  assert.equal((await responseBody(notArrayResponse)).error.code, "INVALID_PAGE_HASHES");
  assert.deepEqual(notArrayHarness.calls, []);

  const malformedHarness = makeHarness();
  const malformedResponse = await createHandler(malformedHarness.deps)(
    makeRequest(validBody({ pageHashes: [PREVIEW_HASHES[0].toUpperCase(), PREVIEW_HASHES[1]] })),
  );
  assert.equal(malformedResponse.status, 400);
  assert.equal((await responseBody(malformedResponse)).error.code, "INVALID_PAGE_HASHES");
  assert.deepEqual(malformedHarness.calls, []);

  const duplicateHarness = makeHarness();
  const duplicateResponse = await createHandler(duplicateHarness.deps)(
    makeRequest(validBody({ pageHashes: [PREVIEW_HASHES[0], PREVIEW_HASHES[0]] })),
  );
  assert.equal(duplicateResponse.status, 400);
  assert.equal((await responseBody(duplicateResponse)).error.code, "DUPLICATE_PAGE_HASH");
  assert.deepEqual(duplicateHarness.calls, []);
});

test("prepare 只给缺失对象签发非 upsert 的固定路径 token", async () => {
  const objects = seedObjects([512, 1024]);
  const harness = makeHarness({ role: "submitter", objects });
  const response = await createHandler(harness.deps)(makeRequest(validBody()));
  const body = await responseBody(response);
  const expectedMissingPath = `${WORKSPACE_ID}/${DECK_ID}/preview/2.png`;

  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    uploads: [{ path: expectedMissingPath, token: "upload-token-1" }],
  });
  assert.deepEqual(harness.signedCalls, [
    { bucket: "ppt-library", path: expectedMissingPath, options: { upsert: false } },
  ]);
  assert.equal(
    harness.serviceOperations.filter((item) => item === "storage.info").length,
    3,
  );
  assert.equal(
    harness.serviceOperations.filter((item) => item === "storage.createSignedUploadUrl").length,
    1,
  );
  assert.equal(harness.serviceRpcCalls.length, 0);
});

test("已有对象大小匹配时 prepare 返回空 uploads，不重复签发", async () => {
  const harness = makeHarness({ objects: seedObjects([512, 1024, 2048]) });
  const response = await createHandler(harness.deps)(makeRequest(validBody()));
  const body = await responseBody(response);

  assert.equal(response.status, 200);
  assert.deepEqual(body, { uploads: [] });
  assert.equal(harness.signedCalls.length, 0);
});

test("对象大小兼容旧 metadata，现代 size 无效时不接受 fallback", async () => {
  const objects = seedObjects([512, 1024, 2048]);
  const legacy = makeHarness({ objects, infoShape: "legacy" });
  const modern = makeHarness({ objects, infoShape: "invalid-modern" });
  assert.equal((await createHandler(legacy.deps)(makeRequest(validBody()))).status, 200);
  const malformed = await createHandler(modern.deps)(makeRequest(validBody()));
  assert.equal(malformed.status, 502);
  assert.equal((await responseBody(malformed)).error.code, "STORAGE_METADATA_INVALID");
  assert.equal(modern.signedCalls.length, 0);
});

test("已有对象大小不匹配时拒绝 Prepare，避免覆盖", async () => {
  const objects = seedObjects([999, 1024, 2048]);
  const harness = makeHarness({ objects });
  const response = await createHandler(harness.deps)(makeRequest(validBody()));
  const body = await responseBody(response);

  assert.equal(response.status, 409);
  assert.equal(body.error.code, "OBJECT_SIZE_MISMATCH");
  assert.equal(harness.signedCalls.length, 0);
});

test("缺少任一页面时拒绝发布且不调用发布 RPC", async () => {
  const harness = makeHarness({ objects: seedObjects([512, 1024]) });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ action: "publish", aiAllowed: false })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 409);
  assert.equal(body.error.code, "OBJECT_MISSING");
  assert.equal(harness.signedCalls.length, 0);
  assert.equal(harness.serviceRpcCalls.length, 0);
});

test("judge 且 aiAllowed=true 可发布并透传安全完整结果", async () => {
  const fullResult = {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
    preEvaluation: { queued: true, jobId: "job-1" },
  };
  const harness = makeHarness({
    role: "judge",
    objects: seedObjects([512, 1024, 2048]),
    publishResults: [fullResult],
  });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ action: "publish", aiAllowed: true })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 200);
  assert.deepEqual(body, fullResult);
  assert.equal(harness.serviceRpcCalls.length, 1);
  assert.equal(harness.serviceRpcCalls[0].name, "single_judge_publish_upload");
  assert.deepEqual(harness.serviceRpcCalls[0].args, {
    w: WORKSPACE_ID,
    actor: USER_ID,
    p_ai_allowed: true,
    p_deck: {
      id: DECK_ID,
      title: "季度复盘",
      images: [
        {
          id: "page-1",
          name: "第 1 页.png",
          path: `${WORKSPACE_ID}/${DECK_ID}/preview/1.png`,
          thumbPath: `${WORKSPACE_ID}/${DECK_ID}/preview/1.png`,
          position: 1,
        },
        {
          id: "page-2",
          name: "第 2 页.png",
          path: `${WORKSPACE_ID}/${DECK_ID}/preview/2.png`,
          thumbPath: `${WORKSPACE_ID}/${DECK_ID}/preview/2.png`,
          position: 2,
        },
      ],
    },
    p_original: {
      name: "资料.pptx",
      sha256: SHA256,
      sizeBytes: 512,
      path: `${WORKSPACE_ID}/${DECK_ID}/original/${SHA256}.pptx`,
    },
  });
  assert.deepEqual(harness.userOperations, [
    "auth.getUser",
    "rpc:single_judge_access",
  ]);
  assert.equal(
    harness.serviceOperations.filter((item) => item === "storage.info").length,
    3,
  );
  assert.deepEqual(
    harness.serviceOperations.filter((item) => item.startsWith("rpc:")),
    ["rpc:single_judge_publish_upload"],
  );
  assert.equal(harness.signedCalls.length, 0);
  assert.equal(
    harness.serviceOperations.filter((item) => item === "storage.download").length,
    3,
  );
});

test("管理员可发布，但即使 AI 开启也只返回无队列安全摘要", async () => {
  const fullResult = {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
    preEvaluation: { queued: true, jobId: "must-not-leak" },
  };
  const harness = makeHarness({
    role: "admin",
    objects: seedObjects([512, 1024, 2048]),
    publishResults: [fullResult],
  });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ action: "publish", aiAllowed: true })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
  });
  assert.equal(JSON.stringify(body).includes("preEvaluation"), false);
  assert.equal(harness.serviceRpcCalls[0].args.p_ai_allowed, true);
  assert.equal(harness.serviceRpcCalls[0].args.actor, USER_ID);
});

test("submitter 可发布，响应不暴露队列或他人素材", async () => {
  const fullResult = {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
    preEvaluation: { queued: true, jobId: "submitter-must-not-see" },
    images: [{ path: "other-user/material.png" }],
  };
  const harness = makeHarness({
    role: "submitter",
    objects: seedObjects([512, 1024, 2048]),
    publishResults: [fullResult],
  });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ action: "publish", aiAllowed: true })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
  });
  assert.equal(JSON.stringify(body).includes("preEvaluation"), false);
  assert.equal(JSON.stringify(body).includes("other-user"), false);
  assert.deepEqual(harness.userOperations, [
    "auth.getUser",
    "rpc:single_judge_access",
  ]);
  assert.deepEqual(
    harness.serviceOperations.filter((item) => item.startsWith("rpc:")),
    ["rpc:single_judge_publish_upload"],
  );
});

test("judge 明确选择仅人工时也剥离队列结果", async () => {
  const fullResult = {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
    preEvaluation: { queued: true, jobId: "manual-only-must-not-leak" },
  };
  const harness = makeHarness({
    role: "judge",
    objects: seedObjects([512, 1024, 2048]),
    publishResults: [fullResult],
  });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ action: "publish", aiAllowed: false })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
  });
  assert.equal(harness.serviceRpcCalls[0].args.p_ai_allowed, false);
  assert.equal(JSON.stringify(body).includes("preEvaluation"), false);
});

test("重复发布会安全返回 idempotent=true", async () => {
  const harness = makeHarness({
    role: "submitter",
    objects: seedObjects([512, 1024, 2048]),
    publishResults: [
      {
        ok: true,
        deckId: DECK_ID,
        deckVersion: "deck-version-1",
        idempotent: false,
        preEvaluation: { queued: true },
      },
      {
        ok: true,
        deckId: DECK_ID,
        deckVersion: "deck-version-1",
        idempotent: true,
        preEvaluation: { queued: true },
      },
    ],
  });
  const handler = createHandler(harness.deps);
  const body = validBody({ action: "publish", aiAllowed: false });
  const first = await responseBody(await handler(makeRequest(body)));
  const second = await responseBody(await handler(makeRequest(body)));

  assert.deepEqual(first, {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: false,
  });
  assert.deepEqual(second, {
    ok: true,
    deckId: DECK_ID,
    deckVersion: "deck-version-1",
    idempotent: true,
  });
  assert.equal(harness.serviceRpcCalls.length, 2);
});

test("发布 RPC 错误不会暴露上游 message 或 service role key", async () => {
  const harness = makeHarness({
    role: "admin",
    objects: seedObjects([512, 1024, 2048]),
    publishError: {
      code: "XX000",
      message: `upstream raw body ${SERVICE_KEY}`,
      details: `credential ${SERVICE_KEY}`,
    },
  });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ action: "publish", aiAllowed: true })),
  );
  const text = await response.text();

  assert.equal(response.status, 502);
  assert.equal(text.includes(SERVICE_KEY), false);
  assert.equal(text.includes("upstream raw body"), false);
  assert.deepEqual(JSON.parse(text), {
    error: { code: "PUBLISH_FAILED", message: "导入发布失败" },
  });
});

test("页数与大小上限在认证前严格拒绝", async () => {
  const tooManyHarness = makeHarness();
  const tooManyResponse = await createHandler(tooManyHarness.deps)(
    makeRequest(validBody({ pageSizes: Array.from({ length: 601 }, () => 1) })),
  );
  const tooManyBody = await responseBody(tooManyResponse);
  assert.equal(tooManyResponse.status, 400);
  assert.equal(tooManyBody.error.code, "INVALID_PAGE_COUNT");
  assert.deepEqual(tooManyHarness.calls, []);

  const tooLargePageHarness = makeHarness();
  const tooLargePageResponse = await createHandler(tooLargePageHarness.deps)(
    makeRequest(validBody({ pageSizes: [25 * MIB + 1] })),
  );
  const tooLargePageBody = await responseBody(tooLargePageResponse);
  assert.equal(tooLargePageResponse.status, 400);
  assert.equal(tooLargePageBody.error.code, "INVALID_SIZE");
  assert.deepEqual(tooLargePageHarness.calls, []);

  const tooLargeTotalHarness = makeHarness();
  const tooLargeTotalResponse = await createHandler(tooLargeTotalHarness.deps)(
    makeRequest(validBody({ pageSizes: Array.from({ length: 13 }, () => 24 * MIB) })),
  );
  const tooLargeTotalBody = await responseBody(tooLargeTotalResponse);
  assert.equal(tooLargeTotalResponse.status, 400);
  assert.equal(tooLargeTotalBody.error.code, "PAGE_TOTAL_TOO_LARGE");
  assert.deepEqual(tooLargeTotalHarness.calls, []);
});



test("未知 action 在认证前被拒绝", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ action: "delete" })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 400);
  assert.equal(body.error.code, "INVALID_ACTION");
  assert.deepEqual(harness.calls, []);
});

test("原件格式与 25 MiB 上限被严格校验", async () => {
  const formatHarness = makeHarness();
  const formatResponse = await createHandler(formatHarness.deps)(
    makeRequest(
      validBody({
        original: { name: "资料.doc", sha256: SHA256, sizeBytes: 512 },
      }),
    ),
  );
  const formatBody = await responseBody(formatResponse);
  assert.equal(formatResponse.status, 400);
  assert.equal(formatBody.error.code, "INVALID_ORIGINAL_TYPE");
  assert.deepEqual(formatHarness.calls, []);

  const sizeHarness = makeHarness();
  const sizeResponse = await createHandler(sizeHarness.deps)(
    makeRequest(
      validBody({
        original: { name: "资料.pdf", sha256: SHA256, sizeBytes: 25 * MIB + 1 },
      }),
    ),
  );
  const sizeBody = await responseBody(sizeResponse);
  assert.equal(sizeResponse.status, 400);
  assert.equal(sizeBody.error.code, "INVALID_SIZE");
  assert.deepEqual(sizeHarness.calls, []);
});

test("600 页和 300 MiB 是包含边界", async () => {
  const pageSize = 300 * MIB / 600;
  const harness = makeHarness({ role: "judge" });
  const response = await createHandler(harness.deps)(
    makeRequest(validBody({ pageSizes: Array.from({ length: 600 }, () => pageSize) })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 200);
  assert.equal(body.uploads.length, 601);
  assert.equal(harness.signedCalls.length, 601);
  assert.equal(
    harness.signedCalls.filter((item) => item.options?.upsert === false).length,
    601,
  );
});



test("原件字节与声明哈希不符时不能发布，即使大小相同", async () => {
  const fixture = makeHarness({ objects: seedObjects([512, 1024, 2048]), wrongOriginalBytes: true });
  const response = await createHandler(fixture.deps)(makeRequest(validBody({ action: "publish" })));
  assert.equal(response.status, 409);
  assert.equal((await responseBody(response)).error.code, "ORIGINAL_HASH_MISMATCH");
  assert.deepEqual(fixture.serviceRpcCalls, []);
});

test("任一页预览哈希不符时不能发布，即使对象大小相同", async () => {
  const previewBytes = PREVIEW_BYTES.map((bytes) => bytes.slice());
  previewBytes[1][0] ^= 0xff;
  const fixture = makeHarness({
    objects: seedObjects([512, 1024, 2048]),
    previewBytes,
  });
  const response = await createHandler(fixture.deps)(
    makeRequest(validBody({ action: "publish", aiAllowed: false })),
  );
  const body = await responseBody(response);

  assert.equal(response.status, 409);
  assert.equal(body.error.code, "PREVIEW_HASH_MISMATCH");
  assert.deepEqual(fixture.serviceRpcCalls, []);
  assert.equal(
    fixture.serviceOperations.filter((item) => item === "storage.download").length,
    3,
  );
});

test("没有 Content-Length 时也在读取过程中限制 JSON 大小", async () => {
  const fixture = makeHarness();
  const response = await createHandler(fixture.deps)(makeRequest({ title: "x".repeat(70 * 1024) }));
  assert.equal(response.status, 413);
  assert.equal((await responseBody(response)).error.code, "REQUEST_TOO_LARGE");
  assert.deepEqual(fixture.calls, []);
});
