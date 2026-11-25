// Server-to-server only: no CORS headers are returned.
// Function-level JWT verification stays enabled by default; every path that reaches
// role-based storage/RPC access also validates the caller with Auth getUser.

const BUCKET = "ppt-library";
const MAX_PAGES = 600;
const MIB = 1024 * 1024;
const MAX_PAGE_BYTES = 25 * MIB;
const MAX_TOTAL_PAGE_BYTES = 300 * MIB;
const MAX_ORIGINAL_BYTES = 25 * MIB;
const MAX_BODY_BYTES = 64 * 1024;
const STORAGE_CONCURRENCY = 8;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DECK_ID_PATTERN = /^import-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

type Action = "prepare" | "publish" | "library-prepare" | "library-publish";
type AccessRole = "judge" | "admin" | "submitter";
type JsonObject = Record<string, unknown>;

interface NormalizedImport {
  action: "prepare" | "publish";
  aiAllowed: boolean;
  workspaceId: string;
  deckId: string;
  title: string;
  original: {
    name: string;
    sha256: string;
    sizeBytes: number;
  };
  pageSizes: number[];
  pageHashes: string[];
}

interface LibraryPage {
  id: string;
  name: string;
  position: number;
}

interface NormalizedLibraryImport {
  action: "library-prepare" | "library-publish";
  workspaceId: string;
  deckId: string;
  title: string;
  metadata: JsonObject;
  pages: LibraryPage[];
  pageSizes: number[];
  pageHashes: string[];
  review: JsonObject;
  sourceGroupSha256: string;
}

interface StorageErrorLike {
  message?: string;
  status?: number;
  statusCode?: number;
  code?: string;
}

interface StorageInfoResult {
  data: unknown;
  error: StorageErrorLike | null;
}

interface SignedUploadResult {
  data: { token?: string; path?: string } | null;
  error: StorageErrorLike | null;
}

interface RpcResult {
  data: unknown;
  error: StorageErrorLike | null;
}

interface AuthResult {
  data: { user: { id?: string } | null };
  error: StorageErrorLike | null;
}

interface SupabaseClientLike {
  auth: {
    getUser(jwt?: string): Promise<AuthResult>;
  };
  storage: {
    from(bucket: string): {
      info(path: string): Promise<StorageInfoResult>;
      download(path: string): Promise<{ data: Blob | null; error: StorageErrorLike | null }>;
      createSignedUploadUrl(
        path: string,
        options?: { upsert?: boolean },
      ): Promise<SignedUploadResult>;
    };
  };
  rpc(name: string, args?: JsonObject): Promise<RpcResult>;
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
  readSupabaseUrl(): string;
  readServiceRoleKey(): string;
  createUserClient(config: ClientConfig): Promise<SupabaseClientLike>;
  createServiceClient(config: ServiceClientConfig): Promise<SupabaseClientLike>;
}

interface ObjectSpec {
  path: string;
  sizeBytes: number;
}

class HttpError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.code = code;
  }
}

function isRecord(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requiredEnv(name: string): string {
  const deno = (globalThis as { Deno?: { env?: { get(key: string): string | undefined } } }).Deno;
  const value = deno?.env?.get(name);
  if (typeof value !== "string" || value.trim() === "") {
    throw new HttpError(500, "EDGE_CONFIG_INVALID", "边缘函数配置不完整");
  }
  return value;
}

function defaultDependencies(): HandlerDependencies {
  return {
    readSupabaseUrl: () => requiredEnv("SUPABASE_URL"),
    readServiceRoleKey: () => requiredEnv("SUPABASE_SERVICE_ROLE_KEY"),
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
      }) as unknown as SupabaseClientLike;
    },
    async createServiceClient(config) {
      const { createClient } = await import("npm:@supabase/supabase-js@2.119.2");
      return createClient(config.url, config.apiKey, {
        auth: {
          persistSession: false,
          autoRefreshToken: false,
          detectSessionInUrl: false,
        },
      }) as unknown as SupabaseClientLike;
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
    return jsonResponse(
      { error: { code: error.code, message: error.message } },
      error.status,
    );
  }
  console.error("[single-judge-import] request_failed");
  return jsonResponse(
    { error: { code: "INTERNAL_ERROR", message: "导入服务暂时不可用" } },
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

function unicodeLength(value: string): number {
  return Array.from(value).length;
}

function parsePositiveByteSize(value: unknown, maxBytes: number, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0 || (value as number) > maxBytes) {
    throw new HttpError(400, "INVALID_SIZE", `${label}大小无效`);
  }
  return value as number;
}

