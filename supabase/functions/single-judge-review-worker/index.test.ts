import test from "node:test";
import assert from "node:assert/strict";
import { createHandler } from "./index.ts";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const USER_ID = "22222222-2222-4222-8222-222222222222";
const ADMIN_ID = "33333333-3333-4333-8333-333333333333";
const JOB_ID = "44444444-4444-4444-8444-444444444444";
const RECORD_ID = "55555555-5555-4555-8555-555555555555";
const DECK_ID = "import-66666666-6666-4666-8666-666666666666";
const ALLOWED_REF_ID = "import-77777777-7777-4777-8777-777777777777";
const MANUAL_REF_ID = "import-88888888-8888-4888-8888-888888888888";
const JWT = "header.payload.signature";
const WORKER_TOKEN = "worker_token_1234567890_abcdefghijklmnop";
const WRONG_WORKER_TOKEN = "wrong_token_1234567890_abcdefghijklmnopq";
const ANON_KEY = "sb_publishable_test";
const SERVICE_KEY = "service-role-secret-for-test";
const REQUEST_PATH = "/functions/v1/single-judge-review-worker";

type JsonObject = Record<string, unknown>;

interface RpcResult {
  data: unknown;
  error: { status?: number; statusCode?: number; code?: string; message?: string } | null;
}

interface HarnessOptions {
  role?: "judge" | "admin" | "submitter" | null;
  authError?: boolean;
  accessError?: { status?: number; code?: string; message?: string } | null;
  claimResult?: unknown;
  finishResults?: unknown[];
  calibrationResult?: unknown;
  deckVersion?: string;
  rubricVersion?: string;
  tables?: Record<string, JsonObject[]>;
  queryErrors?: Record<string, { status?: number; code?: string; message?: string }>;
  claimThrows?: Error;
  rpcOverrides?: Record<string, (args: JsonObject) => RpcResult | Promise<RpcResult>>;
}

function validJob(overrides: JsonObject = {}): JsonObject {
  return {
    id: JOB_ID,
    deckId: DECK_ID,
    sourceKind: "uploaded",
    scope: "deck",
    unitId: "",
    position: 0,
    variant: "baseline",
    bucket: 1,
    split: "calibration",
    deckVersion: "deck-v1",
    rubricVersion: "rubric-v1",
    maxScore: 5,
    attempts: 1,
    ...overrides,
  };
}

function workspaceRow(overrides: JsonObject = {}): JsonObject {
  return {
    id: WORKSPACE_ID,
    single_judge_admin_id: ADMIN_ID,
    dedicated_judge_id: USER_ID,
    settings: {
      revision: 3,
      maxScore: 5,
      dimensions: [
        { id: "visual", name: "视觉", description: "视觉表达" },
        { id: "layout", name: "排版", description: "版式秩序" },
      ],
      internalSecret: "must-not-leak",
    },
    ...overrides,
  };
}

function reviewSettingsRow(overrides: JsonObject = {}): JsonObject {
  return {
    workspace_id: WORKSPACE_ID,
    vision_weight: 40,
    layout_weight: 30,
    match_weight: 30,
    human_threshold: 0.85,
    model_hint: "gpt-6.2-sol",
    reason_catalog: [{ code: "visual_impact", label: "视觉冲击", internal: "hidden" }],
    updated_by: ADMIN_ID,
    api_key: "must-not-leak",
    ...overrides,
  };
}

function uploadedDeck(overrides: JsonObject = {}): JsonObject {
  return {
    id: DECK_ID,
    workspace_id: WORKSPACE_ID,
    source_kind: "uploaded",
    ai_allowed: true,
    data: {
      title: "上传作品",
      images: [{ id: "page-1", name: "第 1 页.png", path: `${WORKSPACE_ID}/${DECK_ID}/preview/1.png` }],
    },
    owner_id: "must-not-leak",
    ...overrides,
  };
}

function jobRow(overrides: JsonObject = {}): JsonObject {
  return {
    id: JOB_ID,
    workspace_id: WORKSPACE_ID,
    deck_id: DECK_ID,
    source_kind: "uploaded",
    status: "running",
    attempts: 1,
    ...overrides,
  };
}

