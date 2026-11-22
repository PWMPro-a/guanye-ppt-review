// PGlite fixture for M31 activation capacity and lease write-back.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');

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
 '20261122_28_single_judge_admission','20261122_29_single_judge_generation_references',
 '20261122_30_single_judge_creation'
];
const migrations=migrationNames.map(name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));

async function query(db,sql,values=[]){return (await db.query(sql,values)).rows;}
async function resetRole(db){await db.exec('reset role');}
async function asService(db){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub','',false)");await db.exec('set role service_role');}
async function asAuthenticated(db,userId){await resetRole(db);await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[userId]);await db.exec('set role authenticated');}

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
  create table storage.objects(id uuid primary key,name text not null,bucket_id text not null, unique(bucket_id,name));
  alter table storage.objects enable row level security;
  create function storage.foldername(text) returns text[] language sql as $$
   select string_to_array($1,'/')
  $$;
 `);
 await db.exec(legacySchema);
 await db.exec('grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role; grant select,insert,update,delete on all tables in schema public,storage to authenticated,anon; grant all on all tables in schema public,storage to service_role;');
 for(const sql of migrations)await db.exec(sql);
 return db;
}

function uuid(){return crypto.randomUUID();}
const ACTIVE=uuid(), FUTURE=uuid(), LEGACY=uuid(), MISSING=uuid();
const JUDGE=uuid(), OTHER_JUDGE=uuid(), ADMIN=uuid(), SUBMITTER=uuid();
const migration=fs.readFileSync('cloud/migrations/20261122_31_single_judge_image_runtime.sql','utf8');
const ownerMigration=fs.readFileSync('cloud/migrations/20261122_34_single_judge_owner_activation.sql','utf8');
const read=(db,sql,params=[])=>query(db,sql,params).then(rows=>rows[0].value);
const activate=(db,w,judge)=>query(db,'select public.single_judge_activate($1,$2,$3)',[w,judge,ADMIN]);
const settings=(db,w)=>read(db,'select to_jsonb(s) as value from public.ai_generation_settings s where workspace_id=$1',[w]);
const claim=(db,w,actor,name)=>read(db,'select public.ai_generation_claim_pages($1,$2,$3,32,1800) as value',[w,actor,name]);
const fail=(db,w,actor,page,token,error='fixture failure')=>read(db,'select public.ai_generation_page_fail($1,$2,$3,$4,$5) as value',[w,actor,page,token,error]);
const activateOwner=(db,w,owner)=>query(db,'select public.single_judge_activate_owner($1,$2)',[w,owner]);
const heartbeat=(db,w,actor,page,token)=>read(db,'select public.ai_generation_heartbeat($1,$2,$3,$4,1800) as value',[w,actor,page,token]);
const uploadTarget=(db,w,actor,page,token)=>read(db,'select public.ai_generation_page_upload_target($1,$2,$3,$4) as value',[w,actor,page,token]);
const succeed=(db,w,actor,page,token,path,sha)=>read(db,'select public.ai_generation_page_succeed($1,$2,$3,$4,$5,$6,$7,1920,1080) as value',[w,actor,page,token,path,'image/png',sha]);
const retry=(db,w,actor,job,page)=>read(db,'select public.ai_generation_retry_pages($1,$2,$3,$4::jsonb) as value',[w,actor,job,JSON.stringify([page])]);
const rejectCode=(promise,code)=>assert.rejects(promise,error=>error.code===code);

async function workspace(db,w,judge){
 await query(db,`insert into public.workspaces(id,name,owner_email,invite_hash)
  values($1,$1::uuid::text,$1::uuid::text||'@example.test','unused')`,[w]);
 await query(db,`insert into public.members(workspace_id,user_id,name,role)
  values($1,$2,'judge','reviewer'),($1,$3,'admin','admin'),($1,$4,'submitter','reviewer')`,[w,judge,ADMIN,SUBMITTER]);
 await query(db,`update public.ai_generation_settings set max_workers=4,updated_by=$2,
  updated_at='2026-11-21 00:00:00+00' where workspace_id=$1`,[w,ADMIN]);
}

async function addJob(db,w,actor,count){
 const id=uuid(), deck=`generated-${uuid().replaceAll('-','').slice(0,24)}`;
 await query(db,`insert into public.ai_generation_jobs(id,workspace_id,user_id,plan_id,deck_id,plan_snapshot,config)
  values($1,$2,$3,$4,$5,'{}','{"workers":4}')`,[id,w,actor,uuid(),deck]);
 return query(db,`insert into public.ai_generation_pages(job_id,workspace_id,position,storage_path)
  select $1,$2,p,$2::uuid::text||'/'||$3::text||'/'||p||'.png' from generate_series(1,$4) p
  returning id,position`,[id,w,deck,count]).then(pages=>({id,pages}));
}

const allTaskRows=db=>read(db,`select jsonb_build_object(
 'jobs',(select jsonb_agg(to_jsonb(j) order by j.id) from public.ai_generation_jobs j),
 'pages',(select jsonb_agg(to_jsonb(p) order by p.id) from public.ai_generation_pages p),
 'reviews',(select jsonb_agg(to_jsonb(r) order by r.workspace_id,r.deck_id,r.user_id) from public.reviews r),
 'decks',(select jsonb_agg(to_jsonb(d) order by d.id) from public.decks d)) as value`);

const pageAndJob=(db,page)=>read(db,`select jsonb_build_object('page',to_jsonb(p),'job',to_jsonb(j)) as value
 from public.ai_generation_pages p join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=p.workspace_id
 where p.id=$1`,[page]);

async function seedLease(db,w,actor,state='running',seconds=600,token=uuid()){
 const job=await addJob(db,w,actor,1);
 const {id:page}=job.pages[0];
 await query(db,`update public.ai_generation_pages set status=$2,lease_token=$3,
  lease_owner='fixture-worker',lease_expires_at=clock_timestamp()+make_interval(secs=>$4),attempts=2, error='fixture before'
  where id=$1`,[page,state,token,seconds]);
 await query(db,'update public.ai_generation_jobs set status=$2 where id=$1',[job.id,state==='succeeded'?'completed':'running']);
 return {job:job.id,page,token};
}

(async()=>{
 const db=await bootstrap();
 try{
  await query(db,'insert into auth.users(id) select unnest($1::uuid[])',[[JUDGE,OTHER_JUDGE,ADMIN,SUBMITTER]]);
  for(const w of [ACTIVE,FUTURE,LEGACY,MISSING])await workspace(db,w,w===FUTURE?OTHER_JUDGE:JUDGE);
  const legacyDeck=`import-${uuid()}`;
  await query(db,`insert into public.decks(id,workspace_id,data) values($1,$2,jsonb_build_object('title','fixture','images','[]'::jsonb))`,[legacyDeck,LEGACY]);
  await query(db,`insert into public.reviews(workspace_id,deck_id,user_id,data) values($1,$2,$3,'{"mode":"deck","scores":{"design":5}}')`,[LEGACY,legacyDeck,JUDGE]);
  for(const w of [ACTIVE,FUTURE,LEGACY]){
   const actor=w===FUTURE?OTHER_JUDGE:JUDGE;
   await addJob(db,w,actor,10);
   await addJob(db,w,actor,10);
  }
  await asService(db);
  await activate(db,ACTIVE,JUDGE);
  await activate(db,MISSING,JUDGE);
  await query(db,'delete from public.ai_generation_settings where workspace_id=$1',[MISSING]);
  assert.equal((await settings(db,ACTIVE)).max_workers,4,'M25 preserves explicit four before M31');
  assert.equal((await claim(db,ACTIVE,JUDGE,'pre-M31')).length,4);
  assert.equal((await claim(db,FUTURE,OTHER_JUDGE,'pre-activation')).length,4);
  const legacyBefore=await settings(db,LEGACY), futureBefore=await settings(db,FUTURE);
  const tasksBefore=await allTaskRows(db);
  await resetRole(db);
  await db.exec(migration);
  await db.exec(ownerMigration);
  assert.deepEqual(await allTaskRows(db),tasksBefore,'M31 must not modify existing scoring or queue data');
  await asService(db);
  assert.equal((await settings(db,ACTIVE)).max_workers,16);
  assert.equal((await settings(db,ACTIVE)).updated_by,ADMIN);
  assert.equal((await settings(db,MISSING)).max_workers,16,'missing active setting is inserted');
  assert.deepEqual(await settings(db,LEGACY),legacyBefore);
  assert.deepEqual(await settings(db,FUTURE),futureBefore);
  const fill=await claim(db,ACTIVE,JUDGE,'post-M31');
  await rejectCode(claim(db,ACTIVE,ADMIN,'nonowner-admin'),'42501');
  await rejectCode(claim(db,ACTIVE,SUBMITTER,'submitter'),'42501');
  assert.equal(fill.length,12,'existing four live leases count against the new workspace-wide cap');
  assert.deepEqual(await claim(db,ACTIVE,JUDGE,'another-worker'),[]);
  assert.equal((await claim(db,LEGACY,JUDGE,'legacy-worker')).length,4,'legacy explicit four still governs claims');
  assert.deepEqual(await claim(db,LEGACY,JUDGE,'other-legacy-worker'),[]);
  assert.deepEqual(await claim(db,FUTURE,OTHER_JUDGE,'still-four'),[]);
  const beforeActivation=await allTaskRows(db);
  await activateOwner(db,FUTURE,ADMIN);
  assert.equal((await settings(db,FUTURE)).max_workers,16);
  assert.deepEqual(await allTaskRows(db),beforeActivation,'activation capacity changes must not reconfigure tasks');
  const adminFill=await claim(db,FUTURE,ADMIN,'now-sixteen'); assert.equal(adminFill.length,12);
  await rejectCode(claim(db,FUTURE,OTHER_JUDGE,'third-worker'),'42501');
  const activationSetting=await settings(db,FUTURE);
  await activateOwner(db,FUTURE,ADMIN);
  assert.deepEqual(await settings(db,FUTURE),activationSetting,'idempotent activation does not rewrite an already-correct setting');
  await asAuthenticated(db,ADMIN);
  await rejectCode(read(db,'select public.ai_generation_set_workers($1,$2,4) as value',[ACTIVE,ADMIN]),'42501');
  await asService(db);
  const reviewerPage=fill[0];
  assert.equal((await heartbeat(db,ACTIVE,JUDGE,reviewerPage.pageId,reviewerPage.leaseToken)).ok,true);
  assert.equal((await uploadTarget(db,ACTIVE,JUDGE,reviewerPage.pageId,reviewerPage.leaseToken)).alreadySucceeded,false);
  await succeed(db,ACTIVE,JUDGE,reviewerPage.pageId,reviewerPage.leaseToken,reviewerPage.storagePath,'b'.repeat(64));
  assert.equal((await pageAndJob(db,reviewerPage.pageId)).page.status,'succeeded','reviewer owner finishes image runtime');
  const adminPage=adminFill[0];
  assert.equal((await heartbeat(db,FUTURE,ADMIN,adminPage.pageId,adminPage.leaseToken)).ok,true);
  assert.equal((await uploadTarget(db,FUTURE,ADMIN,adminPage.pageId,adminPage.leaseToken)).alreadySucceeded,false);
  await succeed(db,FUTURE,ADMIN,adminPage.pageId,adminPage.leaseToken,adminPage.storagePath,'c'.repeat(64));
  assert.equal((await pageAndJob(db,adminPage.pageId)).page.status,'succeeded','admin owner finishes image runtime');
  const guarded=await seedLease(db,ACTIVE,JUDGE);
  const guardedState=await pageAndJob(db,guarded.page);
  for(const actor of [ADMIN,SUBMITTER,null]){
   await rejectCode(heartbeat(db,ACTIVE,actor,guarded.page,guarded.token),'42501');
   await rejectCode(uploadTarget(db,ACTIVE,actor,guarded.page,guarded.token),'42501');
   await rejectCode(succeed(db,ACTIVE,actor,guarded.page,guarded.token,guardedState.page.storage_path,'d'.repeat(64)),'42501');
   await rejectCode(fail(db,ACTIVE,actor,guarded.page,guarded.token),'42501');
  }

  const expired=await seedLease(db,ACTIVE,JUDGE,'running',-5);
  const absentExpiry=await seedLease(db,ACTIVE,JUDGE);
  const absentToken=await seedLease(db,ACTIVE,JUDGE,'running',600,null);
  const live=await seedLease(db,ACTIVE,JUDGE);
  const complete=await seedLease(db,ACTIVE,JUDGE,'succeeded',-5);
  await query(db,'update public.ai_generation_pages set lease_expires_at=null where id=$1',[absentExpiry.page]);
  for(const [row,msg,token] of [[expired,/页面租约已过期/,expired.token],
   [absentExpiry,/页面租约已过期/,absentExpiry.token],[absentToken,/页面租约已失效/,null],[live,/页面租约已失效/,uuid()]]){
   const before=await pageAndJob(db,row.page);
   await assert.rejects(fail(db,ACTIVE,JUDGE,row.page,token),msg);
   assert.deepEqual(await pageAndJob(db,row.page),before,'rejected failure cannot change the page, lease, or job');
  }

  // Simulate preflight followed by SQL write after the lease expires, while
  // now() still reads earlier than expiry. A transaction-clock check would pass.
  const race=await seedLease(db,ACTIVE,JUDGE);
  await db.transaction(async tx=>{
   await query(tx,"update public.ai_generation_pages set lease_expires_at=clock_timestamp()+interval '400 milliseconds' where id=$1",[race.page]);
   const target=await read(tx,'select public.ai_generation_page_upload_target($1,$2,$3,$4) as value',[ACTIVE,JUDGE,race.page,race.token]);
   assert.equal(target.alreadySucceeded,false);
   assert.equal(await read(tx,'select clock_timestamp()<lease_expires_at as value from public.ai_generation_pages where id=$1',[race.page]),true);
   const before=await pageAndJob(tx,race.page);
   await tx.exec('select pg_sleep(0.5); savepoint fail_after_expiry;');
   assert.equal(await read(tx,`select now()<lease_expires_at and clock_timestamp()>=lease_expires_at as value
    from public.ai_generation_pages where id=$1`,[race.page]),true);
   await assert.rejects(fail(tx,ACTIVE,JUDGE,race.page,race.token),/页面租约已过期/);
   await tx.exec('rollback to savepoint fail_after_expiry');
   assert.deepEqual(await pageAndJob(tx,race.page),before);
  });

  const before=await pageAndJob(db,live.page);
  for(const actor of [ADMIN,SUBMITTER,OTHER_JUDGE,null])await rejectCode(fail(db,ACTIVE,actor,live.page,live.token),'42501');
  await assert.rejects(fail(db,FUTURE,ADMIN,live.page,live.token),/生成页面不存在/);
  await query(db,"update public.members set role='admin' where workspace_id=$1 and user_id=$2",[ACTIVE,JUDGE]);
  assert.equal((await uploadTarget(db,ACTIVE,JUDGE,live.page,live.token)).alreadySucceeded,false);
  await query(db,"update public.members set role='reviewer' where workspace_id=$1 and user_id=$2",[ACTIVE,JUDGE]);
  await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[ADMIN]);
  await assert.rejects(fail(db,ACTIVE,JUDGE,live.page,live.token),/操作人身份不匹配/);
  await asAuthenticated(db,JUDGE);
  await rejectCode(fail(db,ACTIVE,JUDGE,live.page,live.token),'42501');
  await resetRole(db); await db.exec('set role anon');
  await rejectCode(fail(db,ACTIVE,JUDGE,live.page,live.token),'42501');
  await asService(db);
  assert.deepEqual(await pageAndJob(db,live.page),before);
  const failed=await fail(db,ACTIVE,JUDGE,live.page,live.token,'  image task failed  ');
  assert.equal(failed.status,'failed'); assert.equal(failed.pages[0].error,'image task failed');
  const failedState=await pageAndJob(db,live.page);
  for(const key of ['lease_token','lease_owner','lease_expires_at'])assert.equal(failedState.page[key],null);
  assert.equal(failedState.page.attempts,2);
  await assert.rejects(fail(db,ACTIVE,JUDGE,live.page,live.token),/页面租约已失效/);
  assert.deepEqual(await pageAndJob(db,live.page),failedState);
  const completeBefore=await pageAndJob(db,complete.page);
  await rejectCode(fail(db,ACTIVE,ADMIN,complete.page,null),'42501');
  assert.equal((await fail(db,ACTIVE,JUDGE,complete.page,null)).status,'completed');
  assert.deepEqual(await pageAndJob(db,complete.page),completeBefore,'succeeded retry is read-only even without a live lease');
  const legacyExpired=await seedLease(db,LEGACY,JUDGE,'running',-5);
  assert.equal((await fail(db,LEGACY,ADMIN,legacyExpired.page,legacyExpired.token,' ')).pages[0].error,'页面生成失败',
   'legacy accepts a member and a matching expired lease, as before');
  assert.deepEqual(await settings(db,LEGACY),legacyBefore);
  const reviewerRetry=await seedLease(db,ACTIVE,JUDGE);
  await fail(db,ACTIVE,JUDGE,reviewerRetry.page,reviewerRetry.token);
  const adminRetry=await seedLease(db,FUTURE,ADMIN);
  await fail(db,FUTURE,ADMIN,adminRetry.page,adminRetry.token);
  await asAuthenticated(db,ADMIN);
  await rejectCode(retry(db,ACTIVE,ADMIN,reviewerRetry.job,reviewerRetry.page),'42501');
  await asAuthenticated(db,SUBMITTER);
  await rejectCode(retry(db,ACTIVE,SUBMITTER,reviewerRetry.job,reviewerRetry.page),'42501');
  await asAuthenticated(db,JUDGE);
  assert.equal((await retry(db,ACTIVE,JUDGE,reviewerRetry.job,reviewerRetry.page)).pages[0].status,'queued','reviewer owner retries image pages');
  await asAuthenticated(db,ADMIN);
  assert.equal((await retry(db,FUTURE,ADMIN,adminRetry.job,adminRetry.page)).pages[0].status,'queued','admin owner retries image pages');
  assert.equal((await retry(db,LEGACY,ADMIN,legacyExpired.job,legacyExpired.page)).pages[0].status,'queued','legacy admin retry remains member-based');
  await asService(db);

  const catalog=await read(db,`select jsonb_build_object(
   'definer',p.prosecdef,'searchPath',p.proconfig,
   'service',has_function_privilege('service_role',p.oid,'EXECUTE'),
   'authenticated',has_function_privilege('authenticated',p.oid,'EXECUTE'),
   'anon',has_function_privilege('anon',p.oid,'EXECUTE'),
   'failAnchor',position('SINGLE_JUDGE_IMAGE_FAIL_LEASE_M31' in p.prosrc)>0,
   'activationTrigger',exists(select 1 from pg_trigger t join pg_proc f on f.oid=t.tgfoid
    where t.tgrelid='public.workspaces'::regclass and t.tgname='single_judge_image_capacity_after_activation'
      and t.tgenabled='O' and position('SINGLE_JUDGE_IMAGE_CAPACITY_M31' in f.prosrc)>0)
  ) as value from pg_proc p where p.oid='public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text)'::regprocedure`);
  assert.ok(catalog.definer&&catalog.service&&!catalog.authenticated&&!catalog.anon&&catalog.failAnchor&&catalog.activationTrigger);
  assert.ok(catalog.searchPath.some(x=>['search_path=','search_path=""'].includes(x)));
  const gates=await read(db,`select jsonb_build_object(
   'count',count(*)::int,
   'roleLeak',bool_or(position('m.role' in p.prosrc)>0)
  ) as value from pg_proc p where p.oid in (
   'public.ai_generation_claim_pages(uuid,uuid,text,integer,integer)'::regprocedure,
   'public.ai_generation_heartbeat(uuid,uuid,uuid,uuid,integer)'::regprocedure,
   'public.ai_generation_page_upload_target(uuid,uuid,uuid,uuid)'::regprocedure,
   'public.ai_generation_page_succeed(uuid,uuid,uuid,uuid,text,text,text,integer,integer)'::regprocedure,
   'public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text)'::regprocedure
  ) and position('single_judge_image_actor' in p.prosrc)>0`);
  assert.deepEqual(gates,{count:5,roleLeak:false},'all image runtime entry points use the owner gate without member.role branching');
  console.log('PASS M31: admin/reviewer owners across claim/heartbeat/finish/fail/retry, non-owner and legacy guards, capacity, leases and ACLs');
 }finally{await db.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
