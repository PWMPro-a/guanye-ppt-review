// npm install --prefix .test-output/sql @electric-sql/pglite
// Exercise the professional web migration with real PostgreSQL permissions and data.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');

const W='50100000-0000-4000-8000-000000000001';
const JUDGE='50100000-0000-4000-8000-000000000003';
const ADMIN='50100000-0000-4000-8000-000000000004';
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');

const migrationNames=fs.readdirSync('cloud/migrations').filter(name=>/^2026112[123]_[0-9]{2}_.+\.sql$/.test(name)).sort();
const migrations=migrationNames.map(name=>fs.readFileSync('cloud/migrations/'+name,'utf8'));
async function query(db,sql,values=[]){return (await db.query(sql,values)).rows;}
async function resetRole(db){await db.exec('reset role');}
async function asService(db){await resetRole(db);await db.exec('set role service_role');}
async function asAuthenticated(db,userId){
 await resetRole(db);
 await query(db,"select set_config('request.jwt.claim.sub',$1,false)",[userId]);
 await db.exec('set role authenticated');
}

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
  create table auth.users(id uuid primary key,email text);
  create function auth.uid() returns uuid language sql stable as $$
   select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid
  $$;
  create function auth.jwt() returns jsonb language sql stable as $$
   select jsonb_build_object('email',current_setting('request.jwt.claim.email',true))
  $$;
  create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint);
  create table storage.objects(id uuid,name text,bucket_id text,owner_id text,metadata jsonb);
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

function deckData(id){
 return {
  id,title:id,name:id,origin:'import',
  images:[
   {id:'p1',name:'第 1 页.png',position:1,path:`${W}/${id}/preview/1.png`,thumbPath:`${W}/${id}/preview/1.png`},
   {id:'p2',name:'第 2 页.png',position:2,path:`${W}/${id}/preview/2.png`,thumbPath:`${W}/${id}/preview/2.png`}
  ]
 };
}

async function versions(db,deckId){
 return (await query(db,'select public.ai_review_deck_version($1,$2) as "deckVersion",public.ai_review_rubric_version($1) as "rubricVersion"',[W,deckId]))[0];
}

