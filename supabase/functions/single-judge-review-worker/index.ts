// Server-to-server only: no CORS and no browser-facing route surface.
// The Windows review worker receives the user JWT and a separate broker token.
// Supabase service_role stays exclusively inside this Edge process.

const FUNCTION_PATH = "/single-judge-review-worker";
const REQUEST_PATHS = new Set([FUNCTION_PATH, "/functions/v1" + FUNCTION_PATH]);
const WORKER_TOKEN_HEADER = "x-guanye-worker-token";
const MAX_BODY_BYTES = 256 * 1024;
const MAX_RESULT_BYTES = 128 * 1024;
const MAX_RESULT_DEPTH = 32;
const MAX_RESULT_KEYS = 256;
const MAX_RESULT_ARRAY_ITEMS = 2000;
const MAX_RESULT_STRING_CHARS = 32 * 1024;
const MAX_ERROR_CHARS = 1000;
const MAX_BEARER_CHARS = 16 * 1024;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SAFE_DECK_ID_PATTERN = /^[A-Za-z0-9_-]{1,100}$/;
const SAFE_WORKER_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,256}$/;
const SAFE_STORAGE_PATH_PATTERN = /^[^\r\n\x00]+$/;

type JsonObject = Record<string, unknown>;
type ClaimKind = "uploaded" | "generated";
type JobScope = "deck" | "page";
type JobVariant = "baseline" | "assisted";
type JobSplit = "calibration" | "holdout";
type FinishStatus = "completed" | "failed";
type FinishOutcomeStatus = FinishStatus | "lease_lost" | "version_changed";

interface SupabaseErrorLike {
  message?: string;
  status?: number;
  statusCode?: number;
  code?: string;
}

interface RpcResult {
  data: unknown;
  error: SupabaseErrorLike | null;
}

interface AuthResult {
  data: { user: { id?: string } | null };
  error: SupabaseErrorLike | null;
}

interface QueryResult {
  data: unknown;
  error: SupabaseErrorLike | null;
}

interface QueryBuilderLike {
  select(columns: string): QueryBuilderLike;
  eq(column: string, value: unknown): QueryBuilderLike;
  maybeSingle(): Promise<QueryResult>;
}

export interface UserClientLike {
  auth: {
    getUser(jwt?: string): Promise<AuthResult>;
  };
  rpc(name: string, args?: JsonObject): Promise<RpcResult>;
}

export interface ServiceClientLike extends UserClientLike {
  from(table: string): QueryBuilderLike;
}

interface ClientConfig {
  url: string;
  apiKey: string;
  jwt: string;
}

interface ServiceClientConfig {
  url: string;
  apiKey: string;
}

export interface HandlerDependencies {
  readEnv(name: string): string | undefined;
  createUserClient(config: ClientConfig): Promise<UserClientLike>;
  createServiceClient(config: ServiceClientConfig): Promise<ServiceClientLike>;
}

interface WorkspaceState {
  maxScore: number;
  dimensions: Array<{ id: string; name: string; description: string }>;
}

interface ReviewSettings {
  vision_weight: number;
  layout_weight: number;
  match_weight: number;
  human_threshold: number;
  model_hint: string;
  reason_catalog: Array<{ code: string; label: string }>;
}

interface NormalizedJob {
  id: string;
  deckId: string;
  sourceKind: ClaimKind;
  scope: JobScope;
  unitId: string;
  position: number;
  variant: JobVariant;
  bucket: number;
  split: JobSplit;
  deckVersion: string;
  rubricVersion: string;
  maxScore: number;
  attempts: number;
}

interface ClaimRequest {
  action: "claim";
  kind: ClaimKind;
}

interface FinishRequest {
  action: "finish";
  jobId: string;
  attempt: number;
  status: FinishStatus;
  result: JsonObject | null;
  error: string | null;
  terminal: boolean;
}

type NormalizedRequest = ClaimRequest | FinishRequest;

class HttpError extends Error {
  status: number;
  code: string;
  retryable: boolean;
  refreshable: boolean;

  constructor(
    status: number,
    code: string,
    message: string,
    options: { retryable?: boolean; refreshable?: boolean } = {},
  ) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.code = code;
    this.retryable = options.retryable === true;
    this.refreshable = options.refreshable === true;
  }
}


function isRecord(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requiredEnv(dependencies: HandlerDependencies, name: string): string {
  const value = dependencies.readEnv(name);
  if (typeof value !== "string" || value.trim() === "") {
    throw new HttpError(500, "EDGE_CONFIG_INVALID", "边缘函数配置不完整");
  }
  return value.trim();
}

function readProjectUrl(dependencies: HandlerDependencies): string {
  const value = requiredEnv(dependencies, "SUPABASE_URL").replace(/\/+$/, "");
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new HttpError(500, "EDGE_CONFIG_INVALID", "边缘函数配置不完整");
  }
  if (
    (parsed.protocol !== "https:" && parsed.protocol !== "http:") ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.search !== "" ||
    parsed.hash !== "" ||
    (parsed.pathname !== "" && parsed.pathname !== "/")
  ) {
    throw new HttpError(500, "EDGE_CONFIG_INVALID", "边缘函数配置不完整");
  }
  return parsed.origin;
}