async function sha256Hex(blob: Blob): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()));
  return Array.from(digest, (value) => value.toString(16).padStart(2, "0")).join("");
}

function parseImportRequest(value: unknown): NormalizedImport {
  if (!isRecord(value)) {
    throw new HttpError(400, "INVALID_JSON", "请求体必须是 JSON 对象");
  }
  assertExactKeys(value, [
    "action",
    "aiAllowed",
    "workspaceId",
    "deckId",
    "title",
    "original",
    "pageSizes",
    "pageHashes",
  ]);

  if (value.action !== "prepare" && value.action !== "publish") {
    throw new HttpError(400, "INVALID_ACTION", "仅支持 prepare 或 publish");
  }
  if (typeof value.aiAllowed !== "boolean") {
    throw new HttpError(400, "INVALID_AI_ALLOWED", "aiAllowed 必须是布尔值");
  }
  if (typeof value.workspaceId !== "string" || !UUID_PATTERN.test(value.workspaceId)) {
    throw new HttpError(400, "INVALID_WORKSPACE", "workspaceId 必须是小写规范 UUID");
  }
  if (typeof value.deckId !== "string" || !DECK_ID_PATTERN.test(value.deckId)) {
    throw new HttpError(400, "INVALID_DECK_ID", "deckId 必须为 import- 加小写规范 UUID");
  }
  if (
    typeof value.title !== "string" ||
    value.title.trim() === "" ||
    unicodeLength(value.title.trim()) > 100 ||
    /[\u0000-\u001f\u007f]/.test(value.title)
  ) {
    throw new HttpError(400, "INVALID_TITLE", "title 长度或字符无效");
  }
  if (!isRecord(value.original)) {
    throw new HttpError(400, "INVALID_ORIGINAL", "original 必须是对象");
  }
  assertExactKeys(value.original, ["name", "sha256", "sizeBytes"]);

  const name = value.original.name;
  if (
    typeof name !== "string" ||
    unicodeLength(name.trim()) < 1 ||
    unicodeLength(name) > 255 ||
    name.includes("/") ||
    name.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(name)
  ) {
    throw new HttpError(400, "INVALID_ORIGINAL_NAME", "原始文件名无效");
  }
  const dot = name.lastIndexOf(".");
  const extension = dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (extension !== "ppt" && extension !== "pptx" && extension !== "pdf") {
    throw new HttpError(400, "INVALID_ORIGINAL_TYPE", "原始文件仅支持 ppt、pptx 或 pdf");
  }
  if (typeof value.original.sha256 !== "string" || !SHA256_PATTERN.test(value.original.sha256)) {
    throw new HttpError(400, "INVALID_SHA256", "sha256 必须是小写 64 位十六进制");
  }
  const originalSize = parsePositiveByteSize(
    value.original.sizeBytes,
    MAX_ORIGINAL_BYTES,
    "原始文件",
  );

  if (!Array.isArray(value.pageSizes) || value.pageSizes.length < 1 || value.pageSizes.length > MAX_PAGES) {
    throw new HttpError(400, "INVALID_PAGE_COUNT", "pageSizes 必须包含 1 到 600 页");
  }

  let totalPageBytes = 0;
  const pageSizes = value.pageSizes.map((item, index) => {
    const size = parsePositiveByteSize(item, MAX_PAGE_BYTES, `第 ${index + 1} 页`);
    totalPageBytes += size;
    return size;
  });
  if (totalPageBytes > MAX_TOTAL_PAGE_BYTES) {
    throw new HttpError(400, "PAGE_TOTAL_TOO_LARGE", "页面总大小超过 300 MiB");
  }

  // pageHashes is required. The professional importer is the only client and has
  // not shipped without this field, so a pageSizes-only protocol would silently
  // remove the preview content binding instead of providing safe compatibility.
  if (!Array.isArray(value.pageHashes)) {
    throw new HttpError(400, "INVALID_PAGE_HASHES", "pageHashes 必须是哈希数组");
  }
  if (value.pageHashes.length !== pageSizes.length) {
    throw new HttpError(400, "INVALID_PAGE_HASH_COUNT", "pageHashes 必须与 pageSizes 一一对应");
  }
  const seenPageHashes = new Set<string>();
  const pageHashes = value.pageHashes.map((item) => {
    if (typeof item !== "string" || !SHA256_PATTERN.test(item)) {
      throw new HttpError(400, "INVALID_PAGE_HASHES", "每页必须提供小写 64 位十六进制 SHA-256");
    }
    if (seenPageHashes.has(item)) {
      throw new HttpError(400, "DUPLICATE_PAGE_HASH", "pageHashes 不能包含重复哈希");
    }
    seenPageHashes.add(item);
    return item;
  });

  return {
    action: value.action,
    aiAllowed: value.aiAllowed,
    workspaceId: value.workspaceId,
    deckId: value.deckId,
    title: value.title.trim(),
    original: {
      name,
      sha256: value.original.sha256,
      sizeBytes: originalSize,
    },
    pageSizes,
    pageHashes,
  };
}

