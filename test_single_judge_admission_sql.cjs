// npm install --prefix .test-output/sql @electric-sql/pglite
// Real PGlite fixture for the M28 single-judge admission and aggregate comparison.
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
 '20261122_26_single_judge_score_revisions','20261122_27_single_judge_review_workspace',
 '20261122_28_single_judge_admission'
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

const {createHash}=require('node:crypto');
const generation={jobId:'fixture-generation',references:[{deckId:'source-original',pageId:'p1'}]};
const scores=(score,kind='generated')=>kind==='generated'?{visual:score,layout:score,match:score}:{visual:score,layout:score};
function deckId(stem,bucket){
 for(let i=0;;i++){
  const id=`${stem}-${i}`;
  if(createHash('md5').update(id).digest()[0]%5===bucket)return id;
 }
}
async function deck(db,w,id,kind='generated'){
 await resetRole(db);
 const data={id,title:id,images:[1,2].map(position=>({id:`p${position}`,position,path:`${w}/${id}/preview/${position}.png`}))};
 if(kind==='generated')data.generation=generation;
 await query(db,'insert into public.decks(id,workspace_id,data,source_kind) values($1,$2,$3::jsonb,$4)',[id,w,JSON.stringify(data),kind]);
}
async function binding(db,w,id){
 await asService(db);
 return (await query(db,`select $2::text as "deckId",public.ai_review_deck_version($1,$2) as "deckVersion",
  public.ai_review_rubric_version($1) as "rubricVersion"`,[w,id]))[0];
}
const bundle=(binding,whole,pages={},kind='generated')=>({...binding,units:[
 {scope:'deck',scores:scores(whole,kind)},
 ...Object.entries(pages).map(([unitId,value])=>({scope:'page',unitId,scores:scores(value,kind)}))
]});
async function submit(db,body){
 await asAuthenticated(db,JUDGE);
 const [row]=await query(db,'select public.single_judge_submit_bundle($1,$2::jsonb) as result',[W,JSON.stringify(body)]);
 await resetRole(db);
 const {rows}=await db.query(`select s.id as submission,d.id as decision,s.unit_id,d.created_at::text
  from public.ai_review_submissions s join public.ai_review_decisions d on d.submission_id=s.id
  where s.id in (select value::uuid from jsonb_array_elements_text($1::jsonb))`,[JSON.stringify(row.result.submissionIds)]);
 return Object.fromEntries(rows.map(entry=>[entry.unit_id||'deck',entry]));
}
async function confirmService(db,w,submission,score){
 await asService(db);
 return (await query(db,'select public.ai_review_confirm($1,$2,$3::jsonb) as value',[
  w,JUDGE,JSON.stringify({submissionId:submission,formalScores:scores(score)})
 ]))[0].value.decisionId;
}
async function legacyScore(db,w,id,score){
 const info=await binding(db,w,id);
 const [submitted]=await query(db,'select public.ai_review_submit($1,$2,$3::jsonb) as value',[w,JUDGE,JSON.stringify({
  ...info,scope:'deck',units:[{scores:scores(score)}]
 })]);
 const submission=submitted.value.submissionIds[0];
 return {submission,decision:await confirmService(db,w,submission,score)};
}
async function admit(db,w,{submission,decision}){
 return (await query(db,'select public.single_judge_admit($1,$2,$3) as value',[w,submission,decision]))[0].value;
}
async function legacyAdmit(db,w,{submission,decision}){
 await asService(db);
 return (await query(db,'select public.ai_review_admit($1,$2,$3::jsonb) as value',[
  w,JUDGE,JSON.stringify({submissionId:submission,decisionId:decision})
 ]))[0].value;
}
async function compare(db,w){
 return (await query(db,'select public.single_judge_comparison($1) as value',[w]))[0].value;
}
const rejectsCode=(promise,code)=>assert.rejects(promise,error=>error?.code===code);
async function visible(db,w,id){
 await resetRole(db);
 return query(db,`select id,decision_id,scope,unit_id,source,reference_refs,formal_scores,formal_total::float8,
  ai_self_learning,library_label,admitted_by from public.ai_generated_entries
  where workspace_id=$1 and deck_id=$2 and library_visible order by scope,unit_id,id`,[w,id]);
}
async function aiPair(db,id,kind){
 const version=await binding(db,W,id);
 for(const [variant,score] of [['baseline',3],['assisted',4]]){
  const [job]=await query(db,`insert into public.ai_review_jobs
   (workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score,status)
   values($1,$2,$3,'deck','',0,$4,0,'holdout',$5,$6,5,'completed')
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant)
   do update set status='completed' returning id`,[W,id,kind,variant,version.deckVersion,version.rubricVersion]);
  await query(db,`insert into public.ai_review_records
   (job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,deck_version,rubric_version,max_score,model_hint,ai_result)
   values($1,$2,$3,$4,'deck','',$5,0,'holdout',$6,$7,5,'SECRET_MODEL_HINT',$8::jsonb)`,[
   job.id,W,id,kind,variant,version.deckVersion,version.rubricVersion,JSON.stringify({scores:scores(score,kind),reasons:['SECRET_AI_DETAIL']})
  ]);
 }
}