function readApiKey(dependencies: HandlerDependencies, name: string): string {
  const value = requiredEnv(dependencies, name);
  if (value.length > 8192) {
    throw new HttpError(500, "EDGE_CONFIG_INVALID", "边缘函数配置不完整");
  }
  return value;
}

function readWorkspaceId(dependencies: HandlerDependencies): string {
  const value = requiredEnv(dependencies, "GUANYE_WORKSPACE_ID").toLowerCase();
  if (!UUID_PATTERN.test(value)) {
    throw new HttpError(500, "EDGE_CONFIG_INVALID", "边缘函数配置不完整");
  }
  return value;
}

function readExpectedWorkerToken(dependencies: HandlerDependencies): string {
  const value = requiredEnv(dependencies, "GUANYE_REVIEW_WORKER_TOKEN");
  if (!SAFE_WORKER_TOKEN_PATTERN.test(value)) {
    throw new HttpError(500, "EDGE_CONFIG_INVALID", "边缘函数配置不完整");
  }
  return value;
}

function defaultDependencies(): HandlerDependencies {
  return {
    readEnv(name) {
      const deno = (globalThis as { Deno?: { env?: { get(key: string): string | undefined } } }).Deno;
      return deno?.env?.get(name);
    },
    async createUserClient(config) {
      const { createClient } = await import("npm:@supabase/supabase-js@2.119.2");
      return createClient(config.url, config.apiKey, {
        auth: {
          persistSession: false,
          autoRefreshToken: false,
          detectSessionInUrl: false,
        },
        global: {
          headers: {
            Authorization: `Bearer ${config.jwt}`,
          },
        },
      }) as unknown as UserClientLike;
    },
    async createServiceClient(config) {
      const { createClient } = await import("npm:@supabase/supabase-js@2.119.2");
      return createClient(config.url, config.apiKey, {
        auth: {
          persistSession: false,
          autoRefreshToken: false,
          detectSessionInUrl: false,
        },
      }) as unknown as ServiceClientLike;
    },
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function errorResponse(error: unknown): Response {
  if (error instanceof HttpError) {
    const payload: JsonObject = { code: error.code, message: error.message };
    if (error.retryable) payload.retryable = true;
    if (error.refreshable) payload.refreshable = true;
    return jsonResponse({ error: payload }, error.status);
  }
  console.error("[single-judge-review-worker] request_failed");
  return jsonResponse(
    { error: { code: "INTERNAL_ERROR", message: "预评服务暂时不可用" } },
    500,
  );
}

function assertExactKeys(value: JsonObject, expected: readonly string[]): void {
  const allowed = new Set(expected);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new HttpError(400, "UNKNOWN_FIELD", "请求包含不允许的字段");
    }
  }
  for (const key of expected) {
    if (!Object.prototype.hasOwnProperty.call(value, key)) {
      throw new HttpError(400, "INVALID_REQUEST", "请求字段不完整");
    }
  }
}

function assertJsonValue(value: unknown, depth = 0): void {
  if (depth > MAX_RESULT_DEPTH) {
    throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
  }
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "string") {
    if (Array.from(value).length > MAX_RESULT_STRING_CHARS || value.includes("\u0000")) {
      throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
    }
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
    }
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_RESULT_ARRAY_ITEMS) {
      throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
    }
    for (const item of value) assertJsonValue(item, depth + 1);
    return;
  }
  if (isRecord(value)) {
    const keys = Object.keys(value);
    if (keys.length > MAX_RESULT_KEYS) {
      throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
    }
    for (const key of keys) {
      if (key.includes("\u0000") || Array.from(key).length > 256) {
        throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
      }
      assertJsonValue(value[key], depth + 1);
    }
    return;
  }
  throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
}

function jsonByteLength(value: unknown): number {
  try {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) {
      throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
    }
    return new TextEncoder().encode(encoded).byteLength;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, "INVALID_RESULT", "result 结构无效");
  }
}