function makeRequest(
  body: unknown,
  options: {
    jwt?: string | null;
    workerToken?: string | null;
    method?: string;
    path?: string;
    contentType?: string | null;
    headers?: Record<string, string>;
  } = {},
) {
  const headers = new Headers();
  const contentType = options.contentType === undefined ? "application/json; charset=utf-8" : options.contentType;
  if (contentType !== null) headers.set("content-type", contentType);
  const jwt = options.jwt === undefined ? JWT : options.jwt;
  if (jwt !== null) headers.set("authorization", `Bearer ${jwt}`);
  const workerToken = options.workerToken === undefined ? WORKER_TOKEN : options.workerToken;
  if (workerToken !== null) headers.set("x-guanye-worker-token", workerToken);
  for (const [key, value] of Object.entries(options.headers ?? {})) headers.set(key, value);
  return new Request(`https://example.supabase.co${options.path ?? REQUEST_PATH}`, {
    method: options.method ?? "POST",
    headers,
    body: options.method === "GET" || options.method === "OPTIONS" ? undefined : JSON.stringify(body),
  });
}

async function responseBody(response: Response): Promise<JsonObject> {
  return await response.json() as JsonObject;
}

function success(data: unknown): RpcResult {
  return { data, error: null };
}

function makeHarness(options: HarnessOptions = {}) {
  const calls: string[] = [];
  const userRpcCalls: Array<{ name: string; args: JsonObject }> = [];
  const serviceRpcCalls: Array<{ name: string; args: JsonObject }> = [];
  const queryCalls: Array<{ table: string; columns: string; eqs: Array<[string, unknown]> }> = [];
  let userConfig: Record<string, unknown> | null = null;
  let serviceConfig: Record<string, unknown> | null = null;
  let finishIndex = 0;
  const role = options.role === undefined ? "judge" : options.role;
  const tables: Record<string, JsonObject[]> = {
    workspaces: [workspaceRow()],
    ai_review_settings: [reviewSettingsRow()],
    decks: [uploadedDeck()],
    ai_review_jobs: [jobRow()],
    ...(options.tables ?? {}),
  };
  const env: Record<string, string> = {
    SUPABASE_URL: "https://example.supabase.co",
    SUPABASE_ANON_KEY: ANON_KEY,
    SUPABASE_SERVICE_ROLE_KEY: SERVICE_KEY,
    GUANYE_WORKSPACE_ID: WORKSPACE_ID,
    GUANYE_REVIEW_WORKER_TOKEN: WORKER_TOKEN,
  };

  const userClient = {
    auth: {
      async getUser(jwt?: string) {
        calls.push("user.auth.getUser");
        if (options.authError) {
          return { data: { user: null }, error: { status: 401, message: SERVICE_KEY } };
        }
        assert.equal(jwt, JWT);
        return { data: { user: { id: USER_ID } }, error: null };
      },
    },
    async rpc(name: string, args?: JsonObject) {
      calls.push(`user.rpc:${name}`);
      const payload = args ?? {};
      userRpcCalls.push({ name, args: payload });
      if (options.accessError) return { data: null, error: options.accessError };
      return {
        data: role === null ? null : { mode: "single-judge", role },
        error: null,
      };
    },
  };

  const serviceClient = {
    auth: {
      async getUser() {
        throw new Error("服务客户端不得查询用户");
      },
    },
    from(table: string) {
      const eqs: Array<[string, unknown]> = [];
      let columns = "";
      const builder = {
        select(value: string) {
          columns = value;
          return builder;
        },
        eq(column: string, value: unknown) {
          eqs.push([column, value]);
          return builder;
        },
        async maybeSingle() {
          queryCalls.push({ table, columns, eqs: [...eqs] });
          const queryError = options.queryErrors?.[table];
          if (queryError) return { data: null, error: queryError };
          const row = (tables[table] ?? []).find((candidate) =>
            eqs.every(([column, value]) => candidate[column] === value)
          );
          return { data: row ?? null, error: null };
        },
      };
      return builder;
    },
    async rpc(name: string, args?: JsonObject) {
      calls.push(`service.rpc:${name}`);
      const payload = args ?? {};
      serviceRpcCalls.push({ name, args: payload });
      const override = options.rpcOverrides?.[name];
      if (override) return await override(payload);
      if (name === "ai_review_claim_jobs") {
        if (options.claimThrows) throw options.claimThrows;
        return success(options.claimResult === undefined ? [validJob()] : options.claimResult);
      }
      if (name === "ai_review_deck_version") {
        return success(options.deckVersion ?? "deck-v1");
      }
      if (name === "ai_review_rubric_version") {
        return success(options.rubricVersion ?? "rubric-v1");
      }
      if (name === "ai_review_calibration") {
        return success(options.calibrationResult ?? {
          samples: [],
          corrections: [],
          selectedSamples: 0,
          selectedCorrections: 0,
          kind: "generated",
          requestedScope: "deck",
        });
      }
      if (name === "ai_review_finish_attempt") {
        const result = options.finishResults?.[finishIndex++] ?? {
          ok: true,
          status: payload.p_status,
          jobId: JOB_ID,
          aiVisible: false,
        };
        return success(result);
      }
      return success(null);
    },
  };

  return {
    deps: {
      readEnv(name: string) {
        return env[name];
      },
      async createUserClient(config: Record<string, unknown>) {
        userConfig = config;
        calls.push("createUserClient");
        return userClient;
      },
      async createServiceClient(config: Record<string, unknown>) {
        serviceConfig = config;
        calls.push("createServiceClient");
        return serviceClient;
      },
    },
    calls,
    userRpcCalls,
    serviceRpcCalls,
    queryCalls,
    get userConfig() {
      return userConfig;
    },
    get serviceConfig() {
      return serviceConfig;
    },
  };
}

