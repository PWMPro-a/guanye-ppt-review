#!/usr/bin/env node
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
const expectedSourceIds = new Set((catalog.pages || []).map((item) => String(item.source_id)));
const expectedPageCount = Array.isArray(catalog.pages) ? catalog.pages.length : 0;

const authResponse = await fetch(`${url}/auth/v1/token?grant_type=password`, {
  method: 'POST',
  headers: { apikey: key, 'content-type': 'application/json' },
  body: JSON.stringify({ email, password }),
});
if (!authResponse.ok) throw new Error(`auth ${authResponse.status} ${await authResponse.text()}`);
const auth = await authResponse.json();
const headers = { apikey: key, Authorization: `Bearer ${auth.access_token}`, 'content-type': 'application/json' };
async function rpc(name, args) {
  const response = await fetch(`${url}/rest/v1/rpc/${name}`, {
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

const localDecks = Array.isArray(decks) ? decks.filter(isLocal) : [];
const firstLocalDeck = localDecks[0] || decks[0] || null;
const firstImage = Array.isArray(firstLocalDeck?.images) ? firstLocalDeck.images[0] : null;
const pageProbe = firstLocalDeck?.deckId && firstImage?.id
  ? await rpc('single_judge_ppt_training_samples', { w: workspace, p_scope: 'page', p_deck_id: firstLocalDeck.deckId, p_page_id: firstImage.id, p_limit: 1 })
  : [];
const coverProbe = firstLocalDeck?.deckId
  ? await rpc('single_judge_ppt_training_samples', { w: workspace, p_scope: 'cover', p_deck_id: firstLocalDeck.deckId, p_limit: 1 })
  : [];
const localPageCount = localDecks.reduce((sum, sample) => sum + (Array.isArray(sample.images) ? sample.images.length : 0), 0);
const localCoverCount = localDecks.filter((sample) => Array.isArray(sample.images) && sample.images.length > 0).length;
const localDeckIds = new Set(localDecks.map(localSourceId).filter(Boolean));
const missingSourceIds = [...expectedSourceIds].filter((id) => !localDeckIds.has(id)).sort();
const invalidLocalDecks = localDecks.filter((sample) => {
  const provenance = provenanceOf(sample);
  const imageCount = Array.isArray(sample.images) ? sample.images.length : 0;
  return provenance.sourceId == null || provenance.pageCount !== imageCount || imageCount < 1;
}).map((sample) => ({ deckId: sample.deckId, sourceId: localSourceId(sample), pageCount: provenanceOf(sample).pageCount, imageCount: Array.isArray(sample.images) ? sample.images.length : 0 }));
const localReferences = Array.isArray(references) ? references.filter((item) => item?.deckId && localDeckIds.has(String(item.deckId))).length : 0;

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
  },
  references: {
    total: Array.isArray(references) ? references.length : null,
    localInTopReferences: localReferences,
    preview: Array.isArray(references) ? references.slice(0, 3) : [],
  },
  pass: missingSourceIds.length === 0 && invalidLocalDecks.length === 0 && localDecks.length === expectedSourceIds.size && localPageCount === expectedPageCount && localCoverCount === expectedSourceIds.size && Array.isArray(pageProbe) && pageProbe.length === 1 && Array.isArray(coverProbe) && coverProbe.length === 1,
};
console.log(JSON.stringify(result, null, 2));
if (!result.pass) process.exitCode = 2;
