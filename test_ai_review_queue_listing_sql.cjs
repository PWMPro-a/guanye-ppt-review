// npm install --prefix .test-output/sql @electric-sql/pglite
// Exact queue pagination/count/job checks for migration 10.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict');

const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const W1='30000000-0000-4000-8000-000000000001';
const W2='30000000-0000-4000-8000-000000000002';
const W3='30000000-0000-4000-8000-000000000003';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');
const migrationName='20261121_10_review_queue_listing';
const migration=(name)=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8');
const throughNine=[
 '20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning',
 '20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history',
 '20261121_07_review_calibration','20261121_08_review_comparison','20261121_09_review_backfill'
];
const freshThroughNine=throughNine.slice(1);

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
  for(const name of throughNine)await db.exec(migration(name));
 }else{
  await db.exec(schema);
  for(const name of freshThroughNine)await db.exec(migration(name));
 }
 const absent=(await db.query(`select
  to_regprocedure('public.ai_review_queue_decks(uuid,text,text,integer)') is null as decks_absent,
  to_regprocedure('public.ai_review_queue_counts(uuid)') is null as counts_absent,
  to_regprocedure('public.ai_review_queue_jobs(uuid,text)') is null as jobs_absent`)).rows[0];
 assert.deepEqual(absent,{decks_absent:true,counts_absent:true,jobs_absent:true},`${mode}: migration 10 must be absent before upgrade`);
 await db.exec(migration(migrationName));
 await db.exec(migration(migrationName));
 await db.exec(`
  grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role;
  grant all on all tables in schema public,storage to service_role;
 `);
 return db;
}

async function resetRole(db){await db.exec('reset role');}
async function asRole(db,role){await resetRole(db);await db.exec(`set role ${role}`);}
async function asService(db){await asRole(db,'service_role');}
async function addWorkspace(db,id,label){await resetRole(db);await db.query(
 'insert into workspaces(id,name,owner_email,invite_hash) values($1,$2,$3,$4)',[id,label,`${label}@example.test`,`invite-${label}`]
);}

async function decksPage(db,w,kind,after=null,limit=50){
 await asService(db);
 return (await db.query(
  'select public.ai_review_queue_decks($1::uuid,$2::text,$3::text,$4::integer) as value',
  [w,kind,after,limit]
 )).rows[0].value;
}
async function counts(db,w){
 await asService(db);
 return (await db.query('select public.ai_review_queue_counts($1::uuid) as value',[w])).rows[0].value;
}
async function jobs(db,w,kind=null){
 await asService(db);
 return (await db.query('select public.ai_review_queue_jobs($1::uuid,$2::text) as value',[w,kind])).rows[0].value;
}
async function callAs(db,role,sql,params=[]){
 await asRole(db,role);
 try{return await db.query(sql,params);}finally{await resetRole(db);}
}
const sortedKeys=(value)=>Object.keys(value).sort();
const ids=(value)=>value.map(item=>item.id);
const range=(start,end)=>Array.from({length:end-start+1},(_,index)=>`uploaded-${String(start+index).padStart(4,'0')}`);

