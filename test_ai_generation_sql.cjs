// npm install --prefix .test-output/sql @electric-sql/pglite
// Ordered integration test: legacy schema -> 01 -> 02 -> 03 -> 04 -> 05 -> 06.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict');
const W1='11111111-1111-4111-8111-111111111111';
const W2='22222222-2222-4222-8222-222222222222';
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const C='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const D='dddddddd-dddd-4ddd-8ddd-dddddddddddd';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const foundationMarker='-- AI_REVIEW_FOUNDATION_BODY_START';
const legacySchema=schema.slice(0,schema.indexOf(foundationMarker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');
const migrations=['01_ai_review_foundation','02_ai_reviews','03_ai_planning','04_ai_generation','05_review_queue','06_generation_history'].map(name=>fs.readFileSync(`cloud/migrations/20261121_${name}.sql`,'utf8'));

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
 await db.exec('grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role; grant all on all tables in schema public,storage to service_role; grant execute on all functions in schema public to service_role;');
 for(const sql of migrations) await db.exec(sql);
 return db;
}
async function resetRole(db){await db.exec('reset role');}
async function login(db,id,email){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub',$1,false),set_config('request.jwt.claim.email',$2,false)",[id,email]);await db.exec('set role authenticated');}
async function asService(db){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub','',false),set_config('request.jwt.claim.email','',false)");await db.exec('set role service_role');}
async function asAuthenticated(db){await resetRole(db);await db.exec('set role authenticated');}
async function asAnon(db){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub','',false),set_config('request.jwt.claim.email','',false)");await db.exec('set role anon');}
const page=(title,body,styleReferenceDeckIds=[])=>({index:0,title,copy:[{kind:'title',text:title},{kind:'body',text:body}],layout:'左对齐，单栏网格',visualBrief:`${title}的克制视觉说明`,contentSources:['测试来源'],styleReferenceDeckIds});
function draft(title,pages,references=[]){return {schemaVersion:1,title,mode:'content_draft',pageCount:pages.length,style:{name:'克制深青',summary:'白底、深青、无渐变',palette:['#FFFFFF','#164E5A'],typography:{heading:'思源宋体',body:'思源黑体',numbers:'等宽数字'},layoutLanguage:'大留白与严格网格',decoration:'细线',avoid:['渐变','发光'],rationale:'便于逐页核对',referenceDeckIds:references.map(x=>x.deckId)},pages:pages.map((x,i)=>({...x,index:i+1})),references,templateSample:null,generationBrief:'严格按已确认文案生成，不得改写。',assumptions:[],warnings:[],sourceManifest:[]};}
function normalizePlan(planId,payload){
 const pages=payload.pages.map((row,index)=>({
  planPageId:`page-${String(index+1).padStart(4,'0')}`,
  position:index+1,
  title:row.title,
  outline:{index,title:row.title,copy:row.copy,layout:row.layout,visualBrief:row.visualBrief,contentSources:row.contentSources,styleReferenceDeckIds:row.styleReferenceDeckIds},
  description:{text:[row.title,...row.copy.map(x=>x.text),row.layout,row.visualBrief,payload.generationBrief].join('\n')},
  prompt:[row.title,...row.copy.map(x=>x.text),row.layout,row.visualBrief].join('\n'),
  references:[],
  contentType:'image/png'
 }));
 return {outline:{title:payload.title,brief:payload.generationBrief,pages:pages.map(x=>x.outline)},styleText:`风格：${payload.style.name}\n${payload.style.summary}\n版式语言：${payload.style.layoutLanguage}`,pages,snapshot:payload};
}
async function confirmPlan(db,planId,value){
 await asService(db);
 const saved=(await db.query('select ai_planning_save_draft($1,$2,$3,$4,$5::jsonb) as value',[W1,planId,A,0,JSON.stringify(value)])).rows[0].value;
 assert.equal(saved.status,'draft');
 const confirmed=(await db.query('select ai_planning_confirm($1,$2,$3,$4,$5::jsonb) as value',[W1,planId,A,saved.revision,JSON.stringify(value)])).rows[0].value;
 assert.equal(confirmed.status,'confirmed');
 const immutable=(await db.query('select ai_planning_read_confirmed_plan($1) as value',[planId])).rows[0].value;
 assert.equal(immutable.planId,planId);
 await login(db,A,'owner@example.test');
 const authorized=(await db.query('select ai_planning_get_plan($1,$2) as value',[W1,planId])).rows[0].value;
 assert.equal(authorized.status,'confirmed');
 assert.equal(authorized.confirmed.contentHash,immutable.contentHash);
 return immutable;
}
async function createRaw(db,actor,planId,deckId,planSnapshot,title,styleText,outline,references,pages){
 return db.query(`select ai_generation_create(
  $1::uuid,$2::uuid,$3,$4,$5::jsonb,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11::jsonb
 ) as job`,[W1,actor,planId,deckId,JSON.stringify(planSnapshot),title,styleText,JSON.stringify(outline),JSON.stringify(references||[]),JSON.stringify(pages),JSON.stringify({workers:4})]);
}
async function createJob(db,planId,payload,deckId){
 const plan=payload.plan;
 const result=await createRaw(db,A,planId,deckId,plan.snapshot,payload.title,plan.styleText,plan.outline,payload.references,plan.pages);
 return result.rows[0].job;
}
async function claim(db,worker,limit){return (await db.query('select ai_generation_claim_pages($1,$2,$3,$4,$5) as pages',[W1,A,worker,limit,1800])).rows[0].pages;}
async function succeed(db,page,hash){const target=(await db.query('select ai_generation_page_upload_target($1,$2,$3,$4) as target',[W1,A,page.pageId,page.leaseToken])).rows[0].target;assert.equal(target.storagePath,page.storagePath);return (await db.query('select ai_generation_page_succeed($1,$2,$3,$4,$5,$6,$7,$8,$9) as job',[W1,A,page.pageId,page.leaseToken,target.storagePath,'image/png',hash,1600,900])).rows[0].job;}
async function fail(db,page,error){return (await db.query('select ai_generation_page_fail($1,$2,$3,$4,$5) as job',[W1,A,page.pageId,page.leaseToken,error])).rows[0].job;}
(async()=>{
 const db=await bootstrap();
 try{
  await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,$2,$3,encode(extensions.digest('invite-one','sha256'),'hex')),($4,$5,$6,encode(extensions.digest('invite-two','sha256'),'hex'))",[W1,'工作区一','owner@example.test',W2,'工作区二','other@example.test']);
  await db.query('insert into auth.users(id) values($1),($2),($3),($4)',[A,B,C,D]);
  await login(db,A,'owner@example.test');await db.query('select join_workspace($1,$2,$3)',[W1,'管理员','invite-one']);
  await login(db,B,'reviewer@example.test');await db.query('select join_workspace($1,$2,$3)',[W1,'普通评审','invite-one']);
  await login(db,C,'other@example.test');await db.query('select join_workspace($1,$2,$3)',[W2,'其他管理员','invite-two']);
  await login(db,D,'empty@example.test');await db.query('select join_workspace($1,$2,$3)',[W1,'空历史成员','invite-one']);

  const planAId='33333333-3333-4333-8333-333333333333';
  const planBId='44444444-4444-4444-8444-444444444444';
  const planDraftId='55555555-5555-4555-8555-555555555555';
  const missingPlanId='66666666-6666-4666-8666-666666666666';
  const referencePath=`${W1}/refs/high-score.png`;
  const reference={deckId:'deck-ref',title:'高分库参考',style:'深青',score:4.8,rank:1,imagePaths:[referencePath]};
  const draftA=draft('套件 A',[page('封面','品牌发布',['deck-ref']),page('方法','三步方法',['deck-ref']),page('结论','留下结论')],[reference]);
  const draftB=draft('套件 B',[page('另一套封面','第二套内容'),page('另一套结尾','谢谢观看')],[]);
  const payloadA=await confirmPlan(db,planAId,draftA);
  const payloadB=await confirmPlan(db,planBId,draftB);
  await asAuthenticated(db);await login(db,B,'reviewer@example.test');
  await assert.rejects(db.query('select ai_planning_get_plan($1,$2)',[W1,planAId]),/独立授权/,'only explicit version access may read the confirmed plan');
  await asService(db);
  await assert.rejects(
   createRaw(db,A,missingPlanId,'generated-missing',{},'不存在','','{}',[],[]),
   /不存在|确认/
  ,'missing plan must be rejected');
  const draftSaved=(await db.query('select ai_planning_save_draft($1,$2,$3,$4,$5::jsonb) as value',[W1,planDraftId,A,0,JSON.stringify(draftB)])).rows[0].value;
  assert.equal(draftSaved.status,'draft');
  await assert.rejects(
   createRaw(db,A,planDraftId,'generated-draft',{},draftB.title,'','{}',[],[]),
   /尚未确认|确认/
  ,'unconfirmed plan must be rejected');
  await assert.rejects(
   createRaw(db,B,planAId,'generated-unauthorized',payloadA,payloadA.title,'','{}',payloadA.references,[]),
   /生成授权/
  ,'unauthorized member must be rejected');
  await assert.rejects(
   createRaw(db,A,planAId,'generated-tampered',{...payloadA,title:'篡改标题'},payloadA.title,'','{}',payloadA.references,[]),
   /快照/
  ,'tampered snapshot must be rejected');
  await login(db,A,'owner@example.test');
  await assert.rejects(
   createRaw(db,A,planAId,'generated-direct-auth',payloadA,payloadA.title,'','{}',payloadA.references,[]),
   /permission denied/
  ,'ordinary authenticated caller must not execute service-only create RPC');
  await asService(db);
  const planA=normalizePlan(planAId,payloadA),planB=normalizePlan(planBId,payloadB);
  const jobA=await createJob(db,planAId,{title:payloadA.title,references:payloadA.references,plan:planA},'generated-job-a');
  const duplicate=await createJob(db,planAId,{title:payloadA.title,references:payloadA.references,plan:planA},'generated-job-a');
  assert.equal(jobA.id,duplicate.id,'same confirmed plan is idempotent');
  assert.equal((await db.query('select count(*)::int as n from ai_generation_pages where job_id=$1',[jobA.id])).rows[0].n,3);
  const jobB=await createJob(db,planBId,{title:payloadB.title,references:payloadB.references,plan:planB},'generated-job-b');
  assert.notEqual(jobA.id,jobB.id);
  assert.deepEqual((await db.query('select ai_generation_get_settings($1,$2) as value',[W1,A])).rows[0].value.maxWorkers,4);
  // Workspace-global cap applies across all suites and all worker processes.
  await db.query('select ai_generation_set_workers($1,$2,$3)',[W1,A,2]);
  let claimed=await claim(db,'worker-one',10);
  assert.equal(claimed.length,2,'configured workspace global cap must be enforced');
  assert.deepEqual(claimed.map(x=>x.position),[1,2],'first suite page order must be preserved');

  await db.query('select ai_generation_set_workers($1,$2,$3)',[W1,A,4]);
  let more=await claim(db,'worker-two',10);
  assert.equal(more.length,2,'second claim fills the global four slots');
  let zero=await claim(db,'worker-three',10);
  assert.equal(zero.length,0,'no claim may exceed the workspace global four-slot cap');
  const all=[...claimed,...more];
  assert.equal(new Set(all.map(x=>x.pageId)).size,4,'leases are unique across suites');
  assert.ok(claimed.every(x=>x.hasReferenceImage===true),'suite A pages must carry a server-confirmed reference');
  const suiteBPage=more.find(x=>x.jobId===jobB.id);
  assert.equal(suiteBPage.hasReferenceImage,false,'suite B without references must report no reference image');
  const pageA1ForReference=claimed.find(x=>x.jobId===jobA.id&&x.position===1);
  await assert.rejects(
   db.query('select ai_generation_page_reference_path($1,$2,$3,$4)',[W1,A,pageA1ForReference.pageId,'00000000-0000-4000-8000-000000000000']),
   /租约/
  ,'reference read must require the current live lease');
  const selectedReference=(await db.query('select ai_generation_page_reference_path($1,$2,$3,$4) as path',[W1,A,pageA1ForReference.pageId,pageA1ForReference.leaseToken])).rows[0].path;
  assert.equal(selectedReference,referencePath,'page-selected library reference must be returned without a client path');

  // Complete one page, freeing one shared slot for the remaining second-suite page.
  let current=new Map(all.map(x=>[x.pageId,x]));
  const pageA1=all.find(x=>x.jobId===jobA.id&&x.position===1);
  await succeed(db,pageA1,'1'.repeat(64));
  const fifth=await claim(db,'worker-four',1);
  assert.equal(fifth.length,1,'shared slot goes to whichever suite is next in FIFO order');
  assert.equal(fifth[0].jobId,jobB.id);
  current.set(fifth[0].pageId,fifth[0]);
  assert.equal((await claim(db,'worker-five',1)).length,0);

  // Expired lease is reclaimable without increasing the global active count.
  const stolen=all.find(x=>x.jobId===jobA.id&&x.position===2);
  await db.query("update ai_generation_pages set lease_expires_at=now()-interval '1 second' where id=$1",[stolen.pageId]);
  const reclaimed=await claim(db,'worker-six',1);
  assert.equal(reclaimed.length,1);assert.equal(reclaimed[0].pageId,stolen.pageId);assert.ok(reclaimed[0].attempts>stolen.attempts);
  current.set(reclaimed[0].pageId,reclaimed[0]);
  // Finish suite A; publishing is atomic and only happens after every page succeeds.
  for(const row of [...current.values()].filter(x=>x.jobId===jobA.id).sort((a,b)=>a.position-b.position)){
   assert.equal((await db.query("select count(*)::int as n from decks where id='generated-job-a'")).rows[0].n,0);
   if(row.pageId===pageA1.pageId) continue;
   await succeed(db,row,String(row.position).padStart(64,'a'));
  }
  const deck=(await db.query("select source_kind,data from decks where id='generated-job-a'")).rows[0];
  assert.equal(deck.source_kind,'generated');
  assert.equal(deck.data.origin,'generated');
  assert.deepEqual(deck.data.images.map(x=>x.name),['page-0001.png','page-0002.png','page-0003.png']);
  assert.deepEqual(deck.data.generation.images,undefined);
  assert.ok(deck.data.generation.style&&typeof deck.data.generation.style==='object');
  assert.deepEqual(Object.keys(deck.data.generation).sort(),['outline','pages','references','style']);
  assert.ok(deck.data.images.every(x=>x.path.startsWith(`${W1}/generated-job-a/`)));
  // Suite B failure stays page-local; only that failed page can be retried.
  const bPages=[...current.values()].filter(x=>x.jobId===jobB.id).sort((a,b)=>a.position-b.position);
  assert.equal(bPages.length,2);
  await succeed(db,bPages[1],'b'.repeat(64));
  const partial=await fail(db,bPages[0],'上游单页失败');
  assert.equal(partial.status,'partial');
  assert.equal((await db.query("select count(*)::int as n from decks where id='generated-job-b'")).rows[0].n,0);
  await assert.rejects(db.query('select ai_generation_retry_pages($1,$2,$3,$4::jsonb)',[W1,A,jobB.id,JSON.stringify([bPages[1].pageId])]),/仅可重试失败页面/);
  await db.query('select ai_generation_retry_pages($1,$2,$3,$4::jsonb)',[W1,A,jobB.id,JSON.stringify([bPages[0].pageId])]);
  const retry=await claim(db,'worker-seven',1);assert.equal(retry.length,1);assert.equal(retry[0].pageId,bPages[0].pageId);
  assert.equal((await succeed(db,retry[0],'c'.repeat(64))).status,'completed');
  assert.equal((await db.query("select count(*)::int as n from decks where id='generated-job-b'")).rows[0].n,1);

  // Generation history is persisted, owner-scoped, deterministic, and read-only.
  await db.query("update ai_generation_jobs set created_at=$3::timestamptz,updated_at=$3::timestamptz where id in ($1,$2)",[jobA.id,jobB.id,'2026-11-20T10:00:00Z']);
  await db.query(`
   insert into ai_generation_jobs(
    id,workspace_id,user_id,plan_id,deck_id,title,plan_snapshot,status,created_at,updated_at
   )
   select ('90000000-0000-4000-8000-'||lpad(n::text,12,'0'))::uuid,
    $1::uuid,$2::uuid,'history-b-'||n,'history-b-'||n,'成员 B 任务 '||n,
    '{}'::jsonb,'completed',$3::timestamptz,$3::timestamptz
   from generate_series(1,32) as n
  `,[W1,B,'2026-11-20T12:00:00Z']);
  await db.query("insert into ai_generation_jobs(id,workspace_id,user_id,plan_id,deck_id,title,plan_snapshot,status) values('88888888-8888-4888-8888-888888888888',$1,$2,'history-c','history-c','跨工作区任务','{}','completed')",[W2,C]);

  await login(db,A,'owner@example.test');
  const historyA=(await db.query('select ai_generation_recent($1,$2) as value',[W1,A])).rows[0].value;
  assert.deepEqual(Object.keys(historyA),['jobs']);
  assert.equal(historyA.jobs.length,2,'admins still see only their own generation jobs');
  const expectedAIds=[jobA.id,jobB.id].sort().reverse();
  assert.deepEqual(historyA.jobs.map(x=>x.id),expectedAIds,'created_at ties must resolve by id descending');
  assert.deepEqual(historyA.jobs.map(x=>x.pageCount),expectedAIds.map(id=>id===jobA.id?3:2),'pageCount must summarize actual job pages');
  assert.ok(historyA.jobs.every(x=>x.status==='completed'&&x.createdAt==='2026-11-20T10:00:00+00:00'));
  const summaryKeys=['createdAt','id','pageCount','planId','status','title','updatedAt'];
  for(const job of historyA.jobs){
   assert.deepEqual(Object.keys(job).sort(),summaryKeys,'history summaries must expose no private generation details');
   for(const field of ['userId','deckId','outline','planSnapshot','pages','image','storagePath']) assert.equal(Object.prototype.hasOwnProperty.call(job,field),false);
  }

  await login(db,B,'reviewer@example.test');
  const historyB=(await db.query('select ai_generation_recent($1,$2) as value',[W1,B])).rows[0].value;
  assert.equal(historyB.jobs.length,30,'recent history must cap at 30 jobs');
  const expectedBIds=Array.from({length:30},(_,index)=>`90000000-0000-4000-8000-${String(32-index).padStart(12,'0')}`);
  assert.deepEqual(historyB.jobs.map(x=>x.id),expectedBIds,'equal timestamps must use id descending deterministically');
  assert.ok(historyB.jobs.every(x=>x.pageCount===0));

  await login(db,D,'empty@example.test');
  const historyEmpty=(await db.query('select ai_generation_recent($1,$2) as value',[W1,D])).rows[0].value;
  assert.deepEqual(historyEmpty,{jobs:[]},'members without jobs receive an empty list');

  await login(db,A,'owner@example.test');
  await assert.rejects(db.query('select ai_generation_recent($1,$2)',[W1,B]),/身份不匹配/,'actor substitution must be rejected');
  await assert.rejects(db.query('select ai_generation_recent($1,$2)',[W2,A]),/请先加入团队/,'cross-workspace reads must be rejected');

  await resetRole(db);
  const historyAcl=(await db.query("select has_function_privilege('authenticated','public.ai_generation_recent(uuid,uuid)','EXECUTE') as authenticated_ok,has_function_privilege('anon','public.ai_generation_recent(uuid,uuid)','EXECUTE') as anon_ok,has_function_privilege('service_role','public.ai_generation_recent(uuid,uuid)','EXECUTE') as service_ok")).rows[0];
  assert.deepEqual(historyAcl,{authenticated_ok:true,anon_ok:false,service_ok:false});
  await asAnon(db);
  await assert.rejects(db.query('select ai_generation_recent($1,$2)',[W1,A]),/permission denied/,'anonymous callers must not execute generation history');
  await asService(db);
  await assert.rejects(db.query('select ai_generation_recent($1,$2)',[W1,A]),/permission denied/,'service_role execution must remain revoked');

  await asAuthenticated(db);
  await assert.rejects(db.query('select * from ai_generation_jobs'),/permission denied/);
  await assert.rejects(db.query('select * from ai_generation_pages'),/permission denied/);
  await asService(db);
  assert.equal((await db.query('select count(*)::int as n from ai_generation_jobs')).rows[0].n,35);
  console.log('PASS: ordered 01→06 migrations, real confirmed plan, workspace-global multi-process cap, lease takeover, generated deck publication, storage paths, single-page retry, and owner-scoped generation history');
 } finally {await db.close();}
})().catch(error=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});