const SUBMITTER='50100000-0000-4000-8000-000000000005';
const OTHER='50100000-0000-4000-8000-000000000006';
const deck='upload-web-fixture';
const generated='generated-web-fixture';
const dims=[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}];
const score=(visual,layout=visual)=>({visual,layout});
async function value(db,sql,args=[W]){return (await query(db,sql,args))[0].value;}
async function submit(db,id,v,units){return value(db,'select public.single_judge_submit_bundle($1,$2) value',[W,{deckId:id,...v,units}]);}
(async()=>{
 const db=await bootstrap();
 try{
  await query(db,"insert into auth.users values($1,'judge@ex.test'),($2,'admin@ex.test'),($3,'submitter@ex.test'),($4,'new@ex.test')",[JUDGE,ADMIN,SUBMITTER,OTHER]);
  await query(db,"insert into public.workspaces(id,name,owner_email,invite_hash,settings) values($1,'Web','admin@ex.test','invite',$2)",[W,{revision:0,maxScore:5,dimensions:dims}]);
  await query(db,"insert into public.members(workspace_id,user_id,name,role) values($1,$2,'Judge','reviewer'),($1,$3,'Admin','admin'),($1,$4,'Submitter','reviewer')",[W,JUDGE,ADMIN,SUBMITTER]);
  await query(db,"insert into public.decks(id,workspace_id,data,source_kind,ai_allowed) values($1,$2,$3,'uploaded',false),($4,$2,$5,'generated',true)",[deck,W,deckData(deck),generated,{...deckData(generated),generation:{references:[]}}]);
  const legacy={scores:score(1),note:'keep original review',mode:'deck'};
  await query(db,'insert into public.reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4)',[W,JUDGE,deck,legacy]);
  const v=await versions(db,deck), gv=await versions(db,generated);
  await asAuthenticated(db,ADMIN);
  await assert.rejects(()=>query(db,'select public.professional_web_activate($1,$2,$3)',[W,JUDGE,ADMIN]),/permission denied/);
  await asService(db);
  await assert.rejects(()=>query(db,'select public.professional_web_activate($1,$2,$3)',[W,ADMIN,ADMIN]),/两个不同/);
  await query(db,'select public.professional_web_activate($1,$2,$3)',[W,JUDGE,ADMIN]);
  await asAuthenticated(db,SUBMITTER);
  assert.deepEqual(await value(db,'select public.single_judge_access($1) value'),{mode:'single-judge',role:'submitter',professionalWeb:true});
  for(const sql of ['public.get_review_library','public.professional_score_summary','public.professional_admin_directory'])
   await assert.rejects(()=>value(db,'select '+sql+'($1) value'),/权读取|加入团队|管理员/);
  await assert.rejects(()=>submit(db,deck,v,[{scope:'deck',scores:score(5)}]),/评分师/);
  await asAuthenticated(db,JUDGE);
  await assert.rejects(()=>value(db,'select public.professional_admin_directory($1) value'),/管理员/);
  assert.deepEqual(await value(db,'select public.professional_score_summary($1) value'),{});
  const first=await submit(db,deck,v,[{scope:'deck',scores:score(3)}]);
  assert.equal(first.decisions.length,1);
  await asAuthenticated(db,ADMIN);
  await submit(db,deck,v,[{scope:'page',unitId:'p1',scores:score(4)}]);
  assert.deepEqual(await value(db,'select public.professional_score_summary($1) value'),{[deck]:{formalScore:3,formalPageScores:{p1:4}}});
  await submit(db,deck,v,[{scope:'deck',scores:score(5)}]);
  assert.equal((await value(db,'select public.professional_score_summary($1) value'))[deck].formalScore,5);
  await asAuthenticated(db,JUDGE);
  const again=await submit(db,deck,v,[{scope:'deck',scores:score(3)}]);
  assert.notEqual(first.decisions[0].decisionId,again.decisions[0].decisionId);
  assert.equal((await submit(db,deck,v,[{scope:'deck',scores:score(3)}])).decisions.length,0);
  const workspace=await value(db,"select public.single_judge_review_workspace($1,'uploaded',$2) value",[W,deck]);
  assert.equal(workspace.decisions[0].decisionId,again.decisions[0].decisionId);
  assert.equal(workspace.decisions[0].actorId,JUDGE);
  assert.equal(workspace.submissions.length,4);
  assert.deepEqual(await value(db,'select public.professional_score_summary($1) value'),{[deck]:{formalScore:3,formalPageScores:{p1:4}}});
  const admit=await submit(db,generated,gv,[{scope:'deck',scores:{...score(5),match:5}}]);
  await asAuthenticated(db,ADMIN);
  assert.equal((await value(db,'select public.single_judge_admit($1,$2,$3) value',[W,admit.submissionIds[0],admit.decisions[0].decisionId])).admitted,true);
  await query(db,'select public.professional_admin_archive($1,$2,true)',[W,deck]);
  assert.ok(!(await value(db,'select public.get_review_library($1) value')).decks.some(x=>x.id===deck));
  assert.ok(!(await value(db,"select public.single_judge_review_queue($1,'uploaded',null,8) value")).items.some(x=>x.id===deck));
  await assert.rejects(()=>submit(db,deck,v,[{scope:'deck',scores:score(5)}]),/作品不存在/);
  assert.equal((await value(db,'select public.professional_admin_directory($1) value')).archived[0].id,deck);
  await query(db,'select public.professional_admin_archive($1,$2,false)',[W,deck]);
  assert.equal((await value(db,'select public.professional_score_summary($1) value'))[deck].formalScore,3);
  await query(db,"select public.professional_admin_add_member($1,'NEW@EX.TEST','New member')",[W]);
  assert.equal((await value(db,'select public.professional_admin_directory($1) value')).members.find(m=>m.id===OTHER).role,'submitter');
  await query(db,"select public.professional_admin_member($1,$2,'',false)",[W,OTHER]);
  const archive=await value(db,'select public.professional_admin_export($1) value');
  assert.equal(archive.scoreHistory.length,5);
  assert.deepEqual(archive.teamReviews.map(x=>x.data),[legacy]);
  const updated=await value(db,'select public.professional_admin_settings($1,0,10,$2) value',[W,JSON.stringify(dims)]);
  assert.equal(updated.maxScore,10);
  await assert.rejects(()=>value(db,'select public.professional_admin_settings($1,0,11,$2) value',[W,JSON.stringify(dims)]),/已变化/);
  assert.deepEqual(await value(db,'select public.professional_score_summary($1) value'),{});
  await assert.rejects(()=>submit(db,deck,v,[{scope:'deck',scores:score(5)}]),/版本已变化/);
  await asService(db);
  await query(db,'select public.professional_web_rollback($1)',[W]);
  await asAuthenticated(db,JUDGE);
  assert.equal(await value(db,'select public.single_judge_access($1) value'),null);
  await assert.rejects(()=>value(db,'select public.professional_score_summary($1) value'),/无权/);
  console.log('PASS: professional web cutover, permission isolation, shared latest deck/page scores, history, admission, reversible archive and rollback');
 }finally{await db.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