function parseLibraryRequest(value: unknown): NormalizedLibraryImport {
  if (!isRecord(value)) throw new HttpError(400, "INVALID_JSON", "请求体必须是 JSON 对象");
  assertExactKeys(value, [
    "action", "workspaceId", "deckId", "title", "metadata", "pages",
    "pageSizes", "pageHashes", "review", "sourceGroupSha256",
  ]);
  if (value.action !== "library-prepare" && value.action !== "library-publish") {
    throw new HttpError(400, "INVALID_ACTION", "训练素材请求动作无效");
  }
  if (typeof value.workspaceId !== "string" || !UUID_PATTERN.test(value.workspaceId)) {
    throw new HttpError(400, "INVALID_WORKSPACE", "workspaceId 必须是小写规范 UUID");
  }
  if (typeof value.deckId !== "string" || !DECK_ID_PATTERN.test(value.deckId)) {
    throw new HttpError(400, "INVALID_DECK_ID", "deckId 必须为 import- 加小写规范 UUID");
  }
  if (typeof value.title !== "string" || value.title.trim() === "" || unicodeLength(value.title.trim()) > 100 || /[\u0000-\u001f\u007f]/.test(value.title)) {
    throw new HttpError(400, "INVALID_TITLE", "title 长度或字符无效");
  }
  if (!isRecord(value.metadata) || JSON.stringify(value.metadata).length > 24000) {
    throw new HttpError(400, "INVALID_METADATA", "训练素材元数据无效");
  }
  if (!Array.isArray(value.pages) || value.pages.length < 1 || value.pages.length > MAX_PAGES) {
    throw new HttpError(400, "INVALID_PAGE_COUNT", "pages 必须包含 1 到 600 页");
  }
  const pages = value.pages.map((item, index) => {
    if (!isRecord(item)) throw new HttpError(400, "INVALID_PAGE", "页面元数据无效");
    assertExactKeys(item, ["id", "name", "position"]);
    if (typeof item.id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(item.id) || typeof item.name !== "string" || unicodeLength(item.name) > 255 || typeof item.position !== "number" || item.position !== index + 1) {
      throw new HttpError(400, "INVALID_PAGE", "页面标识或页序无效");
    }
    return { id: item.id, name: item.name, position: item.position };
  });
  if (!Array.isArray(value.pageSizes) || value.pageSizes.length !== pages.length) {
    throw new HttpError(400, "INVALID_PAGE_SIZES", "pageSizes 必须与页面一一对应");
  }
  let totalPageBytes = 0;
  const pageSizes = value.pageSizes.map((item, index) => {
    const size = parsePositiveByteSize(item, MAX_PAGE_BYTES, "第 " + (index + 1) + " 页");
    totalPageBytes += size;
    return size;
  });
  if (totalPageBytes > MAX_TOTAL_PAGE_BYTES) throw new HttpError(400, "PAGE_TOTAL_TOO_LARGE", "页面总大小超过 300 MiB");
  if (!Array.isArray(value.pageHashes) || value.pageHashes.length !== pages.length || value.pageHashes.some((item) => typeof item !== "string" || !SHA256_PATTERN.test(item))) {
    throw new HttpError(400, "INVALID_PAGE_HASHES", "每页必须提供小写 64 位十六进制 SHA-256");
  }
  if (!isRecord(value.review) || JSON.stringify(value.review).length > 24000 || !isRecord(value.review.scores)) {
    throw new HttpError(400, "INVALID_REVIEW", "训练评分无效");
  }
  for (const [key, score] of Object.entries(value.review.scores)) {
    if (!/^[A-Za-z0-9_-]{1,80}$/.test(key) || typeof score !== "number" || !Number.isFinite(score) || score < 0 || score > 5) {
      throw new HttpError(400, "INVALID_REVIEW", "训练评分超出范围");
    }
  }
  if (typeof value.sourceGroupSha256 !== "string" || !SHA256_PATTERN.test(value.sourceGroupSha256)) {
    throw new HttpError(400, "INVALID_SOURCE_HASH", "sourceGroupSha256 无效");
  }
  return { action: value.action, workspaceId: value.workspaceId, deckId: value.deckId, title: value.title.trim(), metadata: value.metadata, pages, pageSizes, pageHashes: [...value.pageHashes], review: value.review, sourceGroupSha256: value.sourceGroupSha256 };
}
function readBearerToken(request: Request): string {
  const authorization = request.headers.get("authorization");
  const match = authorization?.match(/^Bearer ([^\s]+)$/i);
  if (!match) {
    throw new HttpError(401, "AUTH_REQUIRED", "需要用户 Bearer JWT");
  }
  return match[1];
}