function parseRequest(value: unknown): NormalizedRequest {
  if (!isRecord(value)) {
    throw new HttpError(400, "INVALID_JSON", "请求体必须是 JSON 对象");
  }
  if (value.action === "claim") {
    assertExactKeys(value, ["action", "kind"]);
    if (value.kind !== "uploaded" && value.kind !== "generated") {
      throw new HttpError(400, "INVALID_KIND", "kind 仅支持 uploaded 或 generated");
    }
    return { action: "claim", kind: value.kind };
  }
  if (value.action !== "finish") {
    throw new HttpError(400, "INVALID_ACTION", "仅支持 claim 或 finish");
  }

  assertExactKeys(value, [
    "action",
    "jobId",
    "attempt",
    "status",
    "result",
    "error",
    "terminal",
  ]);
  if (typeof value.jobId !== "string" || !UUID_PATTERN.test(value.jobId)) {
    throw new HttpError(400, "INVALID_JOB_ID", "jobId 必须是规范 UUID");
  }
  if (!Number.isInteger(value.attempt) || (value.attempt as number) < 1 || (value.attempt as number) > 3) {
    throw new HttpError(400, "INVALID_ATTEMPT", "attempt 必须是 1 到 3 的整数");
  }
  if (value.status !== "completed" && value.status !== "failed") {
    throw new HttpError(400, "INVALID_STATUS", "status 仅支持 completed 或 failed");
  }
  if (typeof value.terminal !== "boolean") {
    throw new HttpError(400, "INVALID_TERMINAL", "terminal 必须是布尔值");
  }

  let result: JsonObject | null;
  if (value.result === null) {
    result = null;
  } else {
    if (!isRecord(value.result)) {
      throw new HttpError(400, "INVALID_RESULT", "result 必须是对象或 null");
    }
    if (jsonByteLength(value.result) > MAX_RESULT_BYTES) {
      throw new HttpError(400, "RESULT_TOO_LARGE", "result 超过 128 KiB");
    }
    assertJsonValue(value.result);
    result = value.result;
  }

  let errorText: string | null;
  if (value.error === null) {
    errorText = null;
  } else if (typeof value.error === "string") {
    if (
      value.error.trim() === "" ||
      Array.from(value.error).length > MAX_ERROR_CHARS ||
      value.error.includes("\u0000")
    ) {
      throw new HttpError(400, "INVALID_ERROR", "error 长度或字符无效");
    }
    errorText = value.error;
  } else {
    throw new HttpError(400, "INVALID_ERROR", "error 必须是字符串或 null");
  }

  if (value.status === "completed" && (result === null || errorText !== null)) {
    throw new HttpError(400, "INVALID_FINISH", "completed 必须提交 result 且 error 为 null");
  }
  if (value.status === "failed" && result !== null) {
    throw new HttpError(400, "INVALID_FINISH", "failed 的 result 必须为 null");
  }

  return {
    action: "finish",
    jobId: value.jobId,
    attempt: value.attempt as number,
    status: value.status,
    result,
    error: errorText,
    terminal: value.terminal,
  };
}

function readBearerToken(request: Request): string {
  const authorization = request.headers.get("authorization");
  const match = authorization?.match(/^Bearer ([^\s]+)$/i);
  if (!match) {
    throw new HttpError(401, "AUTH_REQUIRED", "需要用户 Bearer JWT", { refreshable: true });
  }
  if (match[1].length > MAX_BEARER_CHARS) {
    throw new HttpError(401, "AUTH_INVALID", "用户 JWT 无效或已过期", { refreshable: true });
  }
  return match[1];
}

async function parseJsonBody(request: Request): Promise<unknown> {
  const contentType = request.headers.get("content-type")?.toLowerCase() ?? "";
  if (!contentType.startsWith("application/json")) {
    throw new HttpError(415, "UNSUPPORTED_CONTENT_TYPE", "仅接受 application/json");
  }

  const contentLength = request.headers.get("content-length");
  if (contentLength !== null) {
    const length = Number(contentLength);
    if (!Number.isSafeInteger(length) || length < 0) {
      throw new HttpError(400, "INVALID_CONTENT_LENGTH", "Content-Length 无效");
    }
    if (length > MAX_BODY_BYTES) {
      throw new HttpError(413, "REQUEST_TOO_LARGE", "请求体过大");
    }
  }

  const reader = request.body?.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let text = "";
  let consumed = 0;
  if (reader) {
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        consumed += value.byteLength;
        if (consumed > MAX_BODY_BYTES) {
          await reader.cancel().catch(() => {});
          throw new HttpError(413, "REQUEST_TOO_LARGE", "请求体过大");
        }
        text += decoder.decode(value, { stream: true });
      }
      text += decoder.decode();
    } catch (error) {
      if (error instanceof HttpError) throw error;
      throw new HttpError(400, "INVALID_JSON", "请求体不是有效 UTF-8 JSON");
    } finally {
      reader.releaseLock();
    }
  }
  if (text.trim() === "") {
    throw new HttpError(400, "INVALID_JSON", "请求体不能为空");
  }

  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "INVALID_JSON", "请求体不是有效 JSON");
  }
}

async function sha256Bytes(value: string): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return new Uint8Array(digest);
}