(async()=>{
 const db=await bootstrap();
 try{
  await query(db,'insert into auth.users(id) values($1),($2),($3),($4),($5)',[JUDGE,ADMIN,SUBMITTER,JUDGE2,ADMIN2]);
  const wsSettings=JSON.stringify({revision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]});
  await query(db,`insert into public.workspaces(id,name,owner_email,invite_hash,settings)
   values($1,'main','main@example.test','legacy',$4::jsonb),($2,'foreign','foreign@example.test','legacy',$4::jsonb),
         ($3,'legacy','legacy@example.test','legacy',$4::jsonb)`,[W,W2,INACTIVE,wsSettings]);
  await query(db,`insert into public.members(workspace_id,user_id,name,role) values
   ($1,$4,'Judge','reviewer'),($1,$5,'Admin','admin'),($1,$6,'Submitter','reviewer'),
   ($2,$7,'Judge2','reviewer'),($2,$8,'Admin2','admin'),($3,$4,'Legacy judge','reviewer')`,[W,W2,INACTIVE,JUDGE,ADMIN,SUBMITTER,JUDGE2,ADMIN2]);
  await query(db,'update public.workspaces set dedicated_judge_id=$1 where id in ($2,$3)',[JUDGE,W,INACTIVE]);
  const historical='pre-activation-duplicate';
  await deck(db,W,historical);
  const historical5=await legacyScore(db,W,historical,5);
  const historical45=await legacyScore(db,W,historical,4.5);
  assert.equal((await legacyAdmit(db,W,historical5)).admitted,true);
  assert.equal((await legacyAdmit(db,W,historical45)).admitted,true);
  assert.equal((await visible(db,W,historical)).length,2);
  await query(db,'select public.single_judge_activate($1,$2,$3),public.single_judge_activate($4,$5,$6)',[W,JUDGE,ADMIN,W2,JUDGE2,ADMIN2]);

  const entire=deckId('generated-entire',1), pageOnly=deckId('generated-pages',1), revised=deckId('generated-revised',1);
  const generatedHoldout=deckId('generated-holdout',0), uploaded=deckId('uploaded-holdout',0), unseen=deckId('unseen-generated',0);
  const originalReference=deckId('original-reference',1);
  for(const id of [entire,pageOnly,revised,generatedHoldout,unseen])await deck(db,W,id);
  await deck(db,W,uploaded,'uploaded');
  await deck(db,W,originalReference,'uploaded');
  for(const [id,kind] of [[generatedHoldout,'generated'],[uploaded,'uploaded'],[unseen,'generated']])await aiPair(db,id,kind);

  // Whole admission is independent of a below-threshold page.
  const eq=await binding(db,W,entire);
  const initial=await submit(db,bundle(eq,5,{p1:2}));
  await asAuthenticated(db,JUDGE);
  const wholeEntry=await admit(db,W,initial.deck);
  assert.equal(wholeEntry.idempotent,false);
  await assert.rejects(admit(db,W,initial.p1),/未达到满分门槛/);
  assert.equal((await admit(db,W,initial.deck)).entryId,wholeEntry.entryId);
  await rejectsCode(admit(db,W,{submission:initial.deck.submission,decision:initial.p1.decision}),'22023');

  const pages=await submit(db,bundle(await binding(db,W,pageOnly),2,{p1:5,p2:2}));
  await asAuthenticated(db,JUDGE);
  const pageEntry=await admit(db,W,pages.p1);
  assert.equal(pageEntry.scope,'page');
  assert.equal(pageEntry.unitId,'p1');
  for(const unit of ['deck','p2'])await assert.rejects(admit(db,W,pages[unit]),/未达到满分门槛/);

  // Even two revisions in one DB transaction are ordered; approval holds the M26 deck lock.
  const revisionBinding=await binding(db,W,revised);
  await db.exec('begin');
  const high=await submit(db,bundle(revisionBinding,5,{p1:5,p2:5}));
  const low=await submit(db,bundle(revisionBinding,2,{p1:2}));
  await db.exec('commit');
  assert.ok(high.p1.created_at<low.p1.created_at);
  await asAuthenticated(db,JUDGE);
  for(const unit of ['deck','p1'])await assert.rejects(admit(db,W,high[unit]),/最新正式评分/);
  await assert.rejects(admit(db,W,low.deck),/未达到满分门槛/);
  assert.equal((await admit(db,W,high.p2)).idempotent,false,'unselected page remains current');
  // The former service confirm can append a decision, but cannot promote an older
  // submission or replace the scores captured in a current human submission.
  const staleConfirm={submission:high.deck.submission,decision:await confirmService(db,W,high.deck.submission,4.8)};
  const changedHuman={submission:low.deck.submission,decision:await confirmService(db,W,low.deck.submission,5)};
  await asAuthenticated(db,JUDGE);
  await assert.rejects(admit(db,W,staleConfirm),/最新正式评分/);
  await assert.rejects(admit(db,W,changedHuman),/人分不一致/);
  await rejectsCode(legacyAdmit(db,W,high.deck),'42501');
  await rejectsCode(query(db,'select guanye_internal.ai_review_admit($1,$2,$3::jsonb)',[W,JUDGE,'{}']),'42501');

  const highAgain=await submit(db,bundle(revisionBinding,5,{p1:5}));
  await asAuthenticated(db,JUDGE);
  assert.notEqual(highAgain.deck.decision,high.deck.decision);
  await assert.rejects(admit(db,W,high.deck),/最新正式评分/);
  assert.equal((await admit(db,W,highAgain.deck)).idempotent,false);
  assert.equal((await admit(db,W,highAgain.p1)).idempotent,false);

  const lower=await submit(db,bundle(eq,2));
  await asAuthenticated(db,JUDGE);
  assert.equal((await admit(db,W,initial.deck)).entryId,wholeEntry.entryId,'older acceptance survives lower revision');
  await rejectsCode(admit(db,W,lower.deck),'22023');
  const raised=await submit(db,bundle(eq,4.5));
  await asAuthenticated(db,JUDGE);
  const sameEntry=await admit(db,W,raised.deck);
  assert.equal(sameEntry.idempotent,true);
  assert.equal(sameEntry.entryId,wholeEntry.entryId);
  assert.equal(sameEntry.decisionId,initial.deck.decision);
  assert.equal((await admit(db,W,historical5)).idempotent,true,'keep pre-activation acceptance');
  const newHistorical=await submit(db,bundle(await binding(db,W,historical),4.75));
  await asAuthenticated(db,JUDGE);
  assert.equal((await admit(db,W,newHistorical.deck)).decisionId,historical45.decision,'reuse same most recent visible historical entry as the library');
  assert.equal((await visible(db,W,historical)).length,2,'migration does not rewrite historical duplicates');

  const [storedWhole]=await visible(db,W,entire);
  assert.deepEqual(storedWhole,{id:wholeEntry.entryId,decision_id:initial.deck.decision,scope:'deck',unit_id:'',
   source:generation,reference_refs:generation.references,formal_scores:scores(5),formal_total:5,
   ai_self_learning:true,library_label:'AI 自我学习生成',admitted_by:JUDGE});
  const [storedPage]=await visible(db,W,pageOnly);
  assert.equal(storedPage.id,pageEntry.entryId);
  assert.deepEqual(storedPage.source,generation);
  assert.deepEqual(storedPage.reference_refs,generation.references);
  assert.deepEqual(storedPage.formal_scores,scores(5));
  assert.equal(storedPage.library_label,'AI 自我学习生成');
  await asAuthenticated(db,JUDGE);
  const [library]=(await query(db,'select public.get_review_library($1) as value',[W])).map(row=>row.value);
  const libraryDeck=id=>library.decks.find(d=>d.id===id).data;
  assert.equal(libraryDeck(entire).formalScore,5);
  assert.equal(libraryDeck(entire).libraryLabel,'AI 自我学习生成');
  assert.equal(libraryDeck(entire).libraryScope,'deck');
  assert.equal(libraryDeck(pageOnly).libraryScope,'pages');
  assert.deepEqual(libraryDeck(pageOnly).images.map(i=>i.id),['p1']);
  assert.equal(library.entries.filter(e=>e.deckId===historical).length,1,'reader already collapses historic duplicate rows');

  const expires='generated-expired', oldVersion='generated-old-version';
  for(const id of [expires,oldVersion])await deck(db,W,id);
  const expired=(await submit(db,bundle(await binding(db,W,expires),5))).deck;
  const outOfDate=(await submit(db,bundle(await binding(db,W,oldVersion),5))).deck;
  await query(db,"update public.ai_review_decisions set expires_at=now()-interval '1 minute' where id=$1",[expired.decision]);
  await query(db,"update public.decks set data=data||'{\"edition\":2}'::jsonb where id=$1",[oldVersion]);
  await asAuthenticated(db,JUDGE);
  await assert.rejects(admit(db,W,expired),/正式评分已过期/);
  await assert.rejects(admit(db,W,outOfDate),/作品版本已变化/);
  await db.exec('begin isolation level repeatable read');
  await rejectsCode(admit(db,W,expired),'25000');
  await db.exec('rollback');

  const up=await submit(db,bundle(await binding(db,W,uploaded),5,{},'uploaded'));
  await submit(db,bundle(await binding(db,W,originalReference),5,{},'uploaded'));
  await submit(db,bundle(await binding(db,W,generatedHoldout),5));
  await asAuthenticated(db,JUDGE);
  await rejectsCode(admit(db,W,up.deck),'22023');
  const judgeComparison=await compare(db,W);
  assert.equal(judgeComparison.holdoutUnits,2,'unreviewed generated AI records are not a sample');
  for(const kind of ['uploaded','generated']){
   assert.equal(judgeComparison.byKind[kind].holdoutUnits,1);
   assert.equal(judgeComparison.byKind[kind].baseline.count,1);
   assert.equal(judgeComparison.byKind[kind].assisted.count,1);
  }
  assert.doesNotMatch(JSON.stringify(judgeComparison),/SECRET_|generated-holdout|unseen-generated/);
  await asAuthenticated(db,ADMIN);
  assert.deepEqual(await compare(db,W),judgeComparison);
  await rejectsCode(admit(db,W,raised.deck),'42501');
  for(const user of [SUBMITTER,JUDGE2,'']){
   await asAuthenticated(db,user);
   await rejectsCode(compare(db,W),'42501');
   await rejectsCode(admit(db,W,raised.deck),'42501');
  }
  await asAuthenticated(db,JUDGE);
  await rejectsCode(compare(db,W2),'42501');
  await rejectsCode(compare(db,INACTIVE),'42501');
  await rejectsCode(admit(db,W2,raised.deck),'42501');
  await asAnon(db);
  await rejectsCode(compare(db,W),'42501');
  await rejectsCode(admit(db,W,raised.deck),'42501');
  await asService(db);
  await rejectsCode(compare(db,W),'42501');
  await rejectsCode(admit(db,W,raised.deck),'42501');
  const [references]=(await query(db,'select public.ai_planning_reference_candidates($1,12) as value',[W])).map(row=>row.value);
  assert.deepEqual(references.map(r=>r.deckId),[originalReference]);
  assert.equal(references[0].sourceKind,'uploaded');

  await deck(db,INACTIVE,'legacy-generated');
  const legacy=await legacyScore(db,INACTIVE,'legacy-generated',5);
  assert.equal((await legacyAdmit(db,INACTIVE,legacy)).idempotent,false);
  assert.equal((await legacyAdmit(db,INACTIVE,legacy)).idempotent,true);
  await asAuthenticated(db,JUDGE);
  await rejectsCode(admit(db,INACTIVE,legacy),'42501');
  await rejectsCode(admit(db,W,legacy),'22023');
  console.log('PASS M28 admission and comparison: whole/page thresholds, stale revisions, duplicate/history, ACL, source provenance, planning, both tracks');
 }finally{await db.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
