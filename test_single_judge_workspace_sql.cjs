// npm install --prefix .test-output/sql @electric-sql/pglite
// Real PGlite fixture for the M27 single-judge review-workspace facade.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const assert=require('node:assert/strict');
const fs=require('node:fs');

const W='60100000-0000-4000-8000-000000000001';
const W2='60200000-0000-4000-8000-000000000001';
const INACTIVE='60300000-0000-4000-8000-000000000001';
const JUDGE='60100000-0000-4000-8000-000000000003';
const ADMIN='60100000-0000-4000-8000-000000000004';
const SUBMITTER='60100000-0000-4000-8000-000000000005';
const JUDGE2='60200000-0000-4000-8000-000000000003';
const ADMIN2='60200000-0000-4000-8000-000000000004';
const uploadMain='import-71000000-0000-4000-8000-000000000001';
const uploadLate='import-71000000-0000-4000-8000-000000000002';
const uploadManualOnly='import-71000000-0000-4000-8000-000000000003';
const generatedMain='import-72000000-0000-4000-8000-000000000001';
const extraUploaded=Array.from({length:9},(_,index)=>`import-71000000-0000-4000-8000-${String(index+10).padStart(12,'0')}`);
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');

const migrationNames=[
 '20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning',
 '20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history',
 '20261121_07_review_calibration','20261121_08_review_comparison','20261121_09_review_backfill',
 '20261121_10_review_queue_listing','20261121_11_planning_history','20261121_12_library_classification',
 '20261121_13_formal_correction','20261121_14_library_formal_scores','20261121_15_planning_original_references',
 '20261121_16_uploaded_baseline','20261121_17_review_workspace_batch','20261121_18_review_backfill_classification',
 '20261121_19_uploaded_source_provenance','20261121_20_uploaded_source_groups','20261121_21_uploaded_comparison_source_dedup',
 '20261122_22_single_judge_foundation','20261122_23_single_judge_original_archive',
 '20261122_24_single_judge_submission','20261122_25_image_workers_default',
 '20261122_26_single_judge_score_revisions','20261122_27_single_judge_review_workspace'
];
const migrations=migrationNames.map(name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));

async function query(db,sql,values=[]){return (await db.query(sql,values)).rows;}
async function resetRole(db){await db.exec('reset role');}
async function asService(db){await resetRole(db);await db.exec('set role service_role');}
async function asAuthenticated(db,userId){
 await resetRole(db);
 await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[userId]);
 await db.exec('set role authenticated');
}
async function asAnon(db){await resetRole(db);await db.exec('set role anon');}

async function bootstrap(){
 const db=new PGlite({extensions:{pgcrypto}});
 await db.exec(`
  create schema auth;
  create schema storage;
  create schema extensions;
  create role anon;
  create role authenticated;
  create role service_role bypassrls;
  create extension if not exists pgcrypto with schema extensions;
  create table auth.users(id uuid primary key);
  create function auth.uid() returns uuid language sql stable as $$
   select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid
  $$;
  create function auth.jwt() returns jsonb language sql stable as $$
   select jsonb_build_object('email',current_setting('request.jwt.claim.email',true))
  $$;
  create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint);
  create table storage.objects(id uuid,name text,bucket_id text);
  alter table storage.objects enable row level security;
  create function storage.foldername(text) returns text[] language sql as $$
   select string_to_array($1,'/')
  $$;
 `);
 await db.exec(legacySchema);
 await db.exec('grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role; grant select,insert,update,delete on all tables in schema public,storage to authenticated,anon; grant all on all tables in schema public,storage to service_role;');
 for(const sql of migrations)await db.exec(sql);
 await db.exec(migrations[migrations.length-1]);
 return db;
}