test("缺少 Bearer JWT 返回可刷新的 401", async () => {
  const harness = makeHarness({ claimResult: [] });
  const response = await createHandler(harness.deps)(makeRequest(
    { action: "claim", kind: "uploaded" },
    { jwt: null },
  ));
  const body = await responseBody(response);
  assert.equal(response.status, 401);
  assert.deepEqual(body, {
    error: { code: "AUTH_REQUIRED", message: "需要用户 Bearer JWT", refreshable: true },
  });
  assert.equal(harness.userConfig, null);
  assert.equal(harness.calls.includes("createUserClient"), false);
});

test("错误工人密钥在 Auth 前返回 403", async () => {
  const harness = makeHarness({ claimResult: [] });
  const response = await createHandler(harness.deps)(makeRequest(
    { action: "claim", kind: "uploaded" },
    { workerToken: WRONG_WORKER_TOKEN },
  ));
  const body = await responseBody(response);
  assert.equal(response.status, 403);
  assert.equal((body.error as JsonObject).code, "WORKER_TOKEN_INVALID");
  assert.equal(harness.userConfig, null);
  assert.equal(harness.serviceConfig, null);
});

test("非 judge 角色返回 403 且不创建服务客户端", async () => {
  const harness = makeHarness({ role: "admin", claimResult: [] });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 403);
  assert.equal((body.error as JsonObject).code, "ACCESS_DENIED");
  assert.equal(harness.calls.includes("createServiceClient"), false);
  assert.equal(harness.serviceRpcCalls.length, 0);
});

test("claim 严格拒绝 workspace、actor、table、path 与扩展动作字段", async () => {
  const forbidden = [
    { action: "claim", kind: "uploaded", workspaceId: WORKSPACE_ID },
    { action: "claim", kind: "uploaded", actor: USER_ID },
    { action: "claim", kind: "uploaded", table: "decks" },
    { action: "claim", kind: "uploaded", path: "x.png" },
  ];
  for (const body of forbidden) {
    const harness = makeHarness({ claimResult: [] });
    const response = await createHandler(harness.deps)(makeRequest(body));
    const payload = await responseBody(response);
    assert.equal(response.status, 400);
    assert.equal((payload.error as JsonObject).code, "UNKNOWN_FIELD");
    assert.equal(harness.userConfig, null);
  }
});

test("路由接受 Edge 运行时去掉 /functions/v1 前缀的固定函数名", async () => {
  const harness = makeHarness({ claimResult: [] });
  const request = makeRequest({ action: "claim", kind: "uploaded" }, { path: "/single-judge-review-worker" });
  const response = await createHandler(harness.deps)(request);
  assert.equal(response.status, 200);
  assert.deepEqual(await responseBody(response), { job: null });
});

