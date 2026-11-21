// npm install --prefix .test-output/sql @electric-sql/pglite
// Ordered queue checks for legacy schema -> 01..05 and fresh schema.sql -> 02..05.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict');

const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const W1='10000000-0000-4000-8000-000000000001';
const W2='10000000-0000-4000-8000-000000000002';
const W3='10000000-0000-4000-8000-000000000003';
const W4='10000000-0000-4000-8000-000000000004';
const W5='10000000-0000-4000-8000-000000000005';
const W6='10000000-0000-4000-8000-000000000006';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');
const migration=(name)=>fs.readFileSync(`cloud/migrations/20261121_${name}.sql`,'utf8');

async function bootstrap(mode){
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
 if(mode==='ordered'){
  await db.exec(legacySchema);
  for(const name of ['01_ai_review_foundation','02_ai_reviews','03_ai_planning','04_ai_generation','05_review_queue'])await db.exec(migration(name));
 }else{
  await db.exec(schema);
  for(const name of ['02_ai_reviews','03_ai_planning','04_ai_generation','05_review_queue'])await db.exec(migration(name));
 }
 await db.exec(`
  grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role;
  grant all on all tables in schema public,storage to service_role;
 `);
 return db;
}

async function resetRole(db){await db.exec('reset role');}
async function asService(db){await resetRole(db);await db.exec('set role service_role');}
async function asAuthenticated(db){await resetRole(db);await db.exec('set role authenticated');}
async function upsertUser(db,id){await resetRole(db);await db.query('insert into auth.users(id) values($1) on conflict(id) do nothing',[id]);}
async function addWorkspace(db,w,{judge=A,ordinary=true}={}){
 await resetRole(db);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,$2,'owner@example.test','x')",[w,`工作区 ${w.slice(-1)}`]);
 if(judge){
  await upsertUser(db,judge);
  await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'专职评委','admin')",[w,judge]);
  if(ordinary){
   await upsertUser(db,B);
   await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'普通评审','reviewer') on conflict do nothing",[w,B]);
  }
  await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[w,judge]);
 }
}
function deckData(w,id,preview=true,title='测试作品'){
 return {
  id,name:title,title,style:'克制深青',origin:'test',
  images:preview?[{id:`${id}-page`,name:'page.png',path:`${w}/${id}/page.png`,position:1}]:[],
  generation:{outline:'测试大纲',style:'测试风格',pages:[],references:[]}
 };
}
async function addDeck(db,w,id,sourceKind='uploaded',{preview=true,title='测试作品'}={}){
 await resetRole(db);
 await db.query('insert into decks(id,workspace_id,source_kind,data) values($1,$2,$3,$4::jsonb)',[id,w,sourceKind,JSON.stringify(deckData(w,id,preview,title))]);
}
async function jobsFor(db,id){
 return (await db.query('select * from ai_review_jobs where deck_id=$1 order by variant,id',[id])).rows;
}
async function prepare(db,w,id){
 await asService(db);
 return (await db.query('select ai_review_prepare($1,$2,$3::jsonb) as value',[w,A,JSON.stringify({deckId:id,scope:'deck'})])).rows[0].value;
}
async function claim(db,w,kind,limit=1){
 await asService(db);
 return (await db.query('select ai_review_claim_jobs($1,$2,$3,$4) as value',[w,A,kind,limit])).rows[0].value;
}
async function finish(db,w,job,attempt,status,result=null,error=null,terminal=false){
 await asService(db);
 return (await db.query('select ai_review_finish_attempt($1,$2,$3,$4,$5,$6::jsonb,$7,$8) as value',[w,A,job,attempt,status,result===null?null:JSON.stringify(result),error,terminal])).rows[0].value;
}

