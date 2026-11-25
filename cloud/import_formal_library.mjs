#!/usr/bin/env node
/**
 * Idempotently copy the scored formal library into the isolated training workspace.
 * Secrets are supplied only through environment variables; never commit them.
 */
import crypto from 'node:crypto';
import process from 'node:process';

const required = [
  'SOURCE_URL', 'SOURCE_ANON_KEY', 'SOURCE_EMAIL', 'SOURCE_PASSWORD',
  'SOURCE_WORKSPACE_ID', 'TARGET_URL', 'TARGET_ANON_KEY', 'TARGET_EMAIL',
  'TARGET_PASSWORD', 'TARGET_WORKSPACE_ID'
];
for (const name of required) {
  if (!String(process.env[name] || '').trim()) throw new Error(`missing ${name}`);
}

const source = {
  url: process.env.SOURCE_URL.replace(/\/+$/, ''),
  key: process.env.SOURCE_ANON_KEY,
  email: process.env.SOURCE_EMAIL,
  password: process.env.SOURCE_PASSWORD,
  workspace: process.env.SOURCE_WORKSPACE_ID,
};
const target = {
  url: process.env.TARGET_URL.replace(/\/+$/, ''),
  key: process.env.TARGET_ANON_KEY,
  email: process.env.TARGET_EMAIL,
  password: process.env.TARGET_PASSWORD,
  workspace: process.env.TARGET_WORKSPACE_ID,
};
const runId = process.env.IMPORT_RUN_ID || 'formal-library-v1';
const concurrency = Math.max(1, Math.min(12, Number(process.env.IMPORT_CONCURRENCY || 6)));
const encoder = new TextEncoder();

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
function jsonHeaders(auth, extra = {}) {
  return { apikey: auth.key, Authorization: `Bearer ${auth.token}`, ...extra };
}
async function responseText(response) {
  const text = await response.text();
  return text.length > 800 ? `${text.slice(0, 800)}…` : text;
}
async function request(url, options = {}, { retries = 5, label = url } = {}) {
  let last;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, options);
      if (response.ok) return response;
      const body = await responseText(response);
      const retryable = response.status === 408 || response.status === 425 || response.status === 429 || response.status >= 500;
      last = new Error(`${label} HTTP ${response.status}: ${body}`);
      if (!retryable || attempt === retries) throw last;
      await sleep(Math.min(30000, 500 * 2 ** attempt));
    } catch (error) {
      last = error;
      if (attempt === retries) throw error;
      await sleep(Math.min(30000, 500 * 2 ** attempt));
    }
  }
  throw last;
}
async function login(config) {
  const response = await request(`${config.url}/auth/v1/token?grant_type=password`, {
    method: 'POST',
    headers: { apikey: config.key, 'content-type': 'application/json' },
    body: JSON.stringify({ email: config.email, password: config.password }),
  }, { label: `${config.url} auth` });
  const value = await response.json();
  if (!value.access_token || !value.user?.id) throw new Error(`auth response incomplete for ${config.url}`);
  return { ...config, token: value.access_token, userId: value.user.id };
}
async function restJson(auth, path, init = {}, label = path) {
  const response = await request(`${auth.url}${path}`, {
    ...init,
    headers: { ...jsonHeaders(auth), ...(init.body ? { 'content-type': 'application/json' } : {}), ...(init.headers || {}) },
  }, { label });
  if (response.status === 204) return null;
  return response.json();
}
async function readBinary(auth, path, label) {
  const encoded = path.split('/').map(encodeURIComponent).join('/');
  const response = await request(`${auth.url}/storage/v1/object/authenticated/ppt-library/${encoded}`, {
    headers: jsonHeaders(auth),
  }, { label });
  return Buffer.from(await response.arrayBuffer());
}
async function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}
async function uploadSigned(auth, path, token, bytes, contentTypeValue, label) {
  const encoded = path.split('/').map(encodeURIComponent).join('/');
  await request(`${auth.url}/storage/v1/object/upload/sign/ppt-library/${encoded}?token=${encodeURIComponent(token)}`, {
    method: 'PUT',
    headers: jsonHeaders(auth, { 'content-type': contentTypeValue || 'application/octet-stream' }),
    body: bytes,
  }, { label, retries: 5 });
}
function contentType(path) {
  const lower = path.toLowerCase();
  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.webp')) return 'image/webp';
  if (lower.endsWith('.gif')) return 'image/gif';
  return 'image/jpeg';
}
function sourceRecordsByDeck(records) {
  const grouped = new Map();
  for (const row of records) {
    if (!row?.deck_id || !row?.data || typeof row.data !== 'object') continue;
    const list = grouped.get(row.deck_id) || [];
    list.push(row);
    grouped.set(row.deck_id, list);
  }
  return grouped;
}
function scoreIsValid(data) {
  const scores = data?.scores;
  return scores && typeof scores === 'object'
    && Number.isFinite(Number(scores.visual)) && Number.isFinite(Number(scores.layout));
}
function chooseLabel(records) {
  const valid = records.filter(row => scoreIsValid(row.data));
  if (!valid.length) return null;
  return [...valid].sort((a, b) => {
    const at = Date.parse(a.data.updatedAt || '') || 0;
    const bt = Date.parse(b.data.updatedAt || '') || 0;
    return bt - at || String(a.user_id).localeCompare(String(b.user_id));
  })[0];
}
function safeName(value, fallback) {
  const raw = String(value || fallback).replace(/[^A-Za-z0-9._-]+/g, '_');
  return raw || fallback;
}
function targetDeckId(sourceWorkspace, sourceDeckId) {
  const hex = crypto.createHash('sha256').update(`formal-library:${sourceWorkspace}:${sourceDeckId}`).digest('hex');
  return `import-${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-8${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}
function libraryPath(workspace, deckId, index) {
  return `${workspace}/${deckId}/preview/${index + 1}.png`;
}
function normalizeReview(row) {
  const data = row.data || {};
  return {
    mode: 'deck',
    note: typeof data.note === 'string' ? data.note : '',
    scores: { visual: Number(data.scores.visual), layout: Number(data.scores.layout) },
    favorite: data.favorite === true,
    updatedAt: data.updatedAt || null,
  };
}
function provenance(records, selected, deck, sourceImageDigests, sourceGroupSha256) {
  return {
    schema: 'formal-library-import-v1',
    runId,
    labelScope: 'deck',
    sourceProjectRef: new URL(source.url).hostname.split('.')[0],
    sourceWorkspaceId: source.workspace,
    sourceDeckId: deck.id,
    sourceReviewRecords: records.map(row => ({
      userId: row.user_id,
      data: row.data,
    })),
    selectedReviewUserId: selected.user_id,
    selectedReviewUpdatedAt: selected.data.updatedAt || null,
    sourceImageSha256: sourceImageDigests,
    sourceGroupSha256,
  };
}

console.log(`formal import start run=${runId}`);
const [sourceAuth, targetAuth] = await Promise.all([login(source), login(target)]);
console.log(`authenticated sourceUser=${sourceAuth.userId} targetUser=${targetAuth.userId}`);
const [decks, reviewRows] = await Promise.all([
  restJson(sourceAuth, `/rest/v1/decks?select=id,workspace_id,source_kind,data&workspace_id=eq.${encodeURIComponent(source.workspace)}&order=id&limit=1000`, {}, 'source decks'),
  restJson(sourceAuth, `/rest/v1/reviews?select=workspace_id,user_id,deck_id,data&workspace_id=eq.${encodeURIComponent(source.workspace)}&order=deck_id&limit=1000`, {}, 'source reviews'),
]);
const byDeck = sourceRecordsByDeck(reviewRows);
const selected = [];
for (const deck of decks) {
  const rows = byDeck.get(deck.id) || [];
  const chosen = chooseLabel(rows);
  if (chosen) selected.push({ deck, rows, chosen });
}
const selectedPageCount = selected.reduce((sum, entry) => sum + (Array.isArray(entry.deck.data?.images) ? entry.deck.data.images.length : 0), 0);
const selectedScores = selected.flatMap(entry => Object.values(entry.chosen.data?.scores || {}).map(Number).filter(Number.isFinite));
const lowScoreCount = selectedScores.filter(score => score <= 2).length;
const highScoreCount = selectedScores.filter(score => score >= 4).length;
console.log(`source decks=${decks.length} reviewRows=${reviewRows.length} scoredDecks=${selected.length} scoredPages=${selectedPageCount} lowScores=${lowScoreCount} highScores=${highScoreCount}`);
if (selected.length !== 127) throw new Error(`expected 127 scored decks, got ${selected.length}`);
if (selectedPageCount !== 983) throw new Error(`expected 983 scored pages, got ${selectedPageCount}`);
if (lowScoreCount < 1 || highScoreCount < 1) throw new Error(`expected both low and high formal scores, got low=${lowScoreCount} high=${highScoreCount}`);

let completed = 0;
const jobs = [];
for (const entry of selected) {
  jobs.push(async () => {
    const sourceDeck = structuredClone(entry.deck.data);
    const sourceImageDigests = {};
    const pageBytes = [];
    const pageSizes = [];
    const pageHashes = [];
    const pages = [];
    if (!Array.isArray(sourceDeck.images) || sourceDeck.images.length < 1) throw new Error(`deck ${entry.deck.id} has no images`);
    const deckId = targetDeckId(source.workspace, entry.deck.id);
    for (let index = 0; index < sourceDeck.images.length; index += 1) {
      const image = sourceDeck.images[index];
      const sourcePath = String(image.path || "");
      if (!sourcePath) throw new Error(`deck ${entry.deck.id} page ${index + 1} missing path`);
      const raw = await readBinary(sourceAuth, sourcePath, `source ${entry.deck.id} page ${index + 1}`);
      const sourceDigest = await sha256(raw);
      sourceImageDigests[sourcePath] = sourceDigest;
      pageBytes.push({ raw, sourcePath });
      pageSizes.push(raw.length);
      pageHashes.push(sourceDigest);
      pages.push({ id: `page-${index + 1}`, name: String(image.name || `第 ${index + 1} 页`), position: index + 1 });
    }
    const sourceGroupSha256 = await sha256(encoder.encode(JSON.stringify({ sourceWorkspace: source.workspace, sourceDeckId: entry.deck.id, pages: sourceImageDigests })));
    const trainingImport = provenance(entry.rows, entry.chosen, entry.deck, sourceImageDigests, sourceGroupSha256);
    const metadata = { name: sourceDeck.name || sourceDeck.title || entry.deck.id, style: sourceDeck.style || null, trainingImport };
    const command = { workspaceId: target.workspace, deckId, title: String(sourceDeck.title || sourceDeck.name || entry.deck.id), metadata, pages, pageSizes, pageHashes, review: normalizeReview(entry.chosen), sourceGroupSha256 };
    const prepared = await restJson(targetAuth, "/functions/v1/single-judge-import", {
      method: "POST", body: JSON.stringify({ action: "library-prepare", ...command }),
    }, `prepare deck ${entry.deck.id}`);
    const uploads = Array.isArray(prepared?.uploads) ? prepared.uploads : [];
    const byPath = new Map(pageBytes.map((item, index) => [libraryPath(target.workspace, deckId, index), { ...item, index }]));
    for (const item of uploads) {
      if (!item || typeof item.path !== "string" || typeof item.token !== "string" || !byPath.has(item.path)) throw new Error(`invalid upload manifest for ${entry.deck.id}`);
      const page = byPath.get(item.path);
      await uploadSigned(targetAuth, item.path, item.token, page.raw, contentType(page.sourcePath), `target ${entry.deck.id} page ${page.index + 1}`);
    }
    const published = await restJson(targetAuth, "/functions/v1/single-judge-import", {
      method: "POST", body: JSON.stringify({ action: "library-publish", ...command }),
    }, `publish deck ${entry.deck.id}`);
    if (published?.ok !== true || published.deckId !== deckId || typeof published.idempotent !== "boolean") throw new Error(`publish not confirmed for ${entry.deck.id}`);
    completed += 1;
    if (completed % 10 === 0 || completed === selected.length) console.log(`decks imported ${completed}/${selected.length}`);
    return { id: deckId, sourceId: entry.deck.id, pages: pages.length, label: normalizeReview(entry.chosen), provenance: trainingImport, idempotent: published.idempotent };
  });
}
const imported = [];
for (let offset = 0; offset < jobs.length; offset += concurrency) {
  const batch = await Promise.all(jobs.slice(offset, offset + concurrency).map(job => job()));
  imported.push(...batch);
}

console.log(`formal import complete decks=${imported.length} pages=${imported.reduce((sum, item) => sum + item.pages, 0)} labels=${imported.length}`);