test("只接受固定函数路径且拒绝任意查询路由", async () => {
  for (const path of [
    `${REQUEST_PATH}/files`,
    `${REQUEST_PATH}?path=decks`,
    `${REQUEST_PATH}?table=ai_review_jobs`,
  ]) {
    const harness = makeHarness({ claimResult: [] });
    const response = await createHandler(harness.deps)(makeRequest(
      { action: "claim", kind: "uploaded" },
      { path },
    ));
    const body = await responseBody(response);
    assert.equal(response.status, 404);
    assert.equal((body.error as JsonObject).code, "NOT_FOUND");
    assert.equal(harness.calls.length, 0);
  }
});

test("无 CORS 且非 POST 直接拒绝", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(makeRequest(
    { action: "claim", kind: "uploaded" },
    { method: "OPTIONS", headers: { origin: "https://attacker.example" } },
  ));
  assert.equal(response.status, 405);
  assert.equal(response.headers.get("access-control-allow-origin"), null);
  assert.equal(response.headers.get("access-control-allow-headers"), null);
});

test("无任务时只返回 job:null", async () => {
  const harness = makeHarness({ claimResult: [] });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  assert.equal(response.status, 200);
  assert.deepEqual(await responseBody(response), { job: null });
  assert.equal(harness.serviceRpcCalls.filter((call) => call.name === "ai_review_claim_jobs").length, 1);
  assert.equal(harness.queryCalls.length, 0);
});

test("uploaded baseline claim 返回精确字段且不请求校准", async () => {
  const harness = makeHarness({ claimResult: [validJob()] });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 200);
  assert.deepEqual(Object.keys(body).sort(), [
    "calibration",
    "deck",
    "deckVersion",
    "job",
    "reviewSettings",
    "rubricVersion",
    "workspaceSettings",
  ]);
  assert.deepEqual(body.job, {
    id: JOB_ID,
    deckId: DECK_ID,
    sourceKind: "uploaded",
    scope: "deck",
    unitId: "",
    position: 0,
    variant: "baseline",
    bucket: 1,
    split: "calibration",
    deckVersion: "deck-v1",
    rubricVersion: "rubric-v1",
    maxScore: 5,
    attempts: 1,
  });
  assert.deepEqual(body.deck, {
    id: DECK_ID,
    workspace_id: WORKSPACE_ID,
    source_kind: "uploaded",
    ai_allowed: true,
    data: {
      title: "上传作品",
      images: [{ id: "page-1", name: "第 1 页.png", path: `${WORKSPACE_ID}/${DECK_ID}/preview/1.png` }],
    },
  });
  assert.deepEqual(body.workspaceSettings, {
    maxScore: 5,
    dimensions: [
      { id: "visual", name: "视觉", description: "视觉表达" },
      { id: "layout", name: "排版", description: "版式秩序" },
    ],
  });
  assert.deepEqual(body.reviewSettings, {
    vision_weight: 40,
    layout_weight: 30,
    match_weight: 30,
    human_threshold: 0.85,
    model_hint: "gpt-6.2-sol",
    reason_catalog: [{ code: "visual_impact", label: "视觉冲击" }],
  });
  assert.equal(body.calibration, null);
  assert.equal(harness.serviceRpcCalls.some((call) => call.name === "ai_review_calibration"), false);
  const serialized = JSON.stringify(body);
  assert.equal(serialized.includes("must-not-leak"), false);
  assert.equal(serialized.includes("api_key"), false);
  assert.equal(serialized.includes("signedUrl"), false);
});

