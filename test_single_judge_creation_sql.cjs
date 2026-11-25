// M30 real SQL/RLS fixture, local PGlite (no Supabase credentials).
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const {spawnSync}=require('node:child_process');

const W='70100000-0000-4000-8000-000000000001';
const W_LEGACY='70200000-0000-4000-8000-000000000001';
const W_OTHER='70300000-0000-4000-8000-000000000001';
const OWNER='70100000-0000-4000-8000-000000000003';
const JUDGE='70100000-0000-4000-8000-000000000004';
const OTHER='70100000-0000-4000-8000-000000000005';
const NONMEMBER='70100000-0000-4000-8000-000000000006';
const LEGACY_OWNER='70200000-0000-4000-8000-000000000003';

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
  '20261122_30_single_judge_creation','20261122_31_single_judge_image_runtime', '20261122_32_single_judge_legacy_backfill',
  '20261122_33_single_judge_review_queue','20261122_34_single_judge_owner_activation',
  '20261122_35_single_judge_intake','20261123_38_cover_training','20261123_39_cover_after_human','20261124_40_cover_progress'
];
const migrations=migrationNames.map(name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));

async function query(db,sql,values=[]){return (await db.query(sql,values)).rows;}
async function resetRole(db){await db.exec('reset role');}
async function asService(db){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub','',false)");await db.exec('set role service_role');}
async function asAuthenticated(db,userId){await resetRole(db);await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[userId]);await db.exec('set role authenticated');}
const ownerActivate=(db,id,owner)=>get(db,'single_judge_activate_owner',[id,owner]);
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
  create table storage.objects(id uuid primary key,name text not null,bucket_id text not null,owner_id text, unique(bucket_id,name));
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
function previewPath(w,deck,position=1){return `${w}/${deck}/preview/${position}.png`;}
function stylePath(w,plan,role='reference',name='style.png'){return `${w}/ai-planning/${plan}/${role}/${name}`;}

async function addUser(db,id){await resetRole(db);await query(db,'insert into auth.users(id) values($1) on conflict do nothing',[id]);}
async function addWorkspace(db,id,name){await resetRole(db);await query(db,'insert into public.workspaces(id,name,owner_email,invite_hash) values($1,$2,$3,$4)',[id,name,`${name}@example.test`,crypto.createHash('sha256').update(name).digest('hex')]);}
async function addMember(db,w,id,role){await resetRole(db);await query(db,'insert into public.members(workspace_id,user_id,name,role) values($1,$2,$3,$4)',[w,id,`member-${id.slice(-4)}`,role]);}
async function addStorageObject(db,path){await resetRole(db);await query(db,"insert into storage.objects(id,name,bucket_id) values($1,$2,'ppt-library')",[uuid(),path]);}
async function addDeck(db,w,id,{kind='uploaded',allowed=true,paths=[],title=id}={}){
 await resetRole(db);
 const images=paths.map((path,index)=>({id:`page-${index+1}`,name:'Page',position:index+1,path,thumbPath:path}));
 await query(db,'insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3,$4,$5)',
  [id,w,JSON.stringify({id,title,images,origin:kind==='generated'?'generated':'import'}),kind,allowed]);
}
const j=JSON.stringify;
const get=(db,fn,args=[])=>query(db,`select public.${fn}(${args.map((_,i)=>`$${i+1}`).join(',')}) as value`,args).then(rows=>rows[0].value);
const rejects=(promise,code)=>assert.rejects(promise,error=>error.code===code);
const save=(db,id,revision,data,w=W)=>get(db,'single_judge_planning_save',[w,id,revision,j(data)]);
const confirm=(db,id,revision,data,w=W)=>get(db,'single_judge_planning_confirm',[w,id,revision,data===null?null:j(data)]);
const input=(db,path,bucket='ppt-library')=>query(db,'insert into storage.objects(id,name,bucket_id) values ($1,$2,$3)',[uuid(),path,bucket]);
const makePath=(w,plan,role,ext)=>stylePath(w,plan,role,`${uuid().replaceAll('-','')}.${ext}`);
const source=(storagePath,role,renderedPagePaths)=>({storagePath,role,name:'source',size:100,mimeType:'application/octet-stream',sha256:'a'.repeat(64),...(renderedPagePaths?{renderedPagePaths}:{})});
const draft=(over={})=>({schemaVersion:1,title:'策划示例',mode:'content_draft',plannerModel:'test',pageCount:1,style:{name:'corporate'},
 pages:[{index:1,title:'Title',copy:[{kind:'body',text:'Content'}]}],references:[],sourceManifest:[],...over});

async function scoredDeck(db,w,kind,allowed,bucket,label,foreignPath){
 let deck; let current;
 await resetRole(db);
 for(let x=0;x<50;x++){
  deck=`import-${uuid()}`;
  current=(await query(db,'select guanye_internal.ai_review_source_group_bucket($1,$2,$3) b',[w,deck,kind]))[0].b;
  if((current===0)===bucket)break;
 }
 assert.equal(current===0,bucket);
 const path=foreignPath||previewPath(w,deck);
 await addDeck(db,w,deck,{kind,allowed,paths:[path],title:label});
 if(!foreignPath)await addStorageObject(db,path);
 const actor=w===W?OWNER:LEGACY_OWNER;
 await db.transaction(async tx=>{
  // Historical fixture rows may predate the formal generated-score gate.
  await tx.exec("select set_config('guanye.ai_review_internal','on',true)");
  await query(tx,'insert into public.reviews(workspace_id,deck_id,user_id,data) values($1,$2,$3,$4)',[w,deck,actor,j({mode:'deck',scores:{design:5}})]);
 });
 return {deck,path};
}

