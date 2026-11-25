#!/usr/bin/env node
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const repo = path.resolve(import.meta.dirname, '..');
const configRaw = await fs.readFile(path.join(repo, 'dist', 'cloud-config.js'), 'utf8');
const configMatch = configRaw.match(/window\.CLOUD_CONFIG\s*=\s*(\{.*\});?\s*$/s);
if (!configMatch) throw new Error('cloud config parse failed');
const config = JSON.parse(configMatch[1]);
const url = String(process.env.TARGET_URL || config.url).replace(/\/+$/, '');
const key = String(process.env.TARGET_ANON_KEY || config.anonKey);
const workspace = String(process.env.TARGET_WORKSPACE_ID || config.workspaceId);
const email = String(process.env.TARGET_EMAIL || 'reviewer5@guanye.local');
const password = process.env.TARGET_PASSWORD;
const libraryRoot = path.resolve(process.env.LOCAL_LIBRARY_ROOT || 'F:/ppt素材库/ppt-material-library-public');
const importSchema = 'local-ppt-material-library-import-v1';
if (!password) throw new Error('TARGET_PASSWORD missing');

const catalog = JSON.parse(await fs.readFile(path.join(libraryRoot, 'data', 'catalog.json'), 'utf8'));
if (!Array.isArray(catalog.pages) || catalog.pages.length === 0) throw new Error('catalog has no pages');
const expectedPages = new Map();
for (const page of catalog.pages) {
  if (!page.source_id || !page.page_id || !/^[a-f0-9]{64}$/i.test(page.sha256)) throw new Error('invalid catalog page');
  const pages = expectedPages.get(page.source_id) ?? new Map();
  if (pages.has(page.page_id)) throw new Error('duplicate catalog page ID within a source');
  pages.set(page.page_id, page.sha256.toLowerCase());
  expectedPages.set(page.source_id, pages);
}
const expectedSourceIds = new Set(expectedPages.keys());
const expectedPageCount = catalog.pages.length;

