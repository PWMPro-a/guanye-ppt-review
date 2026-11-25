#!/usr/bin/env node
/**
 * Import the local PPT material-library bundle into the isolated training workspace.
 *
 * This intentionally uses the existing single-judge-import Edge Function so the
 * same storage, SHA-256 verification and idempotent publish path is used as the
 * formal-library import. No Python or credentials are persisted by this script.
 *
 * Required environment:
 *   TARGET_PASSWORD - supplied by the PowerShell DPAPI wrapper, never a CLI arg
 * Optional:
 *   TARGET_EMAIL, TARGET_URL, TARGET_ANON_KEY, TARGET_WORKSPACE_ID
 *   LOCAL_LIBRARY_ROOT, LOCAL_IMPORT_RUN_ID, LOCAL_IMPORT_CONCURRENCY
 */
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const repoRoot = path.resolve(import.meta.dirname, '..');
const configPath = path.join(repoRoot, 'dist', 'cloud-config.js');
const defaultLibraryRoot = 'F:/ppt素材库/ppt-material-library-public';
const libraryRoot = path.resolve(process.env.LOCAL_LIBRARY_ROOT || defaultLibraryRoot);
const runId = String(process.env.LOCAL_IMPORT_RUN_ID || 'local-ppt-material-library-v1').trim();
const concurrency = Math.max(1, Math.min(8, Number(process.env.LOCAL_IMPORT_CONCURRENCY || 4)));
const targetEmail = String(process.env.TARGET_EMAIL || 'reviewer5@guanye.local').trim();
const targetPassword = String(process.env.TARGET_PASSWORD || '').trim();
if (!targetPassword) throw new Error('TARGET_PASSWORD is required through the protected wrapper');

function readConfig(raw) {
  const match = raw.match(/window\.CLOUD_CONFIG\s*=\s*(\{.*\});?\s*$/s);
  if (!match) throw new Error(`cannot parse ${configPath}`);
  return JSON.parse(match[1]);
}
const config = readConfig(await fs.readFile(configPath, 'utf8'));
const target = {
  url: String(process.env.TARGET_URL || config.url).replace(/\/+$/, ''),
  key: String(process.env.TARGET_ANON_KEY || config.anonKey),
  workspace: String(process.env.TARGET_WORKSPACE_ID || config.workspaceId),
};
if (!target.url || !target.key || !target.workspace) throw new Error('target cloud config is incomplete');

