// Real PGlite fixture for M29 single-judge generation reference authorization.
// Legacy schema -> M01..M29, then direct SQL claims and reference reads.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');

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
 '20261122_28_single_judge_admission','20261122_29_single_judge_generation_references'
];
const migrations=migrationNames.map(name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));

async function query(db,sql,values=[]){return (await db.query(sql,values)).rows;}
async function resetRole(db){await db.exec('reset role');}
async function asService(db){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub','',false)");await db.exec('set role service_role');}
async function asAuthenticated(db,userId){await resetRole(db);await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[userId]);await db.exec('set role authenticated');}
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
 return db;
}

function uuid(){return crypto.randomUUID();}
function previewPath(w,deck,position=1){return `${w}/${deck}/preview/${position}.png`;}
function legacyImagePath(w,deck,extension='webp'){return `${w}/${deck}/${crypto.randomUUID()}.${extension}`;}
function stylePath(w,plan,role='reference',name='style.png'){return `${w}/ai-planning/${plan}/${role}/${name}`;}

async function addUser(db,id){await resetRole(db);await query(db,'insert into auth.users(id) values($1) on conflict do nothing',[id]);}
async function addWorkspace(db,id,name){await resetRole(db);await query(db,'insert into public.workspaces(id,name,owner_email,invite_hash) values($1,$2,$3,$4)',[id,name,`${name}@example.test`,crypto.createHash('sha256').update(name).digest('hex')]);}
async function addMember(db,w,id,role){await resetRole(db);await query(db,'insert into public.members(workspace_id,user_id,name,role) values($1,$2,$3,$4)',[w,id,`member-${id.slice(-4)}`,role]);}
async function addStorageObject(db,path){await resetRole(db);await query(db,"insert into storage.objects(id,name,bucket_id) values($1,$2,'ppt-library')",[uuid(),path]);}
async function addDeck(db,w,id,{kind='uploaded',allowed=true,paths=[],title=id}={}){
 await resetRole(db);
 const images=paths.map((image,index)=>{
  if(typeof image==='string')return {id:`page-${index+1}`,name:`第 ${index+1} 页.png`,position:index+1,path:image,thumbPath:image};
  return {id:image.id||`page-${index+1}`,name:image.name||`第 ${index+1} 页.png`,position:image.position||index+1,...image};
 });
 const data={id,title,name:title,origin:kind==='generated'?'generated':'import',images};
 if(kind==='generated')data.generation={references:[]};
 await query(db,'insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3::jsonb,$4,$5)',
  [id,w,JSON.stringify(data),kind,allowed]);
}
async function addJobPage(db,{
 jobId=uuid(),pageId=uuid(),w=W,userId=OWNER,planId=uuid(),deckId=`generated-${uuid().replace(/-/g,'').slice(0,24)}`,
 planSnapshot={},referenceData=[],styleDeckIds=[],jobStatus='queued',pageStatus='queued',
 leaseToken=null,leaseExpiresAt=null
}={}){
 await resetRole(db);
 await query(db,`insert into public.ai_generation_jobs(
  id,workspace_id,user_id,plan_id,deck_id,title,outline,reference_data,plan_snapshot,status
 ) values($1,$2,$3,$4,$5,'M29 fixture','{}'::jsonb,$6::jsonb,$7::jsonb,$8)`,
 [jobId,w,userId,planId,deckId,JSON.stringify(referenceData),JSON.stringify(planSnapshot),jobStatus]);
 await query(db,`insert into public.ai_generation_pages(
  id,job_id,workspace_id,position,plan_page_id,title,outline,description,prompt,reference_data,status,storage_path,lease_token,lease_expires_at
 ) values($1,$2,$3,1,'page-1','M29 page',$4::jsonb,'{}'::jsonb,'',$5::jsonb,$6,$7,$8,$9)`,
 [pageId,jobId,w,JSON.stringify({title:'M29 page',styleReferenceDeckIds:styleDeckIds}),JSON.stringify(referenceData),pageStatus,`${w}/${deckId}/generated/${pageId}.png`,leaseToken,leaseExpiresAt]);
 return {jobId,pageId,w,userId,planId,deckId};
}
async function planPayload(styleImages){
 return {
  schemaVersion:1,title:'M29 plan',mode:'content_draft',pageCount:1,
  style:{name:'M29 style'},pages:[{index:1,title:'P1',copy:[],layout:'',visualBrief:'',contentSources:[],styleReferenceDeckIds:[]}],
  references:[],generationStyleImages:styleImages,templateSample:null,generationBrief:'',
  assumptions:[],sourceManifest:[]
 };
}
async function addPlanVersion(db,{w=W,planId=uuid(),versionId=uuid(),owner=OWNER,styleImages=[],status='confirmed',version=1,confirmedBy=null}={}){
 await resetRole(db);
 const payload=await planPayload(styleImages);
 await query(db,`insert into public.ai_planning_plans(
  id,workspace_id,created_by,status,revision,draft,confirmed_at
 ) values($1,$2,$3,$4,1,$5::jsonb,case when $4='confirmed' then now() else null end)`,
 [planId,w,owner,status,JSON.stringify(payload)]);
 await query(db,`insert into public.ai_planning_versions(
  id,plan_id,workspace_id,version,confirmed_by,payload,content_hash
 ) values($1,$2,$3,$4,$5,$6::jsonb,$7)`,
 [versionId,planId,w,version,confirmedBy||owner,JSON.stringify(payload),crypto.createHash('sha256').update(`${planId}:${version}`).digest('hex')]);
 await query(db,'update public.ai_planning_plans set current_version_id=$2 where id=$1 and workspace_id=$3',[planId,versionId,w]);
 return payload;
}
async function currentPlan(db,planId){return (await query(db,'select public.ai_planning_read_confirmed_plan($1) as value',[planId]))[0].value;}
async function selectPath(db,w,jobId,pageId){
 return (await query(db,'select public.ai_generation_select_reference_path($1,$2,$3) as path',[w,jobId,pageId]))[0].path;
}
async function rejectsCode(promise,code){await assert.rejects(promise,error=>String(error.code||'')===code);}

