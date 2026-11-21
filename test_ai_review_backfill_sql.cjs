// npm install --prefix .test-output/sql @electric-sql/pglite
// Real SQL checks for the service-role-only legacy review backfill RPC.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');

const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const C='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const W0='20000000-0000-4000-8000-000000000000';
const W1='20000000-0000-4000-8000-000000000001';
const W2='20000000-0000-4000-8000-000000000002';
const W3='20000000-0000-4000-8000-000000000003';
const W4='20000000-0000-4000-8000-000000000004';
const W5='20000000-0000-4000-8000-000000000005';
const W6='20000000-0000-4000-8000-000000000006';
const W7='20000000-0000-4000-8000-000000000007';
const W8='20000000-0000-4000-8000-000000000008';
const W9='20000000-0000-4000-8000-000000000009';
const W10='20000000-0000-4000-8000-000000000010';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const migration=(name)=>fs.readFileSync(`cloud/migrations/20261121_${name}.sql`,'utf8');
const uploadedBaselineMigration=fs.readFileSync('cloud/migrations/20261121_16_uploaded_baseline.sql','utf8');
const classificationBackfillPath='cloud/migrations/20261121_18_review_backfill_classification.sql';

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
 await db.exec(schema);
 for(const name of [
  '02_ai_reviews','03_ai_planning','04_ai_generation','05_review_queue',
  '06_generation_history','07_review_calibration','08_review_comparison','09_review_backfill'
 ]) await db.exec(migration(name));
 await db.exec(`
  grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role;
  grant all on all tables in schema public,storage to service_role;
 `);
 return db;
}

async function resetRole(db){await db.exec('reset role');}
async function asRole(db,role){await resetRole(db);await db.exec(`set role ${role}`);}
async function asService(db){await asRole(db,'service_role');}
async function loginAs(db,userId){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub',$1,false)",[userId]);await db.exec('set role authenticated');}

async function addWorkspace(db,w,{judge=null}={}){
 await resetRole(db);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,$2,'owner@example.test','x')",[w,`工作区 ${w.slice(-1)}`]);
 for(const [id,name] of [[A,'专职评委'],[B,'普通评审甲'],[C,'普通评审乙']]){
  await db.query('insert into auth.users(id) values($1) on conflict(id) do nothing',[id]);
  await db.query('insert into members(workspace_id,user_id,name,role) values($1,$2,$3,$4)',[w,id,name,id===A?'admin':'reviewer']);
 }
 if(judge) await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[w,judge]);
}

function deckData(w,id,pages){
 return {
  id,title:id,name:id,
  images:pages.map((page,index)=>{
   const item=typeof page==='string'?{id:page}:page;
   const unitId=item.id;
   const position=item.position||index+1;
   const path=item.path!==undefined?item.path:(item.private===false?`other/${id}/${unitId}.png`:`${w}/${id}/${unitId}.png`);
   return {id:unitId,name:`${unitId}.png`,path,position};
  })
 };
}

async function addDeck(db,w,id,sourceKind='uploaded',pages=[]){
 await resetRole(db);
 await db.query('insert into decks(id,workspace_id,source_kind,data) values($1,$2,$3,$4::jsonb)',[id,w,sourceKind,JSON.stringify(deckData(w,id,pages))]);
}

async function addReview(db,w,userId,deckId,data){
 await resetRole(db);
 await db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4::jsonb)',[w,userId,deckId,JSON.stringify(data)]);
}

async function backfill(db,w,actor,kind,offset=0,limit=8){
 await asService(db);
 return (await db.query(
  'select public.ai_review_backfill($1::uuid,$2::uuid,$3::text,$4::integer,$5::integer) as value',
  [w,actor,kind,offset,limit]
 )).rows[0].value;
}

async function jobsFor(db,w){
 return (await db.query('select * from ai_review_jobs where workspace_id=$1 order by scope,unit_id,variant,id',[w])).rows;
}

