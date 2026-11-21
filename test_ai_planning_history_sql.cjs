// npm install --prefix .test-output/sql @electric-sql/pglite
// Exact owner-scoped planning history checks for migration 11.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict');

const CREATOR='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const PEER='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const ADMIN='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const OUTSIDER='dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const W1='30000000-0000-4000-8000-000000000001';
const W2='30000000-0000-4000-8000-000000000002';
const W3='30000000-0000-4000-8000-000000000003';

const P_TIE_HIGH='60000000-0000-4000-8000-000000000001';
const P_TIE_LOW='50000000-0000-4000-8000-000000000002';
const P_THIRD='40000000-0000-4000-8000-000000000003';
const P_FOURTH='30000000-0000-4000-8000-000000000004';
const P_FIFTH='20000000-0000-4000-8000-000000000005';
const P_PEER='70000000-0000-4000-8000-000000000006';
const P_ADMIN='80000000-0000-4000-8000-000000000007';
const P_OTHER_WORKSPACE='90000000-0000-4000-8000-000000000008';
const P_SAVE_RACE='10000000-0000-4000-8000-000000000009';
const UNKNOWN_CURSOR='ffffffff-ffff-4fff-8fff-ffffffffffff';

const schema=fs.readFileSync('cloud/schema.sql','utf8');
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');
const migrationName='20261121_11_planning_history';
const migration=(name)=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8');
const throughTen=[
 '20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning',
 '20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history',
 '20261121_07_review_calibration','20261121_08_review_comparison','20261121_09_review_backfill',
 '20261121_10_review_queue_listing'
];
const freshThroughTen=throughTen.slice(1);
const setup=`
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
`;

async function bootstrap(mode){
 const db=new PGlite({extensions:{pgcrypto}});
 await db.exec(setup);
 if(mode==='incremental'){
  await db.exec(legacySchema);
  for(const name of throughTen)await db.exec(migration(name));
 }else{
  await db.exec(schema);
  for(const name of freshThroughTen)await db.exec(migration(name));
 }
 const absent=(await db.query(`select
  to_regprocedure('public.ai_planning_list_my_plans(uuid,uuid,integer)') is null as rpc_absent,
  to_regclass('public.ai_planning_plans_owner_history_idx') is null as index_absent`)).rows[0];
 assert.deepEqual(absent,{rpc_absent:true,index_absent:true},`${mode}: migration 11 must be absent before upgrade`);
 await db.exec(migration(migrationName));
 await db.exec(migration(migrationName));
 await db.exec(`
  grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role;
  grant all on all tables in schema public,storage to service_role;
 `);
 const metadata=(await db.query(`select
  p.prosecdef,
  p.provolatile,
  pg_get_function_result(p.oid) as result_type,
  p.proconfig,
  has_function_privilege('authenticated',p.oid,'EXECUTE') as authenticated_execute,
  has_function_privilege('anon',p.oid,'EXECUTE') as anon_execute,
  has_function_privilege('service_role',p.oid,'EXECUTE') as service_execute,
  to_regclass('public.ai_planning_plans_owner_history_idx') is not null as history_index
  from pg_proc p
  where p.oid=to_regprocedure('public.ai_planning_list_my_plans(uuid,uuid,integer)')`)).rows[0];
 assert.equal(metadata.prosecdef,true,`${mode}: RPC must be security definer`);
 assert.equal(metadata.provolatile,'s',`${mode}: history RPC must be stable`);
 assert.equal(metadata.result_type,'jsonb',`${mode}: history RPC must return scalar jsonb`);
 assert.ok((metadata.proconfig||[]).some(value=>String(value).startsWith('search_path=')),`${mode}: search_path must be fixed`);
 assert.equal(metadata.authenticated_execute,true,`${mode}: authenticated must execute`);
 assert.equal(metadata.anon_execute,false,`${mode}: anon must not execute`);
 assert.equal(metadata.service_execute,false,`${mode}: service_role must not execute the authenticated-only RPC`);
 assert.equal(metadata.history_index,true,`${mode}: owner history index must exist`);
 return db;
}