(async()=>{
 let db;
 try{
  db=await bootstrap();
  for(const id of [OWNER,JUDGE,OTHER,NONMEMBER,LEGACY_OWNER])await addUser(db,id);
  await addWorkspace(db,W,'m29-active');
  await addWorkspace(db,W_LEGACY,'m29-legacy');
  await addWorkspace(db,W_OTHER,'m29-other');
  await addMember(db,W,OWNER,'admin');
  await addMember(db,W,JUDGE,'reviewer');
  await addMember(db,W,OTHER,'reviewer');
  await addMember(db,W_LEGACY,LEGACY_OWNER,'admin');
  await resetRole(db);
  await db.query('select public.single_judge_activate($1,$2,$3)',[W,JUDGE,OWNER]);

  const legalDeck='import-71000000-0000-4000-8000-000000000001';
  const manualDeck='import-71000000-0000-4000-8000-000000000002';
  const generatedDeck='generated-71000000-0000-4000-8000-000000000003';
  const wrongDeck='import-71000000-0000-4000-8000-000000000004';
  const conflictDeck='import-71000000-0000-4000-8000-000000000005';
  const noStorageDeck='import-71000000-0000-4000-8000-000000000006';
  const legacyDeck='import-71000000-0000-4000-8000-000000000007';
  const directLegacyDeck='import-71000000-0000-4000-8000-000000000008';
  const imagePathDeck='import-71000000-0000-4000-8000-000000000009';
  const storagePathDeck='import-71000000-0000-4000-8000-000000000010';
  const previewJpgDeck='import-71000000-0000-4000-8000-000000000011';
  const excludedPathDeck='import-71000000-0000-4000-8000-000000000012';

  const legalPreview=previewPath(W,legalDeck,1);
  const manualPreview=previewPath(W,manualDeck,1);
  const generatedPreview=previewPath(W,generatedDeck,1);
  const wrongPreview=previewPath(W,wrongDeck,1);
  const noStoragePreview=previewPath(W,noStorageDeck,1);
  const legacyPreview=previewPath(W_LEGACY,legacyDeck,1);
  const directLegacyImage=legacyImagePath(W,directLegacyDeck,'webp');
  const imagePathAlias=legacyImagePath(W,imagePathDeck,'jpg');
  const storagePathAlias=legacyImagePath(W,storagePathDeck,'png');
  const legacyPreviewJpg=`${W}/${previewJpgDeck}/preview/legacy.jpg`;
  const originalPreviewPath=`${W}/${excludedPathDeck}/original/not-a-preview.png`;
  const planningPreviewPath=`${W}/${excludedPathDeck}/ai-planning/plan/reference/not-a-preview.png`;
  const generatedPreviewPath=`${W}/${excludedPathDeck}/generated/not-a-preview.png`;

  for(const path of [legalPreview,manualPreview,generatedPreview,wrongPreview,directLegacyImage,imagePathAlias,storagePathAlias,legacyPreviewJpg,originalPreviewPath,planningPreviewPath,generatedPreviewPath])await addStorageObject(db,path);
  await addDeck(db,W,legalDeck,{paths:[legalPreview]});
  await addDeck(db,W,manualDeck,{allowed:false,paths:[manualPreview]});
  await addDeck(db,W,generatedDeck,{kind:'generated',paths:[generatedPreview]});
  await addDeck(db,W,wrongDeck,{paths:[wrongPreview]});
  await addDeck(db,W,noStorageDeck,{paths:[noStoragePreview]});
  await addDeck(db,W,directLegacyDeck,{paths:[{path:directLegacyImage}]});
  await addDeck(db,W,imagePathDeck,{paths:[{imagePath:imagePathAlias}]});
  await addDeck(db,W,storagePathDeck,{paths:[{storagePath:storagePathAlias}]});
  await addDeck(db,W,previewJpgDeck,{paths:[{path:legacyPreviewJpg}]});
  await addDeck(db,W,excludedPathDeck,{paths:[{path:originalPreviewPath},{path:planningPreviewPath},{path:generatedPreviewPath}]});
  await addDeck(db,W_LEGACY,legacyDeck,{allowed:false,paths:[legacyPreview]});

  const legalJob=await addJobPage(db,{
   referenceData:[{deckId:legalDeck,imagePaths:[legalPreview]}],styleDeckIds:[legalDeck]
  });
  assert.equal(await selectPath(db,W,legalJob.jobId,legalJob.pageId),legalPreview,
   'AI-allowed uploaded deck preview is the only legal uploaded reference');

  await addDeck(db,W,conflictDeck,{allowed:false,paths:[legalPreview]});
  assert.equal(await selectPath(db,W,legalJob.jobId,legalJob.pageId),null,
   'a manual-only deck owning the same path must force fail-closed');
  await resetRole(db);
  await query(db,'delete from public.decks where workspace_id=$1 and id=$2',[W,conflictDeck]);
  assert.equal(await selectPath(db,W,legalJob.jobId,legalJob.pageId),legalPreview,
   'resolving the manual conflict restores the explicit allowed source');

  const directLegacyJob=await addJobPage(db,{
   referenceData:[{deckId:directLegacyDeck,imagePaths:[directLegacyImage]}],styleDeckIds:[directLegacyDeck]
  });
  assert.equal(await selectPath(db,W,directLegacyJob.jobId,directLegacyJob.pageId),directLegacyImage,
   'legacy importImages path workspace/deck/<uuid>.webp without thumbPath remains legal');
  const imagePathJob=await addJobPage(db,{
   referenceData:[{deckId:imagePathDeck,imagePaths:[imagePathAlias]}],styleDeckIds:[imagePathDeck]
  });
  assert.equal(await selectPath(db,W,imagePathJob.jobId,imagePathJob.pageId),imagePathAlias,
   'historical imagePath alias remains legal');
  const storagePathJob=await addJobPage(db,{
   referenceData:[{deckId:storagePathDeck,imagePaths:[storagePathAlias]}],styleDeckIds:[storagePathDeck]
  });
  assert.equal(await selectPath(db,W,storagePathJob.jobId,storagePathJob.pageId),storagePathAlias,
   'historical storagePath alias remains legal');
  const previewJpgJob=await addJobPage(db,{
   referenceData:[{deckId:previewJpgDeck,imagePaths:[legacyPreviewJpg]}],styleDeckIds:[previewJpgDeck]
  });
  assert.equal(await selectPath(db,W,previewJpgJob.jobId,previewJpgJob.pageId),legacyPreviewJpg,
   'legacy nonnumeric preview filenames and jpg extension remain legal');

  await addDeck(db,W,conflictDeck,{allowed:false,paths:[{imagePath:directLegacyImage}]});
  assert.equal(await selectPath(db,W,directLegacyJob.jobId,directLegacyJob.pageId),null,
   'manual conflict is detected through historical imagePath aliases');
  await resetRole(db);
  await query(db,'delete from public.decks where workspace_id=$1 and id=$2',[W,conflictDeck]);

  for(const [label,path] of [['original',originalPreviewPath],['planning',planningPreviewPath],['generated',generatedPreviewPath]]){
   const job=await addJobPage(db,{
    referenceData:[{deckId:excludedPathDeck,imagePaths:[path]}],styleDeckIds:[excludedPathDeck]
   });
   assert.equal(await selectPath(db,W,job.jobId,job.pageId),null,`${label} path shape must be excluded`);
  }

  const manualJob=await addJobPage(db,{
   referenceData:[{deckId:manualDeck,imagePaths:[manualPreview]}],styleDeckIds:[manualDeck]
  });
  assert.equal(await selectPath(db,W,manualJob.jobId,manualJob.pageId),null,'manual-only upload must not reach image generation');

  const generatedJob=await addJobPage(db,{
   referenceData:[{deckId:generatedDeck,imagePaths:[generatedPreview]}],styleDeckIds:[generatedDeck]
  });
  assert.equal(await selectPath(db,W,generatedJob.jobId,generatedJob.pageId),null,'generated decks cannot be reference sources');

  const wrongDeckJob=await addJobPage(db,{
   referenceData:[{deckId:wrongDeck,imagePaths:[legalPreview]}],styleDeckIds:[wrongDeck]
  });
  assert.equal(await selectPath(db,W,wrongDeckJob.jobId,wrongDeckJob.pageId),null,'a path cannot masquerade as another deck preview');

  const originalMasquerade=`${W}/${legalDeck}/original/${'a'.repeat(64)}.pptx`;
  const originalJob=await addJobPage(db,{
   referenceData:[{deckId:legalDeck,imagePaths:[originalMasquerade]}],styleDeckIds:[legalDeck]
  });
  assert.equal(await selectPath(db,W,originalJob.jobId,originalJob.pageId),null,'uploaded originals cannot masquerade as previews');

  const missingStorageJob=await addJobPage(db,{
   referenceData:[{deckId:noStorageDeck,imagePaths:[noStoragePreview]}],styleDeckIds:[noStorageDeck]
  });
  assert.equal(await selectPath(db,W,missingStorageJob.jobId,missingStorageJob.pageId),null,
   'deck metadata without a private storage object must fail closed');

  const legalPlan='72000000-0000-4000-8000-000000000001';
  const legalPlanVersion='72100000-0000-4000-8000-000000000001';
  const legalStyle=stylePath(W,legalPlan,'reference','legal.png');
  await addStorageObject(db,legalStyle);
  await addPlanVersion(db,{planId:legalPlan,versionId:legalPlanVersion,styleImages:[legalStyle],owner:JUDGE,confirmedBy:JUDGE});
  const legalPlanSnapshot=await currentPlan(db,legalPlan);
  const styleJob=await addJobPage(db,{
   userId:JUDGE,planId:legalPlan,planSnapshot:legalPlanSnapshot,referenceData:[],styleDeckIds:[]
  });
  assert.equal(await selectPath(db,W,styleJob.jobId,styleJob.pageId),legalStyle,
   'current plan owner style input must remain legal');

  const styleRenderedPlan='72000000-0000-4000-8000-000000000002';
  const styleRenderedVersion='72100000-0000-4000-8000-000000000002';
  const renderedStyle=stylePath(W,styleRenderedPlan,'style-rendered','rendered.png');
  await addStorageObject(db,renderedStyle);
  await addPlanVersion(db,{planId:styleRenderedPlan,versionId:styleRenderedVersion,styleImages:[renderedStyle],owner:JUDGE,confirmedBy:JUDGE});
  const styleRenderedJob=await addJobPage(db,{
   userId:JUDGE,planId:styleRenderedPlan,planSnapshot:await currentPlan(db,styleRenderedPlan)
  });
  assert.equal(await selectPath(db,W,styleRenderedJob.jobId,styleRenderedJob.pageId),renderedStyle,
   'planning-rendered template style input remains legal');

  const stalePlan='72000000-0000-4000-8000-000000000003';
  const staleV1='72100000-0000-4000-8000-000000000003';
  const staleV2='72100000-0000-4000-8000-000000000004';
  const staleStyle=stylePath(W,stalePlan,'reference','stale.png');
  await addStorageObject(db,staleStyle);
  await addPlanVersion(db,{planId:stalePlan,versionId:staleV1,styleImages:[staleStyle],owner:JUDGE,confirmedBy:JUDGE});
  const staleSnapshot=await currentPlan(db,stalePlan);
  const staleJob=await addJobPage(db,{userId:JUDGE,planId:stalePlan,planSnapshot:staleSnapshot});
  const stalePayload=await planPayload([]);
  await resetRole(db);
  await query(db,`insert into public.ai_planning_versions(
   id,plan_id,workspace_id,version,confirmed_by,payload,content_hash
  ) values($1,$2,$3,2,$4,$5::jsonb,$6)`,
   [staleV2,stalePlan,W,JUDGE,JSON.stringify(stalePayload),crypto.createHash('sha256').update('stale-v2').digest('hex')]);
  await query(db,'update public.ai_planning_plans set current_version_id=$2 where id=$1 and workspace_id=$3',[stalePlan,staleV2,W]);
  assert.equal(await selectPath(db,W,staleJob.jobId,staleJob.pageId),null,
   'a queued job with an obsolete plan version must not read style inputs');

  const wrongOwnerPlan='72000000-0000-4000-8000-000000000004';
  const wrongOwnerVersion='72100000-0000-4000-8000-000000000005';
  const wrongOwnerStyle=stylePath(W,wrongOwnerPlan,'reference','owner.png');
  await addStorageObject(db,wrongOwnerStyle);
  await addPlanVersion(db,{planId:wrongOwnerPlan,versionId:wrongOwnerVersion,styleImages:[wrongOwnerStyle],owner:JUDGE,confirmedBy:JUDGE});
  const wrongOwnerJob=await addJobPage(db,{
   userId:OTHER,planId:wrongOwnerPlan,planSnapshot:await currentPlan(db,wrongOwnerPlan)
  });
  assert.equal(await selectPath(db,W,wrongOwnerJob.jobId,wrongOwnerJob.pageId),null,
   'a non-judge member cannot read the judge-owned style input');
  await resetRole(db);
  await query(db,`insert into public.ai_planning_version_access(version_id,user_id,access_role,granted_by)
   values($1,$2,'viewer',$3)`,[wrongOwnerVersion,OTHER,OWNER]);
  assert.equal(await selectPath(db,W,wrongOwnerJob.jobId,wrongOwnerJob.pageId),null,
   'legacy version access grants do not widen professional style authority');

  const adminOwnPlan='72000000-0000-4000-8000-000000000008';
  const adminOwnVersion='72100000-0000-4000-8000-000000000009';
  const adminOwnStyle=stylePath(W,adminOwnPlan,'reference','admin-own.png');
  await addStorageObject(db,adminOwnStyle);
  await addPlanVersion(db,{planId:adminOwnPlan,versionId:adminOwnVersion,styleImages:[adminOwnStyle],owner:OWNER,confirmedBy:OWNER});
  const adminOwnJob=await addJobPage(db,{
   userId:OWNER,planId:adminOwnPlan,planSnapshot:await currentPlan(db,adminOwnPlan)
  });
  assert.equal(await selectPath(db,W,adminOwnJob.jobId,adminOwnJob.pageId),null,
   'legacy admin ownership is insufficient; only the current dedicated judge may use style inputs');

  const wrongPrefixPlan='72000000-0000-4000-8000-000000000005';
  const wrongPrefixVersion='72100000-0000-4000-8000-000000000006';
  const otherPlanPath=stylePath(W,'72000000-0000-4000-8000-999999999999','reference','cross.png');
  await addStorageObject(db,otherPlanPath);
  await addPlanVersion(db,{planId:wrongPrefixPlan,versionId:wrongPrefixVersion,styleImages:[otherPlanPath],owner:JUDGE,confirmedBy:JUDGE});
  const wrongPrefixJob=await addJobPage(db,{userId:JUDGE,planId:wrongPrefixPlan,planSnapshot:await currentPlan(db,wrongPrefixPlan)});
  assert.equal(await selectPath(db,W,wrongPrefixJob.jobId,wrongPrefixJob.pageId),null,
   'plan payload cannot point at another plan private path');

  const wrongRolePlan='72000000-0000-4000-8000-000000000006';
  const wrongRoleVersion='72100000-0000-4000-8000-000000000007';
  const wrongRolePath=stylePath(W,wrongRolePlan,'content','not-style.png');
  await addStorageObject(db,wrongRolePath);
  await addPlanVersion(db,{planId:wrongRolePlan,versionId:wrongRoleVersion,styleImages:[wrongRolePath],owner:JUDGE,confirmedBy:JUDGE});
  const wrongRoleJob=await addJobPage(db,{userId:JUDGE,planId:wrongRolePlan,planSnapshot:await currentPlan(db,wrongRolePlan)});
  assert.equal(await selectPath(db,W,wrongRoleJob.jobId,wrongRoleJob.pageId),null,
   'only reference and style-rendered planning roles are style inputs');

  const missingStylePlan='72000000-0000-4000-8000-000000000007';
  const missingStyleVersion='72100000-0000-4000-8000-000000000008';
  const missingStylePath=stylePath(W,missingStylePlan,'reference','missing.png');
  await addPlanVersion(db,{planId:missingStylePlan,versionId:missingStyleVersion,styleImages:[missingStylePath],owner:JUDGE,confirmedBy:JUDGE});
  const missingStyleJob=await addJobPage(db,{userId:JUDGE,planId:missingStylePlan,planSnapshot:await currentPlan(db,missingStylePlan)});
  assert.equal(await selectPath(db,W,missingStyleJob.jobId,missingStyleJob.pageId),null,
   'style metadata without private storage remains unavailable');

  const legacyJob=await addJobPage(db,{
   w:W_LEGACY,userId:LEGACY_OWNER,referenceData:[{deckId:legacyDeck,imagePaths:[legacyPreview]}],styleDeckIds:[legacyDeck]
  });
  assert.equal(await selectPath(db,W_LEGACY,legacyJob.jobId,legacyJob.pageId),legacyPreview,
   'non-activated workspace keeps exact M04 behavior');

  await resetRole(db);
  await query(db,"update public.ai_generation_jobs set status='cancelled' where workspace_id=$1",[W]);
  await query(db,"update public.ai_generation_pages set status='succeeded' where workspace_id=$1",[W]);
  for(const job of [legalJob,manualJob,styleJob]){
   await query(db,"update public.ai_generation_jobs set status='queued' where id=$1 and workspace_id=$2",[job.jobId,W]);
   await query(db,'update public.ai_generation_pages set status=$3,attempts=0,lease_token=null,lease_owner=null,lease_expires_at=null,started_at=null,finished_at=null where id=$1 and workspace_id=$2',
    [job.pageId,W,'queued']);
  }
  await asService(db);
  const claimed=(await query(db,'select public.ai_generation_claim_pages($1,$2,$3,$4,$5) as value',
   [W,OWNER,'m29-worker',3,1800]))[0].value;
  assert.equal(claimed.length,3,'claim must return the three staged pages');
  const byPage=new Map(claimed.map(row=>[row.pageId,row]));
  assert.equal(byPage.get(legalJob.pageId).hasReferenceImage,true,'claim advertises legal uploaded reference');
  assert.equal(byPage.get(manualJob.pageId).hasReferenceImage,false,'claim hides manual-only reference');
  assert.equal(byPage.get(styleJob.pageId).hasReferenceImage,true,'claim advertises legal current-plan style input');

  const legalLease=byPage.get(legalJob.pageId).leaseToken;
  const manualLease=byPage.get(manualJob.pageId).leaseToken;
  const styleLease=byPage.get(styleJob.pageId).leaseToken;
  assert.equal((await query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4) as path',
   [W,OWNER,legalJob.pageId,legalLease]))[0].path,legalPreview,'valid lease rereads legal uploaded preview');
  assert.equal((await query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4) as path',
   [W,OWNER,manualJob.pageId,manualLease]))[0].path,null,'valid manual lease still cannot read a path');
  assert.equal((await query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4) as path',
   [W,OWNER,styleJob.pageId,styleLease]))[0].path,legalStyle,'valid lease rereads legal current-plan style');

  await assert.rejects(
   query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4)',[W,OWNER,legalJob.pageId,uuid()]),
   /页面租约已失效/
  );
  await resetRole(db);
  await query(db,"update public.ai_generation_pages set lease_expires_at=now()-interval '1 second' where id=$1 and workspace_id=$2",[legalJob.pageId,W]);
  await asService(db);
  await assert.rejects(
   query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4)',[W,OWNER,legalJob.pageId,legalLease]),
   /页面租约已过期/
  );

  await resetRole(db);
  await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[OTHER]);
  await db.exec('set role service_role');
  await assert.rejects(
   query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4)',[W,OWNER,styleJob.pageId,styleLease]),
   /操作人身份不匹配/,
   'another authenticated user cannot impersonate the claim actor'
  );

  await resetRole(db);
  await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[NONMEMBER]);
  await db.exec('set role service_role');
  await assert.rejects(
   query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4)',[W,NONMEMBER,styleJob.pageId,styleLease]),
   /请先加入团队/,
   'non-members cannot read reference paths even with a leaked lease token'
  );

  const legacyLease=uuid();
  await resetRole(db);
  await query(db,`update public.ai_generation_pages
   set status='running',lease_token=$3,lease_expires_at=now()+interval '10 minutes'
   where id=$1 and workspace_id=$2`,[legacyJob.pageId,W_LEGACY,legacyLease]);
  await asService(db);
  assert.equal((await query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4) as path',
   [W_LEGACY,LEGACY_OWNER,legacyJob.pageId,legacyLease]))[0].path,legacyPreview,
   'legacy read path remains compatible under a valid lease');

  await resetRole(db);
  const acl=(await query(db,`select
   has_function_privilege('service_role','public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)','EXECUTE') service_ok,
   has_function_privilege('authenticated','public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)','EXECUTE') authenticated_ok,
   has_function_privilege('anon','public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)','EXECUTE') anon_ok,
   has_function_privilege('service_role','public.ai_generation_select_reference_path(uuid,uuid,uuid)','EXECUTE') selector_service_ok,
   has_function_privilege('authenticated','guanye_internal.single_judge_generation_uploaded_reference_ok(uuid,text,text)','EXECUTE') helper_authenticated_ok`))[0];
  assert.deepEqual(acl,{
   service_ok:true,authenticated_ok:false,anon_ok:false,selector_service_ok:false,helper_authenticated_ok:false
  });
  await asAnon(db);
  await assert.rejects(
   query(db,'select public.ai_generation_page_reference_path($1,$2,$3,$4)',[W,OWNER,legalJob.pageId,legalLease]),
   /permission denied/
  );

  console.log('PASS: M29 authenticates legacy/new uploaded preview shapes, aliases, manual conflicts, current-judge style plans, leases, claim flags, ACLs, and legacy behavior');
 }finally{
  if(db)await db.close();
 }
})().catch(error=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});