test("generated 作品只保留已确认 AI 引用作品的 deckId 与同源安全路径", async () => {
  const safeAllowedPath = `${WORKSPACE_ID}/${ALLOWED_REF_ID}/preview/1.png`;
  const unsafeManualPath = `${WORKSPACE_ID}/${MANUAL_REF_ID}/preview/1.png`;
  const arbitraryUrl = "https://attacker.example/reference.png";
  const generatedData = {
    title: "生成作品",
    images: [{ id: "page-1", name: "第 1 页.png", path: `${WORKSPACE_ID}/${DECK_ID}/generated/1.png` }],
    references: [
      { deckId: ALLOWED_REF_ID, title: "顶层敏感原文", imagePaths: [safeAllowedPath, arbitraryUrl] },
      { deckId: MANUAL_REF_ID, title: "仅人工原文", imagePaths: [unsafeManualPath] },
    ],
    generation: {
      outline: { title: "生成作品" },
      style: { name: "克制专业" },
      pages: [{
        id: "page-1",
        title: "第一页",
        references: [
          { deckId: ALLOWED_REF_ID, imagePaths: [safeAllowedPath, arbitraryUrl], summary: "页级敏感原文" },
          { deckId: MANUAL_REF_ID, imagePaths: [unsafeManualPath], summary: "页级仅人工原文" },
          { imagePaths: [safeAllowedPath], text: "没有 deckId 的敏感原文" },
        ],
      }],
      references: [
        { deckId: ALLOWED_REF_ID, title: "引用标题", style: "引用风格", score: 99, imagePaths: [safeAllowedPath, arbitraryUrl] },
        { deckId: MANUAL_REF_ID, title: "仅人工标题", imagePaths: [unsafeManualPath] },
        { deckId: ALLOWED_REF_ID, path: `${WORKSPACE_ID}/${ALLOWED_REF_ID}/preview/2.png`, note: "另一段敏感原文" },
        { imagePaths: [safeAllowedPath], text: "缺少标识" },
      ],
    },
  };
  const harness = makeHarness({
    claimResult: [validJob({ sourceKind: "generated", deckId: DECK_ID })],
    tables: {
      decks: [
        {
          id: DECK_ID,
          workspace_id: WORKSPACE_ID,
          source_kind: "generated",
          ai_allowed: true,
          data: generatedData,
        },
        { id: ALLOWED_REF_ID, workspace_id: WORKSPACE_ID, source_kind: "uploaded", ai_allowed: true, data: { images: [{ path: safeAllowedPath }] } },
        { id: MANUAL_REF_ID, workspace_id: WORKSPACE_ID, source_kind: "uploaded", ai_allowed: false, data: {} },
      ],
    },
  });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "generated" }));
  const body = await responseBody(response);
  assert.equal(response.status, 200);
  const deck = body.deck as JsonObject;
  assert.deepEqual(deck, {
    id: DECK_ID,
    workspace_id: WORKSPACE_ID,
    source_kind: "generated",
    ai_allowed: true,
    data: {
      title: "生成作品",
      images: [{ id: "page-1", name: "第 1 页.png", path: `${WORKSPACE_ID}/${DECK_ID}/generated/1.png` }],
      references: [
        { deckId: ALLOWED_REF_ID, imagePaths: [safeAllowedPath] },
        { deckId: MANUAL_REF_ID },
      ],
      generation: {
        outline: { title: "生成作品" },
        style: { name: "克制专业" },
        pages: [{
          id: "page-1",
          title: "第一页",
          references: [
            { deckId: ALLOWED_REF_ID, imagePaths: [safeAllowedPath] },
            { deckId: MANUAL_REF_ID },
          ],
        }],
        references: [
          { deckId: ALLOWED_REF_ID, imagePaths: [safeAllowedPath] },
          { deckId: MANUAL_REF_ID },
          { deckId: ALLOWED_REF_ID },
        ],
      },
    },
  });
  const serialized = JSON.stringify(body);
  for (const forbidden of [
    arbitraryUrl,
    unsafeManualPath,
    `${WORKSPACE_ID}/${ALLOWED_REF_ID}/preview/2.png`,
    "敏感原文",
    "引用标题",
    "引用风格",
    "score",
    "summary",
  ]) {
    assert.equal(serialized.includes(forbidden), false);
  }
});

test("assisted 才请求并原样返回 raw calibration bundle", async () => {
  const rawCalibration = {
    samples: [{ refId: "sample-1", scores: { visual: 4 } }],
    corrections: [{ refId: "correction-1", formalScores: { layout: 3 } }],
    selectedSamples: 1,
    selectedCorrections: 1,
    kind: "uploaded",
    requestedScope: "page",
  };
  const harness = makeHarness({
    claimResult: [validJob({ variant: "assisted", scope: "page", unitId: "page-1", position: 1 })],
    calibrationResult: rawCalibration,
  });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 200);
  assert.deepEqual(body.calibration, rawCalibration);
  const calibrationCall = harness.serviceRpcCalls.find((call) => call.name === "ai_review_calibration");
  assert.deepEqual(calibrationCall?.args, {
    w: WORKSPACE_ID,
    actor: USER_ID,
    p_kind: "uploaded",
    p_limit: 8,
    p_deck_id: DECK_ID,
    p_scope: "page",
  });
});