async function resetRole(db){await db.exec('reset role');}
async function setActor(db,role,userId=null){
 await resetRole(db);
 await db.query("select set_config('request.jwt.claim.sub',$1,false)",[userId||'']);
 await db.exec(`set role ${role}`);
}
async function asService(db){await setActor(db,'service_role');}
async function addWorkspace(db,id,label){await resetRole(db);await db.query(
 'insert into workspaces(id,name,owner_email,invite_hash) values($1,$2,$3,$4)',
 [id,label,`${label}@example.test`,`invite-${label}`]
);}

function heavyDraft(title,mode='content_draft'){
 return {
  schemaVersion:'1',title,mode,pageCount:1,style:{tone:'editorial'},
  pages:[{title:'概览',copy:[]}],
  references:[{deckId:'ref-1',imagePaths:['private/reference.png']}],
  generationStyleImages:['private/style.png'],
  sourceManifest:[{name:'secret.pdf',path:'private/source.pdf'}],
  draftBody:'DO_NOT_EXPOSE'
 };
}
function validDraft(title){
 return {
  schemaVersion:'1',title,mode:'content_draft',style:{tone:'editorial'},
  pages:[{title:'第一页',copy:[]}],pageCount:1,references:[],sourceManifest:[]
 };
}

async function seed(db){
 await resetRole(db);
 for(const id of [CREATOR,PEER,ADMIN,OUTSIDER])await db.query('insert into auth.users(id) values($1)',[id]);
 await addWorkspace(db,W1,'planning-history-main');
 await addWorkspace(db,W2,'planning-history-other');
 await addWorkspace(db,W3,'planning-history-pages');
 await db.query(`
  insert into members(workspace_id,user_id,name,role) values
   ($1,$2,'创建者','reviewer'),($1,$3,'同组成员','reviewer'),
   ($1,$4,'管理员','admin'),($5,$2,'跨组创建者','reviewer'),
   ($6,$2,'分页创建者','reviewer')
 `,[W1,CREATOR,PEER,ADMIN,W2,W3]);
 const rows=[
  [P_TIE_HIGH,W1,CREATOR,'draft',1,'并列高','2026-11-21 10:00:00+00'],
  [P_TIE_LOW,W1,CREATOR,'confirmed',3,'并列低','2026-11-21 10:00:00+00'],
  [P_THIRD,W1,CREATOR,'draft',2,'第三','2026-11-21 09:00:00+00'],
  [P_FOURTH,W1,CREATOR,'draft',1,'第四','2026-11-21 08:00:00+00'],
  [P_FIFTH,W1,CREATOR,'draft',1,'第五','2026-11-21 07:00:00+00'],
  [P_PEER,W1,PEER,'draft',1,'同组他人','2026-11-21 11:00:00+00'],
  [P_ADMIN,W1,ADMIN,'draft',1,'管理员本人','2026-11-21 11:30:00+00'],
  [P_OTHER_WORKSPACE,W2,CREATOR,'draft',1,'跨组本人','2026-11-21 12:00:00+00']
 ];
 await resetRole(db);
 for(const [id,w,user,status,revision,title,createdAt] of rows){
  await db.query(`
   insert into ai_planning_plans(id,workspace_id,created_by,status,revision,draft,created_at,updated_at)
   values($1::uuid,$2::uuid,$3::uuid,$4,$5,$6::jsonb,$7::timestamptz,$7::timestamptz)
  `,[id,w,user,status,revision,JSON.stringify(heavyDraft(title)),createdAt]);
 }
 await db.query(`
  insert into ai_planning_plans(id,workspace_id,created_by,status,revision,draft,created_at,updated_at)
  select md5('planning-page-'||i)::uuid,$1::uuid,$2::uuid,'draft',1,
   jsonb_build_object(
    'schemaVersion','1','title','循环 '||i,'mode','content_draft',
    'pageCount',case when i=1 then 'broken'::text else '1'::text end,
    'pages',case when i=1 then to_jsonb('broken'::text) else jsonb_build_array(jsonb_build_object('title','页','copy','[]'::jsonb)) end,
    'style','{}'::jsonb,'references','[]'::jsonb,'sourceManifest','[]'::jsonb
   ),
   timestamptz '2026-11-20 00:00:00+00' + i * interval '1 second',
   timestamptz '2026-11-20 00:00:00+00' + i * interval '1 second'
  from generate_series(1,21) as i
 `,[W3,CREATOR]);
}

