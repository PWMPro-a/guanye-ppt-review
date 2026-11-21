// npm install --prefix .test-output/sql @electric-sql/pglite
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const W1='11111111-1111-4111-8111-111111111111';
const W2='22222222-2222-4222-8222-222222222222';
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const C='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const startMarker='-- AI_REVIEW_FOUNDATION_BODY_START';
const endMarker='-- AI_REVIEW_FOUNDATION_BODY_END';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const migration=fs.readFileSync('cloud/migrations/20261121_01_ai_review_foundation.sql','utf8');
const reviewMigration=fs.readFileSync('cloud/migrations/20261121_02_ai_reviews.sql','utf8');
const calibrationMigration=fs.readFileSync('cloud/migrations/20261121_07_review_calibration.sql','utf8');
const classificationMigration=fs.readFileSync('cloud/migrations/20261121_12_library_classification.sql','utf8');
const summaryMigration=fs.readFileSync('cloud/migrations/20261121_14_library_formal_scores.sql','utf8');
const uploadedBaselineMigration=fs.readFileSync('cloud/migrations/20261121_16_uploaded_baseline.sql','utf8');
function foundationBody(sql){
 const start=sql.indexOf(startMarker),end=sql.indexOf(endMarker);
 assert.ok(start>=0&&end>start,'foundation markers must exist');
 return sql.slice(start,end).replace(/\r\n/g,'\n').trim();
}
assert.equal(foundationBody(schema),foundationBody(migration),'fresh schema and migration body must stay identical');
const legacySchema=schema.slice(0,schema.indexOf(startMarker));

