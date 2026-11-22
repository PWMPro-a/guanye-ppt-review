// M35 direct-intake wrapper plus M32/M33/M34 regression, real SQL/RLS PGlite fixture.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const names=['20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning','20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history','20261121_07_review_calibration','20261121_08_review_comparison','20261121_09_review_backfill','20261121_10_review_queue_listing','20261121_11_planning_history','20261121_12_library_classification','20261121_13_formal_correction','20261121_14_library_formal_scores','20261121_15_planning_original_references','20261121_16_uploaded_baseline','20261121_17_review_workspace_batch','20261121_18_review_backfill_classification','20261121_19_uploaded_source_provenance','20261121_20_uploaded_source_groups','20261121_21_uploaded_comparison_source_dedup','20261122_22_single_judge_foundation','20261122_23_single_judge_original_archive','20261122_24_single_judge_submission','20261122_25_image_workers_default','20261122_26_single_judge_score_revisions','20261122_27_single_judge_review_workspace','20261122_28_single_judge_admission','20261122_29_single_judge_generation_references','20261122_30_single_judge_creation','20261122_31_single_judge_image_runtime','20261122_32_single_judge_legacy_backfill','20261122_33_single_judge_review_queue','20261122_34_single_judge_owner_activation','20261122_35_single_judge_intake'];
const W='70100000-0000-4000-8000-000000000001';
const JUDGE='70100000-0000-4000-8000-000000000004';
const ADMIN='70100000-0000-4000-8000-000000000005';
const SUBMITTER='70100000-0000-4000-8000-000000000006';
const OTHER='70100000-0000-4000-8000-000000000007';
const W_OWNER='70400000-0000-4000-8000-000000000001';
const OWNER='70400000-0000-4000-8000-000000000002';
const W_LEGACY='70500000-0000-4000-8000-000000000001';
const LEGACY_MEMBER='70500000-0000-4000-8000-000000000002';
const NONMEMBER='70500000-0000-4000-8000-000000000003';
const DECK='legacy-backfill-deck';

async function query(db,sql,values=[]){return (await db.query(sql,values)).rows;}
async function resetRole(db){await db.exec('reset role');}
async function asAuth(db,id){await resetRole(db);await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[id]);await db.exec('set role authenticated');}
async function asService(db){await resetRole(db);await query(db,"select set_config('request.jwt.claim.sub','',false)");await db.exec('set role service_role');}
async function asAnon(db){await resetRole(db);await query(db,"select set_config('request.jwt.claim.sub','',false)");await db.exec('set role anon');}
async function rejectCode(fn,code){await assert.rejects(fn,e=>e.code===code);}

