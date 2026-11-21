// npm install --prefix .test-output/sql @electric-sql/pglite
// Real SQL checks for M20 source-group routing.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');

const W='41000000-0000-4000-8000-000000000000';
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const C='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const SETTINGS={revision:0,maxScore:5,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]};
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const migration=name=>fs.readFileSync(`cloud/migrations/20261121_${name}.sql`,'utf8');
const migrations=['02_ai_reviews','03_ai_planning','04_ai_generation','05_review_queue',
 '06_generation_history','07_review_calibration','08_review_comparison','09_review_backfill',
 '10_review_queue_listing','11_planning_history','12_library_classification',
 '13_formal_correction','14_library_formal_scores','15_planning_original_references',
 '16_uploaded_baseline','17_review_workspace_batch','18_review_backfill_classification',
 '19_uploaded_source_provenance','20_uploaded_source_groups'];

const md5Bucket=id=>crypto.createHash('md5').update(id).digest()[0]%5;
const sourceBucket=bytes=>crypto.createHash('sha256').update(bytes).digest()[0]%5;
function idForBucket(prefix,bucket){
 for(let index=0;index<100000;index++){
  const id=`${prefix}-${index}`;
  if(md5Bucket(id)===bucket)return id;
 }
 throw new Error(`id bucket ${bucket} not found`);
}
function bytesForBucket(prefix,bucket){
 for(let index=0;index<100000;index++){
  const value=Buffer.from(`${prefix}-${index}`);
  if(sourceBucket(value)===bucket)return value;
 }
 throw new Error(`source bucket ${bucket} not found`);
}

