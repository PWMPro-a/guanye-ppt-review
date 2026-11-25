#!/usr/bin/env node
/**
 * Apply one SQL migration through the Supabase Management API using the
 * project's existing guanye_internal.schema_migrations history contract.
 * Secrets come only from the environment.
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

function required(name) {
  const value = String(process.env[name] || '').trim();
  if (!value) throw new Error(`missing ${name}`);
  return value;
}

function normalizeLf(text) {
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function stripOuterTransaction(sql) {
  let text = normalizeLf(sql).trim();
  const begin = text.match(/^([ \t]*begin[ \t]*;[ \t]*\n)/im);
  if (begin) {
    const prefix = text.slice(0, begin.index);
    if (prefix.split('\n').some(line => line.trim() && !line.trimStart().startsWith('--'))) {
      throw new Error('migration transaction control must appear before SQL');
    }
    text = text.slice(0, begin.index) + text.slice(begin.index + begin[0].length);
  }
  const commit = text.match(/\n[ \t]*commit[ \t]*;[ \t]*\s*$/i);
  if (commit) text = text.slice(0, commit.index).trimEnd();
  return text;
}

function sqlLiteral(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function atomicSql(body) {
  let sql = body.trim();
  if (!sql.endsWith(';')) sql += ';';
  for (const tag of ['$guanye_migration$', '$guanye_migration_body$']) {
    if (sql.includes(tag)) throw new Error('migration body uses a reserved dollar-quote tag');
  }
  return `do $guanye_migration$\nbegin\n execute $guanye_migration_body$\n${sql}\n$guanye_migration_body$;\nend\n$guanye_migration$;`;
}

const token = required('SUPABASE_ACCESS_TOKEN');
const ref = required('PROJECT_REF');
const file = path.resolve(required('MIGRATION_FILE'));
const version = path.basename(file, '.sql');
const raw = fs.readFileSync(file);
const text = normalizeLf(raw.toString('utf8'));
const checksum = crypto.createHash('sha256').update(text, 'utf8').digest('hex');
const body = stripOuterTransaction(text);
const record = `insert into guanye_internal.schema_migrations(version,checksum) values (${sqlLiteral(version)}, ${sqlLiteral(checksum)});`;
const query = atomicSql(`${body}\n${record}`);

const response = await fetch(`https://api.supabase.com/v1/projects/${ref}/database/query`, {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'content-type': 'application/json' },
  body: JSON.stringify({ query }),
});
const payload = await response.text();
if (!response.ok) throw new Error(`migration HTTP ${response.status}: ${payload.slice(0, 800)}`);
console.log(JSON.stringify({ version, checksum, applied: true }));