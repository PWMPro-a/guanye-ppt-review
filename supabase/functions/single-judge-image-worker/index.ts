import { createHash, timingSafeEqual } from "node:crypto";

const ROUTE = "/single-judge-image-worker";
const BUCKET = "ppt-library";
const JSON_LIMIT = 32 * 1024;
const IMAGE_LIMIT = 25 * 1024 * 1024;
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const TOKEN = /^[A-Za-z0-9_-]{32,256}$/;
const INVALID_CHAR = /[\p{Cc}]/u;
const DECK_ID = /^[A-Za-z0-9_-]{1,100}$/;
const IMAGE_EXT = { "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp" } as const;
const SAFE_FAILURE = "页面生成失败";

type ContentType = keyof typeof IMAGE_EXT;
type Json = Record<string, unknown>;
type Rpc = (name: string, args: Json) => PromiseLike<Result>;
type UpstreamError = { code?: string; message?: string; status?: number };
type Result = { data: unknown; error: UpstreamError | null; status?: number };
type ClientConfig = { url: string; key: string };
type UserClient = { auth: { getUser(jwt: string): PromiseLike<Result> }; rpc: Rpc };
type ServiceClient = {
  rpc: Rpc;
  storage: { from(bucket: string): {
    createSignedUploadUrl(path: string, options: { upsert: false }): PromiseLike<Result>;
  } };
};
type Dependencies = {
  env(name: string): string | undefined;
  user(config: ClientConfig, jwt: string): Promise<UserClient>;
  service(config: ClientConfig): Promise<ServiceClient>;
  fetch: typeof fetch;
};
type Lease = { page_id: string; lease_token: string };
type Command =
  | { action: "claim"; worker_id: string; limit: number; lease_seconds: number; cover_training?: true }
  | (Lease & { action: "heartbeat"; lease_seconds: number })
  | (Lease & { action: "reference" | "issue-upload" | "fail" })
  | (Lease & { action: "complete"; content_type: ContentType; content_sha256: string;
      width: number | null; height: number | null });

class EdgeError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const invalid = () => new EdgeError(400, "INVALID_REQUEST", "工作请求字段无效");
const denied = () => new EdgeError(403, "ACCESS_DENIED", "当前账号不是该项目的评分师");
const unavailable = () => new EdgeError(503, "UPSTREAM_UNAVAILABLE", "图片工作服务暂时不可用");
const badResponse = () => new EdgeError(502, "UPSTREAM_RESPONSE_INVALID", "图片工作服务返回异常");
const notFound = () => new EdgeError(404, "PAGE_NOT_FOUND", "生成页面不存在");
const leaseLost = () => new EdgeError(409, "LEASE_LOST", "页面租约已失效或已过期");
const contentConflict = () => new EdgeError(409, "CONTENT_CONFLICT", "存储中的图片与提交内容不一致");
const badImage = () => new EdgeError(422, "IMAGE_INVALID", "存储中的图片超限或格式不符");
const unauthorized = () => new EdgeError(401, "AUTH_INVALID", "用户凭证无效或已过期");

function record(value: unknown): value is Json {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function uuid(value: unknown): value is string {
  return typeof value === "string" && value.length === 36 && UUID.test(value);
}
function int(value: unknown, min: number, max: number): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= min && value <= max;
}
function contentType(value: unknown): value is ContentType {
  return typeof value === "string" && Object.hasOwn(IMAGE_EXT, value);
}
function exact(value: Json, keys: string[]) {
  if (Object.keys(value).length !== keys.length || !keys.every((key) => Object.hasOwn(value, key))) {
    throw invalid();
  }
}

