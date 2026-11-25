#!/usr/bin/env node
/** Deploy one Edge Function directory through the Supabase Management API. */
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

function required(name) {
  const value = String(process.env[name] || '').trim();
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

const token = required('SUPABASE_ACCESS_TOKEN');
const ref = required('PROJECT_REF');
const slug = required('FUNCTION_SLUG');
const dir = path.resolve(required('FUNCTION_DIR'));
const entrypoint = path.join(dir, 'index.ts');
const body = fs.readFileSync(entrypoint);
const verifyJwt = String(process.env.VERIFY_JWT ?? 'true').toLowerCase() !== 'false';

const form = new FormData();
form.append('metadata', JSON.stringify({
  name: slug,
  entrypoint_path: 'index.ts',
  verify_jwt: verifyJwt,
  static_patterns: [],
}));
form.append('file', new Blob([body], { type: 'application/typescript' }), 'index.ts');

const response = await fetch(`https://api.supabase.com/v1/projects/${ref}/functions/deploy?slug=${encodeURIComponent(slug)}`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}` },
  body: form,
});
const text = await response.text();
if (!response.ok) throw new Error(`deploy HTTP ${response.status}: ${text.slice(0, 800)}`);
let parsed;
try { parsed = JSON.parse(text); } catch { parsed = { raw: text.slice(0, 400) }; }
console.log(JSON.stringify({ slug, verifyJwt, status: response.status, version: parsed.version ?? null, id: parsed.id ?? null }));