test("AI 开关为 false 时 terminal fail 且不返回 deck", async () => {
  const harness = makeHarness({
    claimResult: [validJob()],
    tables: { decks: [uploadedDeck({ ai_allowed: false })] },
  });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 200);
  assert.deepEqual(body, { job: null });
  const finishCall = harness.serviceRpcCalls.find((call) => call.name === "ai_review_finish_attempt");
  assert.equal(finishCall?.args.p_status, "failed");
  assert.equal(finishCall?.args.p_terminal, true);
  assert.equal(finishCall?.args.p_result, null);
  assert.equal(JSON.stringify(body).includes("预评分数"), false);
});

test("版本变化时 terminal fail 且不返回 deck", async () => {
  const harness = makeHarness({
    claimResult: [validJob({ deckVersion: "old-deck-version" })],
  });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 200);
  assert.deepEqual(body, { job: null });
  const finishCall = harness.serviceRpcCalls.find((call) => call.name === "ai_review_finish_attempt");
  assert.equal(finishCall?.args.p_status, "failed");
  assert.equal(finishCall?.args.p_terminal, true);
});

test("工作区 consent 不可用时 terminal fail 且不返回 deck", async () => {
  const harness = makeHarness({
    claimResult: [validJob()],
    tables: { workspaces: [workspaceRow({ single_judge_admin_id: null })] },
  });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  assert.deepEqual(await responseBody(response), { job: null });
  assert.equal(harness.serviceRpcCalls.some((call) => call.name === "ai_review_finish_attempt"), true);
});

test("claim 后的瞬时查询故障返回 retryable 503，不伪造 job:null 完成", async () => {
  const harness = makeHarness({
    claimResult: [validJob()],
    queryErrors: { decks: { status: 500, message: SERVICE_KEY } },
  });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 503);
  assert.equal((body.error as JsonObject).code, "RETRYABLE");
  assert.equal((body.error as JsonObject).retryable, true);
  assert.equal(JSON.stringify(body).includes(SERVICE_KEY), false);
  assert.equal(harness.serviceRpcCalls.some((call) => call.name === "ai_review_finish_attempt"), false);
});

test("claim 传输失败只请求一次，不自动二次 claim", async () => {
  const harness = makeHarness({ claimThrows: new Error(`upstream ${SERVICE_KEY}`) });
  const response = await createHandler(harness.deps)(makeRequest({ action: "claim", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 503);
  assert.equal((body.error as JsonObject).retryable, true);
  assert.equal(harness.serviceRpcCalls.filter((call) => call.name === "ai_review_claim_jobs").length, 1);
  assert.equal(JSON.stringify(body).includes(SERVICE_KEY), false);
});

test("finish completed 只返回白名单字段且不返回 ai_result 或预评分数", async () => {
  const harness = makeHarness({
    finishResults: [{
      ok: true,
      status: "completed",
      jobId: JOB_ID,
      recordId: RECORD_ID,
      matchedSubmissions: 2,
      aiVisible: false,
      ai_result: { scores: { visual: 5 } },
      preEvaluationScores: { visual: 5 },
    }],
  });
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "completed",
    result: { scores: { visual: 4 }, summary: "完成" },
    error: null,
    terminal: false,
  }));
  const body = await responseBody(response);
  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    ok: true,
    status: "completed",
    jobId: JOB_ID,
    aiVisible: false,
    recordId: RECORD_ID,
    matchedSubmissions: 2,
  });
  assert.equal(JSON.stringify(body).includes("ai_result"), false);
  assert.equal(JSON.stringify(body).includes("preEvaluationScores"), false);
  const call = harness.serviceRpcCalls.find((item) => item.name === "ai_review_finish_attempt");
  assert.equal(call?.args.p_status, "completed");
  assert.deepEqual(call?.args.p_result, { scores: { visual: 4 }, summary: "完成" });
  assert.equal(harness.serviceConfig?.apiKey, SERVICE_KEY);
});

test("finish failed 正常保留失败结果", async () => {
  const harness = makeHarness({
    finishResults: [{ ok: true, status: "failed", jobId: JOB_ID, aiVisible: false, internal: SERVICE_KEY }],
  });
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "failed",
    result: null,
    error: "模型超时",
    terminal: false,
  }));
  const body = await responseBody(response);
  assert.deepEqual(body, {
    ok: true,
    status: "failed",
    jobId: JOB_ID,
    aiVisible: false,
  });
  assert.equal(JSON.stringify(body).includes(SERVICE_KEY), false);
  const call = harness.serviceRpcCalls.find((item) => item.name === "ai_review_finish_attempt");
  assert.equal(call?.args.p_error, "模型超时");
  assert.equal(call?.args.p_terminal, false);
});