async function readJson(req: Request): Promise<unknown> {
  if (req.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") {
    throw new EdgeError(415, "JSON_REQUIRED", "仅接受 JSON 请求");
  }
  const tooLarge = () => new EdgeError(413, "REQUEST_TOO_LARGE", "请求体过大");
  if (Number(req.headers.get("content-length")) > JSON_LIMIT) throw tooLarge();
  if (!req.body) throw invalid();
  const reader = req.body.getReader();
  try {
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let length = 0;
    let text = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > JSON_LIMIT) throw tooLarge();
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    const parsed = JSON.parse(text);
    // The wire contract is flat. Reject repeated JSON member names, including escaped aliases.
    const keys = new Set<string>();
    for (const match of text.matchAll(/"(?:[^"\\]|\\.)*"(\s*:)?/gs)) {
      if (!match[1]) continue;
      const key = JSON.parse(match[0].slice(0, -match[1].length));
      if (keys.has(key)) throw invalid();
      keys.add(key);
    }
    return parsed;
  } catch (error) {
    void reader.cancel().catch(() => {});
    if (error instanceof EdgeError) throw error;
    throw invalid();
  } finally {
    reader.releaseLock();
  }
}

function parse(body: unknown): Command {
  if (!record(body)) throw invalid();
  const { action, worker_id, limit, lease_seconds, page_id, lease_token } = body;
  if (action === "claim") {
    const cover = Object.hasOwn(body, "cover_training");
    exact(body, ["action", "worker_id", "limit", "lease_seconds", ...(cover ? ["cover_training"] : [])]);
    if (cover && body.cover_training !== true) throw invalid();
    if (typeof worker_id !== "string" || INVALID_CHAR.test(worker_id) ||
        !int([...worker_id.trim()].length, 1, 200) || !int(limit, 1, cover ? 20 : 32) ||
        !int(lease_seconds, 60, 7200)) throw invalid();
    return { action, worker_id: worker_id.trim(), limit, lease_seconds, ...(cover ? {cover_training: true as const} : {}) };
  }
  if (!uuid(page_id) || !uuid(lease_token)) throw invalid();
  const lease = { page_id: page_id.toLowerCase(), lease_token: lease_token.toLowerCase() };
  const base = ["action", "page_id", "lease_token"];
  switch (action) {
    case "reference":
    case "issue-upload":
      exact(body, base);
      return { action, ...lease };
    case "heartbeat":
      exact(body, [...base, "lease_seconds"]);
      if (!int(lease_seconds, 60, 7200)) throw invalid();
      return { action, ...lease, lease_seconds };
    case "fail":
      exact(body, [...base, "error"]);
      if (typeof body.error !== "string" || [...body.error].length > 2000) throw invalid();
      // Free-form worker/provider diagnostics are not trusted enough to persist or echo.
      return { action, ...lease };
    case "complete": {
      exact(body, [...base, "content_type", "content_sha256", "width", "height"]);
      const { content_type, content_sha256, width, height } = body;
      if (!contentType(content_type) || typeof content_sha256 !== "string" ||
          content_sha256.length !== 64 || !/^[a-f0-9]{64}$/i.test(content_sha256) ||
          !(width === null || int(width, 1, 2147483647)) ||
          !(height === null || int(height, 1, 2147483647))) throw invalid();
      return { action, ...lease, content_type, content_sha256: content_sha256.toLowerCase(), width, height };
    }
    default:
      throw invalid();
  }
}

function setting(deps: Dependencies, name: string): string {
  const value = deps.env(name);
  if (!value) throw new EdgeError(500, "CONFIG_INVALID", "图片工作服务配置无效");
  return value;
}