async function exerciseAclAndValidation(db){
 await addWorkspace(db,W0,{judge:A});
 const acl=(await db.query(`select
  has_function_privilege('service_role','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE') as service_ok,
  has_function_privilege('authenticated','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE') as authenticated_ok,
  has_function_privilege('anon','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE') as anon_ok`)).rows[0];
 assert.equal(acl.service_ok,true);
 assert.equal(acl.authenticated_ok,false);
 assert.equal(acl.anon_ok,false);

 await asRole(db,'authenticated');
 await assert.rejects(db.query('select public.ai_review_backfill($1::uuid,$2::uuid,$3::text,$4::integer,$5::integer)',[W0,A,'uploaded',0,8]),/permission denied/i);
 await resetRole(db);

 await assert.rejects(backfill(db,W0,B,'uploaded',0,8),/仅专职评委/);
 await assert.rejects(backfill(db,W0,A,'video',0,8),/队列类型无效/);
 await assert.rejects(backfill(db,W0,A,'uploaded',-1,8),/分页偏移无效/);
 await assert.rejects(backfill(db,W0,A,'uploaded',0,0),/分页大小无效/);
 await assert.rejects(backfill(db,W0,A,'uploaded',0,21),/分页大小无效/);
}

async function exercisePaginationAndTracks(db){
 await addWorkspace(db,W1);
 await db.query(`
  insert into decks(id,workspace_id,source_kind,data)
  select 'u-'||lpad(i::text,4,'0'),$1::uuid,'uploaded',jsonb_build_object(
   'id','u-'||lpad(i::text,4,'0'),'title','批量上传 '||i,
   'images',jsonb_build_array(jsonb_build_object(
    'id','p-'||i,'name','p-'||i||'.png','path',$1::text||'/bulk/'||i||'.png','position',1
   ))
  )
  from generate_series(1,1005) i
 `,[W1]);
 await addDeck(db,W1,'g-1','generated',['g1']);
 await addDeck(db,W1,'g-2','generated',['g2']);
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W1,A]);

 const first=await backfill(db,W1,A,'uploaded',0,20);
 assert.equal(first.ok,true);assert.equal(first.kind,'uploaded');assert.equal(first.offset,0);
 assert.deepEqual(Object.keys(first).sort(),['decks','kind','nextOffset','offset','ok','preparedUnits','submittedUnits','totalDecks']);
 assert.deepEqual(
  {decks:first.decks,preparedUnits:first.preparedUnits,submittedUnits:first.submittedUnits,totalDecks:first.totalDecks,nextOffset:first.nextOffset},
  {decks:20,preparedUnits:20,submittedUnits:0,totalDecks:1005,nextOffset:20}
 );
 const late=await backfill(db,W1,A,'uploaded',1000,20);
 assert.deepEqual(
  {decks:late.decks,preparedUnits:late.preparedUnits,submittedUnits:late.submittedUnits,totalDecks:late.totalDecks,nextOffset:late.nextOffset},
  {decks:5,preparedUnits:5,submittedUnits:0,totalDecks:1005,nextOffset:null}
 );
 const lateIds=(await db.query("select distinct deck_id from ai_review_jobs where workspace_id=$1 and source_kind='uploaded' and deck_id>='u-1000' order by deck_id",[W1])).rows.map(row=>row.deck_id);
 assert.deepEqual(lateIds,['u-1001','u-1002','u-1003','u-1004','u-1005']);
 const uploadedJobs=(await db.query("select count(*)::int as n from ai_review_jobs where workspace_id=$1 and source_kind='uploaded'",[W1])).rows[0].n;

 const generated=await backfill(db,W1,A,'generated',0,20);
 assert.deepEqual(
  {decks:generated.decks,preparedUnits:generated.preparedUnits,submittedUnits:generated.submittedUnits,totalDecks:generated.totalDecks,nextOffset:generated.nextOffset},
  {decks:2,preparedUnits:2,submittedUnits:0,totalDecks:2,nextOffset:null}
 );
 const generatedJobs=(await db.query("select count(*)::int as n from ai_review_jobs where workspace_id=$1 and source_kind='generated'",[W1])).rows[0].n;
 assert.equal((await db.query("select count(*)::int as n from ai_review_jobs where workspace_id=$1 and source_kind='uploaded'",[W1])).rows[0].n,uploadedJobs);
 assert.equal((await db.query("select count(*)::int as n from ai_review_jobs where workspace_id=$1 and source_kind='generated'",[W1])).rows[0].n,generatedJobs);
 assert.ok(generatedJobs>=2);
 assert.deepEqual([...new Set((await db.query("select scope from ai_review_jobs where workspace_id=$1 and source_kind='generated'",[W1])).rows.map(row=>row.scope))],['deck']);
}