const authResponse = await fetch(`${url}/auth/v1/token?grant_type=password`, {
  redirect: 'error',
  signal: AbortSignal.timeout(30000),
  method: 'POST',
  headers: { apikey: key, 'content-type': 'application/json' },
  body: JSON.stringify({ email, password }),
});
if (!authResponse.ok) throw new Error(`auth ${authResponse.status} ${await authResponse.text()}`);
const auth = await authResponse.json();
const headers = { apikey: key, Authorization: `Bearer ${auth.access_token}`, 'content-type': 'application/json' };
async function rpc(name, args) {
  const response = await fetch(`${url}/rest/v1/rpc/${name}`, {
    redirect: 'error',
  signal: AbortSignal.timeout(30000),
  method: 'POST',
    headers,
    body: JSON.stringify(args),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${name} ${response.status} ${text.slice(0, 800)}`);
  return JSON.parse(text);
}
function provenanceOf(sample) {
  return sample?.provenance && typeof sample.provenance === 'object' ? sample.provenance : {};
}
function isLocal(sample) {
  return provenanceOf(sample).schema === importSchema;
}
function localSourceId(sample) {
  const value = provenanceOf(sample).sourceId;
  return typeof value === 'string' ? value : null;
}

const counts = await rpc('single_judge_ppt_training_counts', { w: workspace });
const decks = await rpc('single_judge_ppt_training_samples', { w: workspace, p_scope: 'deck', p_limit: 2000 });
const references = await rpc('single_judge_cover_references', { w: workspace });

if (![decks, references].every(Array.isArray)) throw new Error('invalid training RPC response');
const localDecks = decks.filter(isLocal);
const firstLocalDeck = localDecks.find((deck) => deck.images?.length > 1);
const coverImage = firstLocalDeck?.images[0];
const interiorImage = firstLocalDeck?.images[1];
const pageProbe = interiorImage?.id
  ? await rpc('single_judge_ppt_training_samples', { w: workspace, p_scope: 'page', p_deck_id: firstLocalDeck.deckId, p_page_id: interiorImage.id, p_limit: 1 })
  : [];
const coverProbe = coverImage?.id
  ? await rpc('single_judge_ppt_training_samples', { w: workspace, p_scope: 'cover', p_deck_id: firstLocalDeck.deckId, p_limit: 1 })
  : [];
function checkProbe(rows, scope, image, position) {
  const row = rows?.[0];
  return rows?.length === 1 && isLocal(row) && row.scope === scope && row.deckId === firstLocalDeck?.deckId &&
    row.pageId === image?.id && row.position === position && row.images?.length === 1 && row.images[0]?.path === image?.path;
}
const pageProbePassed = checkProbe(pageProbe, 'page', interiorImage, 2);
const coverProbePassed = checkProbe(coverProbe, 'cover', coverImage, 1);
const localPageCount = localDecks.reduce((sum, sample) => sum + (Array.isArray(sample.images) ? sample.images.length : 0), 0);
const localCoverCount = localDecks.filter((sample) => Array.isArray(sample.images) && sample.images.length > 0).length;
const localSourceIds = new Set(localDecks.map(localSourceId).filter(Boolean));
const localDeckIds = new Set(localDecks.map((sample) => sample.deckId));
const missingSourceIds = [...expectedSourceIds].filter((id) => !localSourceIds.has(id)).sort();
const invalidLocalDecks = [];
const objects = [];
for (const sample of localDecks) {
  const provenance = provenanceOf(sample);
  const sourceId = localSourceId(sample);
  const expected = expectedPages.get(sourceId);
  const images = sample.images;
  if (!expected || !Array.isArray(images) || expected.size !== images.length ||
      provenance.pageCount !== images.length || provenance.pageHashes?.length !== images.length ||
      provenance.scoreAuthority !== 'catalog-proxy' || provenance.humanVerified !== false ||
      new Set(images.map((image) => image?.id)).size !== images.length ||
      images.some((image, index) => image?.position !== index + 1 ||
        image.path !== `${workspace}/${sample.deckId}/preview/${index + 1}.png` ||
        !expected.has(image.id) || provenance.pageHashes[index] !== expected.get(image.id))) {
    invalidLocalDecks.push({ deckId: sample.deckId, sourceId });
    continue;
  }
  for (const image of images) objects.push({ path: image.path, hash: expected.get(image.id) });
}
const localReferences = references.filter((item) => localDeckIds.has(item?.deckId)).length;
const verifyStorage = process.argv.slice(2).includes('--verify-storage');
let objectsVerified = 0;
const storageFailures = [];
if (verifyStorage) {
  let next = 0;
  const worker = async () => {
    while (next < objects.length) {
      const object = objects[next++];
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const encoded = object.path.split('/').map(encodeURIComponent).join('/');
          const response = await fetch(`${url}/storage/v1/object/authenticated/ppt-library/${encoded}`, {
            headers, redirect: 'error', signal: AbortSignal.timeout(30000),
          });
          if (!response.ok) {
            await response.body?.cancel();
            const error = new Error(`HTTP ${response.status}`);
            error.retryable = response.status === 429 || response.status >= 500;
            throw error;
          }
          const hash = createHash('sha256');
          let bytes = 0;
          for await (const chunk of response.body) {
            bytes += chunk.length;
            if (bytes > 25 * 1024 * 1024) throw new Error('image exceeds 25 MiB');
            hash.update(chunk);
          }
          if (hash.digest('hex') !== object.hash) throw new Error('SHA-256 mismatch');
          objectsVerified++;
          break;
        } catch (error) {
          if (attempt === 0 && (error.retryable || ['TimeoutError', 'AbortError', 'TypeError'].includes(error.name))) {
            await new Promise((resolve) => setTimeout(resolve, 1000));
            continue;
          }
          storageFailures.push({ path: object.path, reason: error.message });
          break;
        }
      }
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));
}
const result = {
  workspace,
  expectedLocalLibrary: { sourceCount: expectedSourceIds.size, pageCount: expectedPageCount, root: libraryRoot },
  counts,
  trainingSamples: {
    deckTotal: Array.isArray(decks) ? decks.length : null,
    localDecks: localDecks.length,
    localPages: localPageCount,
    localCovers: localCoverCount,
    pageProbeRows: Array.isArray(pageProbe) ? pageProbe.length : null,
    coverProbeRows: Array.isArray(coverProbe) ? coverProbe.length : null,
    missingLocalSourceIds: missingSourceIds,
    invalidLocalDecks,
    pageProbePassed,
    coverProbePassed,
  },
  references: {
    total: Array.isArray(references) ? references.length : null,
    localInTopReferences: localReferences,
    priorLibraryInTopReferences: references.length - localReferences,
  },
  storage: { checked: verifyStorage, objectsVerified, failureCount: storageFailures.length, examples: storageFailures.slice(0, 5) },
  pass: missingSourceIds.length === 0 && invalidLocalDecks.length === 0 && localDecks.length === expectedSourceIds.size && localPageCount === expectedPageCount && localCoverCount === expectedSourceIds.size && pageProbePassed && coverProbePassed &&
    (!verifyStorage || objectsVerified === expectedPageCount),
};
console.log(JSON.stringify(result, null, 2));
if (!result.pass) process.exitCode = 2;