const encoder = new TextEncoder();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function authHeaders(token, extra = {}) {
  return { apikey: target.key, Authorization: `Bearer ${token}`, ...extra };
}
async function responseBody(response) {
  const text = await response.text();
  return text.length > 1200 ? `${text.slice(0, 1200)}…` : text;
}
async function request(url, options = {}, label = url, retries = 5) {
  let last;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, options);
      if (response.ok) return response;
      const body = await responseBody(response);
      const retryable = response.status === 408 || response.status === 425 || response.status === 429 || response.status >= 500;
      last = new Error(`${label} HTTP ${response.status}: ${body}`);
      if (!retryable || attempt === retries) throw last;
      await sleep(Math.min(30000, 500 * (2 ** attempt)));
    } catch (error) {
      last = error;
      if (attempt === retries) throw error;
      await sleep(Math.min(30000, 500 * (2 ** attempt)));
    }
  }
  throw last;
}
async function login() {
  const response = await request(`${target.url}/auth/v1/token?grant_type=password`, {
    method: 'POST',
    headers: { apikey: target.key, 'content-type': 'application/json' },
    body: JSON.stringify({ email: targetEmail, password: targetPassword }),
  }, 'target auth');
  const value = await response.json();
  if (!value.access_token || !value.user?.id) throw new Error('target auth response incomplete');
  return { token: value.access_token, userId: value.user.id };
}
async function restJson(auth, pathName, init = {}, label = pathName) {
  const response = await request(`${target.url}${pathName}`, {
    ...init,
    headers: { ...authHeaders(auth.token), ...(init.body ? { 'content-type': 'application/json' } : {}), ...(init.headers || {}) },
  }, label);
  if (response.status === 204) return null;
  return response.json();
}
async function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}
function uuidFromDigest(digest) {
  const bytes = Buffer.from(digest.slice(0, 32), 'hex');
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}
function deckIdFor(sourceId, sourceSha) {
  return `import-${uuidFromDigest(crypto.createHash('sha256').update(`local-library:${runId}:${sourceId}:${sourceSha}`).digest('hex'))}`;
}
function pagePath(relative) {
  const clean = String(relative || '').replaceAll('\\', '/');
  const full = path.resolve(libraryRoot, clean);
  const rootWithSep = libraryRoot.endsWith(path.sep) ? libraryRoot : `${libraryRoot}${path.sep}`;
  if (full !== libraryRoot && !full.startsWith(rootWithSep)) throw new Error(`library path escapes root: ${relative}`);
  return full;
}
function scoreFor(page) {
  // This is a clearly labelled catalog proxy, not a human label. The formal
  // scored library remains the authoritative human-labelled training source.
  const role = String(page.role || '');
  const style = String(page.style || '');
  const confidence = String(page.confidence || '');
  let visual = role === '封面' ? 4.5 : 4.3;
  let layout = role === '封面' ? 4.2 : 4.3;
  if (style === '未确定') { visual -= 0.3; layout -= 0.2; }
  if (confidence === '低') { visual -= 0.2; layout -= 0.2; }
  if (page.small_size === true) { visual -= 0.1; }
  return { visual: Number(visual.toFixed(1)), layout: Number(layout.toFixed(1)) };
}
function titleFor(source, pages) {
  const first = pages.find((page) => page.role === '封面') || pages[0];
  const raw = String(first?.title || source.name || source.source_id).trim();
  return raw.slice(0, 100) || source.source_id;
}
function metadataFor(source, pages, sourceSha, pageHashes) {
  return {
    name: titleFor(source, pages),
    title: titleFor(source, pages),
    style: source.primary_style || source.selected_style || '',
    sourceKind: 'local-ppt-material-library',
    trainingImport: {
      schema: 'local-ppt-material-library-import-v1',
      runId,
      sourceRoot: libraryRoot,
      sourceId: source.source_id,
      batch: source.batch || null,
      sourceFileName: source.file_name || source.name || null,
      sourceSha256: source.sha256 || null,
      sourceGroupSha256: sourceSha,
      pageCount: pages.length,
      pageHashes,
      classificationAuthority: 'catalog-derived',
      scoreAuthority: 'catalog-proxy',
      humanVerified: false,
      note: '素材来自本机 F 盘 PPT 素材库；本批 visual/layout 分为目录分类代理分，不冒充人工评分。正式库人工评分样本另行保留。',
      pages: pages.map((page, index) => ({
        id: page.page_id,
        position: index + 1,
        title: page.title || null,
        role: page.role || null,
        style: page.style || null,
        layout: page.layout || null,
        purpose: page.purpose || null,
        sourcePageNo: page.page_no ?? null,
        smallSize: page.small_size === true,
      })),
    },
  };
}
function contentType(filePath) {
  return filePath.toLowerCase().endsWith('.png') ? 'image/png' : 'image/jpeg';
}
async function uploadSigned(auth, objectPath, token, bytes, contentTypeValue, label) {
  const encoded = objectPath.split('/').map(encodeURIComponent).join('/');
  await request(`${target.url}/storage/v1/object/upload/sign/ppt-library/${encoded}?token=${encodeURIComponent(token)}`, {
    method: 'PUT',
    headers: authHeaders(auth.token, { 'content-type': contentTypeValue }),
    body: bytes,
  }, label);
}
async function importSource(auth, source, pages) {
  const pageBytes = [];
  const pageHashes = [];
  for (const page of pages) {
    const relative = page.image || page.preview;
    if (!relative) throw new Error(`page ${page.page_id} has no image path`);
    const localPath = pagePath(relative);
    const bytes = await fs.readFile(localPath);
    const digest = await sha256(bytes);
    if (page.sha256 && String(page.sha256).toLowerCase() !== digest) {
      throw new Error(`page hash mismatch ${page.page_id}: catalog=${page.sha256} actual=${digest}`);
    }
    pageBytes.push({ bytes, localPath, page });
    pageHashes.push(digest);
  }
  const sourceSha = await sha256(encoder.encode(JSON.stringify({
    schema: 'local-ppt-material-library-source-group-v1',
    sourceId: source.source_id,
    sourceSha256: source.sha256 || null,
    pages: pageHashes,
  })));
  const deckId = deckIdFor(source.source_id, sourceSha);
  const command = {
    workspaceId: target.workspace,
    deckId,
    title: titleFor(source, pages),
    metadata: metadataFor(source, pages, sourceSha, pageHashes),
    pages: pages.map((page, index) => ({ id: String(page.page_id), name: String(page.title || `第 ${index + 1} 页`), position: index + 1 })),
    pageSizes: pageBytes.map((item) => item.bytes.length),
    pageHashes,
    review: {
      mode: 'deck',
      note: 'catalog-proxy; humanVerified=false; formal-library scores remain separate',
      favorite: false,
      updatedAt: new Date().toISOString(),
      scores: scoreFor(pages.find((page) => page.role === '封面') || pages[0]),
    },
    sourceGroupSha256: sourceSha,
  };
  const prepared = await restJson(auth, '/functions/v1/single-judge-import', {
    method: 'POST', body: JSON.stringify({ action: 'library-prepare', ...command }),
  }, `prepare ${source.source_id}`);
  const uploads = Array.isArray(prepared?.uploads) ? prepared.uploads : [];
  const byPath = new Map();
  for (let index = 0; index < pageBytes.length; index += 1) {
    byPath.set(`${target.workspace}/${deckId}/preview/${index + 1}.png`, { ...pageBytes[index], index });
  }
  for (const upload of uploads) {
    const item = byPath.get(upload?.path);
    if (!item || typeof upload.token !== 'string') throw new Error(`invalid upload manifest ${source.source_id}`);
    await uploadSigned(auth, upload.path, upload.token, item.bytes, contentType(item.localPath), `upload ${source.source_id} ${item.index + 1}`);
  }
  const published = await restJson(auth, '/functions/v1/single-judge-import', {
    method: 'POST', body: JSON.stringify({ action: 'library-publish', ...command }),
  }, `publish ${source.source_id}`);
  if (published?.ok !== true || published.deckId !== deckId || typeof published.idempotent !== 'boolean') {
    throw new Error(`publish not confirmed ${source.source_id}`);
  }
  return { sourceId: source.source_id, deckId, pages: pages.length, idempotent: published.idempotent, sourceSha256: sourceSha };
}