async function exerciseNoPreview(db){
 await addWorkspace(db,W2);
 await addDeck(db,W2,'no-preview','uploaded',[]);
 await addReview(db,W2,B,'no-preview',{mode:'deck',scores:{visual:4,layout:3}});
 await addDeck(db,W2,'external-preview','generated',[{id:'g1',path:`other/${W2}/g1.png`}]);
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W2,A]);

 const uploaded=await backfill(db,W2,A,'uploaded',0,8);
 const generated=await backfill(db,W2,A,'generated',0,8);
 for(const result of [uploaded,generated]){
  assert.equal(result.decks,1);assert.equal(result.preparedUnits,0);assert.equal(result.submittedUnits,0);
 }
 assert.equal((await jobsFor(db,W2)).length,0,'decks without a workspace-private preview must not enqueue');
}

async function reviewSnapshot(db,w){
 return (await db.query('select user_id,deck_id,data from reviews where workspace_id=$1 order by user_id,deck_id',[w])).rows;
}

async function exercisePageMatchingAndPersistence(db){
 await addWorkspace(db,W4);
 await addDeck(db,W4,'multi-author','uploaded',[
  {id:'p2',position:2},{id:'p1',position:1},{id:'p4',position:4},{id:'p5',position:5},{id:'p6',position:6},{id:'p7',position:7},{id:'p3',position:3,private:false}
 ]);
 // Page mode may retain valid top-level scores from switching granularity; one author must queue both scopes.
 await addReview(db,W4,A,'multi-author',{mode:'page',scores:{visual:4,layout:3},pages:{
  p2:{scores:{visual:4,layout:3}},p4:{scores:{visual:99}},p5:{scores:{madeUp:4}},p6:{scores:{visual:'4'}},p7:{scores:'not-an-object'}
 }});
 await addReview(db,W4,B,'multi-author',{mode:'page',pages:{
  p1:{scores:{visual:3}},p2:{scores:{visual:5}},p3:{scores:{visual:4}},ghost:{scores:{visual:2}}
 }});
 await addReview(db,W4,C,'multi-author',{mode:'deck',scores:'not-an-object'});
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W4,A]);
 const before=await reviewSnapshot(db,W4);

 const result=await backfill(db,W4,A,'uploaded',0,8);
 assert.deepEqual(
  {decks:result.decks,preparedUnits:result.preparedUnits,submittedUnits:result.submittedUnits,totalDecks:result.totalDecks},
  {decks:1,preparedUnits:3,submittedUnits:0,totalDecks:1}
 );
 let jobs=await jobsFor(db,W4);
 assert.deepEqual([...new Set(jobs.map(job=>`${job.scope}:${job.unit_id}`))].sort(),['deck:','page:p1','page:p2']);
 const jobCount=jobs.length;
 const repeated=await backfill(db,W4,A,'uploaded',0,8);
 assert.equal(repeated.preparedUnits,3);
 assert.equal((await jobsFor(db,W4)).length,jobCount,'repeated backfill must not create duplicate jobs');
 assert.deepEqual(await reviewSnapshot(db,W4),before,'legacy review rows and scores must remain unchanged');
 assert.equal((await db.query('select count(*)::int as n from ai_review_submissions where workspace_id=$1',[W4])).rows[0].n,0,'backfill must not fabricate submissions');
 assert.equal((await db.query('select count(*)::int as n from ai_review_decisions where workspace_id=$1',[W4])).rows[0].n,0);

 await asService(db);
 const claimed=(await db.query('select public.ai_review_claim_jobs($1::uuid,$2::uuid,$3::text,$4::integer) as value',[W4,A,'uploaded',20])).rows[0].value;
 assert.ok(claimed.length>=1);
 const job=claimed[0];
 const finished=(await db.query(
  "select public.ai_review_finish_attempt($1::uuid,$2::uuid,$3::uuid,$4::integer,'completed',$5::jsonb,null::text,false) as value",
  [W4,A,job.id,job.attempts,JSON.stringify({scores:{visual:4,layout:3},modelHint:'backfill-test'})]
 )).rows[0].value;
 assert.equal(finished.ok,true);
 assert.equal(finished.status,'completed');
 assert.equal((await db.query('select count(*)::int as n from ai_review_records where workspace_id=$1',[W4])).rows[0].n,1);
 assert.equal((await db.query('select count(*)::int as n from ai_review_matches')).rows[0].n,0);
 assert.equal((await db.query('select count(*)::int as n from ai_review_submissions where workspace_id=$1',[W4])).rows[0].n,0);
 assert.deepEqual(await reviewSnapshot(db,W4),before,'finish_attempt must not rewrite legacy reviews');
}

