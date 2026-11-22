const assert = require('node:assert/strict');

module.exports = async function verifySingleJudge(db) {
  const w = '30100000-0000-4000-8000-000000000001';
  const another = '30100000-0000-4000-8000-000000000002';
  const judge = '30100000-0000-4000-8000-000000000003';
  const admin = '30100000-0000-4000-8000-000000000004';
  const legacy = '30100000-0000-4000-8000-000000000005';
  const deck = 'single-judge-m22-fixture';
  async function query(sql, values=[]) { return (await db.query(sql,values)).rows; }
  async function actor(id) {
    await db.exec('reset role');
    await query("select set_config('request.jwt.claim.sub',$1,false)",[id]);
    await db.exec('set role authenticated');
  }
  await query('insert into auth.users values ($1),($2),($3)',[judge,admin,legacy]);
  await query("insert into public.workspaces(id,name,owner_email,invite_hash) values ($1,'Fixture','admin@example.test','legacy'),($2,'Second workspace','fixture@example.test','legacy')",[w,another]);
  await query("insert into public.members(workspace_id,user_id,name,role) values ($1,$2,'Judge','reviewer'),($1,$3,'Admin','admin'),($1,$4,'Previous member','reviewer'),($5,$4,'Previous member','reviewer')",[w,judge,admin,legacy,another]);
  await query('insert into public.decks(id,workspace_id,data) values ($1,$2,$3::jsonb)',[deck,w,JSON.stringify({images:[{id:'p1'},{id:'p2',position:2},{id:'p3'}]})]);
  const historical = {scores:{visual:4,layout:4},pages:{p3:{scores:{visual:3,layout:3},note:'历史原分'}}};
  await query('insert into public.reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4::jsonb)',[w,judge,deck,JSON.stringify(historical)]);
  assert.deepEqual(await query('select single_judge_admin_id from public.workspaces where id=$1',[w]),[{single_judge_admin_id:null}]);
  await actor(admin);
  await assert.rejects(query('select public.single_judge_activate($1,$2,$3)',[w,judge,admin]),{code:'42501'});
  await db.exec('reset role');
  await assert.rejects(query('select public.single_judge_activate($1,$2,$3)',[w,admin,admin]),{code:'22023'});
  await query('select public.single_judge_activate($1,$2,$3)',[w,judge,admin]);
  assert.deepEqual((await query('select data from public.reviews where workspace_id=$1',[w]))[0].data,historical);
  const [version] = await query('select public.ai_review_deck_version($1,$2) as "deckVersion", public.ai_review_rubric_version($1) as "rubricVersion"',[w,deck]);
  const preeval = async (scope, page) => {
    const [job] = await query(`insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,deck_version,rubric_version,max_score,status)
      values($1,$2,'uploaded',$3,$4,'baseline',0,'holdout',$5,$6,5,'running') returning id`,[w,deck,scope,page,version.deckVersion,version.rubricVersion]);
    return (await query("select public.ai_review_finish_job($1,$2,$3,'completed',$4::jsonb,null) result",[w,judge,job.id,JSON.stringify({scores:{visual:4,layout:4},reason:'fixture'})]))[0].result;
  };
  await preeval('deck','');
  const body={deckId:deck,...version,units:[{scope:'deck',scores:{visual:4.5,layout:4.5},reasons:['other'],reasonOther:'清楚'},
    {scope:'page',unitId:'p2',position:99,scores:{visual:5,layout:4.5}}]};
  const submit = (value) => query('select public.single_judge_submit_bundle($1,$2::jsonb) as result',[w,JSON.stringify(value)]);
  await actor(admin);
  assert.deepEqual((await query('select public.single_judge_access($1) as access',[w]))[0].access,{mode:'single-judge',role:'admin'});
  await assert.rejects(submit(body),{code:'42501'});
  await assert.rejects(query('select public.save_review_state($1,$2)',[w,'{}']),{code:'42501'});
  await assert.rejects(query('select public.set_dedicated_judge($1,$2)',[w,admin]),{code:'42501'});
  await assert.rejects(query('select public.delete_review_deck($1,$2,0)',[w,deck]),{code:'42501'});
  await assert.rejects(query('select ai_result from public.ai_review_records'),{code:'42501'});
  await actor(legacy);
  assert.deepEqual(await query('select public.is_member($1) as professional, public.is_member($2) as legacy',[w,another]),[{professional:false,legacy:true}]);
  assert.equal((await query('select id from public.decks where workspace_id=$1',[w])).length,0);
  await assert.rejects(query("select public.join_workspace($1,'No','ABC')",[w]),{code:'42501'});
  await actor(judge);
  assert.deepEqual((await query('select public.single_judge_access($1) as access',[w]))[0].access,{mode:'single-judge',role:'judge'});
  await assert.rejects(submit({...body,rubricVersion:'stale'}),{code:'22023'});
  const invalid=structuredClone(body);
  invalid.units[1].reasonOther='x'.repeat(501);
  await assert.rejects(submit(invalid),/其他原因过长/);
  assert.deepEqual((await query('select data from public.reviews where workspace_id=$1',[w]))[0].data,historical);
  const [{result}]=await submit(body);
  assert.equal(result.submissionIds.length,2);
  assert.equal(result.decisions.length,2);
  assert.equal(result.deck.predictionAvailable,true);
  assert.equal(result.pages.predictionAvailable,false);
  const [saved]=await query('select data from public.reviews where workspace_id=$1',[w]);
  assert.deepEqual(saved.data.scores,body.units[0].scores);
  assert.deepEqual(saved.data.pages.p2.scores,body.units[1].scores);
  assert.deepEqual(saved.data.pages.p3,historical.pages.p3);
  assert.equal(saved.data.pages.p1,undefined);
  await db.exec('reset role');
  await preeval('page','p2');
  const [totals]=await query(`select (select count(*)::int from public.ai_review_matches m join public.ai_review_submissions s on s.id=m.submission_id where s.workspace_id=$1) as pairs,
    (select count(*)::int from public.ai_review_decisions where workspace_id=$1) as decisions,
    (select position from public.ai_review_submissions where workspace_id=$1 and scope='page') as position`,[w]);
  assert.deepEqual(totals,{pairs:1,decisions:2,position:2});
  await actor(judge);
  assert.equal((await submit(body))[0].result.pages.predictionAvailable,false,'retry cannot pair a late AI record');
  await db.exec('reset role');
  const importedId='import-5c8bf18d-0a6a-4bd0-b69e-a02a6594cf85';
  const sha='a'.repeat(64);
  const originalPath=`${w}/${importedId}/original/${sha}.pdf`;
  const previewPath=`${w}/${importedId}/preview/1.png`;
  const imported={id:importedId,title:'PDF 原档',images:[{id:'pdf-1',position:1,path:previewPath,thumbPath:previewPath}]};
  const source={name:'资料.pdf',sizeBytes:50,sha256:sha,path:originalPath};
  const publish=(actorId=judge,data=imported)=>query('select public.single_judge_publish_import($1,$2,$3::jsonb,$4::jsonb) result',
    [w,actorId,JSON.stringify(data),JSON.stringify(source)]);
  await assert.rejects(publish(admin),{code:'42501'});
  await assert.rejects(publish(),/原始文件尚未同步/);
  await query("insert into storage.objects(id,name,bucket_id) values(gen_random_uuid(),$1,'ppt-library')",[originalPath]);
  await assert.rejects(publish(),/预览第 1 页尚未同步/);
  assert.equal((await query('select id from public.decks where id=$1',[importedId])).length,0);
  await query("insert into storage.objects(id,name,bucket_id) values(gen_random_uuid(),$1,'ppt-library')",[previewPath]);
  assert.equal((await publish())[0].result.idempotent,false);
  assert.equal((await publish())[0].result.idempotent,true);
  assert.ok((await query("select id from public.ai_review_jobs where deck_id=$1 and status='queued'",[importedId])).length>0);
  await actor(admin);
  await assert.rejects(publish(),{code:'42501'});
  const original=id=>query('select public.single_judge_original($1,$2) result',[w,id]);
  assert.deepEqual((await original(deck))[0].result,{available:false});
  const [savedOriginal]=await original(importedId);
  assert.equal(savedOriginal.result.path,originalPath);
  assert.equal(savedOriginal.result.name,'资料.pdf');
  await actor(legacy);
  await assert.rejects(original(importedId),{code:'42501'});
  await db.exec('reset role');
};