async function listPlans(db,userId,after=null,limit=50,w=W1){
 await setActor(db,'authenticated',userId);
 try{
  return (await db.query(
   'select public.ai_planning_list_my_plans($1::uuid,$2::uuid,$3::integer) as value',
   [w,after,limit]
  )).rows[0].value;
 }finally{await resetRole(db);}
}
async function captureError(action){
 try{await action();}catch(error){return error;}
 throw new Error('expected operation to fail');
}
async function serviceCall(db,sql,params=[]){
 await asService(db);
 try{return (await db.query(sql,params)).rows[0].value;}finally{await resetRole(db);}
}
const ids=(items)=>items.map(item=>item.planId);

async function exercise(db,mode){
 await seed(db);

 const page1=await listPlans(db,CREATOR,null,2);
 assert.deepEqual(Object.keys(page1).sort(),['items','nextCursor']);
 assert.deepEqual(ids(page1.items),[P_TIE_HIGH,P_TIE_LOW]);
 assert.equal(page1.nextCursor,P_TIE_LOW);
 const firstItem=page1.items[0];
 assert.deepEqual(Object.keys(firstItem).sort(),['createdAt','mode','pageCount','planId','revision','status','title']);
 assert.equal(firstItem.title,'并列高');
 assert.equal(firstItem.mode,'content_draft');
 assert.equal(firstItem.pageCount,1);
 assert.equal(firstItem.status,'draft');
 assert.equal(firstItem.revision,1);
 assert.equal(new Date(firstItem.createdAt).toISOString(),'2026-11-21T10:00:00.000Z');
 assert.ok(!JSON.stringify(page1).includes('DO_NOT_EXPOSE'));

 const page2=await listPlans(db,CREATOR,page1.nextCursor,2);
 assert.deepEqual(ids(page2.items),[P_THIRD,P_FOURTH]);
 assert.equal(page2.nextCursor,P_FOURTH);
 const page3=await listPlans(db,CREATOR,page2.nextCursor,2);
 assert.deepEqual(ids(page3.items),[P_FIFTH]);
 assert.equal(page3.nextCursor,null);
 const empty=await listPlans(db,CREATOR,P_FIFTH,2);
 assert.deepEqual(empty,{items:[],nextCursor:null});

 const tie1=await listPlans(db,CREATOR,null,1);
 assert.deepEqual(ids(tie1.items),[P_TIE_HIGH]);
 assert.equal(tie1.nextCursor,P_TIE_HIGH);
 const tie2=await listPlans(db,CREATOR,tie1.nextCursor,1);
 assert.deepEqual(ids(tie2.items),[P_TIE_LOW]);
 assert.equal(tie2.nextCursor,P_TIE_LOW);

 assert.deepEqual(ids((await listPlans(db,PEER)).items),[P_PEER]);
 assert.deepEqual(ids((await listPlans(db,ADMIN)).items),[P_ADMIN]);
 assert.deepEqual(ids((await listPlans(db,CREATOR,null,50,W2)).items),[P_OTHER_WORKSPACE]);
 assert.equal((await listPlans(db,CREATOR,null,50)).items.length,5,'other members and admins must not enter my history');

 const stitched=[];
 let loopCursor=null;
 let loopPages=0;
 do{
  const loopPage=await listPlans(db,CREATOR,loopCursor,20,W3);
  assert.ok(loopPage.items.length<=20);
  stitched.push(...loopPage.items);
  loopCursor=loopPage.nextCursor;
  loopPages++;
  assert.ok(loopPages<=3,'pagination loop must terminate');
 }while(loopCursor!==null);
 assert.equal(loopPages,2,'21 rows at page size 20 must use exactly two pages');
 assert.deepEqual(stitched.map(item=>item.title),Array.from({length:21},(_,index)=>`循环 ${21-index}`));
 assert.equal(new Set(stitched.map(item=>item.planId)).size,21,'exact stitching must not duplicate records');
 assert.equal(stitched.at(-1).pageCount,0,'malformed pages must fall back to pageCount 0 without failing history');

 const cursorErrors=[];
 for(const cursor of [P_PEER,P_ADMIN,P_OTHER_WORKSPACE,UNKNOWN_CURSOR]){
  cursorErrors.push(await captureError(()=>listPlans(db,CREATOR,cursor,2)));
 }
 assert.match(cursorErrors[0].message,/分页游标无效/);
 assert.ok(cursorErrors.every(error=>error.message===cursorErrors[0].message),`${mode}: unknown, foreign-owner and foreign-workspace cursors must share one error`);
 const limitErrors=[];
 for(const limit of [0,51,null])limitErrors.push(await captureError(()=>listPlans(db,CREATOR,null,limit)));
 assert.ok(limitErrors.every(error=>/分页数量必须为 1–50/.test(error.message)),`${mode}: invalid limits must fail`);

 const outsider=await captureError(()=>listPlans(db,OUTSIDER,null,10));
 assert.match(outsider.message,/请先加入团队/);
 await setActor(db,'authenticated');
 const anonymousActor=await captureError(()=>db.query('select public.ai_planning_list_my_plans($1::uuid,null::uuid,10)',[W1]));
 await resetRole(db);
 assert.match(anonymousActor.message,/请先登录/);

 for(const role of ['anon','service_role']){
  await setActor(db,role);
  const denied=await captureError(()=>db.query('select public.ai_planning_list_my_plans($1::uuid,null::uuid,10)',[W1]));
  await resetRole(db);
  assert.match(denied.message,/permission denied/i,`${mode}: ${role} must not execute history RPC`);
 }

 await setActor(db,'authenticated',CREATOR);
 const directDenied=await captureError(()=>db.query('select * from public.ai_planning_plans'));
 await resetRole(db);
 assert.match(directDenied.message,/permission denied/i,`${mode}: history RPC must not widen direct table access`);

 await setActor(db,'authenticated',CREATOR);
 const ownPlan=(await db.query('select public.ai_planning_get_plan($1::uuid,$2::uuid) as value',[W1,P_TIE_HIGH])).rows[0].value;
 await resetRole(db);
 assert.equal(ownPlan.planId,P_TIE_HIGH);
 await setActor(db,'authenticated',PEER);
 const peerRead=await captureError(()=>db.query('select public.ai_planning_get_plan($1::uuid,$2::uuid)',[W1,P_TIE_HIGH]));
 await resetRole(db);
 assert.match(peerRead.message,/仅策划创建者或管理员可以读取草稿/);

 const saveSql='select public.ai_planning_save_draft($1::uuid,$2::uuid,$3::uuid,$4::bigint,$5::jsonb) as value';
 const saved=await serviceCall(db,saveSql,[W1,P_SAVE_RACE,CREATOR,0,JSON.stringify(validDraft('竞态验证'))]);
 assert.equal(saved.revision,1);
 const stale=await captureError(()=>serviceCall(db,saveSql,[W1,P_SAVE_RACE,CREATOR,0,JSON.stringify(validDraft('旧版本'))]));
 assert.match(stale.message,/版本已变化|其他窗口更新/);
 const updated=await serviceCall(db,saveSql,[W1,P_SAVE_RACE,CREATOR,1,JSON.stringify(validDraft('新版本'))]);
 assert.equal(updated.revision,2);
 const confirmed=await serviceCall(
  db,
  'select public.ai_planning_confirm($1::uuid,$2::uuid,$3::uuid,$4::bigint,$5::jsonb) as value',
  [W1,P_SAVE_RACE,CREATOR,2,JSON.stringify(validDraft('确认版本'))]
 );
 assert.equal(confirmed.status,'confirmed');
 const confirmedSave=await captureError(()=>serviceCall(db,saveSql,[W1,P_SAVE_RACE,CREATOR,3,JSON.stringify(validDraft('禁止保存'))]));
 assert.match(confirmedSave.message,/已确认的策划不可修改/);

 console.log(`PASS planning history ${mode}`);
}

(async()=>{
 for(const mode of ['fresh','incremental']){
  const db=await bootstrap(mode);
  try{await exercise(db,mode);}finally{await db.close();}
 }
})().catch(error=>{console.error(error.stack||error.message);process.exit(1);});
