// npm install --prefix .test-output/sql @electric-sql/pglite
// Real PGlite fixture for M26 single-judge score revisions.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');

const W='50100000-0000-4000-8000-000000000001';
const JUDGE='50100000-0000-4000-8000-000000000003';
const ADMIN='50100000-0000-4000-8000-000000000004';
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
 '20261122_26_single_judge_score_revisions'
];
const migrations=migrationNames.map(name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));

function md5Bucket(value){return crypto.createHash('md5').update(value).digest()[0]%5;}
function importDeckIdWithBucket(bucket){
 while(true){
  const id='import-'+crypto.randomUUID();
  if(md5Bucket(id)===bucket)return id;
 }
}

async function query(db,sql,values=[]){return (await db.query(sql,values)).rows;}
async function resetRole(db){await db.exec('reset role');}
async function asService(db){await resetRole(db);await db.exec('set role service_role');}
async function asAuthenticated(db,userId){
 await resetRole(db);
 await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[userId]);
 await db.exec('set role authenticated');
}

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

function deckData(id){
 return {
  id,title:id,name:id,origin:'import',
  images:[
   {id:'p1',name:'第 1 页.png',position:1,path:`${W}/${id}/preview/1.png`,thumbPath:`${W}/${id}/preview/1.png`},
   {id:'p2',name:'第 2 页.png',position:2,path:`${W}/${id}/preview/2.png`,thumbPath:`${W}/${id}/preview/2.png`}
  ]
 };
}

async function versions(db,deckId){
 return (await query(db,'select public.ai_review_deck_version($1,$2) as "deckVersion",public.ai_review_rubric_version($1) as "rubricVersion"',[W,deckId]))[0];
}
function body(deckId,version,{deck=5,p1=5,p2=null,p1Note='首次'}={}){
 const units=[{scope:'deck',scores:{visual:deck,layout:deck},reasons:['other'],reasonOther:'整套'}];
 units.push({scope:'page',unitId:'p1',position:1,scores:{visual:p1,layout:p1},note:p1Note,reasons:['other'],reasonOther:'页面一'});
 if(p2!==null)units.push({scope:'page',unitId:'p2',position:2,scores:{visual:p2,layout:p2},note:'未改页面',reasons:['other'],reasonOther:'页面二'});
 return {deckId,deckVersion:version.deckVersion,rubricVersion:version.rubricVersion,units};
}
async function submit(db,payload,userId=JUDGE){
 await asAuthenticated(db,userId);
 return (await query(db,'select public.single_judge_submit_bundle($1,$2::jsonb) as result',[W,JSON.stringify(payload)]))[0].result;
}
async function counts(db,deckId){
 await resetRole(db);
 return (await query(db,`select
  (select count(*)::int from public.ai_review_submissions where workspace_id=$1 and deck_id=$2) as submissions,
  (select count(*)::int from public.ai_review_decisions where workspace_id=$1 and deck_id=$2) as decisions,
  (select count(*)::int from public.ai_review_matches m join public.ai_review_submissions s on s.id=m.submission_id where s.workspace_id=$1 and s.deck_id=$2) as matches`, [W,deckId]))[0];
}
async function unitRows(db,deckId,unitId){
 await resetRole(db);
 return query(db,'select id,scope,unit_id,human_version,entry,created_at from public.ai_review_submissions where workspace_id=$1 and deck_id=$2 and unit_id=$3 order by created_at,id',[W,deckId,unitId]);
}
async function decisionRows(db,deckId,unitId){
 await resetRole(db);
 const rows=await query(db,`select d.id,d.submission_id,d.ai_record_id,d.formal_scores,d.formal_total,d.status,d.created_at
  from public.ai_review_decisions d join public.ai_review_submissions s on s.id=d.submission_id
  where d.workspace_id=$1 and d.deck_id=$2 and s.unit_id=$3 order by d.created_at,d.id`,[W,deckId,unitId]);
 return rows.map(row=>({...row,formal_total:row.formal_total===null?null:Number(row.formal_total)}));
}
async function formalScores(db,deckId){
 await asService(db);
 const rows=await query(db,'select scope,unit_id,score::float8 as score from public.ai_review_uploaded_formal_scores($1) where deck_id=$2 order by scope,unit_id',[W,deckId]);
 return rows.map(row=>({...row,score:Number(row.score)}));
}
async function comparison(db){
 await asService(db);
 return (await query(db,'select public.ai_review_comparison($1) as value',[W]))[0].value;
}

