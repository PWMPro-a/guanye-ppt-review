const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');

module.exports = async function verifySingleJudgeSubmission(db) {
  const w = '30200000-0000-4000-8000-000000000001';
  const judge = '30200000-0000-4000-8000-000000000003';
  const admin = '30200000-0000-4000-8000-000000000004';
  const submitter = '30200000-0000-4000-8000-000000000005';
  const shadow = '30200000-0000-4000-8000-000000000006';
  const manualId = 'import-30200000-0000-4000-8000-000000000101';
  const aiId = 'import-30200000-0000-4000-8000-000000000102';
  const shadowId = 'import-30200000-0000-4000-8000-000000000103';
  const manualSha = '01'.repeat(32);
  const aiSha = '02'.repeat(32);
  const shadowSha = '03'.repeat(32);
  const bucket = 'ppt-library';

  async function query(sql, values = []) {
    return (await db.query(sql, values)).rows;
  }
  async function resetRole() {
    await db.exec('reset role');
  }
  async function asService() {
    await resetRole();
    await db.exec('set role service_role');
  }
  async function actor(id) {
    await resetRole();
    await query("select set_config('request.jwt.claim.sub',$1,false)", [id]);
    await db.exec('set role authenticated');
  }
  function deckPayload(id, title, pageCount = 2) {
    return {
      id,
      title,
      images: Array.from({ length: pageCount }, (_, index) => {
        const path = `${w}/${id}/preview/${index + 1}.png`;
        return { id: `p${index + 1}`, position: index + 1, path, thumbPath: path };
      }),
    };
  }
  function sourcePayload(id, sha, ext = 'pptx', name = 'submission.pptx', sizeBytes = 2048) {
    return { name, sha256: sha, sizeBytes, path: `${w}/${id}/original/${sha}.${ext}` };
  }
  async function addObjects(paths) {
    await resetRole();
    for (const path of paths) {
      await query("insert into storage.objects(id,name,bucket_id) values($1,$2,'ppt-library')", [randomUUID(), path]);
    }
  }
  async function primeStorage(data, source) {
    await addObjects([source.path, ...data.images.map((image) => image.path)]);
  }
  async function publish(actorId, data, source, aiAllowed) {
    await asService();
    return (await query('select public.single_judge_publish_upload($1,$2,$3::jsonb,$4::jsonb,$5) as result',
      [w, actorId, JSON.stringify(data), JSON.stringify(source), aiAllowed]))[0].result;
  }
  async function jobs(deckId) {
    await resetRole();
    return query('select id,status,judge_user_id from public.ai_review_jobs where workspace_id=$1 and deck_id=$2 order by id', [w, deckId]);
  }
  async function submit(body) {
    await actor(judge);
    return (await query('select public.single_judge_submit_bundle($1,$2::jsonb) as result', [w, JSON.stringify(body)]))[0].result;
  }

  await resetRole();
  await query('insert into auth.users(id) values ($1),($2),($3),($4)', [judge, admin, submitter, shadow]);
  await query("insert into public.workspaces(id,name,owner_email,invite_hash) values ($1,'Submission fixture','submit@example.test','legacy')", [w]);
  await query("insert into public.members(workspace_id,user_id,name,role) values ($1,$2,'Judge','reviewer'),($1,$3,'Admin','admin'),($1,$4,'Submitter','reviewer')",
    [w, judge, admin, submitter]);
  await query('select public.single_judge_activate($1,$2,$3)', [w, judge, admin]);

  // Existing members get submission identity, never read or scoring rights.
  await actor(submitter);
  assert.deepEqual((await query('select public.single_judge_access($1) as access', [w]))[0].access,
    { mode: 'single-judge', role: 'submitter' });
  assert.deepEqual(await query('select public.is_member($1) as member', [w]), [{ member: false }]);
  assert.equal((await query('select id from public.decks where workspace_id=$1', [w])).length, 0);
  await assert.rejects(query('select public.single_judge_submit_bundle($1,$2::jsonb)', [w, '{}']), { code: '42501' });
  await assert.rejects(query('select public.single_judge_publish_upload($1,$2,$3::jsonb,$4::jsonb,false)',
    [w, submitter, '{}', '{}']), { code: '42501' });
  await assert.rejects(query("select public.join_workspace($1,'No public signup','legacy')", [w]), { code: '42501' });

  await actor(admin);
  assert.deepEqual((await query('select public.single_judge_access($1) as access', [w]))[0].access,
    { mode: 'single-judge', role: 'admin' });
  await assert.rejects(query('select public.single_judge_submit_bundle($1,$2::jsonb)', [w, '{}']), { code: '42501' });

  await actor(shadow);
  assert.deepEqual(await query('select public.single_judge_access($1) as access', [w]), [{ access: null }]);
  assert.deepEqual(await query('select public.is_member($1) as member', [w]), [{ member: false }]);

  const acl = (await (async () => { await resetRole(); return query(`select
    has_function_privilege('service_role','public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)','EXECUTE') as service_ok,
    not has_function_privilege('authenticated','public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)','EXECUTE') as auth_denied,
    not has_function_privilege('anon','public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)','EXECUTE') as anon_denied,
    has_function_privilege('service_role','public.single_judge_publish_import(uuid,uuid,jsonb,jsonb)','EXECUTE') as legacy_service`); })())[0];
  assert.deepEqual(acl, { service_ok: true, auth_denied: true, anon_denied: true, legacy_service: true });

  // A user with an auth row but no current membership is a shadow actor.
  const shadowDeck = deckPayload(shadowId, 'Shadow');
  const shadowSource = sourcePayload(shadowId, shadowSha);
  await asService();
  await assert.rejects(query('select public.single_judge_publish_upload($1,$2,$3::jsonb,$4::jsonb,true)',
    [w, shadow, JSON.stringify(shadowDeck), JSON.stringify(shadowSource)]), { code: '42501' });

  const manualDeck = deckPayload(manualId, 'Only human');
  const manualSource = sourcePayload(manualId, manualSha, 'pdf', 'manual.pdf');
  await primeStorage(manualDeck, manualSource);
  const manual = await publish(submitter, manualDeck, manualSource, false);
  assert.equal(manual.ok, true);
  assert.equal(manual.idempotent, false);
  assert.equal(manual.aiAllowed, false);
  assert.equal(manual.submittedBy, submitter);
  assert.equal(manual.preEvaluation.skipped, true);
  assert.equal((await jobs(manualId)).length, 0);

  const aiDeck = deckPayload(aiId, 'AI allowed');
  const aiSource = sourcePayload(aiId, aiSha, 'pptx', 'ai-allowed.pptx');
  await primeStorage(aiDeck, aiSource);
  const firstAi = await publish(admin, aiDeck, aiSource, true);
  assert.equal(firstAi.ok, true);
  assert.equal(firstAi.idempotent, false);
  assert.equal(firstAi.aiAllowed, true);
  assert.equal(firstAi.submittedBy, admin);
  assert.ok(firstAi.preEvaluation.jobs.length > 0, 'admin import must pre-evaluate through the designated judge');
  const aiJobsBefore = await jobs(aiId);
  const secondAi = await publish(admin, aiDeck, aiSource, true);
  assert.equal(secondAi.idempotent, true);
  assert.deepEqual((await jobs(aiId)).map((job) => job.id), aiJobsBefore.map((job) => job.id));
  await assert.rejects(publish(submitter, aiDeck, aiSource, true), { code: '42501' });

  await resetRole();
  const stored = await query(`select deck_id,created_by,sha256 from guanye_internal.single_judge_originals
    where deck_id in ($1,$2) order by deck_id`, [manualId, aiId]);
  assert.deepEqual(stored, [
    { deck_id: manualId, created_by: submitter, sha256: manualSha },
    { deck_id: aiId, created_by: admin, sha256: aiSha },
  ]);
  assert.deepEqual((await query('select id,ai_allowed from public.decks where id in ($1,$2) order by id', [manualId, aiId])),
    [{ id: manualId, ai_allowed: false }, { id: aiId, ai_allowed: true }]);
  assert.equal((await query('select id from public.decks where workspace_id=$1 and id not in ($2,$3)', [w, manualId, aiId])).length, 0);

  await actor(submitter);
  await assert.rejects(query('select public.single_judge_original($1,$2)', [w, manualId]), { code: '42501' });

  const [version] = await (async () => {
    await resetRole();
    return query('select public.ai_review_deck_version($1,$2) as "deckVersion", public.ai_review_rubric_version($1) as "rubricVersion"', [w, manualId]);
  })();
  const body = {
    deckId: manualId,
    ...version,
    units: [
      { scope: 'deck', scores: { visual: 4, layout: 5 }, reasons: ['other'], reasonOther: '人工' },
      { scope: 'page', unitId: 'p2', scores: { visual: 4.5, layout: 4 }, reasons: ['hierarchy'] },
    ],
  };
  const scored = await submit(body);
  await resetRole();
  assert.equal(scored.submissionIds.length, 2);
  assert.equal(scored.decisions.length, 2);
  assert.ok(scored.decisions.every((decision) => decision.status === 'confirmed'
    && decision.aiRecordId === null && decision.manualOnly === true && decision.aiVisible === false));
  assert.deepEqual((await query('select data from public.reviews where workspace_id=$1 and user_id=$2 and deck_id=$3', [w, judge, manualId]))[0].data.scores,
    body.units[0].scores);
  assert.equal((await query('select id from public.ai_review_submissions where workspace_id=$1 and deck_id=$2', [w, manualId])).length, 2);
  const manualDecisions = await query(`select status,ai_record_id,formal_total from public.ai_review_decisions
    where workspace_id=$1 and deck_id=$2 order by formal_total`, [w, manualId]);
  assert.equal(manualDecisions.length, 2);
  assert.ok(manualDecisions.every((decision) => decision.status === 'confirmed' && decision.ai_record_id === null));
  assert.equal((await query('select id from public.ai_review_records where workspace_id=$1 and deck_id=$2', [w, manualId])).length, 0,
    'manual-only deck must have no AI record');
  assert.equal((await jobs(manualId)).length, 0);

  await actor(judge);
  const summary = (await query('select public.review_score_summary($1) as result', [w]))[0].result;
  assert.equal(summary[manualId].formalScore, 4.5, 'M14 formal score must reflect the latest manual deck decision');
  assert.equal(summary[manualId].formalPageScores.p2, 4.25, 'M14 page formal score must remain visible');

  await asService();
  await assert.rejects(query('select public.ai_review_confirm($1,$2,$3::jsonb)',
    [w, judge, JSON.stringify({ submissionId: scored.submissionIds[0], formalScores: body.units[0].scores, status: 'corrected' })]),
    /不能生成 AI 纠偏示例/);
  await assert.rejects(query('select public.ai_review_prepare($1,$2,$3::jsonb)',
    [w, judge, JSON.stringify({ deckId: manualId, scope: 'deck' })]), /仅人工/);

  const backfill = (await query("select public.ai_review_backfill($1,$2,'uploaded',0,20) as result", [w, judge]))[0].result;
  assert.equal(backfill.totalDecks, 1, 'backfill counts only AI-authorized decks');
  assert.equal((await jobs(manualId)).length, 0);

  const planning = (await query('select public.ai_planning_reference_candidates($1,12) as result', [w]))[0].result;
  assert.ok(!planning.some((item) => item.deckId === manualId), 'manual-only deck must not become a planning reference');

  await assert.rejects(query("select public.ai_review_calibration($1,$2,'uploaded',12,$3,'deck')", [w, judge, manualId]),
    /仅人工|目标作品不存在/);
  const calibration = (await query("select public.ai_review_calibration($1,$2,'uploaded',12,$3,'deck') as result", [w, judge, aiId]))[0].result;
  assert.ok(!calibration.samples.some((item) => item.deckId === manualId), 'manual-only deck must not enter calibration samples');
  assert.ok(!calibration.corrections.some((item) => item.deckId === manualId), 'manual-only deck must not become a correction example');

  const snapshot = (await query('select public.get_ai_review_sample_snapshot($1) as result', [w]))[0].result;
  assert.equal(snapshot.deckCount, 1);
  assert.ok(!snapshot.decks.some((item) => item.deckId === manualId), 'manual-only deck must not enter model sample snapshot');

  // Removing the membership row models a disabled/historical account: no access.
  await resetRole();
  await query('delete from public.members where workspace_id=$1 and user_id=$2', [w, submitter]);
  await actor(submitter);
  assert.deepEqual(await query('select public.single_judge_access($1) as access', [w]), [{ access: null }]);
  assert.deepEqual(await query('select public.is_member($1) as member', [w]), [{ member: false }]);

  await resetRole();
};