async function bootstrap(through=20){
 const db=new PGlite({extensions:{pgcrypto}});
 await db.exec(`
  create schema auth; create schema storage; create schema extensions;
  create role anon; create role authenticated; create role service_role bypassrls;
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
 await db.exec(schema);
 for(const name of migrations.slice(0,through-1)) await db.exec(migration(name));
 await db.exec(`grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role;
  grant all on all tables in schema public,storage to service_role;`);
 return db;
}

async function reset(db){await db.exec('reset role');}
async function asService(db){await reset(db);await db.exec('set role service_role');}
async function asRole(db,role){await reset(db);await db.exec(`set role ${role}`);}

async function setupWorkspace(db){
 await db.query("insert into workspaces(id,name,owner_email,invite_hash,settings) values($1,'M20','owner@example.test','x',$2::jsonb)",[W,JSON.stringify(SETTINGS)]);
 for(const [id,name,role] of [[A,'Judge','admin'],[B,'Reviewer','reviewer'],[C,'Outsider','reviewer']]){
  await db.query('insert into auth.users(id) values($1) on conflict(id) do nothing',[id]);
  if(id!==C) await db.query('insert into members(workspace_id,user_id,name,role) values($1,$2,$3,$4)',[W,id,name,role]);
 }
}

function deck(w,id,label,source='uploaded'){
 const images=[{id:`${id}-p1`,name:'0001.png',path:`${w}/${id}/p1.png`,thumbPath:`${w}/${id}/p1.png`,position:1}];
 const value={id,name:`作品 ${label}`,title:`作品 ${label}`,origin:source==='generated'?'generated':'import',images};
 if(source==='generated')value.generation={outline:{title:label},style:{name:'AI'},pages:images.map(image=>({...image})),references:[]};
 return value;
}
async function importSourceDeck(db,w,actor,id,hash,label=id){
 await asService(db);
 try{
  await db.query('select public.ai_import_review_deck($1::uuid,$2::uuid,$3::jsonb,$4::text)',[
   w,actor,JSON.stringify(deck(w,id,label)),hash,
  ]);
 }finally{await reset(db);}
}
async function insertDeck(db,id,value,source='uploaded'){
 await db.query("insert into decks(id,workspace_id,source_kind,data) values($1,$2,$3,$4::jsonb)",[
  id,W,source,JSON.stringify(value),
 ]);
}
async function prepare(db,id,scope='deck',unitIds=[]){
 await asService(db);
 try{
  const payload={deckId:id,scope};
  if(scope==='page')payload.unitIds=unitIds;
  return (await db.query('select public.ai_review_prepare($1::uuid,$2::uuid,$3::jsonb) as value',[
   W,A,JSON.stringify(payload),
  ])).rows[0].value;
 }finally{await reset(db);}
}
async function jobsForDeck(db,id){
 return (await db.query('select bucket,split,variant,scope from ai_review_jobs where workspace_id=$1 and deck_id=$2 order by scope,variant',[W,id])).rows;
}
async function insertReview(db,deckId,scores){
 await db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4::jsonb)',[
  W,B,deckId,JSON.stringify({mode:'deck',scores,note:''}),
 ]);
}
async function versions(db,deckId){
 return (await db.query('select public.ai_review_deck_version($1,$2) as deck_version,public.ai_review_rubric_version($1) as rubric_version',[W,deckId])).rows[0];
}
async function seedRecord(db,deckId,variant,bucket,split,scores={visual:4,layout:3}){
 const v=await versions(db,deckId),jobId=crypto.randomUUID(),recordId=crypto.randomUUID();
 await db.query(`insert into ai_review_jobs(id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score,status,attempts,judge_user_id)
  values($1,$2,$3,'uploaded','deck','',0,$4,$5,$6,$7,$8,5,'completed',1,$9)`,[
  jobId,W,deckId,variant,bucket,split,v.deck_version,v.rubric_version,A,
 ]);
 await db.query(`insert into ai_review_records(id,job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,deck_version,rubric_version,max_score,model_hint,ai_result,judge_user_id)
  values($1,$2,$3,$4,'uploaded','deck','',$5,$6,$7,$8,$9,5,'m20',$10::jsonb,$11)`,[
  recordId,jobId,W,deckId,variant,bucket,split,v.deck_version,v.rubric_version,JSON.stringify({scores,classification:{label:'AI'},reasons:[]}),A,
 ]);
 return {jobId,recordId,...v};
}
async function seedCorrection(db,deckId,bucket,split='calibration'){
 const record=await seedRecord(db,deckId,'assisted',bucket,split,{visual:4,layout:4});
 const submissionId=crypto.randomUUID(),decisionId=crypto.randomUUID();
 await db.query(`insert into ai_review_submissions(id,workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,deck_version,rubric_version,human_version,max_score,entry)
  values($1,$2,$3,'uploaded','deck','',0,$4,$5,$6,$7,5,$8::jsonb)`,[
  submissionId,W,deckId,B,record.deck_version,record.rubric_version,`human-${crypto.randomUUID()}`,JSON.stringify({scores:{visual:5,layout:5}}),
 ]);
 await db.query("insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,'assisted')",[submissionId,record.recordId]);
 await db.query(`insert into ai_review_decisions(id,workspace_id,deck_id,submission_id,ai_record_id,judge_user_id,decision_key,status,classification,reasons,reason_other,formal_scores,formal_total,deck_version,rubric_version,max_score,score_ratio,summary)
  values($1,$2,$3,$4,$5,$6,$7,'corrected',$8::jsonb,'[]'::jsonb,'',$9::jsonb,4.5,$10,$11,5,0.9,'fixture')`,[
  decisionId,W,deckId,submissionId,record.recordId,A,`key-${crypto.randomUUID()}`,JSON.stringify({label:'纠偏'}),JSON.stringify({visual:4,layout:5}),record.deck_version,record.rubric_version,
 ]);
 return decisionId;
}

async function main(){
 const db=await bootstrap(20);
 try{
  await setupWorkspace(db);
  const sourceA=bytesForBucket('source-a',1),hashA=crypto.createHash('sha256').update(sourceA).digest('hex');
  const sourceB=bytesForBucket('source-b',2),hashB=crypto.createHash('sha256').update(sourceB).digest('hex');
  const sourceHoldout=bytesForBucket('source-holdout',0),hashHoldout=crypto.createHash('sha256').update(sourceHoldout).digest('hex');
  const target=idForBucket('known-target',2),sameSource=idForBucket('known-same',0);
  const other=idForBucket('known-other',3),holdout=idForBucket('known-holdout',1);
  const holdoutCorrection=idForBucket('known-holdout-correction',2);
  const unknown=idForBucket('unknown-upload',0),generated=idForBucket('generated',4);
  assert.equal(md5Bucket(sameSource),0,'same-source fixture must deliberately be old holdout by ID');
  assert.notEqual(sourceBucket(sourceA),0);

  // These fixtures stay before judge assignment so their jobs are created by
  // the explicit fallback/prepare checks below, not by the M05 AFTER trigger.
  await importSourceDeck(db,W,B,holdout,hashHoldout,'holdout source');
  await importSourceDeck(db,W,B,holdoutCorrection,hashHoldout,'holdout correction source');
  await insertDeck(db,unknown,deck(W,unknown,'unknown upload'));
  await insertDeck(db,generated,deck(W,generated,'generated','generated'),'generated');
  await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[A,W]);

  // The original M05 deck AFTER trigger must call M20 ai_review_prepare during
  // the same M19 import transaction and see the provenance row written first.
  await importSourceDeck(db,W,B,target,hashA,'target');
  await importSourceDeck(db,W,B,sameSource,hashA,'same source');
  await importSourceDeck(db,W,B,other,hashB,'other source');
  const targetJobs=await jobsForDeck(db,target);
  const sameJobs=await jobsForDeck(db,sameSource);
  const targetPage=await prepare(db,target,'page',[`${target}-p1`]);
  const unknownDeck=await prepare(db,unknown,'deck');
  const generatedDeck=await prepare(db,generated,'deck');
  assert.equal(targetJobs.length,1,'M05 trigger must create the initial deck job');
  assert.equal(targetJobs[0].bucket,sourceBucket(sourceA));
  assert.equal(targetJobs[0].split,'calibration');
  assert.equal(sameJobs.length,1);
  assert.equal(sameJobs[0].bucket,sourceBucket(sourceA));
  assert.equal(targetPage.jobs[0].bucket,sourceBucket(sourceA));
  assert.equal(unknownDeck.jobs[0].bucket,md5Bucket(unknown));
  assert.equal(unknownDeck.jobs[0].split,'holdout');
  assert.equal(generatedDeck.jobs[0].bucket,md5Bucket(generated));
  assert.equal(generatedDeck.jobs[0].variant,'assisted');

  await asService(db);
  const snapshot=(await db.query('select public.get_ai_review_sample_snapshot($1) as value',[W])).rows[0].value;
  await reset(db);
  const byId=Object.fromEntries(snapshot.decks.map(row=>[row.deckId,row]));
  assert.equal(byId[target].bucket,sourceBucket(sourceA));
  assert.equal(byId[sameSource].bucket,sourceBucket(sourceA));
  assert.equal(byId[target].split,'calibration');
  assert.equal(byId[unknown].bucket,md5Bucket(unknown));
  assert.equal(byId[unknown].split,'holdout');
  assert.equal(snapshot.splitAlgorithm,'source-sha256-or-md5-first-byte-mod-5-v1');
  assert.equal(JSON.stringify(snapshot).includes(hashA),false,'source hash must not enter snapshot payload');

  await insertReview(db, sameSource,{visual:5,layout:5});
  await insertReview(db, other,{visual:4,layout:4});
  await insertReview(db, holdout,{visual:5,layout:5});
  await seedCorrection(db,sameSource,sourceBucket(sourceA),'calibration');
  const otherCorrection=await seedCorrection(db,other,sourceBucket(sourceB),'calibration');
  await seedCorrection(db,holdoutCorrection,0,'holdout');
  await asService(db);
  const calibration=(await db.query('select public.ai_review_calibration($1,$2,$3,$4,$5,$6) as value',[W,A,'uploaded',12,target,'deck'])).rows[0].value;
  await reset(db);
  assert.equal(calibration.samples.some(row=>row.deckId===sameSource),false,'same source sample must be excluded');
  assert.equal(calibration.samples.some(row=>row.deckId===holdout),false,'holdout sample must be excluded');
  assert.equal(calibration.samples.some(row=>row.deckId===other),true,'different calibration source should remain eligible');
  assert.equal(calibration.corrections.some(row=>row.correctionId===otherCorrection),true,'different-source correction should remain eligible');
  assert.equal(calibration.corrections.some(row=>row.deckId===sameSource),false,'same-source correction must be excluded');
  assert.equal(calibration.corrections.some(row=>row.deckId===holdoutCorrection),false,'holdout correction must be excluded');
  assert.equal(JSON.stringify(calibration).includes(hashA),false,'source hash must not enter calibration payload');

  await asService(db);
  const references=(await db.query('select public.ai_planning_reference_candidates($1,20) as value',[W])).rows[0].value;
  await reset(db);
  assert.equal(references.some(row=>row.deckId===holdout),false,'holdout deck must not enter planning references');
  assert.equal(JSON.stringify(references).includes(hashHoldout),false,'source hash must not enter planning payload');

  // Make a real legacy comparison pair on a source bucket 0 deck whose old
  // deck-id bucket is not 0. The M20 route must still count it.
  await seedRecord(db,holdout,'baseline',0,'holdout',{visual:4,layout:4});
  await seedRecord(db,holdout,'assisted',0,'holdout',{visual:5,layout:5});
  await asService(db);
  const comparison=(await db.query('select public.ai_review_comparison($1) as value',[W])).rows[0].value;
  await reset(db);
  assert.ok(comparison.holdoutUnits>=1,'M08 must include source group holdout deck even when deck-id bucket differs');
  assert.equal(comparison.splitAlgorithm,'source-sha256-or-md5-first-byte-mod-5-v1');

  const acl=(await db.query(`select
   has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE') as service_ok,
   not has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_source_group_key(uuid,text,text)'),'EXECUTE') as key_private,
   has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE') as authenticated_ok,
   has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE') as anon_ok,
   not has_table_privilege('authenticated',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT') as authenticated_private,
   not has_table_privilege('anon',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT') as anon_private,
   not has_table_privilege('service_role',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT') as service_private,
   has_schema_privilege('service_role','guanye_internal','USAGE') as service_schema`)).rows[0];
  assert.deepEqual(acl,{service_ok:true,key_private:true,authenticated_ok:false,anon_ok:false,authenticated_private:true,anon_private:true,service_private:true,service_schema:true});
  for(const role of ['authenticated','anon']){
   await asRole(db,role);
   await assert.rejects(()=>db.query('select guanye_internal.ai_review_source_group_bucket($1,$2,$3)',[W,target,'uploaded']),/permission denied/);
   await assert.rejects(()=>db.query('select count(*) from guanye_internal.uploaded_source_provenance'),/permission denied/);
   await reset(db);
  }
  await asService(db);
  const helperBucket=(await db.query('select guanye_internal.ai_review_source_group_bucket($1,$2,$3) as value',[W,target,'uploaded'])).rows[0].value;
  await assert.rejects(()=>db.query('select guanye_internal.ai_review_source_group_key($1,$2,$3)',[W,target,'uploaded']),/permission denied/);
  await reset(db);
  assert.equal(helperBucket,sourceBucket(sourceA));
 }finally{await db.close();}

 // M19 can be installed one release before M20. If an old-algorithm queue/record
 // already exists for a known source, M20 must fail closed and roll back all DDL.
 const conflict=await bootstrap(19);
 try{
  await setupWorkspace(conflict);
  const bytes=bytesForBucket('conflict-source',1);
  const hash=crypto.createHash('sha256').update(bytes).digest('hex');
  const deckId=idForBucket('conflict-deck',0);
  await importSourceDeck(conflict,W,B,deckId,hash,'conflict');
  const v=await versions(conflict,deckId);
  await conflict.query(`insert into ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score,status,attempts,judge_user_id)
   values($1,$2,'uploaded','deck','',0,'baseline',0,'holdout',$3,$4,5,'queued',0,$5)`,[W,deckId,v.deck_version,v.rubric_version,A]);
  await assert.rejects(()=>conflict.exec(migration('20_uploaded_source_groups')),/M20 来源分桶冲突/);
  try{await conflict.exec('rollback');}catch{}
  const after=(await conflict.query(`select
   to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)') is null as helper_absent,
   not exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.get_ai_review_sample_snapshot(uuid)') and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0) as functions_rolled_back`)).rows[0];
  assert.deepEqual(after,{helper_absent:true,functions_rolled_back:true});
 }finally{await conflict.close();}

 console.log('PASS uploaded source groups M20');
}

main().catch(error=>{console.error(error.stack||error.message);process.exit(1);});