async function verifySingleJudgeIntake(db){
 const intake1=crypto.randomUUID();
 const intake2=crypto.randomUUID();
 const intake3=crypto.randomUUID();
 const invalid=crypto.randomUUID();
 const rows={
  1:{id:intake1,submitted_by:SUBMITTER,title:'First intake',original_name:'deck.pdf',content_type:'application/pdf',size_bytes:1234,sha256:'a'.repeat(64),ai_allowed:true},
  2:{id:intake2,submitted_by:OTHER,title:'Second intake',original_name:'slides.pptx',content_type:'application/vnd.openxmlformats-officedocument.presentationml.presentation',size_bytes:2345,sha256:'b'.repeat(64),ai_allowed:false},
  3:{id:intake3,submitted_by:SUBMITTER,title:'Retry after fail',original_name:'retry.ppt',content_type:'application/vnd.ms-powerpoint',size_bytes:3456,sha256:'c'.repeat(64),ai_allowed:true},
 };
 const path=(row)=>`${W}/intake/${row.id}/${row.original_name}`;
 const insertRow=(row,submittedBy=row.submitted_by)=>query(db,
  `insert into public.single_judge_intake(id,workspace_id,submitted_by,title,original_name,object_path,content_type,size_bytes,sha256,ai_allowed)
   values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,[row.id,W,submittedBy,row.title,row.original_name,path(row),row.content_type,row.size_bytes,row.sha256,row.ai_allowed]);
 const addObject=(name,ownerId=null)=>query(db,"insert into storage.objects(id,name,bucket_id,owner_id) values($1,$2,'ppt-library',$3)",[crypto.randomUUID(),name,ownerId]);
 const deleteObject=(name)=>query(db,'delete from storage.objects where bucket_id=$1 and name=$2 returning id',['ppt-library',name]);
 const objectExists=async(name)=>(await query(db,'select id from storage.objects where bucket_id=$1 and name=$2',['ppt-library',name])).length===1;
 const visibleObjectCount=async(actorId,name)=>{await asAuth(db,actorId);return (await query(db,'select id from storage.objects where bucket_id=$1 and name=$2',['ppt-library',name])).length;};

 await resetRole(db);
 for(const id of [JUDGE,ADMIN,SUBMITTER,OTHER,OWNER,LEGACY_MEMBER,NONMEMBER])await query(db,'insert into auth.users(id) values($1)',[id]);
 await query(db,"insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'legacy','legacy@example.test',$2,'{\"dimensions\":[{\"id\":\"visual\"},{\"id\":\"layout\"}]}'::jsonb)",[W,crypto.createHash('sha256').update('legacy').digest('hex')]);
 await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'judge','reviewer'),($1,$3,'admin','admin'),($1,$4,'submitter','reviewer'),($1,$5,'other submitter','reviewer')",[W,JUDGE,ADMIN,SUBMITTER,OTHER]);
 await query(db,"insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'Legacy team','legacy-team@example.test',$2,'{\"dimensions\":[{\"id\":\"visual\"}]}'::jsonb)",[W_LEGACY,crypto.createHash('sha256').update('legacy-team').digest('hex')]);
 await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'Legacy member','reviewer')",[W_LEGACY,LEGACY_MEMBER]);
 await asService(db); await query(db,"select public.single_judge_activate($1,$2,$3)",[W,JUDGE,ADMIN]);

 const judgeObject=`${W}/read/judge.pdf`;
 const adminObject=`${W}/read/admin.pdf`;
 const submitterObject=`${W}/read/submitter.pdf`;
 const legacyObject=`${W_LEGACY}/library/legacy.pdf`;
 const malformedFolderObject='not-a-workspace/read.pdf';
 const missingWorkspaceObject=`${crypto.randomUUID()}/read.pdf`;
 for(const name of [judgeObject,adminObject,submitterObject,legacyObject,malformedFolderObject,missingWorkspaceObject])await addObject(name);

 await asAuth(db,JUDGE);
 assert.equal(await visibleObjectCount(JUDGE,judgeObject),1);
 await asAuth(db,ADMIN);
 assert.deepEqual((await query(db,'select public.single_judge_access($1) as value',[W]))[0].value,{mode:'single-judge',role:'admin'});
 assert.equal(await visibleObjectCount(ADMIN,adminObject),1);
 assert.equal(await visibleObjectCount(SUBMITTER,judgeObject),0);
 assert.equal(await visibleObjectCount(SUBMITTER,submitterObject),0);
 await asAuth(db,LEGACY_MEMBER);
 assert.deepEqual(await query(db,'select public.is_member($1) as value',[W_LEGACY]),[{value:true}]);
 assert.equal(await visibleObjectCount(LEGACY_MEMBER,legacyObject),1);
 assert.equal(await visibleObjectCount(LEGACY_MEMBER,judgeObject),0);
 assert.equal(await visibleObjectCount(JUDGE,legacyObject),0);
 assert.equal(await visibleObjectCount(NONMEMBER,judgeObject),0);
 assert.equal(await visibleObjectCount(NONMEMBER,legacyObject),0);
 assert.equal(await visibleObjectCount(JUDGE,malformedFolderObject),0);
 assert.equal(await visibleObjectCount(JUDGE,missingWorkspaceObject),0);
 await asAnon(db);
 assert.equal((await query(db,'select id from storage.objects where bucket_id=$1 and name=$2',['ppt-library',judgeObject])).length,0);

 await asAuth(db,SUBMITTER);
 assert.deepEqual((await query(db,'select public.single_judge_access($1) as value',[W]))[0].value,{mode:'single-judge',role:'submitter'});
 await insertRow(rows[1]);
 await addObject(path(rows[1]),SUBMITTER);
 assert.equal((await deleteObject(path(rows[1]))).length,0); // Registered originals are never cleanup targets.
 assert.equal(await visibleObjectCount(SUBMITTER,path(rows[1])),0);
 assert.equal(await visibleObjectCount(JUDGE,path(rows[1])),1);
 assert.equal(await visibleObjectCount(ADMIN,path(rows[1])),1);
 await asAuth(db,SUBMITTER);
 await rejectCode(()=>addObject(`${W}/other/${intake1}/deck.pdf`),'42501');
 await rejectCode(()=>addObject(`${W}/intake/${intake1}/deck.exe`),'42501');
 await rejectCode(()=>insertRow(rows[1],OTHER),'42501');
 const mismatched={...rows[1],id:invalid,title:'Mismatched path',original_name:'mismatch.pdf'};
 await rejectCode(()=>query(db,`insert into public.single_judge_intake(id,workspace_id,submitted_by,title,original_name,object_path,content_type,size_bytes,sha256,ai_allowed)
  values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,[mismatched.id,W,SUBMITTER,mismatched.title,mismatched.original_name,`${W}/intake/${crypto.randomUUID()}/${mismatched.original_name}`,'application/pdf',100,'d'.repeat(64),true]),'42501');
 await rejectCode(()=>query(db,`insert into public.single_judge_intake(id,workspace_id,submitted_by,title,original_name,object_path,content_type,size_bytes,sha256,ai_allowed)
  values($1,$2,$3,'Bad MIME','bad.pdf',$4,'text/plain',100,$5,true)`,[invalid,W,SUBMITTER,`${W}/intake/${invalid}/bad.pdf`,'e'.repeat(64)]),'23514');
 await rejectCode(()=>query(db,`insert into public.single_judge_intake(id,workspace_id,submitted_by,title,original_name,object_path,content_type,size_bytes,sha256,ai_allowed)
  values($1,$2,$3,'Bad file type','bad.exe',$4,'application/pdf',100,$5,true)`,[invalid,W,SUBMITTER,`${W}/intake/${invalid}/bad.exe`,'f'.repeat(64)]),'42501');

 await asAuth(db,OTHER);
 await insertRow(rows[2]);
 await addObject(path(rows[2]),OTHER);

 const legalOrphanId=crypto.randomUUID();
 const legalOrphan=`${W}/intake/${legalOrphanId}/legal-orphan.pdf`;
 const otherOrphanId=crypto.randomUUID();
 const otherOrphan=`${W}/intake/${otherOrphanId}/other-orphan.pdf`;
 const invalidOrphanId=crypto.randomUUID();
 const invalidOrphan=`${W}/intake/${invalidOrphanId}/invalid-orphan.exe`;
 const nonmemberOrphanId=crypto.randomUUID();
 const nonmemberOrphan=`${W}/intake/${nonmemberOrphanId}/nonmember-orphan.pdf`;
 const normalDeckImage=`${W}/normal-deck/preview/1.png`;

 await resetRole(db);
 await addObject(otherOrphan,OTHER);
 await addObject(invalidOrphan,SUBMITTER);
 await addObject(nonmemberOrphan,NONMEMBER);
 await addObject(normalDeckImage,SUBMITTER);

 await asAuth(db,SUBMITTER);
 await addObject(legalOrphan,SUBMITTER);
 assert.equal(await visibleObjectCount(SUBMITTER,legalOrphan),1);
 assert.equal((await deleteObject(legalOrphan)).length,1);
 await resetRole(db);
 assert.equal(await objectExists(legalOrphan),false);

 await asAuth(db,SUBMITTER);
 assert.equal((await deleteObject(otherOrphan)).length,0);
 assert.equal((await deleteObject(invalidOrphan)).length,0);
 assert.equal((await deleteObject(normalDeckImage)).length,0);
 await asAuth(db,NONMEMBER);
 assert.equal((await deleteObject(nonmemberOrphan)).length,0);
 await resetRole(db);
 assert.equal(await objectExists(otherOrphan),true);
 assert.equal(await objectExists(invalidOrphan),true);
 assert.equal(await objectExists(nonmemberOrphan),true);
 assert.equal(await objectExists(normalDeckImage),true);

 await asAuth(db,SUBMITTER);
 assert.deepEqual((await query(db,'select id,status from public.single_judge_intake order by id')).map(row=>row.id),[intake1]);
 assert.equal((await query(db,'select id from public.single_judge_intake where id=$1',[intake2])).length,0);
 await asAuth(db,OTHER);
 assert.deepEqual((await query(db,'select id from public.single_judge_intake order by id')).map(row=>row.id),[intake2]);

 await asAuth(db,SUBMITTER); await rejectCode(()=>query(db,'select public.single_judge_intake_claim($1,$2,$3)',[W,'intake-test',1800]),'42501');
 await asAuth(db,ADMIN); await rejectCode(()=>query(db,'select public.single_judge_intake_claim($1,$2,$3)',[W,'intake-test',1800]),'42501');
 await asAuth(db,JUDGE);
 const claimed=(await query(db,'select public.single_judge_intake_claim($1,$2,$3) as value',[W,'intake-test',1800]))[0].value;
 assert.equal(claimed.task.id,intake1);
 assert.equal(claimed.task.sha256,rows[1].sha256);
 assert.ok(claimed.task.leaseToken);

 await asAuth(db,ADMIN);
 await rejectCode(()=>query(db,'select public.single_judge_intake_complete($1,$2,$3,$4)',[W,claimed.task.id,claimed.task.leaseToken,'import-'+crypto.randomUUID()]),'42501');
 await rejectCode(()=>query(db,'select public.single_judge_intake_fail($1,$2,$3,$4)',[W,claimed.task.id,claimed.task.leaseToken,'admin fail']),'42501');

 await asAuth(db,JUDGE);
 await rejectCode(()=>query(db,'select public.single_judge_intake_complete($1,$2,$3,$4)',[W,claimed.task.id,crypto.randomUUID(),'import-'+crypto.randomUUID()]),'22023');
 await resetRole(db);
 assert.equal((await query(db,'select status from public.single_judge_intake where id=$1',[claimed.task.id]))[0].status,'running');
 const deckId='import-'+crypto.randomUUID();
 const completed=(await query(db,'select public.single_judge_intake_complete($1,$2,$3,$4) as value',[W,claimed.task.id,claimed.task.leaseToken,deckId]))[0].value;
 assert.equal(completed.status,'completed'); assert.equal(completed.deckId,deckId);
 await resetRole(db);
 const completedRow=(await query(db,'select submitted_by,status,deck_id,lease_owner,lease_token,lease_expires_at,attempts from public.single_judge_intake where id=$1',[claimed.task.id]))[0];
 assert.equal(completedRow.status,'completed'); assert.equal(completedRow.deck_id,deckId); assert.equal(completedRow.lease_token,null); assert.equal(completedRow.lease_owner,null); assert.equal(completedRow.lease_expires_at,null); assert.equal(completedRow.attempts,1);
 await asAuth(db,completedRow.submitted_by);
 assert.deepEqual(await query(db,'select status,deck_id from public.single_judge_intake where id=$1',[claimed.task.id]),[{status:'completed',deck_id:deckId}]);

 await asAuth(db,JUDGE);
 const claimed2=(await query(db,'select public.single_judge_intake_claim($1,$2,$3) as value',[W,'intake-test',1800]))[0].value;
 assert.equal(claimed2.task.id,intake2);
 await rejectCode(()=>query(db,'select public.single_judge_intake_fail($1,$2,$3,$4)',[W,claimed2.task.id,crypto.randomUUID(),'wrong lease']),'22023');
 assert.equal((await query(db,'select public.single_judge_intake_fail($1,$2,$3,$4) as value',[W,claimed2.task.id,claimed2.task.leaseToken,'转换失败'])).length,1);
 await resetRole(db);
 const failedRow=(await query(db,'select status,error,lease_owner,lease_token,lease_expires_at from public.single_judge_intake where id=$1',[intake2]))[0];
 assert.deepEqual(failedRow,{status:'failed',error:'转换失败',lease_owner:null,lease_token:null,lease_expires_at:null});
 await asAuth(db,JUDGE);
 assert.equal((await query(db,'select public.single_judge_intake_claim($1,$2,$3) as value',[W,'intake-test',1800]))[0].value.task,null);

 await asAuth(db,SUBMITTER); await insertRow(rows[3]); await addObject(path(rows[3]),SUBMITTER);
 await asAuth(db,JUDGE);
 const retry1=(await query(db,'select public.single_judge_intake_claim($1,$2,$3) as value',[W,'intake-retry',60]))[0].value;
 assert.equal(retry1.task.id,intake3);
 await resetRole(db); await query(db,"update public.single_judge_intake set lease_expires_at=now()-interval '1 second' where id=$1",[intake3]);
 await asAuth(db,JUDGE);
 const retry2=(await query(db,'select public.single_judge_intake_claim($1,$2,$3) as value',[W,'intake-retry',60]))[0].value;
 assert.equal(retry2.task.id,intake3); assert.notEqual(retry2.task.leaseToken,retry1.task.leaseToken);
 const retryDeck='import-'+crypto.randomUUID();
 await query(db,'select public.single_judge_intake_complete($1,$2,$3,$4)',[W,intake3,retry2.task.leaseToken,retryDeck]);
 await resetRole(db);
 assert.deepEqual(await query(db,'select status,deck_id,attempts from public.single_judge_intake where id=$1',[intake3]),[{status:'completed',deck_id:retryDeck,attempts:2}]);

 await query(db,`insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'owner','owner@example.test',$2,'{"dimensions":[{"id":"visual"}]}'::jsonb)`,[W_OWNER,crypto.createHash('sha256').update('owner').digest('hex')]);
 await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'owner','reviewer')",[W_OWNER,OWNER]);
 await asService(db); await query(db,'select public.single_judge_activate_owner($1,$2)',[W_OWNER,OWNER]);
 await resetRole(db);
 const ownerBinding=(await query(db,'select dedicated_judge_id,single_judge_admin_id from public.workspaces where id=$1',[W_OWNER]))[0];
 assert.equal(ownerBinding.dedicated_judge_id,OWNER); assert.equal(ownerBinding.single_judge_admin_id,OWNER);
 const ownerObject=`${W_OWNER}/read/owner.pdf`;
 await addObject(ownerObject);
 await asAuth(db,OWNER);
 assert.deepEqual((await query(db,'select public.single_judge_access($1) as value',[W_OWNER]))[0].value,{mode:'single-judge',role:'judge'});
 assert.equal(await visibleObjectCount(OWNER,ownerObject),1);
 await rejectCode(()=>query(db,'select public.single_judge_activate_owner($1,$2)',[W_OWNER,OWNER]),'42501');

 await resetRole(db);
 const image=`${W}/${DECK}/preview/1.png`;
 await query(db,"insert into storage.objects(id,name,bucket_id) values($1,$2,'ppt-library')",[crypto.randomUUID(),image]);
 await query(db,"insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3,'uploaded',true)",[DECK,W,JSON.stringify({id:DECK,title:'Legacy',images:[{id:'p1',name:'Page',position:1,path:image,thumbPath:image}]})]);
 await asAuth(db,JUDGE);
 const result=(await query(db,"select public.single_judge_backfill($1,'uploaded',0,8) as value",[W]))[0].value;
 assert.equal(result.ok,true); assert.equal(result.totalDecks,1); assert.ok(result.decks>=1);
 await asService(db);
 assert.ok((await query(db,"select count(*)::int n from public.ai_review_jobs where workspace_id=$1",[W]))[0].n>0);
 await asAuth(db,OTHER); await rejectCode(()=>query(db,"select public.single_judge_backfill($1,'uploaded',0,8)",[W]),'42501');

 await resetRole(db);
 for(let i=1;i<=8;i++)await query(db,"insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3,'uploaded',true)",[`legacy-${i}`,W,JSON.stringify({id:`legacy-${i}`,title:`Legacy ${i}`,images:[]})]);
 await asAuth(db,JUDGE);
 const page1=(await query(db,"select public.single_judge_review_queue($1,'uploaded',null,8) as value",[W]))[0].value;
 assert.equal(page1.items.length,8); assert.ok(page1.nextCursor);
 const page2=(await query(db,"select public.single_judge_review_queue($1,'uploaded',$2,8) as value",[W,page1.nextCursor]))[0].value;
 assert.equal(page2.items.length,1); assert.equal(page2.nextCursor,null);
 await asAuth(db,OTHER); await rejectCode(()=>query(db,"select public.single_judge_review_queue($1,'uploaded',null,8)",[W]),'42501');
 await asAuth(db,ADMIN); await rejectCode(()=>query(db,"select public.single_judge_review_queue($1,'uploaded',null,8)",[W]),'42501');
 await asAuth(db,ADMIN); await rejectCode(()=>query(db,"select public.single_judge_backfill($1,'uploaded',0,8)",[W]),'42501');
 await resetRole(db); await db.exec('set role anon'); await rejectCode(()=>query(db,"select public.single_judge_backfill($1,'uploaded',0,8)",[W]),'42501');
 await resetRole(db);
 console.log('PASS M32/M33/M34/M35: professional storage read ACL, narrow intake orphan delete, direct intake RLS, own receipt read, judge lease lifecycle and legacy regressions');
}