async function constantTimeTokenEqual(actual: string, expected: string): Promise<boolean> {
  const [left, right] = await Promise.all([sha256Bytes(actual), sha256Bytes(expected)]);
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

async function requireWorkerToken(request: Request, dependencies: HandlerDependencies): Promise<void> {
  const expected = readExpectedWorkerToken(dependencies);
  const actual = request.headers.get(WORKER_TOKEN_HEADER);
  if (actual === null || !SAFE_WORKER_TOKEN_PATTERN.test(actual)) {
    throw new HttpError(403, "WORKER_TOKEN_INVALID", "工人密钥无效");
  }
  if (!(await constantTimeTokenEqual(actual, expected))) {
    throw new HttpError(403, "WORKER_TOKEN_INVALID", "工人密钥无效");
  }
}

function mapUserRpcError(error: SupabaseErrorLike, message: string): never {
  const status = Number(error.status ?? error.statusCode ?? 0);
  if (status === 401) {
    throw new HttpError(401, "AUTH_INVALID", "用户 JWT 无效或已过期", { refreshable: true });
  }
  if (status === 403 || error.code === "42501") {
    throw new HttpError(403, "ACCESS_DENIED", message);
  }
  throw new HttpError(503, "ACCESS_CHECK_FAILED", "暂时无法核验评审权限", { retryable: true });
}

async function authenticateAndAuthorize(
  dependencies: HandlerDependencies,
  request: Request,
  workspaceId: string,
): Promise<{ actor: string; supabaseUrl: string }> {
  const jwt = readBearerToken(request);
  const supabaseUrl = readProjectUrl(dependencies);
  const userClient = await dependencies.createUserClient({
    url: supabaseUrl,
    apiKey: readApiKey(dependencies, "SUPABASE_ANON_KEY"),
    jwt,
  });

  let auth: AuthResult;
  try {
    auth = await userClient.auth.getUser(jwt);
  } catch {
    throw new HttpError(401, "AUTH_INVALID", "用户 JWT 无效或已过期", { refreshable: true });
  }
  if (!auth.error && auth.data.user && typeof auth.data.user.id === "string" && UUID_PATTERN.test(auth.data.user.id)) {
    // Continue with the role check below.
  } else {
    throw new HttpError(401, "AUTH_INVALID", "用户 JWT 无效或已过期", { refreshable: true });
  }
  const actor = auth.data.user.id;

  let access: RpcResult;
  try {
    access = await userClient.rpc("single_judge_access", { w: workspaceId });
  } catch {
    throw new HttpError(503, "ACCESS_CHECK_FAILED", "暂时无法核验评审权限", { retryable: true });
  }
  if (access.error) {
    mapUserRpcError(access.error, "当前账号不是该项目的评分师");
  }
  if (!isRecord(access.data) || access.data.mode !== "single-judge" || access.data.role !== "judge") {
    throw new HttpError(403, "ACCESS_DENIED", "当前账号不是该项目的评分师");
  }
  return { actor, supabaseUrl };
}

async function createServiceClient(
  dependencies: HandlerDependencies,
  supabaseUrl: string,
): Promise<ServiceClientLike> {
  return await dependencies.createServiceClient({
    url: supabaseUrl,
    apiKey: readApiKey(dependencies, "SUPABASE_SERVICE_ROLE_KEY"),
  });
}

function serviceRpcError(error: SupabaseErrorLike, message: string): never {
  const status = Number(error.status ?? error.statusCode ?? 0);
  if (status === 401) {
    throw new HttpError(401, "AUTH_INVALID", "用户 JWT 无效或已过期", { refreshable: true });
  }
  if (status === 403 || error.code === "42501") {
    throw new HttpError(403, "ACCESS_DENIED", message);
  }
  if (status === 400 || status === 422 || error.code === "22023") {
    throw new HttpError(400, "INVALID_OPERATION", message);
  }
  if (
    status === 404 ||
    error.code === "42883" ||
    error.code === "PGRST202" ||
    error.code === "PGRST204"
  ) {
    throw new HttpError(503, "SERVICE_UNAVAILABLE", "预评服务尚未就绪", { retryable: true });
  }
  throw new HttpError(503, "RETRYABLE", "预评服务暂时不可用，请稍后重试", { retryable: true });
}

async function rpcService(
  client: ServiceClientLike,
  name: string,
  args: JsonObject,
  message: string,
): Promise<unknown> {
  let result: RpcResult;
  try {
    result = await client.rpc(name, args);
  } catch {
    throw new HttpError(503, "RETRYABLE", "预评服务暂时不可用，请稍后重试", { retryable: true });
  }
  if (result.error) serviceRpcError(result.error, message);
  return result.data;
}

async function selectOne(
  client: ServiceClientLike,
  table: string,
  columns: string,
  workspaceId: string,
  deckId?: string,
): Promise<JsonObject | null> {
  let result: QueryResult;
  try {
    const workspaceColumn = table === "workspaces" ? "id" : "workspace_id";
    let query = client.from(table).select(columns).eq(workspaceColumn, workspaceId);
    if (deckId !== undefined) query = query.eq("id", deckId);
    result = await query.maybeSingle();
  } catch {
    throw new HttpError(503, "RETRYABLE", "预评数据暂时不可用，请稍后重试", { retryable: true });
  }
  if (result.error) {
    throw new HttpError(503, "RETRYABLE", "预评数据暂时不可用，请稍后重试", { retryable: true });
  }
  if (result.data === null) return null;
  if (!isRecord(result.data)) {
    throw new HttpError(502, "INVALID_UPSTREAM", "预评数据格式无效");
  }
  return result.data;
}

function finiteNumber(value: unknown, minimum: number, maximum: number): number | null {
  const number = typeof value === "number"
    ? value
    : typeof value === "string" && /^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(value.trim())
      ? Number(value)
      : Number.NaN;
  if (!Number.isFinite(number) || number < minimum || number > maximum) return null;
  return number;
}

function validText(value: unknown, maximum: number): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (text === "" || Array.from(text).length > maximum || /[\u0000-\u001f\u007f]/.test(text)) return null;
  return text;
}

function normalizeWorkspaceState(row: JsonObject): WorkspaceState | null {
  if (
    row.single_judge_admin_id === null ||
    typeof row.single_judge_admin_id !== "string" ||
    !UUID_PATTERN.test(row.single_judge_admin_id) ||
    !isRecord(row.settings)
  ) {
    return null;
  }
  const maxScore = finiteNumber(row.settings.maxScore, 1, 100);
  const rawDimensions = row.settings.dimensions;
  if (maxScore === null || !Array.isArray(rawDimensions) || rawDimensions.length < 1 || rawDimensions.length > 12) {
    return null;
  }
  const dimensions: WorkspaceState["dimensions"] = [];
  for (const raw of rawDimensions) {
    if (!isRecord(raw)) return null;
    const id = validText(raw.id, 80);
    const name = validText(raw.name, 30);
    const description = typeof raw.description === "string" ? raw.description : "";
    if (!id || !name || Array.from(description).length > 2000 || /[\u0000-\u001f\u007f]/.test(description)) {
      return null;
    }
    dimensions.push({ id, name, description });
  }
  return { maxScore, dimensions };
}