function readApiKey(request: Request): string {
  const apiKey = request.headers.get("apikey")?.trim();
  if (!apiKey || apiKey.length > 8192) {
    throw new HttpError(401, "API_KEY_REQUIRED", "缺少公开 API key");
  }
  return apiKey;
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

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new HttpError(400, "INVALID_JSON", "请求体不是有效 JSON");
  }
  return parsed;
}

async function authenticateUser(client: SupabaseClientLike, jwt: string): Promise<string> {
  const { data, error } = await client.auth.getUser(jwt);
  if (error || !data.user || typeof data.user.id !== "string" || !UUID_PATTERN.test(data.user.id)) {
    throw new HttpError(401, "AUTH_INVALID", "用户 JWT 无效或已过期");
  }
  return data.user.id;
}

async function authorizeImportAccess(client: SupabaseClientLike, workspaceId: string): Promise<AccessRole> {
  let result: RpcResult;
  try {
    result = await client.rpc("single_judge_access", { w: workspaceId });
  } catch {
    throw new HttpError(503, "ACCESS_CHECK_FAILED", "暂时无法核验工作区导入权限");
  }

  if (result.error) {
    const status = Number(result.error.status ?? result.error.statusCode ?? 0);
    if (status === 401) {
      throw new HttpError(401, "AUTH_INVALID", "用户 JWT 无效或已过期");
    }
    if (status === 403) {
      throw new HttpError(403, "ACCESS_DENIED", "没有该工作区的权限");
    }
    throw new HttpError(503, "ACCESS_CHECK_FAILED", "暂时无法核验工作区导入权限");
  }

  if (!isRecord(result.data) || result.data.mode !== "single-judge") {
    throw new HttpError(403, "IMPORT_ACCESS_REQUIRED", "当前工作区未启用专业版导入");
  }
  const role = result.data.role;
  if (role !== "judge" && role !== "admin" && role !== "submitter") {
    throw new HttpError(403, "IMPORT_ACCESS_REQUIRED", "当前账号没有导入权限");
  }
  return role;
}

function buildPaths(command: NormalizedImport): { originalPath: string; previewPaths: string[] } {
  const extension = command.original.name.slice(command.original.name.lastIndexOf(".") + 1).toLowerCase();
  const originalPath =
    `${command.workspaceId}/${command.deckId}/original/${command.original.sha256}.${extension}`;
  const previewPaths = command.pageSizes.map(
    (_size, index) => `${command.workspaceId}/${command.deckId}/preview/${index + 1}.png`,
  );
  return { originalPath, previewPaths };
}

function buildLibraryPaths(command: NormalizedLibraryImport): string[] {
  return command.pages.map((_page, index) => `${command.workspaceId}/${command.deckId}/preview/${index + 1}.png`);
}
function isMissingStorageError(error: StorageErrorLike): boolean {
  const status = Number(error.status ?? error.statusCode ?? 0);
  return status === 404 || /not found|does not exist|object not found/i.test(error.message ?? "");
}

function storedObjectSize(value: unknown): number | null {
  if (!isRecord(value)) return null;
  // Storage info() exposes FileObjectV2.size; older Storage responses only expose metadata.size.
  // Do not replace a malformed canonical size with user-provided metadata.
  const rawSize = Object.hasOwn(value, "size")
    ? value.size
    : isRecord(value.metadata) ? value.metadata.size : undefined;
  if (typeof rawSize === "number" && Number.isSafeInteger(rawSize) && rawSize >= 0) {
    return rawSize;
  }
  if (typeof rawSize === "string" && /^\d+$/.test(rawSize)) {
    const parsed = Number(rawSize);
    return Number.isSafeInteger(parsed) ? parsed : null;
  }
  return null;
}