async function authenticate(req: Request, deps: Dependencies, workspace: string, url: string) {
  const auth = req.headers.get("authorization");
  const jwt = auth && auth.length <= 8192 && /^Bearer ([\w-]+\.[\w-]+\.[\w-]+)$/i.exec(auth)?.[1];
  if (!jwt) throw unauthorized();
  const expected = setting(deps, "GUANYE_IMAGE_WORKER_TOKEN");
  if (!TOKEN.test(expected) || expected.trim() !== expected) {
    throw new EdgeError(500, "CONFIG_INVALID", "图片工作服务配置无效");
  }
  const token = req.headers.get("x-guanye-worker-token") ?? "";
  if (!TOKEN.test(token) || !timingSafeEqual(
    createHash("sha256").update(token).digest(), createHash("sha256").update(expected).digest(),
  )) throw new EdgeError(403, "WORKER_TOKEN_INVALID", "工作节点凭证无效");

  const client = await deps.user({ url, key: setting(deps, "SUPABASE_ANON_KEY") }, jwt);
  const authResult = await client.auth.getUser(jwt);
  if (authResult.error) {
    if ([400, 401, 403, 422].includes(authResult.error.status ?? 0) ||
        ["bad_jwt", "session_not_found", "user_not_found"].includes(authResult.error.code ?? "")) {
      throw unauthorized();
    }
    throw unavailable();
  }
  const user = record(authResult.data) ? authResult.data.user : null;
  if (!record(user) || !uuid(user.id)) throw unauthorized();
  const access = await client.rpc("single_judge_access", { w: workspace });
  if (access.error) {
    const status = access.status ?? access.error.status;
    if (status === 401 || ["PGRST301", "PGRST302", "PGRST303"].includes(access.error.code ?? "")) throw unauthorized();
    if (status === 403 || access.error.code === "42501") throw denied();
    throw unavailable();
  }
  if (!record(access.data) || access.data.mode !== "single-judge" || access.data.role !== "judge") throw denied();
  return user.id.toLowerCase();
}

async function rpc(client: ServiceClient, name: string, args: Json): Promise<unknown> {
  const { data, error } = await client.rpc(name, args);
  if (!error) return data;
  if (error.code === "42501") throw denied();
  if (error.code === "P0001") {
    switch (error.message) {
      case "操作人身份不匹配":
      case "请先加入团队": throw denied();
      case "生成页面不存在": throw notFound();
      case "页面租约已失效":
      case "页面租约已过期": throw leaseLost();
      case "页面已完成且内容不一致": throw contentConflict();
      case "生成作品发布冲突": throw new EdgeError(409, "PUBLICATION_CONFLICT", "生成作品发布冲突");
    }
  }
  if (error.code === "23505") {
    throw new EdgeError(409, "STATE_CONFLICT", "生成页面状态冲突");
  }
  throw unavailable();
}