function normalizeReviewSettings(row: JsonObject): ReviewSettings | null {
  const visionWeight = finiteNumber(row.vision_weight, 0, 100);
  const layoutWeight = finiteNumber(row.layout_weight, 0, 100);
  const matchWeight = finiteNumber(row.match_weight, 0, 100);
  const humanThreshold = finiteNumber(row.human_threshold, 0, 1);
  const modelHint = validText(row.model_hint, 200);
  if (
    visionWeight === null ||
    layoutWeight === null ||
    matchWeight === null ||
    humanThreshold === null ||
    !modelHint ||
    !Array.isArray(row.reason_catalog)
  ) {
    return null;
  }
  const reasonCatalog: ReviewSettings["reason_catalog"] = [];
  for (const raw of row.reason_catalog) {
    if (!isRecord(raw)) return null;
    const code = validText(raw.code, 80);
    const label = validText(raw.label, 120);
    if (!code || !label) return null;
    reasonCatalog.push({ code, label });
  }
  return {
    vision_weight: visionWeight,
    layout_weight: layoutWeight,
    match_weight: matchWeight,
    human_threshold: humanThreshold,
    model_hint: modelHint,
    reason_catalog: reasonCatalog,
  };
}

function normalizeJob(value: unknown, expectedKind: ClaimKind): NormalizedJob {
  if (!isRecord(value)) {
    throw new HttpError(502, "INVALID_JOB", "预评任务格式无效");
  }
  const id = typeof value.id === "string" && UUID_PATTERN.test(value.id) ? value.id : null;
  const deckId = typeof value.deckId === "string" && SAFE_DECK_ID_PATTERN.test(value.deckId)
    ? value.deckId
    : null;
  const sourceKind = value.sourceKind === "uploaded" || value.sourceKind === "generated" ? value.sourceKind : null;
  const scope = value.scope;
  const unitId = value.unitId === undefined ? "" : value.unitId;
  const variant = value.variant;
  const split = value.split;
  const position = value.position;
  const bucket = value.bucket;
  const attempts = value.attempts;
  const maxScore = finiteNumber(value.maxScore, 1, 100);
  const deckVersion = typeof value.deckVersion === "string" && value.deckVersion.length >= 1 && value.deckVersion.length <= 256
    ? value.deckVersion
    : null;
  const rubricVersion = typeof value.rubricVersion === "string" && value.rubricVersion.length >= 1 && value.rubricVersion.length <= 256
    ? value.rubricVersion
    : null;
  if (
    !id ||
    !deckId ||
    sourceKind === null || sourceKind !== expectedKind ||
    (scope !== "deck" && scope !== "page") ||
    typeof unitId !== "string" ||
    Array.from(unitId).length > 100 ||
    /[\u0000-\u001f\u007f]/.test(unitId) ||
    (variant !== "baseline" && variant !== "assisted") ||
    (split !== "calibration" && split !== "holdout") ||
    !Number.isInteger(position) ||
    (position as number) < 0 ||
    (position as number) > 600 ||
    !Number.isInteger(bucket) ||
    (bucket as number) < 0 ||
    (bucket as number) > 4 ||
    !Number.isInteger(attempts) ||
    (attempts as number) < 1 ||
    (attempts as number) > 3 ||
    maxScore === null ||
    !deckVersion ||
    !rubricVersion
  ) {
    throw new HttpError(502, "INVALID_JOB", "预评任务格式无效");
  }
  return {
    id,
    deckId,
    sourceKind,
    scope,
    unitId,
    position: position as number,
    variant,
    bucket: bucket as number,
    split,
    deckVersion,
    rubricVersion,
    maxScore,
    attempts: attempts as number,
  };
}

function safeReferencePath(value: unknown, workspaceId: string, deckId: string): string | null {
  if (typeof value !== "string") return null;
  const path = value.trim();
  if (
    path.length < 1 ||
    Array.from(path).length > 500 ||
    !path.startsWith(`${workspaceId}/${deckId}/`) ||
    path.includes("..") ||
    path.includes("%") ||
    path.split("/").some((segment) => !segment || segment === "." || segment === "..") ||
    path.includes("\\") ||
    path.includes("://") ||
    path.includes("?") ||
    path.includes("#") ||
    !SAFE_STORAGE_PATH_PATTERN.test(path)
  ) {
    return null;
  }
  return path;
}

function rawReferenceDeckId(value: unknown): string | null {
  if (typeof value === "string") {
    return SAFE_DECK_ID_PATTERN.test(value) ? value : null;
  }
  if (!isRecord(value)) return null;
  const candidate = value.deckId;
  return typeof candidate === "string" && SAFE_DECK_ID_PATTERN.test(candidate) ? candidate : null;
}