async function bootstrap(){
 const db=new PGlite({extensions:{pgcrypto}});
 await db.exec("create schema auth; create schema storage; create schema extensions; create role anon; create role authenticated; create table auth.users(id uuid primary key); create function auth.uid() returns uuid language sql stable as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$; create function auth.jwt() returns jsonb language sql stable as $$select jsonb_build_object('email',current_setting('request.jwt.claim.email',true))$$; create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint); create table storage.objects(id uuid,name text,bucket_id text); alter table storage.objects enable row level security; create function storage.foldername(text) returns text[] language sql as $$select string_to_array($1,'/')$$;");
 await db.exec('create role service_role bypassrls');
 return db;
}
async function grants(db){
 await db.exec('grant usage on schema public,auth,storage to authenticated,anon,service_role; grant select,insert,update,delete on all tables in schema public,storage to authenticated,anon; grant all on all tables in schema public,storage to service_role;');
}
async function resetRole(db){await db.exec('reset role');}
async function login(db,id,email){await resetRole(db);await db.query("select set_config('request.jwt.claim.sub',$1,false),set_config('request.jwt.claim.email',$2,false)",[id,email]);await db.exec('set role authenticated');}
async function asService(db){await resetRole(db);await db.exec('set role service_role');}
async function asAnon(db){await resetRole(db);await db.exec('set role anon');}
async function state(db,w){return (await db.query('select get_review_state($1) as s',[w])).rows[0].s;}
async function save(db,w,s){return (await db.query('select save_review_state($1,$2) as s',[w,s])).rows[0].s;}
const image=(id,name)=>({id,name,path:`${W1}/deck/${id}.jpg`});
const mainDeck={id:'deck-main',name:'主作品',title:'主作品',style:'蓝',origin:'import',images:[image('p1','p1.png'),image('p2','p2.png'),image('p3','p3.png')]};
const noteDeck={id:'deck-note-only',name:'笔记作品',title:'笔记作品',style:'米',origin:'import',images:[image('n1','n1.png')]};
const dualDeck={id:'deck-dual',name:'双轨作品',title:'双轨作品',style:'青',origin:'import',images:[image('d1','d1.png'),image('d2','d2.png')]};
const deckOnlyDeck={id:'deck-deck-only',name:'仅整套分作品',title:'仅整套分作品',style:'紫',origin:'import',images:[image('e1','e1.png')]};
const pageOnlyDeck={id:'deck-page-only',name:'仅页面分作品',title:'仅页面分作品',style:'棕',origin:'import',images:[image('f1','f1.png')]};
async function seedHistory(db){
 await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,'工作区一','owner@example.test',encode(extensions.digest('invite-one','sha256'),'hex'))",[W1]);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,'工作区二','other@example.test',encode(extensions.digest('invite-two','sha256'),'hex'))",[W2]);
 for(const id of [A,B,C])await db.query('insert into auth.users values($1)',[id]);
 await login(db,A,'owner@example.test');await db.query('select join_workspace($1,$2,$3)',[W1,'管理员','invite-one']);
 await login(db,B,'reviewer@example.test');await db.query('select join_workspace($1,$2,$3)',[W1,'普通评审','invite-one']);
 await login(db,C,'other@example.test');await db.query('select join_workspace($1,$2,$3)',[W2,'其他管理员','invite-two']);
 await login(db,A,'owner@example.test');
 await db.query('select add_review_deck($1,$2)',[W1,mainDeck]);await db.query('select add_review_deck($1,$2)',[W1,noteDeck]);await db.query('select add_review_deck($1,$2)',[W1,dualDeck]);await db.query('select add_review_deck($1,$2)',[W1,deckOnlyDeck]);await db.query('select add_review_deck($1,$2)',[W1,pageOnlyDeck]);
 let s=await state(db,W1);s.reviews['deck-main']={scores:{visual:4,layout:5},note:'整套评分',favorite:true};s.reviews['deck-note-only']={scores:{},note:'只有笔记',favorite:true};s.reviews['deck-dual']={mode:'deck',scores:{visual:4,layout:5},pages:{d1:{scores:{visual:2},note:'页面实际评分'},d2:{scores:{},note:'页面笔记占位'}}};s.reviews['deck-deck-only']={mode:'page',scores:{visual:3,layout:4},pages:{e1:{scores:{},note:'当前页面没有分数'}}};s.reviews['deck-page-only']={mode:'deck',scores:{},pages:{f1:{scores:{visual:1},note:'仅页面有分数'}}};await save(db,W1,s);
 await login(db,B,'reviewer@example.test');s=await state(db,W1);s.reviews['deck-main']={mode:'page',scores:{},pages:{p1:{scores:{visual:2},note:'第一页'},p2:{scores:{visual:3,layout:4}},p3:{scores:{},note:'第三页只有笔记'}}};await save(db,W1,s);
 await asService(db);
 await db.query("update reviews set data=jsonb_set(data,'{pages,ghost}','{\"scores\":{\"visual\":99},\"note\":\"legacy ghost\"}'::jsonb,true) where workspace_id=$1 and user_id=$2 and deck_id='deck-main'",[W1,B]);
 await resetRole(db);
}
async function fingerprint(db){
 await asService(db);
 const rows=(await db.query("select 'deck:'||id as key,data::text as value from decks where workspace_id=$1 union all select 'review:'||user_id||':'||deck_id,data::text from reviews where workspace_id=$1 union all select 'settings',coalesce((select settings::text from workspaces where id=$1),'') order by key",[W1])).rows;
 await resetRole(db);
 return rows;
}
function splitBucket(id){return crypto.createHash('md5').update(id).digest()[0]%5;}
async function snapshot(db,w){
 await asService(db);
 const value=(await db.query('select get_ai_review_sample_snapshot($1) as s',[w])).rows[0].s;
 await resetRole(db);
 return value;
}
async function setupMigrated(){
 const db=await bootstrap();await db.exec(legacySchema);await grants(db);
 await seedHistory(db);
 const before=await fingerprint(db);
 await db.exec(migration);await db.exec(migration);
 const after=await fingerprint(db);
 assert.deepEqual(after,before,'migration must preserve old manual history');
 await asService(db);
 const old=(await db.query("select source_kind,dedicated_judge_id is null as no_judge from decks d cross join workspaces w where d.workspace_id=$1 and d.id='deck-main' and w.id=$1",[W1])).rows[0];
 assert.equal(old.source_kind,'uploaded');assert.equal(old.no_judge,true);
 await resetRole(db);
 return db;
}
async function setupFresh(){
 const db=await bootstrap();await db.exec(schema);await grants(db);await seedHistory(db);return db;
}
async function setupClassification(){
 const db=await bootstrap();await db.exec(schema);await grants(db);
 await db.exec(reviewMigration);await db.exec(calibrationMigration);await db.exec(classificationMigration);await db.exec(summaryMigration);await db.exec(uploadedBaselineMigration);
 return db;
}
function classificationDeck(id,extra={}){
 return {id,name:id,title:id,origin:'import',images:[{id:`${id}-p1`,name:'p1.png',path:`${W1}/${id}/p1.png`}],...extra};
}
async function seedClassification(db){
 await db.query('insert into auth.users(id) values($1),($2)',[A,B]);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash,settings) values($1,'分类工作区','owner@example.test','hash',$2::jsonb)",[W1,JSON.stringify({revision:0,maxScore:10,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]})]);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'评委','admin'),($1,$3,'评审','reviewer')",[W1,A,B]);
 await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[A,W1]);
 const decks=[
  ['deck-class-human',classificationDeck('deck-class-human')],
  ['deck-class-ai',classificationDeck('deck-class-ai')],
  ['deck-class-existing',classificationDeck('deck-class-existing',{classification:{primary:'   ',label:' 既有分类 '},style:' 既有风格 '})],
  ['deck-class-rubric',classificationDeck('deck-class-rubric')],
 ];
 for(const [id,data] of decks)await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3,$4)',[id,W1,data,'uploaded']);
 await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3,$4)',['deck-class-private',W1,classificationDeck('deck-class-private',{classification:'生成内部分类',style:'生成内部风格'}),'generated']);
}
async function effectiveMeta(db,deckId){
 await asService(db);
 return (await db.query('select ai_review_effective_meta($1,$2) as meta',[W1,deckId])).rows[0].meta;
}
async function prepareClassificationAi(db,deckId){
 await asService(db);
 const prepared=(await db.query('select ai_review_prepare($1,$2,$3::jsonb) as result',[W1,A,JSON.stringify({deckId,scope:'deck'})])).rows[0].result;
 const job=prepared.jobs.find(item=>item.scope==='deck'&&item.variant==='baseline');
 assert.ok(job,'prepare must return the baseline deck job for uploaded works');
 const finished=(await db.query('select ai_review_finish_job($1,$2,$3,$4,$5::jsonb,$6) as result',[W1,A,job.jobId,'completed',JSON.stringify({classification:{primary:' AI 分类 ',label:'ignored',style:' AI 风格 ',internal:'hidden'},scores:{visual:7,layout:8},reasons:[]}),null])).rows[0].result;
 assert.equal(finished.status,'completed');
 return finished;
}
async function submitClassification(db,{deckId,scope='deck',units}){
 await asService(db);
 const versions=(await db.query('select ai_review_deck_version($1,$2) as deck_version,ai_review_rubric_version($1) as rubric_version',[W1,deckId])).rows[0];
 const result=(await db.query('select ai_review_submit($1,$2,$3::jsonb) as result',[W1,B,JSON.stringify({deckId,scope,deckVersion:versions.deck_version,rubricVersion:versions.rubric_version,units})])).rows[0].result;
 return {...result,submissionId:result.submissionIds[0]||null};
}
async function confirmClassification(db,{submissionId,recordId,classification,formalScores}){
 await asService(db);
 const payload={submissionId,formalScores,classification,reasons:[],reasonOther:'',summary:'分类有效元数据回归',status:'corrected',idempotencyKey:`classification-${crypto.randomUUID()}`};
 if(recordId)payload.aiRecordId=recordId;
 return (await db.query('select ai_review_confirm($1,$2,$3::jsonb) as result',[W1,A,JSON.stringify(payload)])).rows[0].result;
}
async function scoreSummary(db,w=W1,userId=B,email='reviewer@example.test'){
 await login(db,userId,email);
 const value=(await db.query('select review_score_summary($1) as summary',[w])).rows[0].summary;
 await resetRole(db);
 return value;
}
async function confirmFormal(db,{deckId,scope='deck',units,formalScores}){
 const submission=await submitClassification(db,{deckId,scope,units});
 const decision=await confirmClassification(db,{submissionId:submission.submissionId,formalScores});
 return {submissionId:submission.submissionId,decisionId:decision.decisionId,formalTotal:decision.formalTotal};
}
async function admitGenerated(db,submissionId,decisionId){
 await asService(db);
 return (await db.query('select ai_review_admit($1,$2,$3::jsonb) as result',[W1,A,JSON.stringify({submissionId,decisionId})])).rows[0].result;
}
async function exerciseFormalSummary(db){
 await resetRole(db);
 await db.query('insert into auth.users(id) values($1) on conflict do nothing',[C]);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,'其他空间','other@example.test','other-hash') on conflict do nothing",[W2]);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'其他管理员','admin') on conflict do nothing",[W2,C]);
 const decks={
  'deck-formal-overlap':{id:'deck-formal-overlap',name:'正式分叠加',title:'正式分叠加',images:[image('fo1','fo1.png'),image('fo2','fo2.png')]},
  'deck-formal-legacy':{id:'deck-formal-legacy',name:'仅旧均分',title:'仅旧均分',images:[image('fl1','fl1.png')]},
  'deck-formal-only':{id:'deck-formal-only',name:'仅正式分',title:'仅正式分',images:[image('fp1','fp1.png')]},
  'deck-formal-stale':{id:'deck-formal-stale',name:'版本失配',title:'版本失配',images:[image('fs1','fs1.png')]},
  'deck-formal-generated-pages':{id:'deck-formal-generated-pages',name:'生成单页',title:'生成单页',origin:'generated',images:[image('fg1','fg1.png'),image('fg2','fg2.png')]},
  'deck-formal-generated-whole':{id:'deck-formal-generated-whole',name:'生成整套',title:'生成整套',origin:'generated',images:[image('fw1','fw1.png'),image('fw2','fw2.png')]},
 };
 for(const [id,data] of Object.entries(decks)){
  const kind=id.includes('generated')?'generated':'uploaded';
  await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3,$4)',[id,W1,data,kind]);
 }
 await db.query("insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4)",[W1,A,'deck-formal-overlap',{mode:'deck',scores:{visual:6,layout:8},note:'旧团队评分'}]);
 await db.query("insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4)",[W1,A,'deck-formal-legacy',{mode:'deck',scores:{visual:6,layout:8},note:'仅旧均分'}]);

 const firstWhole=await confirmFormal(db,{deckId:'deck-formal-overlap',units:[{scores:{visual:8,layout:8}}],formalScores:{visual:8,layout:10}});
 let summary=await scoreSummary(db);

 assert.deepEqual(summary['deck-formal-legacy'],{count:1,mean:7});
 assert.deepEqual(Object.keys(summary['deck-formal-overlap']).sort(),['count','formalScore','mean']);
 assert.equal(summary['deck-formal-overlap'].count,2);
 assert.equal(summary['deck-formal-overlap'].mean,7.5);
 assert.equal(summary['deck-formal-overlap'].formalScore,9);
 await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[B,W1]);
 summary=await scoreSummary(db);
 assert.equal(summary['deck-formal-overlap'].formalScore,undefined,'formal scores require the current dedicated judge');
 assert.equal(summary['deck-formal-overlap'].count,2);
 assert.equal(summary['deck-formal-overlap'].mean,7.5);
 await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[A,W1]);
 const formalAcl=(await db.query("select has_function_privilege('service_role','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE') as service_ok,has_function_privilege('authenticated','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE') as authenticated_ok,has_function_privilege('anon','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE') as anon_ok")).rows[0];
 assert.deepEqual(formalAcl,{service_ok:true,authenticated_ok:false,anon_ok:false});
 await asService(db);
 const formalRows=(await db.query("select deck_id,scope,unit_id,score from ai_review_uploaded_formal_scores($1) where deck_id='deck-formal-overlap'",[W1])).rows;
 assert.equal(formalRows.length,1);
 assert.equal(formalRows[0].scope,'deck');
 assert.equal(formalRows[0].unit_id,'');
 assert.equal(Number(formalRows[0].score),9);

 await confirmFormal(db,{deckId:'deck-formal-overlap',units:[{scores:{visual:7,layout:7}}],formalScores:{visual:6,layout:8}});
 summary=await scoreSummary(db);
 assert.equal(summary['deck-formal-overlap'].count,2);
 assert.equal(summary['deck-formal-overlap'].mean,7);
 assert.equal(summary['deck-formal-overlap'].formalScore,7,'latest current whole-deck decision must win');

 const pageFormal=await confirmFormal(db,{deckId:'deck-formal-overlap',scope:'page',units:[{unitId:'fo1',position:1,scores:{visual:7,layout:9}}],formalScores:{visual:8,layout:10}});
 summary=await scoreSummary(db);
 assert.equal(summary['deck-formal-overlap'].count,1);
 assert.equal(summary['deck-formal-overlap'].mean,7);
 assert.equal(summary['deck-formal-overlap'].formalScore,7);
 assert.deepEqual(summary['deck-formal-overlap'].formalPageScores,{fo1:9});
 await db.query("update ai_review_decisions set expires_at=now()-interval '1 second' where id in ($1,$2)",[firstWhole.decisionId,pageFormal.decisionId]);
 summary=await scoreSummary(db);
 assert.equal(summary['deck-formal-overlap'].formalScore,7,'expired decisions must remain visible');
 assert.equal(summary['deck-formal-overlap'].formalPageScores.fo1,9,'expired page decisions must remain visible');

 const formalOnly=await confirmFormal(db,{deckId:'deck-formal-only',units:[{scores:{visual:8,layout:8}}],formalScores:{visual:8,layout:8}});
 summary=await scoreSummary(db);
 assert.deepEqual(summary['deck-formal-only'],{count:1,mean:8,formalScore:8});
 await db.query("update ai_review_decisions set formal_scores=formal_scores-'layout' where id=$1",[formalOnly.decisionId]);
 summary=await scoreSummary(db);
 assert.deepEqual(summary['deck-formal-only'],{count:1,mean:8},'formal scores require every current rubric dimension');
 await db.query("update ai_review_decisions set formal_scores='{\"visual\":8,\"layout\":8}'::jsonb where id=$1",[formalOnly.decisionId]);

 await confirmFormal(db,{deckId:'deck-formal-stale',units:[{scores:{visual:7,layout:8}}],formalScores:{visual:7,layout:8}});
 summary=await scoreSummary(db);
 assert.deepEqual(summary['deck-formal-stale'],{count:1,mean:7.5,formalScore:7.5});
 await db.query("update decks set data=data||$1::jsonb where workspace_id=$2 and id=$3",[JSON.stringify({note:'deck version changed'}),W1,'deck-formal-stale']);
 summary=await scoreSummary(db);
 assert.deepEqual(summary['deck-formal-stale'],{count:1,mean:7.5},'stale deck versions must not expose uploaded formal scores');

 const pageSubmission=await submitClassification(db,{deckId:'deck-formal-generated-pages',scope:'page',units:[{unitId:'fg1',position:1,scores:{visual:9,layout:8,match:9}}]});
 const pageDecision=await confirmClassification(db,{submissionId:pageSubmission.submissionId,formalScores:{visual:9,layout:8,match:9}});
 const pageAdmission=await admitGenerated(db,pageSubmission.submissionId,pageDecision.decisionId);
 assert.equal(pageAdmission.admitted,true);
 await submitClassification(db,{deckId:'deck-formal-generated-pages',scope:'page',units:[{unitId:'fg2',position:2,scores:{visual:10,layout:10,match:10}}]}).then(submission=>
  confirmClassification(db,{submissionId:submission.submissionId,formalScores:{visual:10,layout:10,match:10}}));
 summary=await scoreSummary(db);
 assert.deepEqual(Object.keys(summary['deck-formal-generated-pages']).sort(),['count','formalPageScores','mean']);
 assert.equal(summary['deck-formal-generated-pages'].count,0);
 assert.equal(summary['deck-formal-generated-pages'].mean,null);
 assert.deepEqual(summary['deck-formal-generated-pages'].formalPageScores,{fg1:8.7},'only admitted current pages may appear');

 await db.query("update decks set data=jsonb_set(data,'{images}',jsonb_build_array(data->'images'->1),false) where workspace_id=$1 and id=$2",[W1,'deck-formal-generated-pages']);
 summary=await scoreSummary(db);
 assert.equal(summary['deck-formal-generated-pages'],undefined,'stale generated pages must be removed');
 await db.query("update decks set data=jsonb_set(data,'{images}',data->'images' || $1::jsonb,true) where workspace_id=$2 and id=$3",[JSON.stringify([image('fg1','fg1.png')]),W1,'deck-formal-generated-pages']);

 const wholeSubmission=await submitClassification(db,{deckId:'deck-formal-generated-whole',units:[{scores:{visual:9,layout:8,match:9}}]});
 const wholeDecision=await confirmClassification(db,{submissionId:wholeSubmission.submissionId,formalScores:{visual:9,layout:8,match:9}});
 await admitGenerated(db,wholeSubmission.submissionId,wholeDecision.decisionId);
 summary=await scoreSummary(db);
 assert.equal(summary['deck-formal-generated-whole'].count,0);
 assert.equal(summary['deck-formal-generated-whole'].mean,null);
 assert.equal(summary['deck-formal-generated-whole'].formalScore,8.7);
 assert.equal(summary['deck-formal-generated-whole'].formalPageScores,undefined);
 assert.equal(summary['deck-class-private'],undefined,'unadmitted generated works must stay hidden');

 await db.query("update ai_review_decisions set expires_at=now()-interval '1 second' where id in ($1,$2)",[pageDecision.decisionId,wholeDecision.decisionId]);
 const beforeTotals=(await db.query('select id,formal_total from ai_review_decisions where id in ($1,$2) order by id',[pageDecision.decisionId,wholeDecision.decisionId])).rows;
 await db.query("update workspaces set settings=jsonb_set(settings,'{maxScore}','20'::jsonb,true) where id=$1",[W1]);
 summary=await scoreSummary(db);
 assert.equal(summary['deck-formal-generated-pages'].formalPageScores.fg1,17.4,'generated page scores must project to the current maximum');
 assert.equal(summary['deck-formal-generated-whole'].formalScore,17.4,'generated whole scores must project to the current maximum');
 assert.equal(summary['deck-formal-overlap'].formalScore,undefined,'uploaded formal scores require the current rubric version');
 assert.equal(summary['deck-formal-overlap'].count,1);
 assert.equal(summary['deck-formal-overlap'].mean,7);
 const afterTotals=(await db.query('select id,formal_total from ai_review_decisions where id in ($1,$2) order by id',[pageDecision.decisionId,wholeDecision.decisionId])).rows;
 assert.deepEqual(afterTotals,beforeTotals,'display projection must not rewrite stored formal totals');

 await asAnon(db);
 await assert.rejects(db.query('select review_score_summary($1)',[W1]),/permission denied/);
 const isolated=await scoreSummary(db,W1,C,'other@example.test');
 assert.deepEqual(isolated,{},'cross-workspace callers must not see another workspace summary');
 assert.equal(JSON.stringify(summary).includes(A),false);
 assert.equal(JSON.stringify(summary).includes(B),false);
 await db.query("update workspaces set settings=jsonb_set(settings,'{maxScore}','10'::jsonb,true) where id=$1",[W1]);
 await resetRole(db);
}
async function exerciseLegacyDelete(db){
 const generatedId='deck-generated',uploadedId='deck-forged';
 await asService(db);
 const seeded=await db.query(`
  insert into public.reviews(workspace_id,user_id,deck_id,data)
  select d.workspace_id,m.user_id,d.id,$4::jsonb
  from public.decks d join public.members m on m.workspace_id=d.workspace_id
  where d.workspace_id=$1 and d.id in ($2,$3)
  returning deck_id`,[W1,generatedId,uploadedId,{scores:{visual:2},note:'保留原始评审'}]);
 assert.deepEqual(seeded.rows.map(({deck_id})=>deck_id).sort(),[uploadedId,uploadedId,generatedId,generatedId]);
 const before=await fingerprint(db);
 await login(db,B,'reviewer@example.test');
 const reviewerRevision=(await state(db,W1)).revision;
 await login(db,A,'owner@example.test');
 const adminRevision=(await state(db,W1)).revision;
 const del=id=>db.query('select delete_review_deck($1,$2,$3) as revision',[W1,id,adminRevision]);
 await assert.rejects(del(generatedId),/生成作品不允许通过此接口删除/);
 assert.equal((await state(db,W1)).revision,adminRevision);
 assert.deepEqual(await fingerprint(db),before,'rejected delete must preserve the generated deck and all reviews');
 await login(db,B,'reviewer@example.test');
 assert.equal((await state(db,W1)).revision,reviewerRevision);
 await login(db,A,'owner@example.test');
 assert.equal((await del(uploadedId)).rows[0].revision,adminRevision+1);
 await login(db,B,'reviewer@example.test');
 assert.equal((await state(db,W1)).revision,reviewerRevision+1);
 assert.deepEqual(await fingerprint(db),before.filter(({key})=>key!==`deck:${uploadedId}`&&!key.endsWith(`:${uploadedId}`)),
  'uploaded delete must remove only the selected deck and its reviews');
}