function deckData(id,origin='import',generation=null){
 const result={
  id,title:`标题 ${id}`,name:`作品 ${id}`,origin,
  images:[
   {id:'p1',name:'第 1 页.png',position:1,path:`${W}/${id}/preview/1.png`,thumbPath:`${W}/${id}/preview/1.png`},
   {id:'p2',name:'第 2 页.png',position:2,path:`${W}/${id}/preview/2.png`,thumbPath:`${W}/${id}/preview/2.png`}
  ]
 };
 if(generation)result.generation=generation;
 return result;
}
async function insertDeck(db,id,{sourceKind='uploaded',aiAllowed=true,generation=null}={}){
 await resetRole(db);
 await query(db,'insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3::jsonb,$4,$5)',[
  id,W,JSON.stringify(deckData(id,sourceKind==='generated'?'generated':'import',generation)),sourceKind,aiAllowed
 ]);
}
async function versions(db,deckId){
 await asService(db);
 return (await query(db,'select public.ai_review_deck_version($1,$2) as "deckVersion",public.ai_review_rubric_version($1) as "rubricVersion"',[W,deckId]))[0];
}
function submitBody(deckId,version,score,note='评分'){
 return {deckId,deckVersion:version.deckVersion,rubricVersion:version.rubricVersion,units:[
  {scope:'deck',scores:{visual:score,layout:score},note,reasons:['other'],reasonOther:'整套'}
 ]};
}
async function submit(db,payload){
 await asAuthenticated(db,JUDGE);
 return (await query(db,'select public.single_judge_submit_bundle($1,$2::jsonb) as result',[W,JSON.stringify(payload)]))[0].result;
}
async function reviewWorkspace(db,w=W,kind='uploaded',deckId=null,userId=JUDGE){
 await asAuthenticated(db,userId);
 return (await query(db,'select public.single_judge_review_workspace($1,$2,$3::text) as value',[w,kind,deckId]))[0].value;
}
async function reviewWorkspaceDefaultDeck(db,w=W,kind='uploaded',userId=JUDGE){
 await asAuthenticated(db,userId);
 return (await query(db,'select public.single_judge_review_workspace($1,$2) as value',[w,kind]))[0].value;
}
async function seedAiPair(db,deckId,scope,unitId,position,baseScores,assistedScores){
 await asService(db);
 const v=await versions(db,deckId);
 for(const [variant,scores] of [['baseline',baseScores],['assisted',assistedScores]]){
  const [job]=await query(db,`insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score,status)
   values($1,$2,'uploaded',$3,$4,$5,$6,0,'holdout',$7,$8,5,'completed')
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant)
   do update set position=excluded.position,bucket=excluded.bucket,split=excluded.split,max_score=excluded.max_score,status='completed',error=null,updated_at=now()
   returning id`,[W,deckId,scope,unitId,position,variant,v.deckVersion,v.rubricVersion]);
  await query(db,`insert into public.ai_review_records(job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,deck_version,rubric_version,max_score,model_hint,ai_result)
   values($1,$2,$3,'uploaded',$4,$5,$6,0,'holdout',$7,$8,5,'fixture',$9::jsonb)
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant)
   do update set job_id=excluded.job_id,bucket=excluded.bucket,split=excluded.split,max_score=excluded.max_score,model_hint=excluded.model_hint,ai_result=excluded.ai_result`,[
   job.id,W,deckId,scope,unitId,variant,v.deckVersion,v.rubricVersion,JSON.stringify({scores,reasons:[]})
  ]);
 }
}
function sortedKeys(value){return Object.keys(value).sort();}
async function expectReject(promise,code){
 await assert.rejects(promise,error=>error&&error.code===code);
}