async function exerciseGranularityAndGeneratedTrack(db){
 await addWorkspace(db,W5);
 await addDeck(db,W5,'page-only','uploaded',['p1']);
 await addReview(db,W5,B,'page-only',{mode:'page',pages:{p1:{scores:{visual:4}},ghost:{scores:{visual:2}}}});
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W5,A]);
 const pageOnly=await backfill(db,W5,A,'uploaded',0,8);
 assert.equal(pageOnly.preparedUnits,1);
 assert.deepEqual([...new Set((await jobsFor(db,W5)).map(job=>job.scope))],['page']);

 await addWorkspace(db,W6);
 await addDeck(db,W6,'fallback-deck','uploaded',['p1']);
 await addReview(db,W6,B,'fallback-deck',{mode:'page',pages:{p1:{scores:{visual:99}}}});
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W6,A]);
 const fallback=await backfill(db,W6,A,'uploaded',0,8);
 assert.equal(fallback.preparedUnits,1);
 assert.deepEqual([...new Set((await jobsFor(db,W6)).map(job=>job.scope))],['deck']);

 await addWorkspace(db,W3);
 await addDeck(db,W3,'generated-existing','generated',['g1']);
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W3,A]);
 await asService(db);
 await db.query('select public.ai_review_prepare($1::uuid,$2::uuid,$3::jsonb)',[W3,A,JSON.stringify({deckId:'generated-existing',scope:'deck'})]);
 const generatedBefore=await jobsFor(db,W3);
 const generated=await backfill(db,W3,A,'generated',0,8);
 assert.equal(generated.preparedUnits,1);
 const generatedAfter=await jobsFor(db,W3);
 assert.equal(generatedAfter.length,generatedBefore.length);
 assert.deepEqual(new Set(generatedAfter.map(job=>job.scope)),new Set(['deck']));
 assert.deepEqual(generatedAfter.map(job=>job.id).sort(),generatedBefore.map(job=>job.id).sort());
}