async function insertDeck(db,deckId,aiAllowed=true){
 await resetRole(db);
 await query(db,'insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3::jsonb,$4,$5)',[
  deckId,W,JSON.stringify(deckData(deckId)),'uploaded',aiAllowed
 ]);
}
async function seedAiPair(db,deckId,scope,unitId,position,baseScores,assistedScores){
 const v=await versions(db,deckId);
 for(const [variant,scores] of [['baseline',baseScores],['assisted',assistedScores]]){
  const [job]=await query(db,`insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score,status)
   values($1,$2,'uploaded',$3,$4,$5,$6,0,'holdout',$7,$8,5,'completed')
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant)
   do update set position=excluded.position,bucket=excluded.bucket,split=excluded.split,max_score=excluded.max_score,status='completed',error=null,updated_at=now()
   returning id`,
   [W,deckId,scope,unitId,position,variant,v.deckVersion,v.rubricVersion]);
  await query(db,`insert into public.ai_review_records(job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,deck_version,rubric_version,max_score,model_hint,ai_result)
   values($1,$2,$3,'uploaded',$4,$5,$6,0,'holdout',$7,$8,5,'fixture',$9::jsonb)
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant)
   do update set job_id=excluded.job_id,bucket=excluded.bucket,split=excluded.split,max_score=excluded.max_score,model_hint=excluded.model_hint,ai_result=excluded.ai_result`,
   [job.id,W,deckId,scope,unitId,variant,v.deckVersion,v.rubricVersion,JSON.stringify({scores,reasons:[]})]);
 }
}