function collectReferenceDeckIds(value: unknown, target: Set<string>): void {
  if (!Array.isArray(value)) return;
  for (const item of value) {
    const deckId = rawReferenceDeckId(item);
    if (deckId) target.add(deckId);
  }
}

function collectNestedReferenceDeckIds(value: unknown, target: Set<string>, depth = 0): void {
  if (depth > 64) throw new HttpError(502, "INVALID_DECK_DATA", "作品数据格式无效");
  if (Array.isArray(value)) {
    for (const item of value) collectNestedReferenceDeckIds(item, target, depth + 1);
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, item] of Object.entries(value)) {
    if (key === "references") collectReferenceDeckIds(item, target);
    else collectNestedReferenceDeckIds(item, target, depth + 1);
  }
}

function storedPreviewPaths(deckData: unknown, workspaceId: string, deckId: string): Set<string> {
  const paths = new Set<string>();
  if (!isRecord(deckData) || !Array.isArray(deckData.images)) return paths;
  for (const image of deckData.images) {
    if (!isRecord(image)) continue;
    const path = safeReferencePath(image.path || image.imagePath || image.storagePath, workspaceId, deckId);
    if (path && /\.(png|jpe?g|webp)$/i.test(path)) paths.add(path);
  }
  return paths;
}

async function confirmedAiReferencePaths(
  client: ServiceClientLike,
  workspaceId: string,
  deckData: unknown,
): Promise<Map<string, Set<string>>> {
  const candidates = new Set<string>();
  collectNestedReferenceDeckIds(deckData, candidates);

  const confirmed = new Map<string, Set<string>>();
  // A plan can have at most twelve library reference works. Do not fan out unbounded reads.
  await Promise.all(Array.from(candidates).slice(0, 12).map(async (deckId) => {
    const row = await selectOne(client, "decks", "id,source_kind,ai_allowed,data", workspaceId, deckId);
    if (row && row.id === deckId && row.source_kind === "uploaded" && row.ai_allowed === true) {
      confirmed.set(deckId, storedPreviewPaths(row.data, workspaceId, deckId));
    }
  }));
  return confirmed;
}

function sanitizeReferenceEntries(
  value: unknown,
  workspaceId: string,
  confirmedPaths: Map<string, Set<string>>,
): JsonObject[] {
  if (!Array.isArray(value)) return [];
  const result: JsonObject[] = [];
  for (const item of value) {
    const deckId = rawReferenceDeckId(item);
    if (!deckId) continue;

    const safePaths: string[] = [];
    const authorized = confirmedPaths.get(deckId);
    if (authorized && isRecord(item)) {
      const candidates: unknown[] = [];
      if (Array.isArray(item.imagePaths)) candidates.push(...item.imagePaths);
      else if (typeof item.imagePaths === "string") candidates.push(item.imagePaths);
      candidates.push(item.path, item.imagePath, item.storagePath);
      for (const candidate of candidates) {
        const path = safeReferencePath(candidate, workspaceId, deckId);
        if (path && authorized.has(path) && !safePaths.includes(path)) safePaths.push(path);
      }
    }

    const clean: JsonObject = { deckId };
    if (safePaths.length > 0) clean.imagePaths = safePaths;
    result.push(clean);
  }
  return result;
}

function sanitizeDeckData(
  value: unknown,
  workspaceId: string,
  confirmedPaths: Map<string, Set<string>>,
): unknown {
  function scrub(item: unknown, depth: number): unknown {
    if (depth > 64) throw new HttpError(502, "INVALID_DECK_DATA", "作品数据格式无效");
    if (Array.isArray(item)) return item.map((entry) => scrub(entry, depth + 1));
    if (!isRecord(item)) return item;
    const clean: JsonObject = Object.create(null);
    for (const [key, entry] of Object.entries(item)) {
      clean[key] = key === "references"
        ? sanitizeReferenceEntries(entry, workspaceId, confirmedPaths)
        : scrub(entry, depth + 1);
    }
    return clean;
  }
  return scrub(value, 0);
}

async function finishAttempt(
  client: ServiceClientLike,
  workspaceId: string,
  actor: string,
  jobId: string,
  attempt: number,
  status: FinishStatus,
  result: JsonObject | null,
  errorText: string | null,
  terminal: boolean,
): Promise<unknown> {
  return await rpcService(client, "ai_review_finish_attempt", {
    w: workspaceId,
    actor,
    target_job: jobId,
    p_attempt: attempt,
    p_status: status,
    p_result: result,
    p_error: errorText,
    p_terminal: terminal,
  }, "预评任务不能完成");
}

async function retireObsoleteClaim(
  client: ServiceClientLike,
  workspaceId: string,
  actor: string,
  job: NormalizedJob,
  message: string,
): Promise<Response> {
  try {
    await finishAttempt(client, workspaceId, actor, job.id, job.attempts, "failed", null, message, true);
  } catch {
    throw new HttpError(503, "RETRYABLE", "预评作废处理暂未完成，请稍后重试", { retryable: true });
  }
  return jsonResponse({ job: null });
}