function idForBucket(prefix,bucket){
 for(let i=0;i<10000;i++){
  const id=`${prefix}-${i}`;
  if(crypto.createHash('md5').update(id).digest()[0]%5===bucket)return id;
 }
 throw new Error(`could not find deck id for bucket ${bucket}`);
}
async function exerciseLegacyAssistedBackfill(db){
 await addWorkspace(db,W7,{judge:A});
 const legacyUploaded=idForBucket('backfill-legacy-uploaded',1);
 const legacyUploadedHoldout=idForBucket('backfill-legacy-uploaded-holdout',0);
 const legacyGenerated=idForBucket('backfill-legacy-generated',2);
 const legacyGeneratedHoldout=idForBucket('backfill-legacy-generated-holdout',0);
 for(const [id,kind] of [[legacyUploaded,'uploaded'],[legacyUploadedHoldout,'uploaded'],[legacyGenerated,'generated'],[legacyGeneratedHoldout,'generated']])await addDeck(db,W7,id,kind,['p1']);

 const legacyJobs=await jobsFor(db,W7);
 const legacyAssisted=legacyJobs.find(job=>job.deck_id===legacyUploaded&&job.scope==='deck'&&job.variant==='assisted');
 assert.ok(legacyAssisted,'legacy uploaded calibration deck must start with assisted work');
 assert.equal(legacyAssisted.status,'queued');
 const legacyAssistedBefore={id:legacyAssisted.id,status:legacyAssisted.status,attempts:legacyAssisted.attempts};
 const legacyUploadedHoldoutIds=legacyJobs.filter(job=>job.deck_id===legacyUploadedHoldout).map(job=>job.id).sort();
 assert.equal(legacyUploadedHoldoutIds.length,2,'pre-migration uploaded holdout must keep both variants');

 await resetRole(db);
 await db.query('update workspaces set dedicated_judge_id=null where id=$1',[W7]);
 const newUploaded=idForBucket('backfill-new-uploaded',3);
 const newUploadedHoldout=idForBucket('backfill-new-uploaded-holdout',0);
 const newGenerated=idForBucket('backfill-new-generated',4);
 const newGeneratedHoldout=idForBucket('backfill-new-generated-holdout',0);
 for(const [id,kind] of [[newUploaded,'uploaded'],[newUploadedHoldout,'uploaded'],[newGenerated,'generated'],[newGeneratedHoldout,'generated']])await addDeck(db,W7,id,kind,['p1']);
 for(const id of [newUploaded,newUploadedHoldout,newGenerated,newGeneratedHoldout]){
  assert.equal((await db.query('select count(*)::int as n from ai_review_jobs where workspace_id=$1 and deck_id=$2',[W7,id])).rows[0].n,0,'post-migration fixture must reach backfill without auto-enqueue');
 }

 await resetRole(db);
 await db.exec(uploadedBaselineMigration);
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W7,A]);

 const uploaded=await backfill(db,W7,A,'uploaded',0,20);
 assert.deepEqual(
  {decks:uploaded.decks,preparedUnits:uploaded.preparedUnits,totalDecks:uploaded.totalDecks,nextOffset:uploaded.nextOffset},
  {decks:4,preparedUnits:4,totalDecks:4,nextOffset:null}
 );
 const uploadedJobs=await jobsFor(db,W7);
 const legacyAfter=uploadedJobs.find(job=>job.id===legacyAssisted.id);
 assert.deepEqual({id:legacyAfter.id,status:legacyAfter.status,attempts:legacyAfter.attempts},legacyAssistedBefore,'backfill must preserve pending legacy assisted work');
 assert.ok(uploadedJobs.some(job=>job.deck_id===legacyUploaded&&job.scope==='deck'&&job.variant==='baseline'),'historical assisted work must not block baseline backfill');
 assert.deepEqual([...new Set(uploadedJobs.filter(job=>job.deck_id===legacyUploadedHoldout).map(job=>job.variant))].sort(),['assisted','baseline']);
 assert.deepEqual(uploadedJobs.filter(job=>job.deck_id===legacyUploadedHoldout).map(job=>job.id).sort(),legacyUploadedHoldoutIds,'holdout backfill must not duplicate legacy variants');
 assert.deepEqual(uploadedJobs.filter(job=>job.deck_id===newUploaded).map(job=>job.variant),['baseline'],'new uploaded calibration deck must backfill baseline only');
 assert.deepEqual(uploadedJobs.filter(job=>job.deck_id===newUploadedHoldout).map(job=>job.variant).sort(),['assisted','baseline'],'new uploaded holdout deck must backfill both variants');

 const generated=await backfill(db,W7,A,'generated',0,20);
 assert.deepEqual(
  {decks:generated.decks,preparedUnits:generated.preparedUnits,totalDecks:generated.totalDecks,nextOffset:generated.nextOffset},
  {decks:4,preparedUnits:4,totalDecks:4,nextOffset:null}
 );
 const generatedJobs=await jobsFor(db,W7);
 assert.deepEqual(generatedJobs.filter(job=>job.deck_id===newGenerated).map(job=>job.variant),['assisted'],'new generated calibration deck must keep assisted behavior');
 assert.deepEqual(generatedJobs.filter(job=>job.deck_id===newGeneratedHoldout).map(job=>job.variant).sort(),['assisted','baseline'],'new generated holdout deck must keep both variants');
}
async function upgradeThroughClassificationBackfill(db){
 for(const name of [
  '10_review_queue_listing','11_planning_history','12_library_classification',
  '13_formal_correction','14_library_formal_scores','15_planning_original_references',
  '16_uploaded_baseline','17_review_workspace_batch'
 ]) await db.exec(migration(name));
 if(fs.existsSync(classificationBackfillPath))await db.exec(fs.readFileSync(classificationBackfillPath,'utf8'));
}