async function exercise(db){
 await addWorkspace(db,W1);
 await addWorkspace(db,W2,{judge:null});
 await addWorkspace(db,W3,{judge:null});
 await addWorkspace(db,W4);
 await addWorkspace(db,W5);
 await addWorkspace(db,W6);

 // No preview means no job, even when a judge is assigned.
 await addDeck(db,W1,'no-preview','uploaded',{preview:false});
 assert.equal((await jobsFor(db,'no-preview')).length,0,'no-preview deck must not enqueue');

 // Both uploaded and generated tracks are auto-enqueued by the deck trigger.
 await addDeck(db,W1,'uploaded-auto','uploaded');
 await addDeck(db,W1,'generated-auto','generated');
 const uploadedJobs=await jobsFor(db,'uploaded-auto');
 const generatedJobs=await jobsFor(db,'generated-auto');
 assert.ok(uploadedJobs.length>=1,'uploaded track must enqueue');
 assert.ok(generatedJobs.length>=1,'generated track must enqueue');
 assert.ok(uploadedJobs.every((job)=>job.source_kind==='uploaded'));
 assert.ok(generatedJobs.every((job)=>job.source_kind==='generated'));
 assert.deepEqual([...new Set(uploadedJobs.map((job)=>job.variant))].sort(),['assisted','baseline'],'holdout deck must enqueue both variants');

 // Re-preparing the same immutable version is idempotent.
 const uploadedIds=uploadedJobs.map((job)=>job.id).sort();
 await prepare(db,W1,'uploaded-auto');
 await prepare(db,W1,'uploaded-auto');
 assert.deepEqual((await jobsFor(db,'uploaded-auto')).map((job)=>job.id).sort(),uploadedIds,'same version must reuse jobs');

 // No dedicated judge means no auto-enqueue.
 await addDeck(db,W2,'no-judge-deck','uploaded');
assert.equal((await db.query("select count(*)::int n from decks where id='no-judge-deck'")).rows[0].n,1,'no-judge workspace must still import the deck');
 assert.equal((await jobsFor(db,'no-judge-deck')).length,0,'workspace without judge must not enqueue');

 // Existing pending work is not auto-backfilled when a judge is assigned later.
 await addDeck(db,W3,'manual-backfill','uploaded');
 assert.equal((await jobsFor(db,'manual-backfill')).length,0);
 await resetRole(db);
 await upsertUser(db,A);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'专职评委','admin')",[W3,A]);
 await db.query('update workspaces set dedicated_judge_id=$2 where id=$1',[W3,A]);
 assert.equal((await jobsFor(db,'manual-backfill')).length,0,'judge assignment must not silently backfill');
 await prepare(db,W3,'manual-backfill');
 assert.equal((await jobsFor(db,'manual-backfill')).length,1,'manual backfill must enqueue exactly one calibration job');

 // Ordinary authenticated users cannot call the service-only finish RPC.
 await asAuthenticated(db);
 await assert.rejects(
  db.query('select ai_review_finish_attempt($1,$2,$3,$4,$5,$6::jsonb,$7,$8) as value',[W1,A,uploadedJobs[0].id,1,'failed',null,'普通账号尝试',false]),
  /permission denied/
 );

 // Failed attempts back off 30s, then 2m, and stop at the 3-attempt budget.
 await addDeck(db,W4,'stale-deck-1','uploaded');
 let failureJob=(await claim(db,W4,'uploaded',1))[0];
 assert.ok(failureJob,'failure test job must be claimed');
 let failed=await finish(db,W4,failureJob.id,failureJob.attempts,'failed',null,'第一次失败',false);
 assert.equal(failed.status,'failed');
 let row=(await db.query("select status,attempts,available_at>now()+interval '25 seconds' as delayed from ai_review_jobs where id=$1",[failureJob.id])).rows[0];
 assert.equal(row.status,'failed');assert.equal(row.attempts,1);assert.equal(row.delayed,true,'first failure must back off about 30 seconds');
 assert.equal((await claim(db,W4,'uploaded',1)).length,0,'job must stay unavailable during backoff');
 await resetRole(db);await db.query("update ai_review_jobs set available_at=now()-interval '1 second' where id=$1",[failureJob.id]);
 failureJob=(await claim(db,W4,'uploaded',1))[0];assert.equal(failureJob.attempts,2);
 failed=await finish(db,W4,failureJob.id,failureJob.attempts,'failed',null,'第二次失败',false);assert.equal(failed.status,'failed');
 row=(await db.query("select available_at>now()+interval '115 seconds' as delayed from ai_review_jobs where id=$1",[failureJob.id])).rows[0];
 assert.equal(row.delayed,true,'second failure must back off about 2 minutes');
 await resetRole(db);await db.query("update ai_review_jobs set available_at=now()-interval '1 second' where id=$1",[failureJob.id]);
 failureJob=(await claim(db,W4,'uploaded',1))[0];assert.equal(failureJob.attempts,3);
 failed=await finish(db,W4,failureJob.id,failureJob.attempts,'failed',null,'第三次失败',false);assert.equal(failed.status,'failed');
 row=(await db.query("select attempts,available_at='infinity'::timestamptz as exhausted from ai_review_jobs where id=$1",[failureJob.id])).rows[0];
 assert.equal(row.attempts,3);assert.equal(row.exhausted,true,'third failure must exhaust the retry budget');
 assert.equal((await claim(db,W4,'uploaded',1)).length,0,'exhausted job must never be claimed again');

 // Expired leases are re-claimable; expired and previous attempt numbers cannot write back.
 await addDeck(db,W5,'expired-deck','uploaded');
 let leaseJob=(await claim(db,W5,'uploaded',1))[0];
 assert.equal(leaseJob.attempts,1);
 await resetRole(db);await db.query("update ai_review_jobs set updated_at=now()-interval '16 minutes' where id=$1",[leaseJob.id]);
 let stale=await finish(db,W5,leaseJob.id,1,'completed',{ok:true});
 assert.equal(stale.ok,false);assert.equal(stale.status,'lease_lost','expired lease must reject completion');
 assert.equal((await db.query('select count(*)::int n from ai_review_records where job_id=$1',[leaseJob.id])).rows[0].n,0);
 leaseJob=(await claim(db,W5,'uploaded',1))[0];
 assert.equal(leaseJob.id,stale.jobId||leaseJob.id);assert.equal(leaseJob.attempts,2,'expired running job must be reclaimed');
 stale=await finish(db,W5,leaseJob.id,1,'completed',{ok:true});
 assert.equal(stale.ok,false);assert.equal(stale.status,'lease_lost','previous attempt number must not write back');
 assert.equal((await db.query('select count(*)::int n from ai_review_records where job_id=$1',[leaseJob.id])).rows[0].n,0);
 const completed=await finish(db,W5,leaseJob.id,2,'completed',{scores:{visual:4,layout:4},modelHint:'test'});
 assert.equal(completed.status,'completed','current attempt must still complete');

 // A deck version that changes after claim cannot produce a stale visible match.
 await addDeck(db,W6,'race-deck','uploaded');
 let raceJobs=await jobsFor(db,'race-deck');assert.equal(raceJobs.length,1);
 const oldJob=raceJobs[0];
 const oldVersion=oldJob.deck_version;
 const rubricVersion=oldJob.rubric_version;
 const raceClaim=(await claim(db,W6,'uploaded',1))[0];assert.equal(raceClaim.id,oldJob.id);assert.equal(raceClaim.attempts,1);
 await resetRole(db);
 await db.query(`insert into ai_review_submissions(
   workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,deck_version,rubric_version,human_version,max_score,entry
  ) values($1,$2,'uploaded','deck','',0,$3,$4,$5,'race-old',5,$6::jsonb)`,[
   W6,'race-deck',B,oldVersion,rubricVersion,JSON.stringify({scores:{visual:4,layout:4}})
  ]);
 await db.query("update ai_review_jobs set updated_at=now()-interval '16 minutes' where id=$1",[oldJob.id]);
 const nextData=deckData(W6,'race-deck',true,'竞态后版本');
 await db.query('update decks set data=$2::jsonb where id=$1',['race-deck',JSON.stringify(nextData)]);
 raceJobs=await jobsFor(db,'race-deck');assert.equal(raceJobs.length,2,'deck change must enqueue the new version');
 const currentJob=raceJobs.find((job)=>job.deck_version!==oldVersion);assert.ok(currentJob);
 const reclaimed=(await claim(db,W6,'uploaded',1))[0];
 assert.equal(reclaimed.id,oldJob.id);assert.equal(reclaimed.attempts,2,'expired old-version job must be re-claimed first');
 let raced=await finish(db,W6,oldJob.id,1,'completed',{scores:{visual:4,layout:4},modelHint:'test'});
 assert.equal(raced.ok,false);assert.equal(raced.status,'lease_lost','old attempt must not overwrite a re-claimed job');
 let oldState=(await db.query("select status,attempts,available_at='infinity'::timestamptz as terminal from ai_review_jobs where id=$1",[oldJob.id])).rows[0];
 assert.equal(oldState.status,'running');assert.equal(oldState.attempts,2);assert.equal(oldState.terminal,false);
 assert.equal((await db.query('select count(*)::int n from ai_review_records where job_id=$1',[oldJob.id])).rows[0].n,0,'old attempt must not write an AI record');
 assert.equal((await db.query('select count(*)::int n from ai_review_matches m join ai_review_submissions s on s.id=m.submission_id where s.human_version=$1',['race-old'])).rows[0].n,0,'old attempt must not create a match');
 raced=await finish(db,W6,oldJob.id,2,'completed',{scores:{visual:4,layout:4},modelHint:'test'});
 assert.equal(raced.ok,false);assert.equal(raced.status,'version_changed','changed deck version must invalidate the current in-flight attempt');
 oldState=(await db.query("select status,available_at='infinity'::timestamptz as terminal from ai_review_jobs where id=$1",[oldJob.id])).rows[0];
 assert.equal(oldState.status,'failed');assert.equal(oldState.terminal,true);
 assert.equal((await db.query('select count(*)::int n from ai_review_records where job_id=$1',[oldJob.id])).rows[0].n,0,'stale attempt must not write an AI record');
 assert.equal((await db.query('select count(*)::int n from ai_review_matches m join ai_review_submissions s on s.id=m.submission_id where s.human_version=$1',['race-old'])).rows[0].n,0,'stale attempt must not create a match');
 assert.equal((await db.query('select status from ai_review_jobs where id=$1',[currentJob.id])).rows[0].status,'queued','current-version job must remain queued');

 return {
  uploadedVariants:[...new Set(uploadedJobs.map((job)=>job.variant))].sort(),
  generatedVariants:[...new Set(generatedJobs.map((job)=>job.variant))].sort(),
  manualBackfill:(await jobsFor(db,'manual-backfill')).length,
  exhausted:(await db.query('select attempts from ai_review_jobs where id=$1',[failureJob.id])).rows[0].attempts,
  raceStatus:raced.status
 };
}

(async()=>{
 const ordered=await bootstrap('ordered');
 const fresh=await bootstrap('fresh');
 try{
  const orderedResult=await exercise(ordered);
  const freshResult=await exercise(fresh);
  assert.deepEqual(freshResult,orderedResult,'fresh and ordered migration paths must agree');
  console.log('PASS: ordered 01->05 and fresh schema->02->05; two-track auto enqueue, manual backfill, idempotency, retry budget/backoff, lease reclaim, stale-attempt protection, service-only finish, and version-race rejection');
 }finally{
  await ordered.close();
  await fresh.close();
 }
})().catch((error)=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});