(async()=>{
 const db=await bootstrap();
 try{
  await resetRole(db);
  await query(db,'insert into auth.users(id) values($1),($2)',[JUDGE,ADMIN]);
  await query(db,"insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'Revision fixture','revision@example.test','legacy',$2::jsonb)",[
   W,JSON.stringify({revision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]})
  ]);
  await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'Judge','reviewer'),($1,$3,'Admin','admin')",[W,JUDGE,ADMIN]);
  await query(db,'select public.single_judge_activate($1,$2,$3)',[W,JUDGE,ADMIN]);

  const deckId=importDeckIdWithBucket(0);
  await insertDeck(db,deckId,true);
  await seedAiPair(db,deckId,'page','p1',1,{visual:4,layout:3},{visual:5,layout:4});
  const v=await versions(db,deckId);

  const initial=await submit(db,body(deckId,v,{deck:5,p1:5,p2:5,p1Note:'首次'}));
  assert.equal(initial.ok,true);
  let c=await counts(db,deckId);
  assert.deepEqual(c,{submissions:3,decisions:3,matches:2},'first submission must create exactly one blind pair');
  let p1Submissions=await unitRows(db,deckId,'p1');
  const firstP1Submission=p1Submissions[0];
  let p1Decisions=await decisionRows(db,deckId,'p1');
  assert.equal(p1Decisions.length,1);
  assert.ok(p1Decisions[0].ai_record_id,'first formal p1 decision must retain the first real match');
  const firstMatchIds=(await query(db,'select id::text from public.ai_review_matches where submission_id=$1 order by id',[firstP1Submission.id] )).map(row=>row.id);
  assert.equal(firstMatchIds.length,2);

  // Exact repeat: no new submission, decision, match, or member revision.
  const revisionBefore=(await query(db,'select revision from public.members where workspace_id=$1 and user_id=$2',[W,JUDGE]))[0].revision;
  const repeat=await submit(db,body(deckId,v,{deck:5,p1:5,p2:5,p1Note:'首次'}));
  assert.equal(repeat.ok,true);
  const revisionAfter=(await query(db,'select revision from public.members where workspace_id=$1 and user_id=$2',[W,JUDGE]))[0].revision;
  assert.equal(revisionAfter,revisionBefore,'identical repeat must be idempotent');
  assert.deepEqual(await counts(db,deckId),c);

  // 5 -> 2: only the selected page changes; p2 stays at its previous formal score.
  const down=await submit(db,body(deckId,v,{deck:5,p1:2,p1Note:'改分'}));
  assert.equal(down.ok,true);
  c=await counts(db,deckId);
  assert.deepEqual(c,{submissions:4,decisions:4,matches:2});
  let formal=await formalScores(db,deckId);
  assert.deepEqual(formal,[{scope:'deck',unit_id:'',score:5},{scope:'page',unit_id:'p1',score:2},{scope:'page',unit_id:'p2',score:5}]);
  const p2Cell=(await query(db,"select data->'pages'->'p2' as value from public.reviews where workspace_id=$1 and user_id=$2 and deck_id=$3",[W,JUDGE,deckId]))[0].value;
  assert.equal(p2Cell.scores.visual,5,'unselected page review must remain unchanged');

  // Side-effect version: same score, changed note still creates a new historical decision.
  const side=await submit(db,body(deckId,v,{deck:5,p1:2,p1Note:'仅改备注'}));
  assert.equal(side.ok,true);
  c=await counts(db,deckId);
  assert.deepEqual(c,{submissions:5,decisions:5,matches:2});
  p1Decisions=await decisionRows(db,deckId,'p1');
  assert.equal(p1Decisions.length,3);
  assert.ok(p1Decisions.slice(1).every(row=>row.ai_record_id===null),'revisions must not be paired with AI records');
  assert.equal(p1Decisions[2].formal_total,2);

  // 2 -> 5: the historical 5 must get a new submission and decision, not reuse the old one.
  const back=await submit(db,body(deckId,v,{deck:5,p1:5,p1Note:'回到五分'}));
  assert.equal(back.ok,true);
  c=await counts(db,deckId);
  assert.deepEqual(c,{submissions:6,decisions:6,matches:2});
  p1Submissions=await unitRows(db,deckId,'p1');
  assert.equal(p1Submissions.length,4,'first 5, 2, side-effect 2, and return 5 must all remain');
  assert.equal(p1Submissions[0].human_version.includes(':r:'),false);
  assert.ok(p1Submissions.slice(1).every(row=>row.human_version.includes(':r:')),'revisions must carry distinct revision suffixes');
  assert.equal(new Set(p1Submissions.map(row=>row.human_version)).size,4,'every historical version must remain distinct');
  formal=await formalScores(db,deckId);
  assert.deepEqual(formal,[{scope:'deck',unit_id:'',score:5},{scope:'page',unit_id:'p1',score:5},{scope:'page',unit_id:'p2',score:5}]);
  const finalP1Decisions=await decisionRows(db,deckId,'p1');
  assert.equal(finalP1Decisions.length,4);
  assert.ok(finalP1Decisions[0].ai_record_id);
  assert.ok(finalP1Decisions.slice(1).every(row=>row.ai_record_id===null),'every revision decision must remain unpaired');
  assert.deepEqual((await query(db,'select id::text from public.ai_review_matches where submission_id=$1 order by id',[firstP1Submission.id] )).map(row=>row.id),firstMatchIds,'first pair must remain byte-for-byte unchanged');

  const stats=await comparison(db);
  assert.equal(stats.holdoutUnits,1,'revisions must not duplicate blind-review MAE samples');
  assert.equal(stats.byKind.uploaded.holdoutUnits,1);
  assert.equal(stats.baseline.count,1);
  assert.equal(stats.assisted.count,1);

  // Historical rows created before M26 may already contain revision matches. They remain
  // in permanent history, but comparison must still select only the first formal pair.
  await resetRole(db);
  const firstPairRows=await query(db,'select record_id,variant from public.ai_review_matches where submission_id=$1 order by id',[firstP1Submission.id]);
  try{
   await db.exec('alter table public.ai_review_matches disable trigger single_judge_freeze_matches');
   for(const row of firstPairRows){
    await query(db,'insert into public.ai_review_matches(submission_id,record_id,variant) values($1,$2,$3) on conflict do nothing',[
     p1Submissions[3].id,row.record_id,row.variant
    ]);
   }
  }finally{
   await db.exec('alter table public.ai_review_matches enable trigger single_judge_freeze_matches');
  }
  const legacyDuplicateCount=(await query(db,'select count(*)::int as count from public.ai_review_matches m join public.ai_review_submissions s on s.id=m.submission_id where s.workspace_id=$1 and s.deck_id=$2',[W,deckId]))[0].count;
  assert.equal(legacyDuplicateCount,4,'fixture must contain historical revision matches');
  const statsAfterHistoricalDupes=await comparison(db);
  assert.equal(statsAfterHistoricalDupes.holdoutUnits,1,'comparison must ignore historical revision matches');
  assert.equal(statsAfterHistoricalDupes.baseline.count,1);
  assert.equal(statsAfterHistoricalDupes.assisted.count,1);

  // Late AI: first submission had no prediction; a later duplicate/revision must never pair.
  const lateDeckId=importDeckIdWithBucket(0);
  await insertDeck(db,lateDeckId,true);
  const lateV=await versions(db,lateDeckId);
  await submit(db,{deckId:lateDeckId,deckVersion:lateV.deckVersion,rubricVersion:lateV.rubricVersion,units:[
   {scope:'deck',scores:{visual:4,layout:4},note:'首次无 AI',reasons:['other'],reasonOther:'整套'}
  ]});
  assert.deepEqual(await counts(db,lateDeckId),{submissions:1,decisions:1,matches:0});
  const lateJobs=await query(db,"select id,variant from public.ai_review_jobs where workspace_id=$1 and deck_id=$2 and scope='deck' order by variant",[W,lateDeckId]);
  assert.equal(lateJobs.length,2,'late fixture requires baseline and assisted jobs');
  for(const job of lateJobs){
   const result=job.variant==='baseline'?{scores:{visual:3,layout:4},reasons:[]}:{scores:{visual:5,layout:4},reasons:[]};
   await query(db,'select public.ai_review_finish_job($1,$2,$3,$4,$5::jsonb,$6)',[W,JUDGE,job.id,'completed',JSON.stringify(result),null]);
  }
  await submit(db,{deckId:lateDeckId,deckVersion:lateV.deckVersion,rubricVersion:lateV.rubricVersion,units:[
   {scope:'deck',scores:{visual:4,layout:4},note:'首次无 AI',reasons:['other'],reasonOther:'整套'}
  ]});
  await submit(db,{deckId:lateDeckId,deckVersion:lateV.deckVersion,rubricVersion:lateV.rubricVersion,units:[
   {scope:'deck',scores:{visual:2,layout:2},note:'晚到修订',reasons:['other'],reasonOther:'整套'}
  ]});
  assert.deepEqual(await counts(db,lateDeckId),{submissions:2,decisions:2,matches:0},'late AI must not create a blind pair');
  const statsAfterLate=(await query(db,'select public.ai_review_comparison($1) as stats',[W]))[0].stats;
  assert.equal(statsAfterLate.byKind.uploaded.holdoutUnits,1,'late AI must not be counted via the mutable legacy reviews fallback');
  const lateFormal=await formalScores(db,lateDeckId);
  assert.deepEqual(lateFormal,[{scope:'deck',unit_id:'',score:2}],'latest late-only formal score must win');

  // A pre-existing legacy review with no formal submission remains independent evidence.
  const historicDeckId=importDeckIdWithBucket(0);
  await insertDeck(db,historicDeckId,true);
  await seedAiPair(db,historicDeckId,'deck','',0,{visual:4,layout:3},{visual:3,layout:4});
  await resetRole(db);
  await query(db,'insert into public.reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4::jsonb)',[
   W,JUDGE,historicDeckId,JSON.stringify({mode:'deck',scores:{visual:5,layout:5},note:'未正式提交的高分历史'})
  ]);
  assert.deepEqual(await counts(db,historicDeckId),{submissions:0,decisions:0,matches:0});
  const statsWithHistoric=await comparison(db);
  assert.equal(statsWithHistoric.byKind.uploaded.holdoutUnits,2,'unformalized legacy review must remain an independent sample');
  assert.equal(statsWithHistoric.baseline.count,2);
  assert.equal(statsWithHistoric.assisted.count,2);
  assert.deepEqual(statsWithHistoric.byKind.uploaded.sourceDedup,{
   knownGroups:0,unknownDecks:2,excludedDuplicateDecks:0
  });

  // Legacy workspaces still rely on the original content fingerprint: no revision suffix
  // and no alternate first-pair policy are introduced outside single-judge mode.
  const legacyW='50200000-0000-4000-8000-000000000001';
  const legacyDeck='legacy-revision-fixture';
  await resetRole(db);
  await query(db,"insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'Legacy fixture','legacy-revision@example.test','legacy',$2::jsonb)",[
   legacyW,JSON.stringify({revision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]})
  ]);
  await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'Judge','reviewer')",[legacyW,JUDGE]);
  await query(db,'update public.workspaces set dedicated_judge_id=$1 where id=$2',[JUDGE,legacyW]);
  await query(db,'insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3::jsonb,$4,true)',[
   legacyDeck,legacyW,JSON.stringify({id:legacyDeck,title:legacyDeck,name:legacyDeck,origin:'import',images:[
    {id:'lp1',name:'legacy.png',position:1,path:`${legacyW}/${legacyDeck}/preview/1.png`,thumbPath:`${legacyW}/${legacyDeck}/preview/1.png`}
   ]}),'uploaded'
  ]);
  const legacyVersion=(await query(db,'select public.ai_review_deck_version($1,$2) as "deckVersion",public.ai_review_rubric_version($1) as "rubricVersion"',[legacyW,legacyDeck]))[0];
  const legacyPayload={deckId:legacyDeck,...legacyVersion,scope:'deck',units:[
   {scope:'deck',scores:{visual:5,layout:5},reasons:['other'],reasonOther:'旧版'}
  ]};
  await asService(db);
  await query(db,'select public.ai_review_submit($1,$2,$3::jsonb)',[legacyW,JUDGE,JSON.stringify(legacyPayload)]);
  await query(db,'select public.ai_review_submit($1,$2,$3::jsonb)',[legacyW,JUDGE,JSON.stringify(legacyPayload)]);
  await resetRole(db);
  const legacySubmissions=await query(db,'select human_version from public.ai_review_submissions where workspace_id=$1 and deck_id=$2',[legacyW,legacyDeck]);
  assert.equal(legacySubmissions.length,1,'legacy duplicate content must retain original dedupe behavior');
  assert.equal(legacySubmissions[0].human_version.includes(':r:'),false);

  // Admin cannot change scores even though admin can submit imported material.
  const beforeAdmin=await counts(db,deckId);
  await assert.rejects(
   submit(db,body(deckId,v,{deck:1,p1:1,p1Note:'越权'}),ADMIN),
   {code:'42501'}
  );
  assert.deepEqual(await counts(db,deckId),beforeAdmin,'admin attempt must not mutate history');

  console.log('PASS M26 score revisions: 5->2->5, idempotent repeat, side effects, unselected page, late AI fallback suppressed, independent legacy sample, historical dedup, admin denial');
 }finally{
  await db.close();
 }
})().catch(error=>{console.error(error.stack||error.message);process.exit(1);});