async function inspectObject(
  client: SupabaseClientLike,
  spec: ObjectSpec,
): Promise<{ exists: true; sizeBytes: number } | { exists: false }> {
  let result: StorageInfoResult;
  try {
    result = await client.storage.from(BUCKET).info(spec.path);
  } catch {
    throw new HttpError(503, "STORAGE_CHECK_FAILED", "暂时无法核对私有存储");
  }

  if (result.error) {
    if (isMissingStorageError(result.error)) {
      return { exists: false };
    }
    throw new HttpError(503, "STORAGE_CHECK_FAILED", "暂时无法核对私有存储");
  }
  if (!isRecord(result.data)) {
    throw new HttpError(502, "STORAGE_METADATA_INVALID", "私有存储返回了无效的对象信息");
  }

  const size = storedObjectSize(result.data);
  if (size === null) {
    throw new HttpError(502, "STORAGE_METADATA_INVALID", "私有存储缺少对象大小");
  }
  return { exists: true, sizeBytes: size };
}

async function mapWithConcurrency<T, R>(
  items: readonly T[],
  limit: number,
  action: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let nextIndex = 0;

  async function worker(): Promise<void> {
    while (true) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= items.length) {
        return;
      }
      results[index] = await action(items[index], index);
    }
  }

  const workerCount = Math.min(limit, items.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  return results;
}

async function createUploadToken(
  client: SupabaseClientLike,
  spec: ObjectSpec,
): Promise<{ path: string; token: string }> {
  let result: SignedUploadResult;
  try {
    result = await client.storage.from(BUCKET).createSignedUploadUrl(spec.path, {
      upsert: false,
    });
  } catch {
    throw new HttpError(503, "SIGN_UPLOAD_FAILED", "暂时无法签发上传地址");
  }

  if (result.error || !result.data || typeof result.data.token !== "string" || result.data.token === "") {
    throw new HttpError(503, "SIGN_UPLOAD_FAILED", "暂时无法签发上传地址");
  }
  if (typeof result.data.path === "string" && result.data.path !== spec.path) {
    throw new HttpError(502, "SIGNED_PATH_INVALID", "存储服务返回了错误的签名路径");
  }
  return { path: spec.path, token: result.data.token };
}

async function prepareImport(
  serviceClient: SupabaseClientLike,
  command: NormalizedImport,
): Promise<Response> {
  const { originalPath, previewPaths } = buildPaths(command);
  const specs: ObjectSpec[] = [
    { path: originalPath, sizeBytes: command.original.sizeBytes },
    ...previewPaths.map((path, index) => ({
      path,
      sizeBytes: command.pageSizes[index],
    })),
  ];

  const outcomes = await mapWithConcurrency(specs, STORAGE_CONCURRENCY, async (spec) => {
    const info = await inspectObject(serviceClient, spec);
    if (!info.exists) {
      return createUploadToken(serviceClient, spec);
    }
    if (info.sizeBytes !== spec.sizeBytes) {
      throw new HttpError(409, "OBJECT_SIZE_MISMATCH", "已有素材大小不匹配，拒绝覆盖");
    }
    return null;
  });

  const uploads = outcomes.filter(
    (item): item is { path: string; token: string } => item !== null,
  );
  return jsonResponse({ uploads });
}

async function verifyAllObjects(
  serviceClient: SupabaseClientLike,
  specs: readonly ObjectSpec[],
): Promise<void> {
  await mapWithConcurrency(specs, STORAGE_CONCURRENCY, async (spec) => {
    const info = await inspectObject(serviceClient, spec);
    if (!info.exists) {
      throw new HttpError(409, "OBJECT_MISSING", "导入素材尚未全部同步到私有存储");
    }
    if (info.sizeBytes !== spec.sizeBytes) {
      throw new HttpError(409, "OBJECT_SIZE_MISMATCH", "已有素材大小不匹配");
    }
  });
}