test("finish 保留 lease_lost stale outcome", async () => {
  const harness = makeHarness({
    finishResults: [{ ok: false, status: "lease_lost", aiVisible: false }],
  });
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 2,
    status: "failed",
    result: null,
    error: "租约失效",
    terminal: false,
  }));
  assert.deepEqual(await responseBody(response), {
    ok: false,
    status: "lease_lost",
    jobId: JOB_ID,
    aiVisible: false,
  });
});

test("finish 保留 version_changed stale outcome", async () => {
  const harness = makeHarness({
    finishResults: [{ ok: false, status: "version_changed", jobId: JOB_ID, aiVisible: false }],
  });
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "failed",
    result: null,
    error: "版本变化",
    terminal: true,
  }));
  assert.deepEqual(await responseBody(response), {
    ok: false,
    status: "version_changed",
    jobId: JOB_ID,
    aiVisible: false,
  });
});

test("手工-only 拒绝 completed，并尽力 terminal fail 后返回 version_changed", async () => {
  const harness = makeHarness({
    tables: { decks: [uploadedDeck({ ai_allowed: false })] },
    finishResults: [{ ok: true, status: "failed", jobId: JOB_ID, aiVisible: false }],
  });
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "completed",
    result: { scores: { visual: 5 } },
    error: null,
    terminal: false,
  }));
  const body = await responseBody(response);
  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    ok: false,
    status: "version_changed",
    jobId: JOB_ID,
    aiVisible: false,
  });
  assert.equal(JSON.stringify(body).includes("visual"), false);
  const finishCalls = harness.serviceRpcCalls.filter((item) => item.name === "ai_review_finish_attempt");
  assert.equal(finishCalls.length, 1);
  assert.equal(finishCalls[0].args.p_status, "failed");
  assert.equal(finishCalls[0].args.p_terminal, true);
  assert.equal(finishCalls[0].args.p_result, null);
});

test("finish 严格拒绝 workspace 等额外字段", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "failed",
    result: null,
    error: "失败",
    terminal: false,
    workspaceId: WORKSPACE_ID,
  }));
  const body = await responseBody(response);
  assert.equal(response.status, 400);
  assert.equal((body.error as JsonObject).code, "UNKNOWN_FIELD");
  assert.equal(harness.userConfig, null);
});

test("finish result 超过 128 KiB 被拒绝", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "completed",
    result: { summary: "x".repeat(129 * 1024) },
    error: null,
    terminal: false,
  }));
  const body = await responseBody(response);
  assert.equal(response.status, 400);
  assert.equal((body.error as JsonObject).code, "RESULT_TOO_LARGE");
  assert.equal(harness.calls.length, 0);
});

test("扩展 action 被拒绝", async () => {
  const harness = makeHarness();
  const response = await createHandler(harness.deps)(makeRequest({ action: "run", kind: "uploaded" }));
  const body = await responseBody(response);
  assert.equal(response.status, 400);
  assert.equal((body.error as JsonObject).code, "INVALID_ACTION");
});

test("finish 的内部异常和 service key 不出现在响应", async () => {
  const harness = makeHarness({
    rpcOverrides: {
      ai_review_finish_attempt: () => ({
        data: null,
        error: { status: 500, message: `upstream ${SERVICE_KEY}` },
      }),
    },
  });
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "failed",
    result: null,
    error: "失败",
    terminal: false,
  }));
  const body = await responseBody(response);
  assert.equal(response.status, 503);
  assert.equal((body.error as JsonObject).retryable, true);
  assert.equal(JSON.stringify(body).includes(SERVICE_KEY), false);
  assert.equal(JSON.stringify(body).includes("upstream"), false);
});

test("finish 不存在的所属 job 返回通用 404", async () => {
  const harness = makeHarness({ tables: { ai_review_jobs: [] } });
  const response = await createHandler(harness.deps)(makeRequest({
    action: "finish",
    jobId: JOB_ID,
    attempt: 1,
    status: "failed",
    result: null,
    error: "失败",
    terminal: false,
  }));
  const body = await responseBody(response);
  assert.equal(response.status, 404);
  assert.equal((body.error as JsonObject).code, "JOB_NOT_FOUND");
});