function referencePath(value: unknown, workspace: string): Json {
  if (value === null) throw new EdgeError(404, "REFERENCE_NOT_FOUND", "该页没有可用的确认参考图");
  if (typeof value !== "string" || value.length > 500 || INVALID_CHAR.test(value) ||
      /[\\%?#]/.test(value) || value.includes("..")) throw badResponse();
  const parts = value.split("/");
  const file = parts.at(-1)!;
  const ext = /\.(png|jpe?g|webp|gif|bmp)$/i.exec(file)?.[1].toLowerCase();
  const shape = parts[1] === "ai-planning"
    ? parts.length === 5 && uuid(parts[2]) && ["reference", "style-rendered"].includes(parts[3])
    : DECK_ID.test(parts[1] ?? "") && (parts.length === 3 || (parts.length === 4 && parts[2] === "preview"));
  if (parts[0] !== workspace || !shape || !ext || !file.trim()) throw badResponse();
  const mime = ext === "jpg" || ext === "jpeg" ? "image/jpeg" : `image/${ext}`;
  return { storagePath: value, contentType: mime };
}

function uploadTarget(value: unknown, workspace: string, page: string) {
  if (!record(value) || !contentType(value.contentType) || typeof value.storagePath !== "string" ||
      typeof value.alreadySucceeded !== "boolean") throw badResponse();
  const [root, deckId, file, ...extra] = value.storagePath.split("/");
  if (root !== workspace || !deckId || !DECK_ID.test(deckId) || INVALID_CHAR.test(deckId) ||
      deckId === "ai-planning" || extra.length !== 0 || file !== `${page}${IMAGE_EXT[value.contentType]}`) {
    throw badResponse();
  }
  return { storagePath: value.storagePath, contentType: value.contentType, alreadySucceeded: value.alreadySucceeded };
}

function magic(bytes: Uint8Array): ContentType | null {
  const starts = (sequence: number[]) => sequence.every((byte, i) => bytes[i] === byte);
  if (starts([137, 80, 78, 71, 13, 10, 26, 10])) return "image/png";
  if (starts([255, 216, 255])) return "image/jpeg";
  if (starts([82, 73, 70, 70]) && bytes[8] === 87 && bytes[9] === 69 && bytes[10] === 66 && bytes[11] === 80) {
    return "image/webp";
  }
  return null;
}

async function verifyImage(
  deps: Dependencies, config: ClientConfig, path: string, expectedType: ContentType, expectedHash: string,
) {
  const target = new URL(`/storage/v1/object/${BUCKET}/${path}`, config.url);
  const res = await deps.fetch(target, {
    method: "GET",
    headers: { apikey: config.key, authorization: `Bearer ${config.key}`, "accept-encoding": "identity", "cache-control": "no-cache" },
    redirect: "error",
    cache: "no-store",
    signal: AbortSignal.timeout(45_000),
  });
  let complete = false;
  const reader = res.body?.getReader();
  try {
    if (res.status === 404) throw new EdgeError(409, "UPLOAD_NOT_FOUND", "尚未读到该页上传的图片");
    if (res.status !== 200 || !reader || res.headers.has("content-range")) throw unavailable();
    const encoding = res.headers.get("content-encoding");
    if (encoding && encoding.toLowerCase() !== "identity") throw unavailable();
    const lengthHeader = res.headers.get("content-length");
    const expectedLength = lengthHeader === null ? null : Number(lengthHeader);
    if (expectedLength !== null && (!/^\d+$/.test(lengthHeader!) || !Number.isFinite(expectedLength))) throw unavailable();
    if (expectedLength !== null && (expectedLength <= 0 || expectedLength > IMAGE_LIMIT)) throw badImage();
    // Retain at most the current network chunk and the 12-byte signature, not a full image per lease.
    const hash = createHash("sha256");
    const signature = new Uint8Array(12);
    let seen = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (seen + value.byteLength > IMAGE_LIMIT) throw badImage();
      if (seen < signature.length) {
        signature.set(value.subarray(0, signature.length - seen), seen);
        if (seen + value.byteLength >= signature.length && magic(signature) !== expectedType) throw badImage();
      }
      seen += value.byteLength;
      hash.update(value);
    }
    complete = true;
    if (expectedLength !== null && seen !== expectedLength) throw unavailable();
    if (magic(signature.subarray(0, Math.min(seen, signature.length))) !== expectedType) throw badImage();
    const digest = hash.digest("hex");
    if (digest !== expectedHash) throw contentConflict();
    return digest;
  } finally {
    if (reader) {
      if (!complete) void reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}

function job(value: unknown, redactErrors = false) {
  if (!record(value) || !uuid(value.id) || !Array.isArray(value.pages)) throw badResponse();
  if (!redactErrors) return value;
  return { ...value, pages: value.pages.map((page) => {
    if (!record(page)) throw badResponse();
    return typeof page.error === "string" ? { ...page, error: SAFE_FAILURE } : page;
  }) };
}

async function run(
  deps: Dependencies, client: ServiceClient, serviceConfig: ClientConfig,
  workspace: string, actor: string, command: Command,
): Promise<unknown> {
  const scope = { p_workspace: workspace, p_actor: actor };
  if (command.action === "claim") {
    const pages = await rpc(client, command.cover_training ? "single_judge_cover_claim_pages" : "ai_generation_claim_pages", {
      ...scope, p_worker_id: command.worker_id, p_limit: command.limit, p_lease_seconds: command.lease_seconds,
    });
    if (!Array.isArray(pages) || !pages.every(record) || pages.length > command.limit) throw badResponse();
    return { pages };
  }
  const lease = { ...scope, p_page: command.page_id, p_lease_token: command.lease_token };
  if (command.action === "heartbeat") {
    const value = await rpc(client, "ai_generation_heartbeat", { ...lease, p_lease_seconds: command.lease_seconds });
    if (!record(value) || value.ok !== true) throw badResponse();
    return { ok: true };
  }
  if (command.action === "reference") {
    return referencePath(await rpc(client, "ai_generation_page_reference_path", lease), workspace);
  }
  // This RPC checks a live lease for running pages, and intentionally returns an idempotency
  // target for an already-succeeded page. The latter must never mint a new write capability.
  const target = uploadTarget(await rpc(client, "ai_generation_page_upload_target", lease), workspace, command.page_id);
  if (command.action === "issue-upload") {
    if (target.alreadySucceeded) throw new EdgeError(409, "ALREADY_SUCCEEDED", "该页已经完成，无需再次上传");
    const { data, error } = await client.storage.from(BUCKET).createSignedUploadUrl(target.storagePath, { upsert: false });
    if (error) throw unavailable();
    if (!record(data) || data.path !== target.storagePath || typeof data.token !== "string" ||
        data.token.length > 8192 || data.token.trim() !== data.token || !/^[\w-]+\.[\w-]+\.[\w-]+$/.test(data.token)) throw badResponse();
    return { storagePath: target.storagePath, contentType: target.contentType, uploadToken: data.token };
  }
  if (command.action === "fail") {
    // M04 fail checks the lease token atomically, but not its expiry. The target preflight above
    // blocks already-expired leases; the RPC signature cannot close the expiry race here.
    return job(await rpc(client, "ai_generation_page_fail", { ...lease, p_error: SAFE_FAILURE }), true);
  }
  if (command.action !== "complete") throw invalid();
  if (command.content_type !== target.contentType) throw badImage();
  const digest = await verifyImage(deps, serviceConfig, target.storagePath, target.contentType, command.content_sha256);
  return job(await rpc(client, "ai_generation_page_succeed", {
    ...lease, p_storage_path: target.storagePath, p_content_type: target.contentType,
    p_content_sha256: digest, p_width: command.width, p_height: command.height,
  }));
}

const authOptions = { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false };
const defaults: Dependencies = {
  env: (name) => Deno.env.get(name),
  async user({ url, key }, jwt) {
    const { createClient } = await import("npm:@supabase/supabase-js@2.119.2");
    return createClient(url, key, { auth: authOptions, global: { headers: { Authorization: `Bearer ${jwt}` } } });
  },
  async service({ url, key }) {
    const { createClient } = await import("npm:@supabase/supabase-js@2.119.2");
    return createClient(url, key, { auth: authOptions });
  },
  fetch: (input, init) => fetch(input, init),
};

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
      "x-content-type-options": "nosniff", ...(status === 405 ? { allow: "POST" } : {}),
    },
  });
}