(async()=>{
 const db=await bootstrap();
 try{
  await resetRole(db);
  await query(db,'insert into auth.users(id) values($1),($2),($3),($4),($5)',[JUDGE,ADMIN,SUBMITTER,JUDGE2,ADMIN2]);
  await query(db,"insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'Facade one','facade1@example.test','legacy',$2::jsonb),($3,'Facade two','facade2@example.test','legacy',$2::jsonb)",[
   W,JSON.stringify({revision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]}),W2
  ]);
  await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'Judge','reviewer'),($1,$3,'Admin','admin'),($1,$4,'Submitter','reviewer'),($5,$6,'Judge2','reviewer'),($5,$7,'Admin2','admin')",[
   W,JUDGE,ADMIN,SUBMITTER,W2,JUDGE2,ADMIN2
  ]);
  await query(db,'select public.single_judge_activate($1,$2,$3)',[W,JUDGE,ADMIN]);
  await query(db,'select public.single_judge_activate($1,$2,$3)',[W2,JUDGE2,ADMIN2]);
  await query(db,"insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'Inactive facade','inactive@example.test','legacy',$2::jsonb)",[
   INACTIVE,JSON.stringify({revision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]})
  ]);
  await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'Judge','reviewer')",[INACTIVE,JUDGE]);
  await query(db,'update public.workspaces set dedicated_judge_id=$1 where id=$2',[JUDGE,INACTIVE]);

  for(const id of [uploadMain,uploadLate,...extraUploaded])await insertDeck(db,id);
  await insertDeck(db,uploadManualOnly,{aiAllowed:false});
  await insertDeck(db,generatedMain,{sourceKind:'generated',generation:{outline:{},style:{},pages:[],summary:'fixture'}});

  const acl=(await query(db,`select
   p.prosecdef,p.pronargs,
   exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) c(value) where c.value in ('search_path=','search_path=""')) as fixed_path,
   has_function_privilege('authenticated','public.single_judge_review_workspace(uuid,text,text)','EXECUTE') as authenticated_ok,
   has_function_privilege('anon','public.single_judge_review_workspace(uuid,text,text)','EXECUTE') as anon_ok,
   has_function_privilege('service_role','public.single_judge_review_workspace(uuid,text,text)','EXECUTE') as service_ok
   from pg_proc p where p.oid=to_regprocedure('public.single_judge_review_workspace(uuid,text,text)')`))[0];
  assert.deepEqual(acl,{prosecdef:true,pronargs:3,fixed_path:true,authenticated_ok:true,anon_ok:false,service_ok:false});

  const unreviewed=await reviewWorkspace(db,W,'uploaded',uploadMain);
  assert.deepEqual(sortedKeys(unreviewed),[
   'ai','blind','contractWarnings','deck','deckVersion','decisions','isJudge','kind','preEvaluationStatus',
   'queue','queueCursors','queues','review','role','rubricVersion','settings','singleJudge','submissions','workspaceSettings'
  ].sort());
  assert.equal(unreviewed.kind,'uploaded');
  assert.equal(unreviewed.deck.id,uploadMain);
  assert.equal(unreviewed.review,null);
  assert.deepEqual(unreviewed.submissions,[]);
  assert.deepEqual(unreviewed.ai,[]);
  assert.deepEqual(unreviewed.decisions,[]);
  assert.equal(unreviewed.role,'reviewer');
  assert.equal(unreviewed.isJudge,true);
  assert.equal(unreviewed.singleJudge,true);
  assert.deepEqual(sortedKeys(unreviewed.settings),['humanThreshold','layoutWeight','matchWeight','modelHint','reasonCatalog','visionWeight'].sort());
  assert.deepEqual(sortedKeys(unreviewed.queue.uploaded),['completed','failed','queued','running'].sort());
  assert.equal(unreviewed.queues.uploaded.length,8,'queue first screen must stay small');
  assert.ok(unreviewed.queueCursors.uploaded,'queue cursor must reflect the truncated first screen');
  assert.ok(unreviewed.preEvaluationStatus==='queued'||unreviewed.preEvaluationStatus==='completed');

  const autoUploaded=await reviewWorkspaceDefaultDeck(db,W,'uploaded');
  assert.equal(autoUploaded.deck.id,uploadMain,'omitted deck id must select the first uploaded queue item');
  assert.equal(autoUploaded.queues.uploaded[0].id,uploadMain);
  assert.equal(autoUploaded.queues.generated[0].id,generatedMain);

  await seedAiPair(db,uploadMain,'deck','',0,{visual:4,layout:3},{visual:5,layout:4});
  const v=await versions(db,uploadMain);
  await submit(db,submitBody(uploadMain,v,5,'首次'));
  const first=await reviewWorkspace(db,W,'uploaded',uploadMain);
  assert.equal(first.review.data.scores.visual,5);
  assert.equal(first.submissions.length,1);
  assert.equal(first.decisions.length,1);
  assert.equal(first.ai.length,2,'first formal submission must unlock the frozen AI pair');
  assert.deepEqual(sortedKeys(first.submissions[0]),['createdAt','deckId','deckVersion','entry','humanVersion','id','position','rubricVersion','scoresComplete','scope','unitId'].sort());
  assert.deepEqual(sortedKeys(first.ai[0]),['deckId','deckVersion','jobId','modelHint','recordId','result','rubricVersion','scope','submissionId','unitId','variant'].sort());
  assert.deepEqual(sortedKeys(first.decisions[0]),['aiRecordId','classification','decisionId','entryId','expiresAt','formalScores','formalTotal','libraryVisible','reasonOther','reasons','scoreRatio','status','submissionId','summary'].sort());
  assert.equal(first.ai.every(row=>typeof row.submissionId==='string'&&row.submissionId.length>0),true);
  const firstRecordIds=first.ai.map(row=>row.recordId).sort();
  assert.equal(first.decisions[0].aiRecordId!==null,true);
  assert.equal(JSON.stringify(first).toLowerCase().includes('prompt'),false);
  assert.equal(JSON.stringify(first).toLowerCase().includes('provider'),false);

  await submit(db,submitBody(uploadMain,v,2,'改分'));
  const revised=await reviewWorkspace(db,W,'uploaded',uploadMain);
  assert.equal(revised.review.data.scores.visual,2);
  assert.equal(revised.submissions.length,2);
  assert.equal(revised.decisions.length,2);
  assert.equal(revised.ai.length,2,'revision must not duplicate or hide the first frozen AI pair');
  assert.deepEqual(revised.ai.map(row=>row.recordId).sort(),firstRecordIds);
  assert.equal(revised.decisions.filter(row=>row.aiRecordId!==null).length,1);
  assert.equal(revised.decisions.filter(row=>row.aiRecordId===null).length,1);

  await submit(db,submitBody(uploadMain,v,5,'回到五分'));
  const returned=await reviewWorkspace(db,W,'uploaded',uploadMain);
  assert.equal(returned.review.data.scores.visual,5);
  assert.equal(returned.submissions.length,3);
  assert.equal(returned.decisions.length,3);
  assert.equal(returned.ai.length,2);
  assert.deepEqual(returned.ai.map(row=>row.recordId).sort(),firstRecordIds);
  assert.equal(returned.decisions.filter(row=>row.aiRecordId!==null).length,1);

  // A stale-version submission/decision stays in history but must not leak into the
  // current-version facade payload.
  await resetRole(db);
  await db.exec('begin');
  await query(db,"select set_config('guanye.single_judge_bundle',$1,true)",[W]);
  const stale=(await db.query(`insert into public.ai_review_submissions(
   workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,deck_version,rubric_version,human_version,max_score,entry
  ) values($1,$2,'uploaded','deck','',0,$3,'stale-deck-version',$4,'stale-human-version',5,$5::jsonb) returning id`,
   [W,uploadMain,JUDGE,v.rubricVersion,JSON.stringify({scores:{visual:5,layout:5},note:'旧版本'})])).rows[0];
  await query(db,`insert into public.ai_review_decisions(
   workspace_id,deck_id,submission_id,judge_user_id,decision_key,status,classification,reasons,reason_other,
   formal_scores,formal_total,deck_version,rubric_version,max_score,score_ratio,expires_at,summary
  ) values($1,$2,$3,$4,'m27-stale-version','confirmed',null,'[]'::jsonb,'',$5::jsonb,5,
   'stale-deck-version',$6,5,1,now()+interval '7 days','旧版本')`,
   [W,uploadMain,stale.id,JUDGE,JSON.stringify({visual:5,layout:5}),v.rubricVersion]);
  await db.exec('commit');
  const staleVisible=await reviewWorkspace(db,W,'uploaded',uploadMain);
  assert.equal(staleVisible.submissions.length,3,'stale deck versions must not enter the current payload');
  assert.equal(staleVisible.decisions.length,3,'stale decisions must not enter the current payload');

  const lateV=await versions(db,uploadLate);
  await submit(db,submitBody(uploadLate,lateV,4,'无 AI 首提'));
  const lateBefore=await reviewWorkspace(db,W,'uploaded',uploadLate);
  assert.equal(lateBefore.review.data.scores.visual,4);
  assert.deepEqual(lateBefore.ai,[]);
  await resetRole(db);
  const lateJobs=await query(db,"select id,variant from public.ai_review_jobs where workspace_id=$1 and deck_id=$2 and scope='deck' order by variant",[W,uploadLate]);
  assert.ok(lateJobs.length>=1);
  for(const job of lateJobs){
   const result=job.variant==='baseline'?{scores:{visual:3,layout:4},reasons:[]}:{scores:{visual:5,layout:4},reasons:[]};
   await query(db,'select public.ai_review_finish_job($1,$2,$3,$4,$5::jsonb,$6)',[W,JUDGE,job.id,'completed',JSON.stringify(result),null]);
  }
  const lateAfter=await reviewWorkspace(db,W,'uploaded',uploadLate);
  assert.deepEqual(lateAfter.ai,[],'late AI must remain hidden without a frozen match');
  assert.equal(lateAfter.review.data.scores.visual,4);

  const manualV=await versions(db,uploadManualOnly);
  await submit(db,submitBody(uploadManualOnly,manualV,3,'仅人工'));
  const manual=await reviewWorkspace(db,W,'uploaded',uploadManualOnly);
  assert.equal(manual.deck.ai_allowed,false);
  assert.deepEqual(manual.ai,[]);
  assert.equal(manual.preEvaluationStatus,'not_queued');
  assert.equal(manual.decisions.length,1);
  assert.equal(manual.decisions[0].aiRecordId,null);
  assert.equal(manual.blind.aiVisible,false);

  const generated=await reviewWorkspaceDefaultDeck(db,W,'generated');
  assert.equal(generated.kind,'generated');
  assert.equal(generated.deck.id,generatedMain);
  assert.equal(generated.deck.source_kind,'generated');
  assert.ok(generated.contractWarnings.length>=1,'generated warning passthrough must remain compatible');
  assert.deepEqual(generated.ai,[]);

  await expectReject(reviewWorkspace(db,W,'uploaded',uploadMain,ADMIN),'42501');
  await expectReject(reviewWorkspace(db,W,'uploaded',uploadMain,SUBMITTER),'42501');
  await expectReject(reviewWorkspace(db,W2,'uploaded',null,JUDGE),'42501');
  await expectReject(reviewWorkspace(db,INACTIVE,'uploaded',null,JUDGE),'42501');
  await expectReject(reviewWorkspace(db,W,'legacy',uploadMain,JUDGE),'22023');
  await expectReject(reviewWorkspace(db,W,'generated',uploadMain,JUDGE),'22023');
  await expectReject(reviewWorkspace(db,W,'uploaded','missing-deck',JUDGE),'22023');

  await asAnon(db);
  await expectReject(query(db,'select public.single_judge_review_workspace($1,$2,$3::text)',[W,'uploaded',uploadMain]),'42501');

  console.log('PASS M27 single-judge review workspace: shape, queues, tracks, unlock, revisions, late AI, manual-only, ACL');
 }finally{
  await db.close();
 }
})().catch(error=>{console.error(error.stack||error.message);process.exit(1);});