async function verifyPreviewObjects(
  serviceClient: SupabaseClientLike,
  previewPaths: readonly string[],
  command: Pick<NormalizedImport, "pageSizes" | "pageHashes">,
): Promise<void> {
  let totalPreviewBytes = 0;
  // Read one preview at a time after metadata checks. Each object is downloaded
  // exactly once, and actual bytes count against the same 300 MiB protocol cap.
  for (let index = 0; index < previewPaths.length; index += 1) {
    let preview: Blob | null;
    try {
      const result = await serviceClient.storage.from(BUCKET).download(previewPaths[index]);
      preview = result.error ? null : result.data;
    } catch {
      preview = null;
    }
    if (!preview) {
      throw new HttpError(503, "PREVIEW_VERIFY_FAILED", "暂时无法核对预览图");
    }
    if (preview.size !== command.pageSizes[index]) {
      throw new HttpError(409, "PREVIEW_SIZE_MISMATCH", "预览图大小与投递清单不一致");
    }
    if (preview.size > MAX_PAGE_BYTES || totalPreviewBytes + preview.size > MAX_TOTAL_PAGE_BYTES) {
      throw new HttpError(409, "PREVIEW_TOTAL_TOO_LARGE", "预览图内容超过大小上限");
    }
    totalPreviewBytes += preview.size;
    if (await sha256Hex(preview) !== command.pageHashes[index]) {
      throw new HttpError(409, "PREVIEW_HASH_MISMATCH", "预览图内容与投递哈希不一致");
    }
  }
}

async function prepareLibraryImport(
  serviceClient: SupabaseClientLike,
  command: NormalizedLibraryImport,
): Promise<Response> {
  const paths = buildLibraryPaths(command);
  const specs = paths.map((path, index) => ({ path, sizeBytes: command.pageSizes[index] }));
  const outcomes = await mapWithConcurrency(specs, STORAGE_CONCURRENCY, async (spec) => {
    const info = await inspectObject(serviceClient, spec);
    if (!info.exists) return createUploadToken(serviceClient, spec);
    if (info.sizeBytes !== spec.sizeBytes) throw new HttpError(409, "OBJECT_SIZE_MISMATCH", "已有训练素材大小不匹配，拒绝覆盖");
    return null;
  });
  return jsonResponse({ uploads: outcomes.filter((item): item is { path: string; token: string } => item !== null) });
}

function libraryDeckData(command: NormalizedLibraryImport, paths: readonly string[]): JsonObject {
  const metadata = { ...command.metadata };
  delete metadata.id;
  delete metadata.images;
  delete metadata.source_kind;
  delete metadata.sourceKind;
  delete metadata.source_group_sha256;
  delete metadata.sourceGroupSha256;
  const name = typeof metadata.name === "string" && metadata.name.trim() !== "" ? metadata.name : command.title;
  return {
    ...metadata,
    id: command.deckId,
    title: command.title,
    name,
    origin: "formal-library",
    images: command.pages.map((page, index) => ({
      id: page.id,
      name: page.name,
      path: paths[index],
      thumbPath: paths[index],
      position: page.position,
    })),
  };
}

async function publishLibraryImport(
  serviceClient: SupabaseClientLike,
  command: NormalizedLibraryImport,
  actor: string,
): Promise<Response> {
  const paths = buildLibraryPaths(command);
  const specs = paths.map((path, index) => ({ path, sizeBytes: command.pageSizes[index] }));
  await verifyAllObjects(serviceClient, specs);
  await verifyPreviewObjects(serviceClient, paths, command);
  let result: RpcResult;
  try {
    result = await serviceClient.rpc("single_judge_publish_training_library", {
      w: command.workspaceId,
      actor,
      deck: libraryDeckData(command, paths),
      review: command.review,
      p_source_group_sha256: command.sourceGroupSha256,
    });
  } catch {
    throw new HttpError(502, "PUBLISH_FAILED", "训练素材发布失败");
  }
  if (result.error) throw publishError(result.error);
  if (!isRecord(result.data) || result.data.ok !== true || typeof result.data.deckId !== "string" || typeof result.data.idempotent !== "boolean") {
    throw new HttpError(502, "PUBLISH_FAILED", "训练素材发布失败");
  }
  return jsonResponse({ ok: true, deckId: result.data.deckId, idempotent: result.data.idempotent });
}
function publishError(error: StorageErrorLike): HttpError {
  const code = typeof error.code === "string" ? error.code : "";
  if (code === "42501") {
    return new HttpError(403, "PUBLISH_FORBIDDEN", "没有权限发布导入");
  }
  if (code === "22023") {
    return new HttpError(400, "PUBLISH_REJECTED", "导入素材校验失败");
  }
  if (code === "42883" || code === "PGRST202") {
    return new HttpError(503, "PUBLISH_UNAVAILABLE", "导入发布接口尚未就绪");
  }
  return new HttpError(502, "PUBLISH_FAILED", "导入发布失败");
}