export function createHandler(overrides: Partial<Dependencies> = {}) {
  const deps = { ...defaults, ...overrides };
  return async (req: Request): Promise<Response> => {
    try {
      const { pathname, search } = new URL(req.url);
      if (search || (pathname !== ROUTE && pathname !== `/functions/v1${ROUTE}`)) {
        throw new EdgeError(404, "NOT_FOUND", "接口不存在");
      }
      if (req.method !== "POST") throw new EdgeError(405, "METHOD_NOT_ALLOWED", "仅支持 POST 请求");
      const command = parse(await readJson(req));
      const workspace = setting(deps, "GUANYE_WORKSPACE_ID").toLowerCase();
      const url = setting(deps, "SUPABASE_URL").replace(/\/+$/, "");
      const project = URL.parse(url);
      if (!uuid(workspace) || !project || !["https:", "http:"].includes(project.protocol) ||
          project.username || project.password || project.search || project.hash || project.pathname !== "/") {
        throw new EdgeError(500, "CONFIG_INVALID", "图片工作服务配置无效");
      }
      const actor = await authenticate(req, deps, workspace, url);
      const config = { url, key: setting(deps, "SUPABASE_SERVICE_ROLE_KEY") };
      const client = await deps.service(config);
      return json(await run(deps, client, config, workspace, actor, command));
    } catch (error) {
      const failure = error instanceof EdgeError ? error : unavailable();
      return json({
        ok: false, code: failure.code, error: failure.message,
        ...(failure.status === 401 ? { refreshable: true } : {}),
        ...([502, 503].includes(failure.status) || failure.code === "UPLOAD_NOT_FOUND" ? { retryable: true } : {}),
      }, failure.status);
    }
  };
}

const handler = createHandler();
if (typeof Deno !== "undefined") Deno.serve(handler);
export default handler;
