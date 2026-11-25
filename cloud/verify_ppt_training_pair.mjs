import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';

const [scope, casePath, outputPath] = process.argv.slice(2);
assert(scope === 'page' || scope === 'deck', 'Usage: <page|deck> <case.json> <new-output.json>');
assert(casePath && outputPath, 'Specify input and new evidence paths');
const configSource = await readFile(new URL('../dist/cloud-config.js', import.meta.url), 'utf8');
const config = JSON.parse(configSource.match(/window\.CLOUD_CONFIG\s*=\s*(\{.*\});?\s*$/s)[1]);
const { url, anonKey, workspaceId, aiGatewayUrl } = config;
assert(url === 'https://etorcisrfcbtagnxrpau.supabase.co'
  && workspaceId === 'dc13c526-4c6a-52c9-8cf7-3dbe0c4c30b7'
  && aiGatewayUrl === 'https://guanye-64-81-113-208.sslip.io:8443'
  && config.coverTrainingSite === true, 'Refusing to run outside the isolated pilot');
const example = JSON.parse(await readFile(casePath, 'utf8'));
const { outline, pageCount, deckId, positions } = example;
assert(typeof outline === 'string' && outline.trim(), 'Case requires outline');
assert(Number.isInteger(pageCount) && (scope === 'page' ? pageCount === 1 : pageCount >= 2 && pageCount <= 30), 'Invalid output page count');
assert(typeof deckId === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(deckId), 'Invalid source deck ID');
assert(Array.isArray(positions) && (scope === 'page' ? positions.length === 1 && positions[0] > 1 : positions.length >= 2)
  && positions.length <= 8 && positions.every((p, i) => Number.isInteger(p) && p > (positions[i - 1] ?? 0)), 'Invalid source page positions');

async function post(base, route, body, headers, timeout = 30000) {
  const response = await fetch(new URL(route, base), {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(timeout),
    headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body),
  });
  const result = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`${route}: HTTP ${response.status} (${result?.code || 'unknown'})`);
  return result;
}
const password = process.env.TARGET_PASSWORD;
delete process.env.TARGET_PASSWORD;
assert(password, 'Load the pilot reviewer credential from local secure storage before running');
const auth = await post(url, '/auth/v1/token?grant_type=password', { email: 'reviewer5@guanye.local', password }, { apikey: anonKey });
assert(typeof auth.access_token === 'string' && auth.access_token, 'Pilot login did not return an access token');
const headers = { Authorization: `Bearer ${auth.access_token}` };
const rows = await post(url, '/rest/v1/rpc/single_judge_ppt_training_samples',
  { w: workspaceId, p_scope: 'deck', p_deck_id: deckId, p_limit: 1 }, { ...headers, apikey: anonKey });
assert(Array.isArray(rows) && rows.length === 1 && rows[0].deckId === deckId, 'Selected source not found');
const source = rows[0];
assert(source.provenance?.schema === 'local-ppt-material-library-import-v1', 'Pair requires an imported local source');
const expected = positions.map(position => {
  const image = source.images[position - 1];
  const sha256 = source.provenance.pageHashes[position - 1];
  assert(image && /^[0-9a-f]{64}$/.test(sha256), `Missing catalog image or digest at ${position}`);
  return { pageId: image.id, path: image.path, position, sha256 };
});
const report = { schema: 'ppt-training-planning-pair-v1', workspaceId, scope, pageCount, outline, sourceId: source.provenance.sourceId, deckId, expected, runs: [] };
const save = flag => writeFile(outputPath, JSON.stringify(report, null, 2) + '\n', { encoding: 'utf8', flag });
await save('wx');
const common = { mode: 'content_draft', trainingScope: scope, pageCount, outline };
for (const arm of ['baseline', 'library-reference']) {
  const run = { arm, submittedAt: new Date().toISOString(), status: 'submitted' };
  report.runs.push(run);
  await save('w');
  try {
    const response = await post(aiGatewayUrl, '/training/api/ai/planning/analyze', arm === 'baseline' ? common : {
      ...common, trainingReference: { deckId, positions },
    }, headers, 480000);
    run.response = response;
    assert(response && typeof response.planId === 'string' && /^[0-9a-f-]{36}$/.test(response.planId), 'No saved plan ID');
    assert.equal(response.plannerModel, 'gpt-6.2-sol');
    assert.deepEqual(response.referenceCandidates, [], 'Automatic reference candidates leaked into the comparison');
    assert.equal(response.plan.pages.length, pageCount);
    assert.equal(response.plan.pageCount, pageCount);
    assert(response.plan.pages.some(p => p.narrativeRole === 'content'), 'Plan has no body page');
    const evidence = response.pptTrainingEvidence;
    assert(evidence?.scope === scope && evidence.arm === arm && evidence.automaticReferences === false, 'Missing or inconsistent server-side evidence');
    assert.deepEqual(evidence.referencePages, arm === 'baseline' ? [] : expected);
    if (arm === 'library-reference') {
      assert.equal(evidence.deckId, deckId);
      assert.equal(evidence.sourceId, source.provenance.sourceId);
      assert.equal(evidence.scoreProvenance, 'catalog-proxy');
      assert.equal(evidence.humanVerified, false);
    }
    run.status = 'verified';
    console.log(JSON.stringify({ arm, planId: response.planId, pageCount, sourcePositions: evidence.referencePages.map(p => p.position) }));
  } catch (error) {
    run.status = run.response ? 'invalid-evidence' : 'not-confirmed';
    run.error = error.name === 'TimeoutError' ? 'Observation timed out; check pilot planning history before retrying' : String(error.message).slice(0, 400);
    process.exitCode = 1;
    console.error(run.error);
    break;
  } finally {
    await save('w');
  }
}