async function seed(db){
 await resetRole(db);
 await db.query('insert into auth.users(id) values($1)',[A]);
 await addWorkspace(db,W1,'queue-main');
 await addWorkspace(db,W2,'queue-other');
 await addWorkspace(db,W3,'queue-whitespace-cursor');
 await db.query(`
  insert into decks(id,workspace_id,source_kind,data)
  select
   'uploaded-'||lpad(i::text,4,'0'),$1::uuid,'uploaded',
   jsonb_build_object(
    'id','uploaded-'||lpad(i::text,4,'0'),
    'title',case when i=1 then '' else '上传标题 '||i end,
    'name',case when i=2 then '' else '上传名称 '||i end,
    'privatePayload',jsonb_build_object('secret',i)
   )
  from generate_series(1,1005) i
 `,[W1]);
 await db.query(`
  insert into decks(id,workspace_id,source_kind,data)
  select
   'generated-'||lpad(i::text,4,'0'),$1::uuid,'generated',
   jsonb_build_object('id','generated-'||lpad(i::text,4,'0'),'title','生成标题 '||i,'name','生成名称 '||i)
  from generate_series(1,4) i
 `,[W1]);
 await db.query(`
  insert into decks(id,workspace_id,source_kind,data)
  select
   'other-'||lpad(i::text,4,'0'),$1::uuid,'uploaded',
   jsonb_build_object('id','other-'||lpad(i::text,4,'0'),'title','别处 '||i,'name','别处名称 '||i)
  from generate_series(1,7) i
 `,[W2]);
 await db.query(`
  insert into decks(id,workspace_id,source_kind,data) values
   ('   ',$1::uuid,'uploaded',jsonb_build_object('id','   ','title','空格 ID','name','空格名称')),
   ('after-space',$1::uuid,'uploaded',jsonb_build_object('id','after-space','title','空格之后','name','空格之后'))
 `,[W3]);

 await db.query(`
  insert into ai_review_jobs(
   id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,
   deck_version,rubric_version,max_score,status,attempts,judge_user_id,error,created_at,updated_at
  )
  select
   md5('uploaded-job-'||i)::uuid,$1::uuid,d.id,'uploaded','deck','',0,'baseline',1,'calibration',
   'deck-v1','rubric-v1',5,
   case i%4 when 1 then 'queued' when 2 then 'running' when 3 then 'completed' else 'failed' end,
   case when i%3=0 then 1 else 0 end,$2::uuid,
   case when i=7 then '仅用于队列状态' else null end,
   timestamptz '2026-11-21 00:00:00+00' + i * interval '1 second',
   timestamptz '2026-11-21 00:00:00+00' + i * interval '1 second'
  from (
   select d.*,right(d.id,4)::int as i from decks d
   where d.workspace_id=$1::uuid and d.source_kind='uploaded'
  ) d
 `,[W1,A]);
 await db.query(`
  insert into ai_review_jobs(
   id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,
   deck_version,rubric_version,max_score,status,attempts,judge_user_id,error,created_at,updated_at
  )
  select
   md5('generated-job-'||i)::uuid,$1::uuid,d.id,'generated','deck','',0,'baseline',1,'calibration',
   'deck-v1','rubric-v1',5,
   case i when 1 then 'queued' when 2 then 'running' when 3 then 'completed' else 'failed' end,
   0,$2::uuid,null,
   timestamptz '2026-11-21 00:00:00+00' + i * interval '1 second',
   timestamptz '2026-11-21 00:00:00+00' + i * interval '1 second'
  from (
   select d.*,right(d.id,4)::int as i from decks d
   where d.workspace_id=$1::uuid and d.source_kind='generated'
  ) d
 `,[W1,A]);
 await db.query(`
  insert into ai_review_jobs(
   id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,
   deck_version,rubric_version,max_score,status,attempts,judge_user_id,error,created_at,updated_at
  )
  select
   md5('other-job-'||i)::uuid,$1::uuid,d.id,'uploaded','deck','',0,'baseline',1,'calibration',
   'deck-v1','rubric-v1',5,
   case i when 1 then 'queued' when 2 then 'queued' when 3 then 'running' when 4 then 'running'
          when 5 then 'completed' when 6 then 'completed' else 'failed' end,
   0,$2::uuid,null,
   timestamptz '2026-11-22 00:00:00+00' + i * interval '1 second',
   timestamptz '2026-11-22 00:00:00+00' + i * interval '1 second'
  from (
   select d.*,right(d.id,4)::int as i from decks d
   where d.workspace_id=$1::uuid
  ) d
 `,[W2,A]);
}

async function functionMetadata(db){
 return (await db.query(`
  select
   p.oid::regprocedure::text as identity,
   p.prosecdef as security_definer,
   p.proretset as returns_set,
   pg_get_function_result(p.oid) as result_type,
   coalesce(array_to_string(p.proconfig,','),'') as config
  from pg_proc p
  where p.oid in (
   to_regprocedure('public.ai_review_queue_decks(uuid,text,text,integer)'),
   to_regprocedure('public.ai_review_queue_counts(uuid)'),
   to_regprocedure('public.ai_review_queue_jobs(uuid,text)')
  )
  order by p.oid::regprocedure::text
 `)).rows;
}