async function claimOne(
  dependencies: HandlerDependencies,
  request: ClaimRequest,
  actor: string,
  supabaseUrl: string,
  workspaceId: string,
): Promise<Response> {
  const serviceClient = await createServiceClient(dependencies, supabaseUrl);
  const claimed = await rpcService(serviceClient, "ai_review_claim_jobs", {
    w: workspaceId,
    actor,
    p_kind: request.kind,
    p_limit: 1,
  }, "预评任务领取被拒绝");

  if (!Array.isArray(claimed)) {
    throw new HttpError(502, "INVALID_CLAIM", "预评任务领取格式无效");
  }
  if (claimed.length === 0) return jsonResponse({ job: null });
  if (claimed.length !== 1) {
    throw new HttpError(502, "INVALID_CLAIM", "预评任务领取格式无效");
  }
  const job = normalizeJob(claimed[0], request.kind);

  const workspaceRow = await selectOne(
    serviceClient,
    "workspaces",
    "id,single_judge_admin_id,dedicated_judge_id,settings",
    workspaceId,
  );
  const workspaceSettings = workspaceRow ? normalizeWorkspaceState(workspaceRow) : null;
  if (
    !workspaceRow ||
    !workspaceSettings ||
    workspaceRow.id !== workspaceId ||
    workspaceRow.dedicated_judge_id !== actor
  ) {
    return await retireObsoleteClaim(
      serviceClient,
      workspaceId,
      actor,
      job,
      "工作区评审授权已不可用，预评任务作废",
    );
  }

  const deckRow = await selectOne(
    serviceClient,
    "decks",
    "id,source_kind,ai_allowed,data",
    workspaceId,
    job.deckId,
  );
  if (
    !deckRow ||
    deckRow.id !== job.deckId ||
    deckRow.source_kind !== job.sourceKind ||
    deckRow.ai_allowed !== true ||
    !isRecord(deckRow.data)
  ) {
    return await retireObsoleteClaim(
      serviceClient,
      workspaceId,
      actor,
      job,
      "作品版本或 AI 授权已变化，预评任务作废",
    );
  }

  const reviewRow = await selectOne(
    serviceClient,
    "ai_review_settings",
    "vision_weight,layout_weight,match_weight,human_threshold,model_hint,reason_catalog",
    workspaceId,
  );
  const reviewSettings = reviewRow ? normalizeReviewSettings(reviewRow) : null;
  if (!reviewSettings) {
    return await retireObsoleteClaim(
      serviceClient,
      workspaceId,
      actor,
      job,
      "工作区评审配置已不可用，预评任务作废",
    );
  }

  const deckVersion = await rpcService(
    serviceClient,
    "ai_review_deck_version",
    { w: workspaceId, target_id: job.deckId },
    "作品版本校验失败",
  );
  const rubricVersion = await rpcService(
    serviceClient,
    "ai_review_rubric_version",
    { w: workspaceId },
    "评分标准版本校验失败",
  );
  if (
    typeof deckVersion !== "string" ||
    typeof rubricVersion !== "string" ||
    deckVersion !== job.deckVersion ||
    rubricVersion !== job.rubricVersion
  ) {
    return await retireObsoleteClaim(
      serviceClient,
      workspaceId,
      actor,
      job,
      "作品或评分标准版本已变化，预评作废",
    );
  }

  let calibration: unknown = null;
  if (job.variant === "assisted") {
    try {
      calibration = await rpcService(serviceClient, "ai_review_calibration", {
        w: workspaceId,
        actor,
        p_kind: job.sourceKind,
        p_limit: 8,
        p_deck_id: job.deckId,
        p_scope: job.scope,
      }, "校准样本读取被拒绝");
    } catch (error) {
      try {
        await finishAttempt(
          serviceClient,
          workspaceId,
          actor,
          job.id,
          job.attempts,
          "failed",
          null,
          "校准样本暂时不可用",
          false,
        );
      } catch {
        // Preserve the original retryable failure without exposing internals.
      }
      throw error;
    }
    if (!isRecord(calibration)) {
      try {
        await finishAttempt(
          serviceClient,
          workspaceId,
          actor,
          job.id,
          job.attempts,
          "failed",
          null,
          "校准样本格式无效",
          true,
        );
      } catch {
        // The response stays fail-closed and returns no deck.
      }
      throw new HttpError(502, "INVALID_CALIBRATION", "校准样本格式无效");
    }
  }

  const confirmedPaths = await confirmedAiReferencePaths(
    serviceClient,
    workspaceId,
    deckRow.data,
  );
  const safeDeck = {
    id: deckRow.id,
    workspace_id: workspaceId,
    source_kind: deckRow.source_kind,
    ai_allowed: true,
    data: sanitizeDeckData(deckRow.data, workspaceId, confirmedPaths),
  };

  return jsonResponse({
    job: {
      id: job.id,
      deckId: job.deckId,
      sourceKind: job.sourceKind,
      scope: job.scope,
      unitId: job.unitId,
      position: job.position,
      variant: job.variant,
      bucket: job.bucket,
      split: job.split,
      deckVersion: job.deckVersion,
      rubricVersion: job.rubricVersion,
      maxScore: job.maxScore,
      attempts: job.attempts,
    },
    deck: safeDeck,
    workspaceSettings,
    reviewSettings,
    deckVersion,
    rubricVersion,
    calibration,
  });
}