function safePublishSummary(value: JsonObject): JsonObject {
  if (
    value.ok !== true ||
    typeof value.deckId !== "string" ||
    typeof value.deckVersion !== "string" ||
    typeof value.idempotent !== "boolean"
  ) {
    throw new HttpError(502, "PUBLISH_FAILED", "导入发布失败");
  }
  return {
    ok: true,
    deckId: value.deckId,
    deckVersion: value.deckVersion,
    idempotent: value.idempotent,
  };
}

async function publishImport(
  serviceClient: SupabaseClientLike,
  command: NormalizedImport,
  actor: string,
  role: AccessRole,
): Promise<Response> {
  const { originalPath, previewPaths } = buildPaths(command);
  const specs: ObjectSpec[] = [
    { path: originalPath, sizeBytes: command.original.sizeBytes },
    ...previewPaths.map((path, index) => ({
      path,
      sizeBytes: command.pageSizes[index],
    })),
  ];
  await verifyAllObjects(serviceClient, specs);
  // The source hash is used to group training and holdout samples. Do not trust
  // a browser-supplied digest, even when immutable object size matches.
  let original: Blob | null;
  try {
    const result = await serviceClient.storage.from(BUCKET).download(originalPath);
    original = result.error ? null : result.data;
  } catch {
    original = null;
  }
  if (!original || original.size !== command.original.sizeBytes) {
    throw new HttpError(503, "ORIGINAL_VERIFY_FAILED", "暂时无法核对原始文件");
  }
  const originalHash = await sha256Hex(original);
  if (originalHash !== command.original.sha256) {
    throw new HttpError(409, "ORIGINAL_HASH_MISMATCH", "原始文件与投递摘要不一致");
  }
  await verifyPreviewObjects(serviceClient, previewPaths, command);

  const images = previewPaths.map((path, index) => {
    const position = index + 1;
    return {
      id: `page-${position}`,
      name: `第 ${position} 页.png`,
      path,
      thumbPath: path,
      position,
    };
  });

  let result: RpcResult;
  try {
    result = await serviceClient.rpc("single_judge_publish_upload", {
      w: command.workspaceId,
      actor,
      p_ai_allowed: command.aiAllowed,
      p_deck: {
        id: command.deckId,
        title: command.title,
        images,
      },
      p_original: {
        name: command.original.name,
        sha256: command.original.sha256,
        sizeBytes: command.original.sizeBytes,
        path: originalPath,
      },
    });
  } catch {
    throw new HttpError(502, "PUBLISH_FAILED", "导入发布失败");
  }

  if (result.error) {
    throw publishError(result.error);
  }
  if (!isRecord(result.data)) {
    throw new HttpError(502, "PUBLISH_FAILED", "导入发布失败");
  }
  if (role !== "judge" || !command.aiAllowed) {
    return jsonResponse(safePublishSummary(result.data));
  }
  return jsonResponse(result.data);
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

      const jwt = readBearerToken(request);
      const apiKey = readApiKey(request);
      const body = await parseJsonBody(request);
      const command = isRecord(body) && (body.action === "library-prepare" || body.action === "library-publish")
        ? parseLibraryRequest(body)
        : parseImportRequest(body);
      const supabaseUrl = dependencies.readSupabaseUrl();
      const userClient = await dependencies.createUserClient({
        url: supabaseUrl,
        apiKey,
        jwt,
      });
      const actor = await authenticateUser(userClient, jwt);
      const role = await authorizeImportAccess(userClient, command.workspaceId);

      const serviceRoleKey = dependencies.readServiceRoleKey();
      const serviceClient = await dependencies.createServiceClient({
        url: supabaseUrl,
        apiKey: serviceRoleKey,
      });

      if (command.action === "library-prepare") {
        return await prepareLibraryImport(serviceClient, command);
      }
      if (command.action === "library-publish") {
        return await publishLibraryImport(serviceClient, command, actor);
      }
      if (command.action === "prepare") {
        return await prepareImport(serviceClient, command);
      }
      return await publishImport(serviceClient, command, actor, role);
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