const catalog = JSON.parse(await fs.readFile(path.join(libraryRoot, 'data', 'catalog.json'), 'utf8'));
if (!Array.isArray(catalog.sources) || !Array.isArray(catalog.pages)) throw new Error('catalog schema invalid');
const sourceById = new Map(catalog.sources.map((source) => [source.source_id, source]));
const groups = new Map();
for (const page of catalog.pages) {
  const source = sourceById.get(page.source_id);
  if (!source) throw new Error(`missing source ${page.source_id}`);
  const list = groups.get(page.source_id) || [];
  list.push(page);
  groups.set(page.source_id, list);
}
for (const pages of groups.values()) pages.sort((a, b) => Number(a.page_no || 0) - Number(b.page_no || 0) || String(a.page_id).localeCompare(String(b.page_id)));
const sources = [...groups.keys()].sort().map((id) => sourceById.get(id));
console.log(`local import start root=${libraryRoot} sources=${sources.length} pages=${catalog.pages.length} bytes=~80MB run=${runId}`);
const auth = await login();
console.log(`authenticated targetUser=${auth.userId}`);
let completed = 0;
const results = [];
let next = 0;
async function worker() {
  while (true) {
    const index = next;
    next += 1;
    if (index >= sources.length) return;
    const source = sources[index];
    const result = await importSource(auth, source, groups.get(source.source_id));
    results[index] = result;
    completed += 1;
    if (completed % 10 === 0 || completed === sources.length) console.log(`local decks imported ${completed}/${sources.length}`);
  }
}
await Promise.all(Array.from({ length: Math.min(concurrency, sources.length) }, () => worker()));
const report = {
  schema: 'local-ppt-material-library-import-report-v1',
  runId,
  libraryRoot,
  targetWorkspace: target.workspace,
  sourceCount: results.length,
  pageCount: results.reduce((sum, item) => sum + item.pages, 0),
  results,
};
const reportPath = path.join(repoRoot, 'cloud', `local-import-report-${runId}.json`);
await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, { encoding: 'utf8', flag: 'w' });
console.log(`local import complete sources=${report.sourceCount} pages=${report.pageCount} report=${reportPath}`);