async function exercise(db){
 await resetRole(db);
 const signatures=(await db.query("select to_regprocedure('public.get_review_state(uuid)') is not null as state,to_regprocedure('public.save_review_state(uuid,jsonb)') is not null as save,to_regprocedure('public.add_review_deck(uuid,jsonb)') is not null as add,to_regprocedure('public.delete_review_deck(uuid,text,bigint)') is not null as del,to_regprocedure('public.review_score_summary(uuid)') is not null as summary")).rows[0];
 assert.deepEqual(signatures,{state:true,save:true,add:true,del:true,summary:true});
 await asService(db);await assert.rejects(db.query('select require_dedicated_judge($1)',[W1]),/尚未指定/);
 const generated={id:'deck-generated',name:'生成作品',title:'生成作品',style:'AI',origin:'generated',images:[image('g1','g1.png')]};
 await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3,$4)',['deck-generated',W1,generated,'generated']);
 await resetRole(db);
 await login(db,A,'owner@example.test');
 const extended={...generated,images:[...generated.images,image('g2','g2.png')]};await assert.rejects(db.query('select add_review_deck($1,$2)',[W1,extended]),/生成作品仅允许服务端写入/);
 const forged={...mainDeck,id:'deck-forged',source_kind:'generated'};await db.query('select add_review_deck($1,$2)',[W1,forged]);
 let row=(await db.query("select source_kind,data from decks where id='deck-forged'")).rows[0];assert.equal(row.source_kind,'uploaded');assert.equal(row.data.source_kind,undefined);
 row=(await db.query("select source_kind,data from decks where id='deck-generated'")).rows[0];assert.equal(row.source_kind,'generated');assert.equal(row.data.images.length,1);
 const forgedAppend={...forged,images:[...forged.images,image('p4','p4.png')]};await db.query('select add_review_deck($1,$2)',[W1,forgedAppend]);row=(await db.query("select source_kind,data from decks where id='deck-forged'")).rows[0];assert.equal(row.source_kind,'uploaded');assert.equal(row.data.images.length,4);
 await assert.rejects(db.query("insert into decks(id,workspace_id,data,source_kind) values('browser-write',$1,$2,'generated')",[W1,generated]),/row-level/);
 const updated=(await db.query("update decks set source_kind='generated' where id='deck-main' returning id")).rows;assert.equal(updated.length,0);assert.equal((await db.query("select source_kind from decks where id='deck-main'")).rows[0].source_kind,'uploaded');
 await asService(db);
 for(let i=0;i<100;i++){
  const id='sample-'+String(i).padStart(3,'0');
  const d={id,name:id,title:id,style:'样本',origin:'generated',images:[{id:id+'-p1',name:'page.png',path:`${W1}/${id}/page.png`}]};
  await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3,$4)',[id,W1,d,'generated']);
 }
 await resetRole(db);
 await login(db,B,'reviewer@example.test');await assert.rejects(db.query('select set_dedicated_judge($1,$2)',[W1,B]),/仅管理员/);
 await login(db,A,'owner@example.test');await assert.rejects(db.query('select set_dedicated_judge($1,$2)',[W1,C]),/同工作区/);
 const assigned=(await db.query('select set_dedicated_judge($1,$2) as id',[W1,B])).rows[0].id;assert.equal(assigned,B);
 await asService(db);
 await assert.rejects(db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[C,W1]),/foreign key|violat/);
 assert.equal((await db.query('select require_dedicated_judge($1) as id',[W1])).rows[0].id,B);
 await resetRole(db);
 await login(db,B,'reviewer@example.test');await assert.rejects(db.query('select get_ai_review_sample_snapshot($1)',[W1]),/permission/);
 await db.exec('reset role;set role anon');await assert.rejects(db.query('select get_ai_review_sample_snapshot($1)',[W1]),/permission/);
 await resetRole(db);
 const one=await snapshot(db,W1),two=await snapshot(db,W1);
 assert.deepEqual(one,two,'snapshot must be stable across calls');
 assert.equal(one.schemaVersion,1);assert.equal(one.workspaceId,W1);assert.equal(one.splitAlgorithm,'md5-first-byte-mod-5-v1');assert.equal(one.dedicatedJudgeId,B);
 assert.equal(one.deckCount,one.decks.length);assert.equal(one.ratedDeckCount,4);assert.equal(one.unratedDeckCount,one.deckCount-4);
 assert.deepEqual(one.calibrationDeckIds.slice().sort(),one.decks.filter(d=>d.bucket!==0).map(d=>d.deckId).sort());
 assert.deepEqual(one.holdoutDeckIds.slice().sort(),one.decks.filter(d=>d.bucket===0).map(d=>d.deckId).sort());
 assert.ok(one.holdoutDeckIds.length/one.deckCount>=0.1&&one.holdoutDeckIds.length/one.deckCount<=0.3,'holdout should be about 20%');
 for(const d of one.decks){
  const expected=splitBucket(d.deckId);assert.equal(d.bucket,expected);assert.equal(d.split,expected===0?'holdout':'calibration');
  if(d.deckId==='deck-main')assert.equal(d.sourceKind,'uploaded');
  if(d.deckId==='deck-generated')assert.equal(d.sourceKind,'generated');
 }
 const main=one.decks.find(d=>d.deckId==='deck-main'),note=one.decks.find(d=>d.deckId==='deck-note-only');
 assert.equal(main.ratingStatus,'partial');assert.equal(main.ratedReviewCount,2);assert.equal(main.completeReviewCount,1);assert.equal(main.unratedReviewCount,0);
 const adminReview=main.reviews.find(r=>r.userId===A),pageReview=main.reviews.find(r=>r.userId===B);
 assert.equal(adminReview.mode,'deck');assert.equal(adminReview.ratingStatus,'complete');assert.equal(adminReview.units.length,1);assert.equal(adminReview.units[0].unitType,'deck');assert.equal(adminReview.units[0].hasScores,true);assert.deepEqual(adminReview.maxScore,one.settings.maxScore);assert.deepEqual(adminReview.dimensions,one.settings.dimensions);assert.deepEqual(adminReview.original.scores,{visual:4,layout:5});
 assert.equal(pageReview.mode,'page');assert.equal(pageReview.ratingStatus,'partial');assert.equal(pageReview.units.length,3);assert.deepEqual(pageReview.units.map(u=>u.imageId),mainDeck.images.map(i=>i.id));assert.ok(pageReview.original.pages.ghost);assert.ok(!pageReview.units.some(u=>u.unitId==='ghost'));for(let i=0;i<3;i++){assert.deepEqual(pageReview.units[i].image,mainDeck.images[i]);assert.equal(pageReview.units[i].position,i+1);}assert.equal(pageReview.units[2].hasScores,false);assert.equal(pageReview.units[2].scores.visual,undefined);assert.deepEqual(pageReview.otherScopeUnits,[]);
 const dual=one.decks.find(d=>d.deckId==='deck-dual'),deckOnly=one.decks.find(d=>d.deckId==='deck-deck-only'),pageOnly=one.decks.find(d=>d.deckId==='deck-page-only');
 const dualReview=dual.reviews[0];assert.equal(dualReview.units[0].unitType,'deck');assert.deepEqual(dualReview.units[0].scores,{visual:4,layout:5});assert.deepEqual(dualReview.original.pages.d1.scores,{visual:2});assert.equal(dualReview.anyHasScores,true);assert.equal(dualReview.overallRatingStatus,'complete');assert.equal(dualReview.ratingStatus,'complete');assert.equal(dualReview.otherScope,'page');assert.equal(dualReview.otherScopeHasScores,true);assert.deepEqual(dualReview.otherScopeUnits.map(u=>u.unitId),['d1']);assert.ok(!dualReview.otherScopeUnits.some(u=>u.unitId==='d2'));assert.equal(dualReview.otherScopeUnits[0].scores.visual,2);assert.equal(dualReview.original.pages.d2.note,'页面笔记占位');
 const deckOnlyReview=deckOnly.reviews[0];assert.equal(deckOnlyReview.mode,'page');assert.equal(deckOnlyReview.ratingStatus,'unrated');assert.equal(deckOnlyReview.overallRatingStatus,'complete');assert.equal(deckOnlyReview.units[0].hasScores,false);assert.equal(deckOnlyReview.otherScope,'deck');assert.deepEqual(deckOnlyReview.otherScopeUnits.map(u=>u.unitType),['deck']);assert.deepEqual(deckOnlyReview.otherScopeUnits[0].scores,{visual:3,layout:4});assert.equal(deckOnlyReview.anyHasScores,true);assert.equal(deckOnly.ratingStatus,'complete');assert.equal(deckOnly.ratedReviewCount,1);
 const pageOnlyReview=pageOnly.reviews[0];assert.equal(pageOnlyReview.mode,'deck');assert.equal(pageOnlyReview.ratingStatus,'unrated');assert.equal(pageOnlyReview.overallRatingStatus,'partial');assert.equal(pageOnlyReview.units[0].hasScores,false);assert.equal(pageOnlyReview.otherScope,'page');assert.deepEqual(pageOnlyReview.otherScopeUnits.map(u=>u.unitId),['f1']);assert.deepEqual(pageOnlyReview.otherScopeUnits[0].scores,{visual:1});assert.equal(pageOnlyReview.anyHasScores,true);assert.equal(pageOnly.ratingStatus,'partial');assert.equal(pageOnly.ratedReviewCount,1);
 assert.equal(note.ratingStatus,'unrated');assert.equal(note.ratedReviewCount,0);assert.equal(note.reviews.length,1);assert.equal(note.reviews[0].hasScores,false);assert.equal(note.reviews[0].anyHasScores,false);assert.equal(note.reviews[0].ratingStatus,'unrated');assert.equal(note.reviews[0].overallRatingStatus,'unrated');assert.equal(note.reviews[0].units[0].hasScores,false);assert.equal(note.reviews[0].original.note,'只有笔记');
 const dbBefore=await fingerprint(db);await snapshot(db,W1);const dbAfter=await fingerprint(db);assert.deepEqual(dbAfter,dbBefore,'snapshot must not mutate data');
 await exerciseLegacyDelete(db);
 return one;
}
async function exerciseEffectiveMeta(db){
 await seedClassification(db);
 await exerciseFormalSummary(db);
 const acl=(await db.query("select has_function_privilege('service_role','public.ai_review_effective_meta(uuid,text)','EXECUTE') as service_ok,has_function_privilege('authenticated','public.ai_review_effective_meta(uuid,text)','EXECUTE') as authenticated_ok,has_function_privilege('anon','public.ai_review_effective_meta(uuid,text)','EXECUTE') as anon_ok")).rows[0];
 assert.deepEqual(acl,{service_ok:true,authenticated_ok:false,anon_ok:false});
 await login(db,B,'reviewer@example.test');
 await assert.rejects(db.query('select ai_review_effective_meta($1,$2)',[W1,'deck-class-human']),/permission denied/);
 const library=(await db.query('select get_review_library($1) as library',[W1])).rows[0].library;
 assert.ok(library.decks.some(deck=>deck.id==='deck-class-human'),'definer library must still project uploaded effective metadata');
 assert.ok(!library.decks.some(deck=>deck.id==='deck-class-private'),'unadmitted generated deck must stay behind the library gate');
 await asAnon(db);
 await assert.rejects(db.query('select ai_review_effective_meta($1,$2)',[W1,'deck-class-human']),/permission denied/);

 const humanRecord=await prepareClassificationAi(db,'deck-class-human');
 const none={effectiveClassification:null,effectiveStyle:null,classificationSource:'none',styleSource:'none'};
 let meta=await effectiveMeta(db,'deck-class-human');
 assert.deepEqual(meta,none,'AI classification must stay hidden before a valid deck submission');

 const first=await submitClassification(db,{deckId:'deck-class-human',units:[{scores:{visual:8,layout:8},classificationCorrection:{primary:' 人工分类 ',label:'ignored',style:' 人工风格 ',internal:'hidden'}}]});
 assert.ok(first.submissionId);
 meta=await effectiveMeta(db,'deck-class-human');
 assert.deepEqual(meta,{effectiveClassification:'人工分类',effectiveStyle:'人工风格',classificationSource:'human',styleSource:'human'});

 await submitClassification(db,{deckId:'deck-class-human',units:[{scores:{visual:9,layout:9}}]});
 meta=await effectiveMeta(db,'deck-class-human');
 assert.equal(meta.effectiveClassification,'人工分类','score-only update must not clear human classification');
 assert.equal(meta.effectiveStyle,'人工风格','score-only update must not clear human style');

 await submitClassification(db,{deckId:'deck-class-human',units:[{scores:{visual:8,layout:10},classificationCorrection:{label:'   ',style:' 更新风格 ',internal:'hidden'}}]});
 meta=await effectiveMeta(db,'deck-class-human');
 assert.equal(meta.effectiveClassification,'人工分类','empty classification must not replace the last non-empty value');
 assert.equal(meta.effectiveStyle,'更新风格','style must be selected independently from classification');

 const stringClass=await submitClassification(db,{deckId:'deck-class-human',units:[{scores:{visual:9,layout:8},classificationCorrection:' 人工分类（字符串） '}]});
 meta=await effectiveMeta(db,'deck-class-human');
 assert.equal(meta.effectiveClassification,'人工分类（字符串）','string corrections must be trimmed');
 assert.equal(meta.effectiveStyle,'更新风格','a correction without style must preserve the last non-empty style');

 const page=await submitClassification(db,{deckId:'deck-class-human',scope:'page',units:[{unitId:'deck-class-human-p1',position:1,scores:{visual:8,layout:8},classificationCorrection:{label:' 页级分类 ',style:' 页级风格 '}}]});
 meta=await effectiveMeta(db,'deck-class-human');
 assert.equal(meta.effectiveClassification,'人工分类（字符串）','page correction must not override deck classification');
 assert.equal(meta.effectiveStyle,'更新风格','page correction must not override deck style');

 const formal=await confirmClassification(db,{submissionId:stringClass.submissionId,recordId:humanRecord.recordId,classification:{label:' 正式分类 ',style:' 正式风格 ',secret:'hidden'},formalScores:{visual:9,layout:9}});
 assert.ok(formal.decisionId);
 meta=await effectiveMeta(db,'deck-class-human');
 assert.deepEqual(meta,{effectiveClassification:'正式分类',effectiveStyle:'正式风格',classificationSource:'human',styleSource:'human'});
 assert.deepEqual(Object.keys(meta).sort(),['classificationSource','effectiveClassification','effectiveStyle','styleSource']);

 const late=await submitClassification(db,{deckId:'deck-class-human',units:[{scores:{visual:7,layout:7},classificationCorrection:{label:' 后期人工分类 ',style:' 后期人工风格 '}}]});
 meta=await effectiveMeta(db,'deck-class-human');
 assert.deepEqual(meta,{effectiveClassification:'正式分类',effectiveStyle:'正式风格',classificationSource:'human',styleSource:'human'},'dedicated judge deck decision must outrank newer ordinary submissions');

 const pageFormal=await confirmClassification(db,{submissionId:page.submissionId,classification:{label:' 页面正式分类 ',style:' 页面正式风格 '},formalScores:{visual:8,layout:8}});
 assert.ok(pageFormal.decisionId);
 meta=await effectiveMeta(db,'deck-class-human');
 assert.equal(meta.effectiveClassification,'正式分类','generated-style page decisions must not override the deck');
 assert.equal(meta.effectiveStyle,'正式风格','page formal style must not override the deck');

 await db.query("update ai_review_decisions set expires_at=now()-interval '1 second' where id=$1",[formal.decisionId]);
 meta=await effectiveMeta(db,'deck-class-human');
 assert.deepEqual(meta,{effectiveClassification:'正式分类',effectiveStyle:'正式风格',classificationSource:'human',styleSource:'human'},'formal classification must persist after its admission deadline');

 await prepareClassificationAi(db,'deck-class-existing');
 meta=await effectiveMeta(db,'deck-class-existing');
 assert.deepEqual(meta,{effectiveClassification:'既有分类',effectiveStyle:'既有风格',classificationSource:'existing',styleSource:'existing'});

 await prepareClassificationAi(db,'deck-class-ai');
 meta=await effectiveMeta(db,'deck-class-ai');
 assert.deepEqual(meta,none,'AI fallback must remain hidden without a valid deck submission');
 await submitClassification(db,{deckId:'deck-class-ai',scope:'page',units:[{unitId:'deck-class-ai-p1',position:1,scores:{visual:8,layout:8},classificationCorrection:{label:' 页级解锁尝试 ',style:' 页级风格 '}}]});
 meta=await effectiveMeta(db,'deck-class-ai');
 assert.deepEqual(meta,none,'a valid page submission must not unlock deck AI metadata');
 await submitClassification(db,{deckId:'deck-class-ai',units:[{scores:{visual:8,layout:9}}]});
 meta=await effectiveMeta(db,'deck-class-ai');
 assert.deepEqual(meta,{effectiveClassification:'AI 分类',effectiveStyle:'AI 风格',classificationSource:'ai',styleSource:'ai'});

 await db.query("update decks set data=data||$1::jsonb where workspace_id=$2 and id=$3",[JSON.stringify({note:'deck version changed'}),W1,'deck-class-ai']);
 meta=await effectiveMeta(db,'deck-class-ai');
 assert.deepEqual(meta,none,'AI fallback must require the current deck version');

 await prepareClassificationAi(db,'deck-class-rubric');
 await submitClassification(db,{deckId:'deck-class-rubric',units:[{scores:{visual:8,layout:8}}]});
 meta=await effectiveMeta(db,'deck-class-rubric');
 assert.equal(meta.classificationSource,'ai');
 await db.query("update workspaces set settings=jsonb_set(settings,'{dimensions}',(settings->'dimensions')||'[{\"id\":\"match\",\"name\":\"匹配\"}]'::jsonb,true) where id=$1",[W1]);
 meta=await effectiveMeta(db,'deck-class-rubric');
 assert.deepEqual(meta,none,'AI fallback must require the current rubric version and complete deck scores');

 await asService(db);
 const calibration=(await db.query('select ai_review_calibration($1,$2,$3,$4,$5,$6) as result',[W1,A,'uploaded',1,'deck-class-human','deck'])).rows[0].result;
 assert.equal(calibration.kind,'uploaded','service-role calibration must still call effective metadata through its definer');
}
function idForBucket(prefix,bucket){
 for(let i=0;i<10000;i++){
  const id=`${prefix}-${i}`;
  if(splitBucket(id)===bucket)return id;
 }
 throw new Error(`could not find deck id for bucket ${bucket}`);
}
async function setupUploadedBaselinePolicy(){
 const db=await bootstrap();await db.exec(schema);await grants(db);await db.exec(reviewMigration);
 await db.query('insert into auth.users(id) values($1),($2)',[A,B]);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash,settings) values($1,'baseline policy','owner@example.test','policy',$2::jsonb)",[W1,JSON.stringify({revision:0,maxScore:10,dimensions:[{id:'visual',name:'Visual'},{id:'layout',name:'Layout'}]})]);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'Judge','admin'),($1,$3,'Reviewer','reviewer')",[W1,A,B]);
 await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[A,W1]);
 return db;
}
async function addPolicyDeck(db,id,kind){
 await asService(db);
 const data=classificationDeck(id,{classification:`class ${id}`,style:'policy'});
 await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3::jsonb,$4)',[id,W1,JSON.stringify(data),kind]);
}
async function preparePolicyDeck(db,id){
 await asService(db);
 return (await db.query('select ai_review_prepare($1,$2,$3::jsonb) as value',[W1,A,JSON.stringify({deckId:id,scope:'deck'})])).rows[0].value;
}
async function policyJobs(db,id){
 await asService(db);
 return (await db.query('select id,variant,status,split,bucket from ai_review_jobs where workspace_id=$1 and deck_id=$2 and scope=$3 order by variant,id',[W1,id,'deck'])).rows;
}
async function exerciseUploadedBaselinePolicy(db){
 const legacyCompleted=idForBucket('legacy-completed',1);
 const legacyPending=idForBucket('legacy-pending',2);
 const uploadedHoldout=idForBucket('legacy-uploaded-holdout',0);
 const generatedCalibration=idForBucket('legacy-generated-calibration',3);
 const generatedHoldout=idForBucket('legacy-generated-holdout',0);
 const preDecks=[
  [legacyCompleted,'uploaded'],[legacyPending,'uploaded'],[uploadedHoldout,'uploaded'],
  [generatedCalibration,'generated'],[generatedHoldout,'generated']
 ];
 for(const [id,kind] of preDecks){await addPolicyDeck(db,id,kind);await preparePolicyDeck(db,id);}

 const completedAssisted=(await policyJobs(db,legacyCompleted)).find(job=>job.variant==='assisted');
 assert.ok(completedAssisted,'legacy completed fixture must start with assisted work');
 await asService(db);
 const finished=(await db.query('select ai_review_finish_job($1,$2,$3,$4,$5::jsonb,$6) as result',[
  W1,A,completedAssisted.id,'completed',JSON.stringify({scores:{visual:6,layout:7},modelHint:'legacy-assisted',summary:'legacy evidence'}),null
 ])).rows[0].result;
 assert.equal(finished.status,'completed');
 const completedRecordBefore=(await db.query('select ai_result,model_hint from ai_review_records where id=$1',[finished.recordId])).rows[0];
 const pendingAssisted=(await policyJobs(db,legacyPending)).find(job=>job.variant==='assisted');
 assert.ok(pendingAssisted,'legacy pending fixture must queue assisted work');
 assert.equal(pendingAssisted.status,'queued');

 await resetRole(db);
 await db.exec(uploadedBaselineMigration);
 for(const [id] of preDecks)await preparePolicyDeck(db,id);

 const newUploadedCalibration=idForBucket('new-uploaded-calibration',1);
 const newUploadedHoldout=idForBucket('new-uploaded-holdout',0);
 const newGeneratedCalibration=idForBucket('new-generated-calibration',2);
 const newGeneratedHoldout=idForBucket('new-generated-holdout',0);
 for(const [id,kind] of [[newUploadedCalibration,'uploaded'],[newUploadedHoldout,'uploaded'],[newGeneratedCalibration,'generated'],[newGeneratedHoldout,'generated']]){
  await addPolicyDeck(db,id,kind);await preparePolicyDeck(db,id);
 }

 const completedAfter=await policyJobs(db,legacyCompleted);
 assert.deepEqual(completedAfter.map(job=>job.variant),['assisted','baseline']);
 assert.equal(completedAfter.find(job=>job.variant==='assisted').id,completedAssisted.id,'legacy assisted job id must not be rewritten');
 assert.equal(completedAfter.find(job=>job.variant==='assisted').status,'completed','completed assisted evidence must stay readable');
 assert.equal(completedAfter.find(job=>job.variant==='baseline').status,'queued');
 const completedRecordAfter=(await db.query('select ai_result,model_hint from ai_review_records where id=$1',[finished.recordId])).rows[0];
 assert.deepEqual(completedRecordAfter,completedRecordBefore,'migration and re-prepare must not overwrite historical assisted output');

 const pendingAfter=await policyJobs(db,legacyPending);
 assert.deepEqual(pendingAfter.map(job=>job.variant),['assisted','baseline']);
 assert.equal(pendingAfter.find(job=>job.variant==='assisted').id,pendingAssisted.id);
 assert.equal(pendingAfter.find(job=>job.variant==='assisted').status,'queued','pending legacy assisted work must not be cancelled');
 assert.equal(pendingAfter.find(job=>job.variant==='baseline').status,'queued','legacy assisted work must not block baseline preparation');

 const holdoutUploadedJobs=await policyJobs(db,uploadedHoldout);
 assert.deepEqual(holdoutUploadedJobs.map(job=>job.variant),['assisted','baseline']);
 assert.ok(holdoutUploadedJobs.every(job=>job.split==='holdout'));
 const oldGeneratedJobs=await policyJobs(db,generatedCalibration);
 assert.deepEqual(oldGeneratedJobs.map(job=>job.variant),['assisted'],'generated calibration policy must remain assisted-only');
 const oldGeneratedHoldoutJobs=await policyJobs(db,generatedHoldout);
 assert.deepEqual(oldGeneratedHoldoutJobs.map(job=>job.variant),['assisted','baseline'],'generated holdout must keep both variants');

 const newUploadedJobs=await policyJobs(db,newUploadedCalibration);
 assert.deepEqual(newUploadedJobs.map(job=>({variant:job.variant,split:job.split})),[{variant:'baseline',split:'calibration'}],'new uploaded calibration works must default to baseline');
 const newUploadedHoldoutJobs=await policyJobs(db,newUploadedHoldout);
 assert.deepEqual(newUploadedHoldoutJobs.map(job=>({variant:job.variant,split:job.split})),[{variant:'assisted',split:'holdout'},{variant:'baseline',split:'holdout'}]);
 const newGeneratedJobs=await policyJobs(db,newGeneratedCalibration);
 assert.deepEqual(newGeneratedJobs.map(job=>({variant:job.variant,split:job.split})),[{variant:'assisted',split:'calibration'}],'new generated calibration works must keep assisted behavior');
 const newGeneratedHoldoutJobs=await policyJobs(db,newGeneratedHoldout);
 assert.deepEqual(newGeneratedHoldoutJobs.map(job=>({variant:job.variant,split:job.split})),[{variant:'assisted',split:'holdout'},{variant:'baseline',split:'holdout'}]);
}
(async()=>{
 const migrated=await setupMigrated(),fresh=await setupFresh();
 const migratedSnapshot=await exercise(migrated),freshSnapshot=await exercise(fresh);
 assert.deepEqual(migratedSnapshot,freshSnapshot,'migrated and fresh schemas must return identical snapshots');
 const classification=await setupClassification();await exerciseEffectiveMeta(classification);
 const baselinePolicy=await setupUploadedBaselinePolicy();await exerciseUploadedBaselinePolicy(baselinePolicy);
 await migrated.close();await fresh.close();await classification.close();await baselinePolicy.close();
 console.log('PASS: legacy preservation, dual-scope ratings, generated write/delete guards, uploaded delete compatibility, admin judge assignment, service-only trusted snapshot, real-page ownership and stable 20% split; deck-scope effective classification, formal priority, AI blind fallback, version/rubric gates, formal summary projection/scaling and service-only metadata; uploaded/generated x holdout/calibration variant routing, legacy assisted preservation, and pending-work compatibility');
})().catch(e=>{console.error(e.stack||e.message,e.where||'');process.exit(1);});