function normalizeFinishOutcome(value: unknown, requestedJobId: string): JsonObject {
  if (!isRecord(value) || typeof value.ok !== "boolean") {
    throw new HttpError(502, "INVALID_FINISH_OUTCOME", "预评完成结果格式无效");
  }
  const status = value.status;
  if (
    status !== "completed" &&
    status !== "failed" &&
    status !== "lease_lost" &&
    status !== "version_changed"
  ) {
    throw new HttpError(502, "INVALID_FINISH_OUTCOME", "预评完成结果格式无效");
  }

  const outcome: JsonObject = {
    ok: value.ok,
    status: status as FinishOutcomeStatus,
    jobId: typeof value.jobId === "string" && UUID_PATTERN.test(value.jobId) ? value.jobId : requestedJobId,
    aiVisible: false,
  };
  if (typeof value.recordId === "string" && UUID_PATTERN.test(value.recordId)) {
    outcome.recordId = value.recordId;
  }
  if (Number.isInteger(value.matchedSubmissions) && (value.matchedSubmissions as number) >= 0) {
    outcome.matchedSubmissions = value.matchedSubmissions;
  }
  return outcome;
}

async function finishOne(
  dependencies: HandlerDependencies,
  request: FinishRequest,
  actor: string,
  supabaseUrl: string,
  workspaceId: string,
): Promise<Response> {
  const serviceClient = await createServiceClient(dependencies, supabaseUrl);

  const workspaceRow = await selectOne(
    serviceClient,
    "workspaces",
    "id,single_judge_admin_id,dedicated_judge_id,settings",
    workspaceId,
  );
  if (
    !workspaceRow ||
    !normalizeWorkspaceState(workspaceRow) ||
    workspaceRow.id !== workspaceId ||
    workspaceRow.dedicated_judge_id !== actor
  ) {
    throw new HttpError(403, "CONSENT_REQUIRED", "当前工作区评审授权不可用");
  }

  const jobRow = await selectOne(
    serviceClient,
    "ai_review_jobs",
    "id,workspace_id,deck_id,source_kind,status,attempts",
    workspaceId,
    request.jobId,
  );
  if (!jobRow) {
    throw new HttpError(404, "JOB_NOT_FOUND", "预评任务不存在");
  }
  if (
    jobRow.id !== request.jobId ||
    jobRow.workspace_id !== workspaceId ||
    typeof jobRow.deck_id !== "string" ||
    !SAFE_DECK_ID_PATTERN.test(jobRow.deck_id) ||
    (jobRow.source_kind !== "uploaded" && jobRow.source_kind !== "generated")
  ) {
    throw new HttpError(404, "JOB_NOT_FOUND", "预评任务不存在");
  }

  const deckRow = await selectOne(
    serviceClient,
    "decks",
    "id,source_kind,ai_allowed",
    workspaceId,
    jobRow.deck_id,
  );
  const consentAvailable = Boolean(
    deckRow &&
    deckRow.id === jobRow.deck_id &&
    deckRow.source_kind === jobRow.source_kind &&
    deckRow.ai_allowed === true,
  );

  if (request.status === "completed" && !consentAvailable) {
    let retirement: JsonObject;
    try {
      retirement = normalizeFinishOutcome(
        await finishAttempt(
          serviceClient,
          workspaceId,
          actor,
          request.jobId,
          request.attempt,
          "failed",
          null,
          "作品已切换为仅人工或版本不可用，预评作废",
          true,
        ),
        request.jobId,
      );
    } catch {
      throw new HttpError(503, "RETRYABLE", "预评作废处理暂未完成，请稍后重试", { retryable: true });
    }
    if (retirement.status === "lease_lost") return jsonResponse(retirement);
    return jsonResponse({
      ok: false,
      status: "version_changed",
      jobId: request.jobId,
      aiVisible: false,
    });
  }

  const outcome = await finishAttempt(
    serviceClient,
    workspaceId,
    actor,
    request.jobId,
    request.attempt,
    request.status,
    request.result,
    request.error,
    request.terminal,
  );
  return jsonResponse(normalizeFinishOutcome(outcome, request.jobId));
}

export function createHandler(
  injected: Partial<HandlerDependencies> = {},
): (request: Request) => Promise<Response> {
  const dependencies: HandlerDependencies = {
    ...defaultDependencies(),
    ...injected,
  };

  return async function handle(request: Request): Promise<Response> {
    try {
      if (request.method !== "POST") {
        throw new HttpError(405, "METHOD_NOT_ALLOWED", "仅支持 POST");
      }
      const url = new URL(request.url);
      if (!REQUEST_PATHS.has(url.pathname) || url.search !== "" || url.hash !== "") {
        throw new HttpError(404, "NOT_FOUND", "接口不存在");
      }

      const body = parseRequest(await parseJsonBody(request));
      await requireWorkerToken(request, dependencies);
      const workspaceId = readWorkspaceId(dependencies);
      const { actor, supabaseUrl } = await authenticateAndAuthorize(
        dependencies,
        request,
        workspaceId,
      );

      if (body.action === "claim") {
        return await claimOne(dependencies, body, actor, supabaseUrl, workspaceId);
      }
      return await finishOne(dependencies, body, actor, supabaseUrl, workspaceId);
    } catch (error) {
      return errorResponse(error);
    }
  };
}

const handler = createHandler();
if (typeof Deno !== "undefined") {
  Deno.serve(handler);
}

export default handler;