(async()=>{
 const db=await bootstrap();
 try{
  for(const id of [OWNER,JUDGE,OTHER,NONMEMBER,LEGACY_OWNER])await addUser(db,id);
  for(const [w,n] of [[W,'m30-active'],[W_LEGACY,'m30-legacy'],[W_OTHER,'m30-other']]){
   await addWorkspace(db,w,n);
   await query(db,"update public.workspaces set settings='{\"dimensions\":[{\"id\":\"design\"}]}' where id=$1",[w]);
  }
  await addMember(db,W,OWNER,'admin'); await addMember(db,W,JUDGE,'reviewer'); await addMember(db,W,OTHER,'admin');
  await addMember(db,W_LEGACY,LEGACY_OWNER,'admin'); await addMember(db,W_OTHER,LEGACY_OWNER,'reviewer');
  await addMember(db,W_OTHER,OWNER,'admin');
  await asService(db);
  await ownerActivate(db,W,OWNER); await ownerActivate(db,W_OTHER,LEGACY_OWNER);
  const creationFunctions=await query(db,`select p.proname,p.prosecdef,p.proconfig,
   has_function_privilege('authenticated',p.oid,'EXECUTE') as jwt,
   has_function_privilege('anon',p.oid,'EXECUTE') as anon,
   has_function_privilege('service_role',p.oid,'EXECUTE') as service
   from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public'
    and (p.proname like 'single_judge_planning_%' or p.proname='single_judge_generation_create')`);
  assert.equal(creationFunctions.length,8);
  for(const row of creationFunctions){
   assert.ok(row.prosecdef&&row.jwt&&!row.anon&&!row.service,row.proname);
   assert.ok(row.proconfig.some(x=>['search_path=','search_path=""'].includes(x)),row.proname);
  }
  const plan=uuid(), otherPlan=uuid(), legacyPlan=uuid();
  await asService(db);
  await get(db,'ai_planning_save_draft',[W,otherPlan,OTHER,0,j(draft())]);
  await get(db,'ai_planning_save_draft',[W_LEGACY,legacyPlan,LEGACY_OWNER,0,j(draft())]);

  const legal=await scoredDeck(db,W,'uploaded',true,false,'authorized');
  const manual=await scoredDeck(db,W,'uploaded',false,false,'manual');
  const holdout=await scoredDeck(db,W,'uploaded',true,true,'holdout');
  const generated=await scoredDeck(db,W,'generated',true,false,'generated');
  const foreign=await scoredDeck(db,W_OTHER,'uploaded',true,false,'foreign');
  const cross=await scoredDeck(db,W,'uploaded',true,false,'bad path',foreign.path);
  const refPath=makePath(W,plan,'reference','webp');
  const styleDoc=makePath(W,plan,'style','pptx');
  const rendered=makePath(W,plan,'style-rendered','png');
  const content=makePath(W,plan,'content','md');
  const refs=[{deckId:legal.deck,imagePaths:[legal.path],title:'caller supplied, not authoritative'}];
  const initial=draft({references:refs,sourceManifest:[source(content,'content'),source(refPath,'reference'),source(styleDoc,'style',[rendered])],
   generationStyleImages:[refPath,rendered],pages:[{...draft().pages[0],index:9,styleReferenceDeckIds:[legal.deck,manual.deck]}]});

  await asAnon(db);
  await rejects(get(db,'single_judge_planning_references',[W,20]),'42501');
  await rejects(input(db,refPath),'42501');
  await asService(db);
  await rejects(get(db,'single_judge_planning_references',[W,20]),'42501');
  for(const actor of [JUDGE,OTHER,NONMEMBER]){
   await asAuthenticated(db,actor);
   for(const [fn,args] of [
    ['single_judge_planning_references',[W,10]],['single_judge_planning_save',[W,plan,0,j(draft())]],
    ['single_judge_planning_get',[W,otherPlan]],['single_judge_planning_history',[W,null,10]],
    ['single_judge_planning_confirm',[W,otherPlan,1,j(draft())]],['single_judge_planning_confirmed',[W,otherPlan]],
    ['ai_planning_get_plan',[W,otherPlan]],['ai_planning_list_my_plans',[W,null,10]],
    ['ai_generation_get',[W,actor,uuid()]],['ai_generation_get_by_plan',[W,actor,plan]],
    ['ai_generation_recent',[W,actor]],['ai_generation_retry_pages',[W,actor,uuid(),'[]']],
    ['ai_generation_get_settings',[W,actor]],['ai_generation_set_workers',[W,actor,1]]
   ]) await rejects(get(db,fn,args),'42501');
   await rejects(input(db,refPath),'42501');
  }
  await asAuthenticated(db,OWNER);
  for(const target of [W_LEGACY,W_OTHER])await rejects(get(db,'single_judge_planning_references',[target,20]),'42501');
  await rejects(get(db,'single_judge_planning_get',[W,otherPlan]),'42501');
  await rejects(get(db,'ai_planning_grant_version_access',[W,uuid(),OWNER]),'42501');
  await rejects(save(db,otherPlan,1,draft()),'42501');
  await rejects(confirm(db,otherPlan,1,draft()),'42501');
  await rejects(get(db,'single_judge_planning_references',[W,21]),'22023');
  const candidates=await get(db,'single_judge_planning_references',[W,20]);
  assert.deepEqual(candidates.map(x=>x.deckId).sort(),[legal.deck,cross.deck].sort());
  assert.deepEqual(candidates.find(x=>x.deckId===legal.deck).imagePaths,[legal.path]);
  assert.deepEqual(candidates.find(x=>x.deckId===cross.deck).imagePaths,[]);
  for(const path of [makePath(W_OTHER,plan,'reference','png'),makePath(W,otherPlan,'reference','png'),makePath(W,legacyPlan,'reference','png'),
   makePath(W,plan,'style','png'),makePath(W,plan,'style-rendered','webp'),makePath(W,plan,'content','png'),
   makePath(W,plan,'reference','pdf'),makePath(W,plan,'original','pdf'),refPath.replace('/reference/','/reference/../'),
   refPath.replace(plan,'not-a-uuid'),refPath.replace('ai-planning','other'),`${W}/unrelated/file.png`]) await rejects(input(db,path),'42501');
  await rejects(input(db,refPath,'other-bucket'),'42501');
  for(const path of [refPath,styleDoc,rendered,content])await input(db,path);
  await rejects(input(db,refPath),'42501');
  await rejects(query(db,`insert into storage.objects(id,name,bucket_id) values($1,$2,'ppt-library')
   on conflict(bucket_id,name) do update set id=excluded.id`,[uuid(),refPath]),'42501');
  assert.deepEqual(await query(db,'update storage.objects set name=$1 where name=$2 returning id',[refPath+'-changed',refPath]),[]);
  assert.deepEqual(await query(db,'delete from storage.objects where name=$1 returning id',[refPath]),[]);
  for(const deckId of [manual.deck,holdout.deck,generated.deck,foreign.deck])await rejects(save(db,plan,0,draft({references:[{deckId}]})),'22023');
  await rejects(save(db,plan,0,draft({references:[{deckId:legal.deck,imagePaths:[manual.path]}]})),'22023');
  await rejects(save(db,plan,0,draft({sourceManifest:[source(legal.path,'reference')]})),'22023');
  await rejects(save(db,plan,0,draft({sourceManifest:[source(refPath,'content')]})),'22023');
  await rejects(save(db,plan,0,draft({sourceManifest:[source(makePath(W,plan,'reference','webp'),'reference')]})),'22023');
  await rejects(save(db,plan,0,draft({generationStyleImages:[refPath]})),'22023');
  await rejects(save(db,plan,0,{...draft(),actor:OWNER}),'22023');
  const created=await save(db,plan,0,initial);
  assert.equal(created.revision,1);
  assert.equal(created.draft.references[0].title,'authorized');
  assert.deepEqual(created.draft.pages[0].styleReferenceDeckIds,[legal.deck]);
  assert.equal(created.draft.pages[0].index,1);
  await input(db,makePath(W,plan,'reference','jpg'));
  await rejects(get(db,'single_judge_planning_confirmed',[W,plan]),'42501');
  const tamper={...created.draft,title:'Changed',references:[{deckId:foreign.deck}],sourceManifest:[],generationStyleImages:[foreign.path],plannerModel:'pretender'};
  const edited=await save(db,plan,1,tamper);
  assert.equal(edited.revision,2);
  for(const key of ['references','sourceManifest','generationStyleImages','plannerModel'])assert.deepEqual(edited.draft[key],created.draft[key]);
  await rejects(save(db,plan,1,initial),'22023');
  const newer=uuid(); await save(db,newer,0,draft());
  await rejects(save(db,plan,2,draft({mode:'template_sample'})),'22023');
  const history=await get(db,'single_judge_planning_history',[W,null,1]);
  assert.equal(history.items[0].planId,newer); assert.equal(history.nextCursor,newer);
  const second=await get(db,'single_judge_planning_history',[W,history.nextCursor,1]);
  assert.equal(second.items[0].planId,plan); assert.equal(second.nextCursor,null);
  await assert.rejects(get(db,'single_judge_planning_history',[W,otherPlan,10]),/游标/);
  await rejects(get(db,'single_judge_planning_get',[W_OTHER,plan]),'42501');

  await rejects(confirm(db,plan,1,tamper),'22023');
  const confirmed=await confirm(db,plan,2,tamper);
  assert.equal(confirmed.immutable,true);
  const fetched=await get(db,'single_judge_planning_get',[W,plan]);
  assert.equal(fetched.createdBy,OWNER); assert.equal(fetched.status,'confirmed');
  assert.equal(fetched.confirmed.payload.title,'Changed');
  assert.equal(fetched.confirmed.payload.references[0].deckId,legal.deck);
  const snapshot=await get(db,'single_judge_planning_confirmed',[W,plan]);
  assert.equal(snapshot.versionId,confirmed.versionId); assert.equal(snapshot.confirmedBy,OWNER);
  assert.deepEqual(snapshot.generationStyleImages,[refPath,rendered]);
  await rejects(save(db,plan,2,initial),'22023');
  await rejects(confirm(db,plan,2,null),'22023');
  await rejects(input(db,makePath(W,plan,'reference','png')),'42501');
  await resetRole(db);
  await assert.rejects(query(db,"update public.ai_planning_versions set payload='{}' where id=$1",[confirmed.versionId]),/不可修改/);

  const deckId=`generated-${uuid().replaceAll('-','').slice(0,24)}`;
  const page={planPageId:'page-0001',position:1,title:'Title',references:[legal.deck],contentType:'image/png',
   outline:{index:1,title:'Title',styleReferenceDeckIds:[legal.deck]},description:{text:'Content'},prompt:'Content'};
  const create=(over={})=>get(db,'single_judge_generation_create',[
   over.w||W,over.plan||plan,over.deck||deckId,j(over.snapshot||snapshot),snapshot.title,'corporate',
   j(over.outline||{title:snapshot.title,sourceManifest:snapshot.sourceManifest,pages:[page.outline]}),
   j(over.references||snapshot.references),j(over.pages||[page])]);
  for(const actor of [JUDGE,OTHER,NONMEMBER]){
   await asAuthenticated(db,actor); await rejects(create(),'42501');
  }
  await asAuthenticated(db,OWNER);
  await rejects(create({plan:newer}),'42501');
  await rejects(create({w:W_OTHER}),'42501');
  await rejects(create({deck:'generated-../../other'}),'22023');
  await rejects(create({snapshot:{...snapshot,title:'unconfirmed'}}),'22023');
  await rejects(create({references:[{...snapshot.references[0],imagePaths:[foreign.path]}]}),'22023');
  await rejects(create({pages:[{...page,references:[manual.deck]}]}),'22023');
  await rejects(create({outline:{sourceManifest:[source(foreign.path,'reference')]}}),'22023');
  await rejects(create({outline:{pages:[{path:foreign.path}]}}),'22023');
  await rejects(create({pages:[{...page,description:{text:'Content',path:foreign.path}}]}),'22023');
  // Exercise the actual loopback generation handler's output against the SQL RPC,
  // not just a manually constructed payload with the right keys.
  const bridge=spawnSync(process.env.PYTHON||process.env.PYTHON_EXECUTABLE||'python',['-B','-c',`
import json,sys
from cloud.professional_creation import ProfessionalCreation
from cloud.ai_gateway import HTTPResponse
source=json.load(sys.stdin)
snapshot=source['snapshot']
def transport(method,url,headers,body,timeout):
    if url.endswith('/single_judge_planning_get'):
        data=source['authorized']
    elif url.endswith('/single_judge_planning_confirmed'):
        data=snapshot
    elif url.endswith('/single_judge_generation_create'):
        print(body.decode('ascii'))
        data={'id':'fixture','pages':[]}
    else:
        raise AssertionError(url)
    return HTTPResponse(200,{},json.dumps(data).encode())
service=ProfessionalCreation({'url':'https://example.test','workspaceId':source['workspace'],'anonKey':'fixture'},'fixture',transport=transport)
assert service.handle('POST','/api/ai/generation',source['judge'],'fixture',{'plan_id':snapshot['planId']},{})[0]==202
`],{input:JSON.stringify({authorized:fetched,snapshot,workspace:W,judge:OWNER}),encoding:'utf8'});
  assert.equal(bridge.status,0,String(bridge.stderr||bridge.error));
  const wired=JSON.parse(bridge.stdout);
  assert.deepEqual(wired.p_plan,snapshot);
  assert.deepEqual(wired.p_references,snapshot.references);
  const wiredCreate=()=>get(db,'single_judge_generation_create',[
   wired.w,wired.p_plan_id,wired.p_deck_id,j(wired.p_plan),wired.p_title,wired.p_style,j(wired.p_outline),j(wired.p_references),j(wired.p_pages),
  ]);
  const job=await wiredCreate(); const duplicate=await wiredCreate();
  assert.equal(job.id,duplicate.id); assert.equal(job.status,'queued'); assert.equal(job.pages.length,1);
  assert.equal(job.userId,OWNER); assert.deepEqual(job.config,{workers:16});
  assert.deepEqual(job.references,snapshot.references);
  assert.equal((await get(db,'ai_generation_get',[W,OWNER,job.id])).id,job.id);
  assert.equal((await get(db,'ai_generation_get_by_plan',[W,OWNER,plan])).id,job.id);
  assert.equal((await get(db,'ai_generation_recent',[W,OWNER])).jobs[0].id,job.id);
  await asAuthenticated(db,OTHER);
  await rejects(get(db,'ai_generation_get',[W,OTHER,job.id]),'42501');
  await rejects(get(db,'ai_generation_create',[W,JUDGE,plan,deckId,j(snapshot),snapshot.title,'style','{}','[]','[]','{}']),'42501');
  await resetRole(db);
  assert.equal((await query(db,'select public.ai_generation_select_reference_path($1,$2,$3) as path',[W,job.id,job.pages[0].id]))[0].path,refPath);
  await query(db,"update public.ai_generation_pages set status='failed' where id=$1",[job.pages[0].id]);
  await asAuthenticated(db,OTHER);
  await rejects(get(db,'ai_generation_retry_pages',[W,OTHER,job.id,j([job.pages[0].id])]),'42501');
  await asAuthenticated(db,OWNER);
  assert.equal((await get(db,'ai_generation_retry_pages',[W,OWNER,job.id,j([job.pages[0].id])])).pages[0].status,'queued');
  await asAuthenticated(db,LEGACY_OWNER);
  assert.equal((await get(db,'ai_planning_get_plan',[W_LEGACY,legacyPlan])).planId,legacyPlan);
  assert.equal((await get(db,'ai_planning_list_my_plans',[W_LEGACY,null,10])).items[0].planId,legacyPlan);
  assert.equal((await get(db,'ai_generation_set_workers',[W_LEGACY,LEGACY_OWNER,7])).maxWorkers,7);
  await input(db,`${W_LEGACY}/old-path/legacy.png`);
  await resetRole(db);
  await rejects(query(db,'delete from public.members where workspace_id=$1 and user_id=$2',[W,OWNER]),'23503');
  await query(db,"update public.members set role='reviewer' where workspace_id=$1 and user_id=$2",[W,OWNER]);
  await asAuthenticated(db,OWNER);
  assert.equal((await get(db,'single_judge_planning_get',[W,plan])).createdBy,OWNER);
  assert.equal((await wiredCreate()).id,job.id,'reviewer owner can still enter generation creation');
  await resetRole(db);
  await query(db,"update public.members set role='admin' where workspace_id=$1 and user_id=$2",[W,OWNER]);
  await asAuthenticated(db,OTHER);
  await rejects(input(db,makePath(W,uuid(),'content','pdf')),'42501');
  // M38 uses the same deployed sequence and real PostgreSQL roles/queue.
  await asAuthenticated(db,OWNER);
  await rejects(get(db,'single_judge_cover_inventory',[W,'',100]),'42501');
  await resetRole(db);
  await query(db,'insert into guanye_internal.cover_training_settings(workspace_id,enabled) values($1,true)',[W]);
  await query(db,"update public.workspaces set settings=$2 where id=$1",[W,j({maxScore:5,dimensions:[{id:'visual'},{id:'layout'}]})]);
  // Avoid unrelated jobs taking the sole slot in this pilot fixture.
  await query(db,"update public.ai_generation_jobs set status='failed' where workspace_id=$1",[W]);
  const newItem=async (label,sha,withPage=true)=>{
   const deck='cover-'+label, path=previewPath(W,deck);
   await resetRole(db);
   await addDeck(db,W,deck,{kind:'uploaded',allowed:true,paths:[path],title:label});
   await addStorageObject(db,path);
   await query(db,'insert into guanye_internal.uploaded_source_provenance(deck_id,workspace_id,source_group_sha256) values($1,$2,$3)',[deck,W,sha]);
   const [v]=await query(db,'select public.ai_review_deck_version($1,$2) as dv,public.ai_review_rubric_version($1) as rv',[W,deck]);
   await asAuthenticated(db,OWNER);
   await get(db,'single_judge_submit_bundle',[W,j({deckId:deck,deckVersion:v.dv,rubricVersion:v.rv,units:[{scope:'deck',scores:{visual:4,layout:4}},...(withPage?[{scope:'page',unitId:'page-1',scores:{visual:4,layout:4}}]:[])]})]);
   return {deck,path};
  };
  const held=await newItem('held','00'+'1'.repeat(62));
  const heldCopy=await newItem('held-copy','00'+'1'.repeat(62));
  const revisionSha='01'+'9'.repeat(62), ambiguousSha='01'+'8'.repeat(62);
  const revisedSource=await newItem('held-revised',revisionSha);
  const ambiguous=await newItem('uncertain-source',ambiguousSha);
  const calibration=await newItem('calibration','01'+'2'.repeat(62));
  const extra=await newItem('second-cal','02'+'3'.repeat(62));
  const deckOnly=await newItem('deck-only','03'+'3'.repeat(62),false);
  await resetRole(db);
  await query(db,'insert into guanye_internal.cover_training_source_review values($1,$2,$3),($1,$4,null)',[W,revisionSha,'00'+'1'.repeat(62),ambiguousSha]);
  await asAuthenticated(db,OTHER);
  await rejects(get(db,'single_judge_cover_inventory',[W,'',100]),'42501');
  await asAuthenticated(db,OWNER);
  await rejects(get(db,'single_judge_cover_inventory',[W_OTHER,'',100]),'42501');
  const inventory=await get(db,'single_judge_cover_inventory',[W,'',100]);
  assert.ok(inventory.rows.find(x=>x.id===held.deck).eligible);
  assert.equal(inventory.rows.find(x=>x.id===revisedSource.deck).sourceGroupSha256,'00'+'1'.repeat(62));
  assert.equal(inventory.rows.find(x=>x.id===ambiguous.deck).exclusion,'sourceReviewExcluded');
  assert.equal(inventory.rows.find(x=>x.id===deckOnly.deck).eligible,false);
  assert.equal(await get(db,'single_judge_cover_publish_style',[W,'# 团队封面\n保持留白',null]),1);
  assert.equal(await get(db,'single_judge_cover_publish_style',[W,'# 新版本',null]),2);
  assert.equal(await get(db,'single_judge_cover_publish_style',[W,null,1]),1);
  const readContext=deck=>get(db,'single_judge_cover_pair_context',[W,deck]);
  const heldContext=await readContext(held.deck);
  assert.deepEqual(heldContext.cases.map(x=>x.id).sort(),[calibration.deck,extra.deck].sort());
  assert.ok(!heldContext.cases.some(x=>x.id===heldCopy.deck || x.id===revisedSource.deck || x.id===ambiguous.deck));
  const recordPair=ctx=>get(db,'single_judge_cover_pair_save',[W,ctx.id,j(ctx),'gpt-6.2-sol:cover-rubric-v1',j({baseline:{visual:5,layout:5},assisted:{visual:5,layout:5}})]);
  const heldPair=await recordPair(heldContext);
  await resetRole(db);
  await rejects(query(db,'delete from guanye_internal.cover_training_source_review where workspace_id=$1 and original_sha256=$2',[W,revisionSha]),'22023');
  await asAuthenticated(db,OWNER);
  await rejects(get(db,'single_judge_cover_correct',[W,heldPair,'禁止留出组回流']),'22023');
  const trainPair=await recordPair(await readContext(calibration.deck));
  const correction=await get(db,'single_judge_cover_correct',[W,trainPair,'标注留白不足']);
  assert.equal((await readContext(held.deck)).cases.find(x=>x.id===calibration.deck).correctionId,correction);

  const coverPlan=uuid();
  const coverRefs=[{deckId:calibration.deck,imagePaths:[calibration.path]}];
  const coverDraft=draft({references:coverRefs,pages:[{...draft().pages[0],narrativeRole:'cover',styleReferenceDeckIds:[calibration.deck]}]});
  await get(db,'single_judge_cover_save_plan',[W,coverPlan,j(coverDraft),1,'[]']);
  await confirm(db,coverPlan,1,null);
  const coverSnapshot=await get(db,'single_judge_planning_confirmed',[W,coverPlan]);
  const coverPage={...page,references:[calibration.deck],outline:{...page.outline,styleReferenceDeckIds:[calibration.deck],narrativeRole:'cover'}};
  const createCover=(path=calibration.path)=>get(db,'single_judge_cover_start',[W,coverPlan,'generated-f1f1f1f1f1f1f1f1f1f1f1f1',j(coverSnapshot),coverSnapshot.title,'style','{}',j(coverSnapshot.references),j([coverPage]),calibration.deck,path]);
  await rejects(createCover(manual.path),'22023');
  const rootJob=await createCover();
  const rootPage=rootJob.pages[0].id;
  assert.equal((await createCover()).id,rootJob.id);
  await asService(db);
  assert.deepEqual(await get(db,'ai_generation_claim_pages',[W,OWNER,'legacy worker',1,300]),[]);
  await resetRole(db);
  const [busyJob]=await query(db,`insert into public.ai_generation_jobs(workspace_id,user_id,plan_id,deck_id,plan_snapshot,status)
   values($1,$2,'capacity-fixture','cover-capacity-fixture','{}','running') returning id`,[W_OTHER,LEGACY_OWNER]);
  await query(db,`with busy_pages as (
   insert into public.ai_generation_pages(job_id,workspace_id,position,storage_path,status,lease_expires_at)
   select $1,$2,i,'capacity-'||i,'running',now()+interval '5 minutes' from generate_series(1,20) as i returning id
  ) insert into guanye_internal.cover_training_candidates(page_id,workspace_id,root_page_id,ordinal,reference_deck_id,reference_path)
   select id,$2,id,0,'fixture','fixture' from busy_pages`,[busyJob.id,W_OTHER]);
  await asService(db);
  assert.deepEqual(await get(db,'single_judge_cover_claim_pages',[W,OWNER,'cover worker',1,300]),[],
   'active training leases in another workspace share the project-wide channel cap');
  await resetRole(db);
  await query(db,'delete from public.ai_generation_jobs where id=$1',[busyJob.id]);
  await asService(db);
  const [leased]=await get(db,'single_judge_cover_claim_pages',[W,OWNER,'cover worker',1,300]);
  assert.equal(leased.pageId,rootPage); assert.equal(leased.coverTraining,true); assert.equal(leased.attempts,1);
  assert.equal(await get(db,'ai_generation_page_reference_path',[W,OWNER,rootPage,leased.leaseToken]),calibration.path);
  await resetRole(db);
  await query(db,'update public.decks set ai_allowed=false where workspace_id=$1 and id=$2',[W,calibration.deck]);
  await asService(db);
  assert.equal(await get(db,'ai_generation_page_reference_path',[W,OWNER,rootPage,leased.leaseToken]),null);
  await resetRole(db);
  await query(db,'update public.decks set ai_allowed=true where workspace_id=$1 and id=$2',[W,calibration.deck]);

  const finishCandidate=async (lease)=>{
   await resetRole(db);
   await addStorageObject(db,lease.storagePath);
   await asService(db);
   return get(db,'ai_generation_page_succeed',[W,OWNER,lease.pageId,lease.leaseToken,lease.storagePath,'image/png','a'.repeat(64),1024,576]);
  };
  await finishCandidate(leased);
  const status=()=>get(db,'single_judge_cover_status',[W,rootJob.id]);
  await asAuthenticated(db,OWNER);
  assert.equal((await status()).candidates[0].feedback,null,'AI results hidden before independent human score');
  const args={keep:'保留层级',change:'扩大正文留白',doNotChange:'逐字文案'};
  const revise=(pageId,record,key)=>get(db,'single_judge_cover_revise',[W,pageId,record,key,j(args)]);
  await rejects(revise(rootPage,uuid(),uuid()),'42501');
  const scoreCandidate=async (lease,scores)=>{
   const {pageId,deckId}=lease;
   await resetRole(db);
   const [v]=await query(db,'select public.ai_review_deck_version($1,$2) as dv,public.ai_review_rubric_version($1) as rv',[W,deckId]);
   const nodes=()=>query(db,`select id,status,attempts from public.ai_review_jobs
    where workspace_id=$1 and deck_id=$2 and scope='page' and unit_id=$3`,[W,deckId,pageId]);
   assert.deepEqual(await nodes(),[]);
   await asAuthenticated(db,OWNER);
   const submit=values=>get(db,'single_judge_submit_bundle',[W,j({deckId,deckVersion:v.dv,rubricVersion:v.rv,units:[
    {scope:'deck',scores:values},{scope:'page',unitId:pageId,scores:values},
   ]})]);
   const bundle=await submit(scores);
   const waiting=(await status()).candidates.find(x=>x.pageId===pageId).feedback;
   assert.deepEqual(waiting.human,scores); assert.equal(waiting.recordId,null); assert.ok(waiting.decisionId);
   await resetRole(db);
   const jobs=await nodes(); assert.equal(jobs.length,1); assert.equal(jobs[0].status,'queued');
   await asService(db);
   assert.deepEqual(await get(db,'single_judge_cover_review_claim',[W,OWNER,'uploaded',1]),[]);
   const [job]=await get(db,'single_judge_cover_review_claim',[W,OWNER,'generated',1]);
   assert.equal(job.id,jobs[0].id); assert.equal(job.attempts,1);
   const done=await get(db,'ai_review_finish_attempt',[W,OWNER,job.id,1,'completed',j({scores:{visual:4,layout:3,match:5},reasons:['需要更清晰的层级']}),null,false]);
   assert.equal(done.matchedSubmissions,1); assert.ok(done.recordId);
   await asAuthenticated(db,OWNER);
   const feedback=(await status()).candidates.find(x=>x.pageId===pageId).feedback;
   assert.equal(feedback.recordId,done.recordId); assert.deepEqual(feedback.human,scores); assert.ok(feedback.decisionId);
   if(pageId===rootPage){
    const corrected={...scores,layout:scores.layout+1};
    await submit(corrected);
    const fixed=(await status()).candidates.find(x=>x.pageId===pageId).feedback;
    assert.equal(fixed.recordId,feedback.recordId); assert.deepEqual(fixed.human,scores);
    assert.deepEqual(fixed.formalScores,corrected); assert.notEqual(fixed.decisionId,feedback.decisionId);
    await resetRole(db); assert.equal((await nodes()).length,1); await asAuthenticated(db,OWNER);
   }
   return {...feedback, submissionId:bundle.submissionIds[1]};
  };
  const rootFeedback=await scoreCandidate(leased,{visual:4,layout:3,match:5});
  await rejects(get(db,'single_judge_admit',[W,rootFeedback.submissionId,rootFeedback.decisionId]),'42501');
  const key=uuid();
  const revised=await revise(rootPage,rootFeedback.recordId,key);
  assert.equal(revised.candidates.length,2);
  assert.equal((await revise(rootPage,rootFeedback.recordId,key)).candidates[1].pageId,revised.candidates[1].pageId);
  await rejects(revise(rootPage,rootFeedback.recordId,uuid()),'22023');
  assert.equal(revised.candidates[0].status,'succeeded');
  await asService(db);
  const [variant]=await get(db,'single_judge_cover_claim_pages',[W,OWNER,'cover worker',1,300]);
  assert.equal(variant.pageId,revised.candidates[1].pageId);
  assert.ok(variant.prompt.startsWith(leased.prompt)); assert.match(variant.prompt,/扩大正文留白/);
  assert.equal(await get(db,'ai_generation_page_reference_path',[W,OWNER,variant.pageId,variant.leaseToken]),calibration.path);
  await finishCandidate(variant);
  const after=await scoreCandidate(variant,{visual:5,layout:5,match:5});
  const selected=await get(db,'single_judge_cover_select',[W,variant.pageId,after.decisionId,'使用更大的留白',true]);
  assert.equal(selected.selectedPageId,variant.pageId);
  const admitted=await get(db,'single_judge_admit',[W,after.submissionId,after.decisionId]);
  assert.equal(admitted.admitted,true);
  assert.equal((await get(db,'single_judge_admit',[W,after.submissionId,after.decisionId])).entryId,admitted.entryId);
  assert.equal(selected.candidates[1].feedback.libraryVisible,false);
  const savedProgress=await status();
  assert.equal(savedProgress.candidates[0].feedback.libraryVisible,false);
  assert.equal(savedProgress.candidates[1].feedback.libraryVisible,true);
  const planMemory=await get(db,'single_judge_cover_style',[W]);
  assert.equal(planMemory.examples[0].reason,'使用更大的留白');
  const currentRoot=(await status()).candidates[0].feedback;
  assert.equal((await get(db,'single_judge_cover_select',[W,rootPage,currentRoot.decisionId,'回看首稿',false])).selectedPageId,rootPage);
  assert.equal((await get(db,'single_judge_cover_select',[W,variant.pageId,after.decisionId,'使用更大的留白',true])).selectedPageId,variant.pageId);
  assert.equal((await revise(variant.pageId,after.recordId,uuid())).candidates.length,3);
  await asService(db);
  const [last]=await get(db,'single_judge_cover_claim_pages',[W,OWNER,'cover worker',1,300]);
  await finishCandidate(last);
  const lastFeedback=await scoreCandidate(last,{visual:5,layout:5,match:2});
  await rejects(revise(last.pageId,lastFeedback.recordId,uuid()),'22023');
  await rejects(get(db,'single_judge_cover_select',[W,last.pageId,lastFeedback.decisionId,'文案错漏',false]),'22023');
  await rejects(get(db,'ai_generation_retry_pages',[W,OWNER,last.jobId,j([last.pageId])]),'42501');
  const final=await status();
  assert.equal(final.candidates[0].image.path,leased.storagePath);
  assert.equal(final.selectedPageId,variant.pageId);
  console.log('PASS M38: pilot opt-in, inventory, group isolation, corrections, style rollback, first generation, score gate, two revisions, selection, preserved first draft');
  console.log('PASS M30: admin/reviewer owner parity, non-owner guards, legacy path, insert-only RLS, M24/M29 references and generation queue');

  // Capture ordinary ranking before M43; the refactor must preserve it byte for byte.
  await resetRole(db);
  const WX='70400000-0000-4000-8000-000000000001';
  await addWorkspace(db,WX,'m43-exact');
  await addMember(db,WX,OWNER,'admin'); await addMember(db,WX,OTHER,'admin');
  await query(db,'update public.workspaces set settings=$2 where id=$1',[WX,j({maxScore:5,dimensions:[{id:'visual'},{id:'layout'}]})]);
  await asService(db);
  await ownerActivate(db,WX,OWNER);
  await resetRole(db);
  await query(db,'insert into guanye_internal.cover_training_settings(workspace_id,enabled) values($1,true)',[WX]);
  const addTrainingFixture = async (id, {allowed=true, score=5, prefix='01', pages=1}={}) => {
   const paths=Array.from({length:pages},(_,i)=>previewPath(WX,id,i+1));
   await addDeck(db,WX,id,{allowed,paths});
   for(const path of paths) await addStorageObject(db,path);
   const sha=prefix+crypto.createHash('sha256').update(id).digest('hex').slice(2);
   await query(db,'insert into guanye_internal.uploaded_source_provenance(deck_id,workspace_id,source_group_sha256) values($1,$2,$3)',[id,WX,sha]);
   if(score!==null) await db.transaction(async tx=>{
    await tx.exec("select set_config('guanye.ai_review_internal','on',true)");
    await query(tx,'insert into public.reviews(workspace_id,deck_id,user_id,data) values($1,$2,$3,$4)',[WX,id,OWNER,j({mode:'deck',scores:{visual:score,layout:score}})]);
   });
   return {paths,sha};
  };
  for(let i=0;i<20;i++) await addTrainingFixture(`training-top-${i}`);
  const long=await addTrainingFixture('training-long',{score:4,pages:22});
  await addTrainingFixture('training-held',{prefix:'00',pages:2});
  await addTrainingFixture('training-manual',{allowed:false,pages:2});
  await addTrainingFixture('training-unscored',{score:null,pages:2});
  const alias=await addTrainingFixture('training-alias',{pages:2});
  await query(db,'insert into guanye_internal.cover_training_source_review values($1,$2,$3)',[WX,alias.sha,'00'+'f'.repeat(62)]);
  const ranked=await get(db,'ai_planning_reference_candidates',[WX,20]);
  assert.equal(ranked.length,20);
  assert.ok(!ranked.some(row=>row.deckId==='training-long'));
  await db.exec(fs.readFileSync('cloud/migrations/20261126_43_ppt_training_exact_reference.sql','utf8'));
  assert.deepEqual(await get(db,'ai_planning_reference_candidates',[WX,20]),ranked);
  const exact=(id,w=WX)=>get(db,'single_judge_ppt_training_reference',[w,id]);
  await asAnon(db); await rejects(exact('training-long'),'42501');
  await asAuthenticated(db,OTHER); await rejects(exact('training-long'),'42501');
  await asAuthenticated(db,OWNER); await rejects(exact('training-long',W_OTHER),'42501');
  for(const id of ['training-held','training-manual','training-unscored','training-alias','training-top-0','does-not-exist']) assert.equal(await exact(id),null,id);
  const longRef=await exact('training-long');
  assert.equal(longRef.pageCount,22);
  assert.equal(longRef.split,'calibration');
  assert.deepEqual(longRef.authorizedPagePaths,long.paths);
  await resetRole(db);
  await query(db,"delete from storage.objects where bucket_id='ppt-library' and name=$1",[long.paths[11]]);
  await asAuthenticated(db,OWNER);
  const partial=await exact('training-long');
  assert.deepEqual(partial.authorizedPagePaths,long.paths.map((p,i)=>i===11?null:p));
  console.log('PASS M43: exact 22-slide reference outside top 20, unchanged ranking, held-out family, missing page, scores and authorization');
 } finally{await db.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