async function exercise(db,mode){
 await seed(db);
 const metadata=await functionMetadata(db);
 assert.equal(metadata.length,3,`${mode}: all three functions must exist`);
 for(const row of metadata){
  assert.equal(row.security_definer,false,`${mode}: ${row.identity} must be security invoker`);
  assert.equal(row.returns_set,false,`${mode}: ${row.identity} must return one scalar row`);
  assert.equal(row.result_type,'jsonb',`${mode}: ${row.identity} must return jsonb`);
  assert.match(row.config,/search_path=/ ,`${mode}: ${row.identity} must pin search_path`);
 }
 const acl=(await db.query(`select
  has_function_privilege('service_role','public.ai_review_queue_decks(uuid,text,text,integer)','EXECUTE') as decks_service,
  has_function_privilege('authenticated','public.ai_review_queue_decks(uuid,text,text,integer)','EXECUTE') as decks_auth,
  has_function_privilege('anon','public.ai_review_queue_decks(uuid,text,text,integer)','EXECUTE') as decks_anon,
  has_function_privilege('service_role','public.ai_review_queue_counts(uuid)','EXECUTE') as counts_service,
  has_function_privilege('authenticated','public.ai_review_queue_counts(uuid)','EXECUTE') as counts_auth,
  has_function_privilege('anon','public.ai_review_queue_counts(uuid)','EXECUTE') as counts_anon,
  has_function_privilege('service_role','public.ai_review_queue_jobs(uuid,text)','EXECUTE') as jobs_service,
  has_function_privilege('authenticated','public.ai_review_queue_jobs(uuid,text)','EXECUTE') as jobs_auth,
  has_function_privilege('anon','public.ai_review_queue_jobs(uuid,text)','EXECUTE') as jobs_anon`)).rows[0];
 assert.deepEqual(acl,{
  decks_service:true,decks_auth:false,decks_anon:false,
  counts_service:true,counts_auth:false,counts_anon:false,
  jobs_service:true,jobs_auth:false,jobs_anon:false
 });
 const indexes=(await db.query(`select indexname,indexdef from pg_indexes
  where schemaname='public' and indexname in (
   'ai_review_queue_decks_page_idx','ai_review_queue_jobs_all_order_idx','ai_review_queue_jobs_kind_order_idx'
  ) order by indexname`)).rows;
 assert.equal(indexes.length,3,`${mode}: listing indexes must exist`);
 const byName=Object.fromEntries(indexes.map(row=>[row.indexname,row.indexdef]));
 assert.match(byName.ai_review_queue_decks_page_idx,/\(workspace_id, source_kind, id\)/);
 assert.match(byName.ai_review_queue_jobs_all_order_idx,/\(workspace_id, created_at, id\)/);
 assert.match(byName.ai_review_queue_jobs_kind_order_idx,/\(workspace_id, source_kind, created_at, id\)/);

 for(const role of ['authenticated','anon']){
  await assert.rejects(callAs(db,role,'select public.ai_review_queue_decks($1::uuid,$2::text,$3::text,$4::integer)',[W1,'uploaded',null,1]),/permission denied/i);
  await assert.rejects(callAs(db,role,'select public.ai_review_queue_counts($1::uuid)',[W1]),/permission denied/i);
  await assert.rejects(callAs(db,role,'select public.ai_review_queue_jobs($1::uuid,$2::text)',[W1,null]),/permission denied/i);
 }

 const first=await decksPage(db,W1,'uploaded',null,50);
 assert.deepEqual(sortedKeys(first),['items','nextCursor']);
 assert.equal(first.items.length,50);
 assert.equal(first.nextCursor,'uploaded-0050');
 assert.deepEqual(ids(first.items),range(1,50));
 for(const item of first.items)assert.deepEqual(sortedKeys(item),['id','name','sourceKind','title']);
 assert.equal(first.items[0].title,'上传名称 1');
 assert.equal(first.items[0].name,'上传名称 1');
 assert.equal(first.items[1].title,'上传标题 2');
 assert.equal(first.items[1].name,'uploaded-0002');
 assert.equal(first.items.some(item=>Object.hasOwn(item,'privatePayload')),false);

 const second=await decksPage(db,W1,'uploaded',first.nextCursor,50);
 assert.deepEqual(ids(second.items),range(51,100));
 assert.equal(second.nextCursor,'uploaded-0100');
 const tail=await decksPage(db,W1,'uploaded','uploaded-1000',50);
 assert.deepEqual(ids(tail.items),range(1001,1005));
 assert.equal(tail.nextCursor,null);
 const empty=await decksPage(db,W1,'uploaded','uploaded-1005',50);
 assert.deepEqual(empty,{items:[],nextCursor:null});
 const generated=await decksPage(db,W1,'generated',null,50);
 assert.deepEqual(ids(generated.items),['generated-0001','generated-0002','generated-0003','generated-0004']);
 assert.ok(generated.items.every(item=>item.sourceKind==='generated'));
 assert.equal(generated.nextCursor,null);
 const other=await decksPage(db,W2,'uploaded',null,50);
 assert.deepEqual(ids(other.items),Array.from({length:7},(_,index)=>`other-${String(index+1).padStart(4,'0')}`));
 assert.equal(other.items.some(item=>item.id.startsWith('uploaded-')),false);
 const whitespaceFirst=await decksPage(db,W3,'uploaded',null,50);
 assert.deepEqual(ids(whitespaceFirst.items),['   ','after-space']);
 assert.equal(whitespaceFirst.items[0].id,'   ','space-only ids must be returned verbatim');
 const whitespaceCursor=await decksPage(db,W3,'uploaded',' ',50);
 assert.deepEqual(ids(whitespaceCursor.items),['   ','after-space'],'space-only cursors must not be trimmed or rejected');
 assert.equal(whitespaceCursor.nextCursor,null);

 assert.deepEqual(await counts(db,W1),{
  uploaded:{queued:252,running:251,completed:251,failed:251},
  generated:{queued:1,running:1,completed:1,failed:1}
 });
 assert.deepEqual(await counts(db,W2),{
  uploaded:{queued:2,running:2,completed:2,failed:1},
  generated:{queued:0,running:0,completed:0,failed:0}
 });

 const expectedAll=(await db.query(`select id from ai_review_jobs
  where workspace_id=$1::uuid order by created_at,id limit 50`,[W1])).rows.map(row=>row.id);
 const allJobs=await jobs(db,W1,null);
 assert.equal(allJobs.length,50);
 assert.deepEqual(allJobs.map(row=>row.id),expectedAll);
 for(const item of allJobs)assert.deepEqual(sortedKeys(item),[
  'attempts','bucket','deck_id','deck_version','error','id','position','rubric_version',
  'scope','source_kind','split','status','unit_id','variant'
 ].sort());
 const expectedUploaded=(await db.query(`select id from ai_review_jobs
  where workspace_id=$1::uuid and source_kind='uploaded' order by created_at,id limit 50`,[W1])).rows.map(row=>row.id);
 const uploadedJobs=await jobs(db,W1,'uploaded');
 assert.equal(uploadedJobs.length,50);
 assert.ok(uploadedJobs.every(row=>row.source_kind==='uploaded'));
 assert.deepEqual(uploadedJobs.map(row=>row.id),expectedUploaded);
 const generatedJobs=await jobs(db,W1,'generated');
 assert.deepEqual(generatedJobs.map(row=>row.deck_id),['generated-0001','generated-0002','generated-0003','generated-0004']);
 assert.equal((await jobs(db,W2,null)).length,7);

 await assert.rejects(decksPage(db,W1,'video',null,10),/队列类型无效/);
 await assert.rejects(decksPage(db,W1,'uploaded',null,0),/分页大小无效/);
 await assert.rejects(decksPage(db,W1,'uploaded',null,51),/分页大小无效/);
 await assert.rejects(decksPage(db,W1,'uploaded','',10),/分页游标无效/);
 await assert.rejects(jobs(db,W1,'video'),/队列类型无效/);
 await asService(db);
 await assert.rejects(db.query('select public.ai_review_queue_decks(null::uuid,$1::text,$2::text,$3::integer)',['uploaded',null,1]),/工作区不能为空/);
 await assert.rejects(db.query('select public.ai_review_queue_counts(null::uuid)'),/工作区不能为空/);
 await assert.rejects(db.query('select public.ai_review_queue_jobs(null::uuid,$1::text)',[null]),/工作区不能为空/);
 await resetRole(db);

 return {
  first:ids(first.items),second:ids(second.items),tail:ids(tail.items),empty,
  counts:await counts(db,W1),all:allJobs.map(row=>row.deck_id),uploaded:uploadedJobs.map(row=>row.deck_id),
  generated:generatedJobs.map(row=>row.deck_id),other:ids(other.items),whitespace:ids(whitespaceCursor.items)
 };
}

(async()=>{
 const ordered=await bootstrap('ordered');
 const fresh=await bootstrap('fresh');
 try{
  const orderedResult=await exercise(ordered,'ordered 01->09 then 10');
  const freshResult=await exercise(fresh,'fresh schema then 02->10');
  assert.deepEqual(freshResult,orderedResult,'fresh and existing database paths must agree');
  console.log('PASS: 1005-deck keyset pagination, exact cross-thousand counts, mixed/other-workspace isolation, 50-job projection/order, ACL, validation, and 01->09/fresh upgrade paths');
 }finally{
  await ordered.close();
  await fresh.close();
 }
})().catch((error)=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});