async function exerciseLegacyPageOnlyClassificationBackfill(db){
 await addWorkspace(db,W8);
 const pageOnly=idForBucket('backfill-page-classification',1);
 const existing=idForBucket('backfill-page-existing-classification',2);
 const generated=idForBucket('backfill-page-generated-isolation',3);
 await addDeck(db,W8,pageOnly,'uploaded',['p1','p2']);
 await addDeck(db,W8,existing,'uploaded',['e1','e2']);
 await addDeck(db,W8,generated,'generated',['g1']);
 await addReview(db,W8,B,pageOnly,{mode:'page',pages:{
  p1:{scores:{visual:4,layout:3}},p2:{scores:{visual:3,layout:4}}
 }});
 await addReview(db,W8,B,existing,{mode:'page',pages:{
  e1:{scores:{visual:4,layout:4}},e2:{scores:{visual:4,layout:4}}
 }});
 await db.query("update decks set data=data||$1::jsonb where workspace_id=$2 and id=$3",[JSON.stringify({classification:'既有分类',style:'既有风格'}),W8,existing]);
 assert.equal((await jobsFor(db,W8)).length,0,'fixture must be old data before the upload trigger can mask the gap');
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W8,A]);
 await upgradeThroughClassificationBackfill(db);

 const beforeReviews=await reviewSnapshot(db,W8);
 const beforeMeta=(await db.query('select public.ai_review_effective_meta($1,$2) as value',[W8,pageOnly])).rows[0].value;
 assert.equal(beforeMeta.effectiveClassification,null);
 const result=await backfill(db,W8,A,'uploaded',0,20);
 assert.deepEqual(
  {decks:result.decks,preparedUnits:result.preparedUnits,totalDecks:result.totalDecks,nextOffset:result.nextOffset},
  {decks:2,preparedUnits:5,totalDecks:2,nextOffset:null},
  'page-only legacy work without classification must prepare both existing pages and the missing deck record'
 );

 let jobs=await jobsFor(db,W8);
 const pageOnlyDeck=jobs.find(job=>job.deck_id===pageOnly&&job.scope==='deck');
 assert.ok(pageOnlyDeck,'legacy page ratings must not suppress the deck AI record that supplies classification');
 assert.deepEqual(jobs.filter(job=>job.deck_id===pageOnly&&job.scope==='page').map(job=>job.unit_id).sort(),['p1','p2']);
 assert.ok(!jobs.some(job=>job.deck_id===existing&&job.scope==='deck'),'existing classification must not be overwritten or backfilled again');
 assert.deepEqual(jobs.filter(job=>job.deck_id===existing&&job.scope==='page').map(job=>job.unit_id).sort(),['e1','e2']);
 assert.ok(!jobs.some(job=>job.deck_id===generated),'uploaded classification backfill must stay isolated from the generated track');

 const jobIds=jobs.map(job=>job.id).sort();
 const repeated=await backfill(db,W8,A,'uploaded',0,20);
 assert.equal(repeated.preparedUnits,5);
 assert.deepEqual((await jobsFor(db,W8)).map(job=>job.id).sort(),jobIds,'repeated classification backfill must remain idempotent');

 await asService(db);
 const claimed=(await db.query('select public.ai_review_claim_jobs($1::uuid,$2::uuid,$3::text,$4::integer) as value',[W8,A,'uploaded',20])).rows[0].value;
 const deckJob=claimed.find(job=>job.deckId===pageOnly&&job.scope==='deck');
 assert.ok(deckJob);
 const finished=(await db.query(
  "select public.ai_review_finish_attempt($1::uuid,$2::uuid,$3::uuid,$4::integer,'completed',$5::jsonb,null::text,false) as value",
  [W8,A,deckJob.id,deckJob.attempts,JSON.stringify({scores:{visual:4,layout:3},classification:{primary:'AI 回填分类',style:'AI 回填风格'},modelHint:'backfill-classification-test'})]
 )).rows[0].value;
 assert.equal(finished.ok,true);assert.equal(finished.status,'completed');
 const meta=(await db.query('select public.ai_review_effective_meta($1,$2) as value',[W8,pageOnly])).rows[0].value;
 assert.deepEqual(meta,{effectiveClassification:'AI 回填分类',effectiveStyle:'AI 回填风格',classificationSource:'ai',styleSource:'ai'});
 assert.equal((await db.query("select count(*)::int as n from ai_review_submissions where workspace_id=$1 and deck_id=$2 and scope='deck'",[W8,pageOnly])).rows[0].n,0,'backfill must not fabricate a human deck rating');
 assert.deepEqual(await reviewSnapshot(db,W8),beforeReviews,'legacy human page ratings must remain byte-for-byte unchanged');
 const existingMeta=(await db.query('select public.ai_review_effective_meta($1,$2) as value',[W8,existing])).rows[0].value;
 assert.equal(existingMeta.effectiveClassification,'既有分类');
 assert.equal(existingMeta.classificationSource,'existing');
}async function exerciseModernPageBlindGate(db){
 await addWorkspace(db,W9,{judge:A});
 await addDeck(db,W9,'modern-page-blind','uploaded',['p1']);
 await asService(db);
 const claimed=(await db.query('select public.ai_review_claim_jobs($1::uuid,$2::uuid,$3::text,$4::integer) as value',[W9,A,'uploaded',8])).rows[0].value;
 const job=claimed.find(item=>item.deckId==='modern-page-blind'&&item.scope==='deck');
 assert.ok(job);
 const finished=(await db.query(
  "select public.ai_review_finish_attempt($1::uuid,$2::uuid,$3::uuid,$4::integer,'completed',$5::jsonb,null::text,false) as value",
  [W9,A,job.id,job.attempts,JSON.stringify({scores:{visual:4,layout:3},classification:{primary:'不应越过盲评',style:'隐藏'},modelHint:'blind-gate-test'})]
 )).rows[0].value;
 assert.equal(finished.status,'completed');
 await resetRole(db);
 await db.query('select public.ai_review_submit($1::uuid,$2::uuid,$3::jsonb)',[W9,B,JSON.stringify({deckId:'modern-page-blind',scope:'page',units:[{unitId:'p1',position:1,scores:{visual:4,layout:3}}]})]);
 const meta=(await db.query('select public.ai_review_effective_meta($1,$2) as value',[W9,'modern-page-blind'])).rows[0].value;
 assert.equal(meta.effectiveClassification,null,'modern page submissions must not unlock deck AI classification');
 assert.equal(meta.classificationSource,'none');
 assert.equal((await db.query("select count(*)::int as n from ai_review_submissions where workspace_id=$1 and deck_id=$2 and scope='deck'",[W9,'modern-page-blind'])).rows[0].n,0);
}async function exerciseHistoricalWholeScoreStylePreserved(db){
 await addWorkspace(db,W10);
 const deckId=idForBucket('backfill-whole-style-only',4);
 await addDeck(db,W10,deckId,'uploaded',['s1','s2']);
 await db.query("update decks set data=data||$1::jsonb where workspace_id=$2 and id=$3",[JSON.stringify({style:'原素材风格'}),W10,deckId]);
 await addReview(db,W10,B,deckId,{mode:'deck',scores:{visual:4,layout:3}});
 assert.equal((await jobsFor(db,W10)).length,0);
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W10,A]);
 const beforeReviews=await reviewSnapshot(db,W10);
 const result=await backfill(db,W10,A,'uploaded',0,8);
 assert.deepEqual(
  {decks:result.decks,preparedUnits:result.preparedUnits,totalDecks:result.totalDecks},
  {decks:1,preparedUnits:1,totalDecks:1},
  'valid historical whole-deck scores must keep the deck task used for classification completion'
 );
 await asService(db);
 const claimed=(await db.query('select public.ai_review_claim_jobs($1::uuid,$2::uuid,$3::text,$4::integer) as value',[W10,A,'uploaded',8])).rows[0].value;
 const deckJob=claimed.find(job=>job.deckId===deckId&&job.scope==='deck');
 assert.ok(deckJob);
 const finished=(await db.query(
  "select public.ai_review_finish_attempt($1::uuid,$2::uuid,$3::uuid,$4::integer,'completed',$5::jsonb,null::text,false) as value",
  [W10,A,deckJob.id,deckJob.attempts,JSON.stringify({scores:{visual:4,layout:3},classification:{primary:'AI 备用分类',style:'AI 不应覆盖'},reasons:['AI 内部理由'],modelHint:'style-preservation-test'})]
 )).rows[0].value;
 assert.equal(finished.status,'completed');
 const meta=(await db.query('select public.ai_review_effective_meta($1,$2) as value',[W10,deckId])).rows[0].value;
 assert.deepEqual(meta,{effectiveClassification:'AI 备用分类',effectiveStyle:'原素材风格',classificationSource:'ai',styleSource:'existing'});
 assert.deepEqual(Object.keys(meta).sort(),['classificationSource','effectiveClassification','effectiveStyle','styleSource'],'effective metadata must not expose AI scores or reasons');
 assert.deepEqual(await reviewSnapshot(db,W10),beforeReviews,'historical whole-deck scores must remain unchanged');
 assert.equal((await db.query("select count(*)::int as n from ai_review_submissions where workspace_id=$1 and deck_id=$2 and scope='deck'",[W10,deckId])).rows[0].n,0);
 await loginAs(db,B);
 const library=(await db.query('select public.get_review_library($1) as value',[W10])).rows[0].value;
 const libraryDeck=library.decks.find(deck=>deck.id===deckId);
 assert.ok(libraryDeck);
 assert.equal(libraryDeck.data.effectiveClassification,'AI 备用分类');
 assert.equal(libraryDeck.data.classificationSource,'ai');
 assert.equal(libraryDeck.data.effectiveStyle,'原素材风格');
 assert.equal(libraryDeck.data.styleSource,'existing');
 assert.equal(libraryDeck.data.aiResult,undefined);
 assert.equal(libraryDeck.data.reasons,undefined);
 await resetRole(db);
}(async()=>{
 const db=await bootstrap();
 try{
  await exerciseAclAndValidation(db);
  await exercisePaginationAndTracks(db);
  await exerciseNoPreview(db);
  await exercisePageMatchingAndPersistence(db);
  await exerciseGranularityAndGeneratedTrack(db);
  await exerciseLegacyAssistedBackfill(db);
  await exerciseLegacyPageOnlyClassificationBackfill(db);
  await exerciseModernPageBlindGate(db);
  await exerciseHistoricalWholeScoreStylePreserved(db);
  console.log('PASS: backfill SQL paging >1000, track isolation, service-only ACL, dedicated judge, private previews, matched pages, multi-author preservation, idempotency, prepare-only persistence, finish_attempt, v16 baseline routing, and v18 page/whole legacy classification backfill with style and blind preservation');
 }finally{await db.close();}
})().catch(error=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});