async function main(){
 const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
 const schema=fs.readFileSync('cloud/schema.sql','utf8');
 const legacySchema=schema.slice(0,schema.indexOf(marker));
 assert.ok(legacySchema.length>1000);
 const db=new PGlite({extensions:{pgcrypto}});
 try{
  await db.exec(`
   create schema auth; create schema storage; create schema extensions;
   create role anon; create role authenticated; create role service_role bypassrls;
   create extension if not exists pgcrypto with schema extensions;
   create table auth.users(id uuid primary key);
   create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
   create function auth.jwt() returns jsonb language sql stable as $$ select jsonb_build_object('email',current_setting('request.jwt.claim.email',true)) $$;
   create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint);
   create table storage.objects(id uuid primary key,name text not null,bucket_id text not null,owner_id text,unique(bucket_id,name));
   alter table storage.objects enable row level security;
   create function storage.foldername(text) returns text[] language sql as $$ select string_to_array($1,'/') $$;
  `);
  await db.exec(legacySchema);
  await db.exec('grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role; grant select,insert,update,delete on all tables in schema public,storage to authenticated,anon; grant all on all tables in schema public,storage to service_role;');
  for(const name of names)await db.exec(fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));
  await verifySingleJudgeIntake(db);
 }finally{await db.close();}
}

module.exports=verifySingleJudgeIntake;
if(require.main===module)main().catch(error=>{console.error(error.stack||error);process.exit(1);});