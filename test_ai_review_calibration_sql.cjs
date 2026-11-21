// npm install --prefix .test-output/sql @electric-sql/pglite
// Real PGlite fixture for legacy 01->06 databases, 07 calibration, 13 formal correction, and isolation.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');

const W1='11111111-1111-4111-8111-111111111111';
const W2='22222222-2222-4222-8222-222222222222';
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const C='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');
const migrations=[
 '20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning',
 '20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history',
 '20261121_07_review_calibration'
].map(name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));
const calibrationDdl=migrations[6];
const formalCorrectionDdl=fs.readFileSync('cloud/migrations/20261121_13_formal_correction.sql','utf8');
const maxScore=10;
const settings={revision:0,maxScore,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'}]};

function bucketOf(id){return parseInt(crypto.createHash('md5').update(id).digest('hex').slice(0,2),16)%5;}
function idForBucket(prefix,bucket){
 for(let index=0;index<10000;index++){
  const id=`${prefix}-${index}`;
  if(bucketOf(id)===bucket)return id;
 }
 throw new Error(`Could not create id in bucket ${bucket}`);
}

async function bootstrap(through){
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
  create function storage.foldername(text) returns text[] language sql as $$select string_to_array($1,'/')$$;
 `);
 await db.exec(legacySchema);
 await db.exec('grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role; grant all on all tables in schema public,storage to service_role;');
 for(let index=0;index<through;index++)await db.exec(migrations[index]);
 return db;
}
async function asService(db){await db.exec('reset role');await db.exec('set role service_role');}
async function asAuthenticated(db){await db.exec('reset role');await db.exec('set role authenticated');}
async function asAnon(db){await db.exec('reset role');await db.exec('set role anon');}
async function asOwner(db){await db.exec('reset role');}

async function insertDeck(db,id,workspace,options={}){
 const source=options.source||'uploaded';
 const style=Object.prototype.hasOwnProperty.call(options,'style')?options.style:'蓝';
 const classification=Object.prototype.hasOwnProperty.call(options,'classification')?options.classification:'汇报';
 const imageInput=options.images||[{id:'p1',name:'p1.png'}];
 const images=imageInput.map((image,index)=>({
  id:image.id,
  name:image.name,
  path:image.path||`${workspace}/${id}/${image.name||`page-${index+1}.png`}`,
  thumbPath:image.thumbPath||`${workspace}/${id}/${image.name||`page-${index+1}.png`}`
 }));
 const data={id,name:id,title:id,origin:source==='generated'?'generated':'import',images};
 if(style!==null&&style!==undefined)data.style=style;
 if(classification!==null&&classification!==undefined)data.classification=classification;
 if(source==='generated')data.generation={outline:{title:id},style:{name:style,summary:'测试生成风格'},pages:images.map((image,index)=>({...image,position:index+1})),references:[]};
 await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3::jsonb,$4)',[id,workspace,JSON.stringify(data),source]);
 return data;
}
async function insertReview(db,workspace,user,deckId,data){
 await db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4::jsonb)',[workspace,user,deckId,JSON.stringify(data)]);
}
async function seedJobRecord(db,{workspace=W1,deckId,kind,scope,unitId='',scores,variant='assisted',bucket=1,split='calibration'}){
 const versions=(await db.query('select ai_review_deck_version($1,$2) deck_version,ai_review_rubric_version($1) rubric_version',[workspace,deckId])).rows[0];
 const jobId=crypto.randomUUID(),recordId=crypto.randomUUID();
 await db.query(`insert into ai_review_jobs(id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score,status,attempts,judge_user_id)
  values($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,$11,$12,'completed',1,$13)`,[jobId,workspace,deckId,kind,scope,unitId,variant,bucket,split,versions.deck_version,versions.rubric_version,maxScore,A]);
 await db.query(`insert into ai_review_records(id,job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,deck_version,rubric_version,max_score,model_hint,ai_result,judge_user_id)
  values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'test',$14::jsonb,$15)`,[recordId,jobId,workspace,deckId,kind,scope,unitId,variant,bucket,split,versions.deck_version,versions.rubric_version,maxScore,JSON.stringify({scores,classification:{label:'模型分类'},reasons:[]}),A]);
 return {jobId,recordId,...versions};
}
async function seedMatchedSubmission(db,{workspace=W1,deckId,kind,scope,unitId='',scores,human=B,entryExtra={},bucket=1,split='calibration'}){
 const job=await seedJobRecord(db,{workspace,deckId,kind,scope,unitId,scores,bucket,split});
 const submissionId=crypto.randomUUID();
 await db.query(`insert into ai_review_submissions(id,workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,deck_version,rubric_version,human_version,max_score,entry)
  values($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,$11,$12::jsonb)`,[submissionId,workspace,deckId,kind,scope,unitId,human,job.deck_version,job.rubric_version,`human-${crypto.randomUUID()}`,maxScore,JSON.stringify({scores,...entryExtra})]);
 await db.query("insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,'assisted')",[submissionId,job.recordId]);
 return {...job,submissionId};
}
async function seedUnmatchedSubmission(db,{workspace=W1,deckId,kind,scope,unitId='',scores,human=B,bucket=1,split='calibration'}){
 const job=await seedJobRecord(db,{workspace,deckId,kind,scope,unitId,scores,bucket,split});
 const submissionId=crypto.randomUUID();
 await db.query(`insert into ai_review_submissions(id,workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,deck_version,rubric_version,human_version,max_score,entry)
  values($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,$11,$12::jsonb)`,[submissionId,workspace,deckId,kind,scope,unitId,human,job.deck_version,job.rubric_version,`human-${crypto.randomUUID()}`,maxScore,JSON.stringify({scores})]);
 return {...job,submissionId};
}
async function insertDirectDecision(db,{workspace=W1,deckId,submission,decisionMaxScore=maxScore,key=crypto.randomUUID()}){
 const decisionId=crypto.randomUUID();
 await db.query(`insert into ai_review_decisions(id,workspace_id,deck_id,submission_id,ai_record_id,judge_user_id,decision_key,status,classification,reasons,reason_other,formal_scores,formal_total,deck_version,rubric_version,max_score,score_ratio,summary)
  values($1,$2,$3,$4,$5,$6,$7,'corrected',$8::jsonb,'[]'::jsonb,'',$9::jsonb,8.5,$10,$11,$12,0.85,'direct fixture')`,[decisionId,workspace,deckId,submission.submissionId,submission.recordId,A,key,JSON.stringify({label:'直接测试'}),JSON.stringify({visual:8,layout:9}),submission.deck_version,submission.rubric_version,decisionMaxScore]);
 return decisionId;
}
async function confirmCorrection(db,submission,scores,key){
 const payload={submissionId:submission.submissionId,aiRecordId:submission.recordId,formalScores:scores,classification:{label:'评委正式分类'},reasons:['hierarchy'],reasonOther:'',summary:'评委确认的正式纠偏',status:'corrected',idempotencyKey:key};
 const result=(await db.query('select ai_review_confirm($1,$2,$3::jsonb) result',[W1,A,JSON.stringify(payload)])).rows[0].result;
 return result.decisionId;
}
async function confirmFormal(db,submission,extra={}){
 await asService(db);
 const payload={submissionId:submission.submissionId,aiRecordId:submission.recordId,...extra};
 return (await db.query('select ai_review_confirm($1,$2,$3::jsonb) result',[W1,A,JSON.stringify(payload)])).rows[0].result;
}
async function calibrate(db,{w=W1,actor=A,kind='uploaded',limit=6,deckId,scope='deck'}){
 await asService(db);
 return (await db.query('select ai_review_calibration($1,$2,$3,$4,$5,$6) result',[w,actor,kind,limit,deckId,scope])).rows[0].result;
}

async function seedFixture(db){
 await asOwner(db);
 for(const id of [A,B,C])await db.query('insert into auth.users(id) values($1)',[id]);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash,settings) values($1,'工作区一','one@example.test','hash',$2::jsonb)",[W1,JSON.stringify(settings)]);
 await db.query("insert into workspaces(id,name,owner_email,invite_hash,settings) values($1,'工作区二','two@example.test','hash',$2::jsonb)",[W2,JSON.stringify(settings)]);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'评委','admin'),($1,$3,'成员','reviewer')",[W1,A,B]);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'另一评委','admin')",[W2,C]);

 const targetUpload='target-upload';
 const sameMeta=idForBucket('up-same-meta',1);
 const otherMeta=idForBucket('up-other-meta',2);
 const unknownMeta=idForBucket('up-unknown-meta',2);
 const pageDeck=idForBucket('up-page-meta',3);
 const incomplete=idForBucket('up-incomplete',4);
 const holdout=idForBucket('up-holdout',0);
 const generatedContam=idForBucket('up-generated-contam',1);
 const newDeck=idForBucket('up-new',2);
 const targetGenerated='target-generated';
 const generatedSample=idForBucket('gen-sample',1);
 const generatedHoldout=idForBucket('gen-holdout',0);
 const correctionOld=idForBucket('corr-old',2);
 const correctionNew=idForBucket('corr-new',3);
 const badNoMatch=idForBucket('bad-no-match',4);
 const badScale=idForBucket('bad-scale',1);

 await insertDeck(db,targetUpload,W1,{images:[{id:'t1',name:'target.png'}]});
 await insertDeck(db,sameMeta,W1,{images:[{id:'s1',name:'same.png'}]});
 await insertDeck(db,otherMeta,W1,{style:'红',classification:'数据',images:[{id:'o1',name:'other.png'}]});
 await insertDeck(db,unknownMeta,W1,{style:null,classification:null,images:[{id:'u1',name:'unknown.png'}]});
 await insertDeck(db,pageDeck,W1,{images:[{id:'p1',name:'page-1.png'},{id:'p2',name:'page-2.png'}]});
 await insertDeck(db,incomplete,W1,{images:[{id:'i1',name:'incomplete.png'}]});
 await insertDeck(db,holdout,W1,{images:[{id:'h1',name:'holdout.png'}]});
 await insertDeck(db,generatedContam,W1,{source:'generated',images:[{id:'g1',name:'generated.png'}]});
 await insertDeck(db,newDeck,W1,{images:[{id:'n1',name:'new.png'}]});
 await insertDeck(db,targetGenerated,W1,{source:'generated',style:'AI蓝',classification:'生成汇报',images:[{id:'tg1',name:'target-generated.png'}]});
 await insertDeck(db,generatedSample,W1,{source:'generated',style:'AI蓝',classification:'生成汇报',images:[{id:'gs1',name:'generated-sample.png'}]});
 await insertDeck(db,generatedHoldout,W1,{source:'generated',style:'AI蓝',classification:'生成汇报',images:[{id:'gh1',name:'generated-holdout.png'}]});
 await insertDeck(db,correctionOld,W1,{images:[{id:'c1',name:'correction-old.png'}]});
 await insertDeck(db,correctionNew,W1,{images:[{id:'c2',name:'correction-new.png'}]});
 await insertDeck(db,badNoMatch,W1,{images:[{id:'bn',name:'bad-no-match.png'}]});
 await insertDeck(db,badScale,W1,{images:[{id:'bs',name:'bad-scale.png'}]});

 await insertReview(db,W1,A,targetUpload,{mode:'deck',scores:{visual:8,layout:8},note:'target'});
 await insertReview(db,W1,B,sameMeta,{mode:'deck',scores:{visual:9,layout:8},note:'legacy same'});
 await insertReview(db,W1,B,otherMeta,{mode:'deck',scores:{visual:4,layout:5},note:'legacy other'});
 await insertReview(db,W1,B,unknownMeta,{mode:'deck',scores:{visual:9,layout:9},note:'newer unknown metadata'});
 await db.query("update decks set created_at=now()+interval '1 hour' where id=$1",[unknownMeta]);
 await insertReview(db,W1,B,pageDeck,{mode:'page',scores:{},pages:{p1:{scores:{visual:7,layout:8},note:'legacy page'}}});
 await insertReview(db,W1,B,incomplete,{mode:'deck',scores:{visual:9},note:'incomplete'});
 await insertReview(db,W1,B,holdout,{mode:'deck',scores:{visual:10,layout:10},note:'holdout'});

 await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[A,W1]);
 await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[C,W2]);

 const newSubmission=await seedMatchedSubmission(db,{deckId:newDeck,kind:'uploaded',scope:'deck',scores:{visual:8,layout:9},entryExtra:{classificationCorrection:'raw submission correction must not become a correction'}});
 const baseline=await seedJobRecord(db,{deckId:newDeck,kind:'uploaded',scope:'deck',scores:{visual:7,layout:7},variant:'baseline'});
 await db.query("insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,'baseline')",[newSubmission.submissionId,baseline.recordId]);

 const generatedSubmission=await seedMatchedSubmission(db,{deckId:generatedSample,kind:'generated',scope:'deck',scores:{visual:8,layout:8,match:9}});
 const generatedHoldoutSubmission=await seedMatchedSubmission(db,{deckId:generatedHoldout,kind:'generated',scope:'deck',scores:{visual:10,layout:10,match:10},bucket:0,split:'holdout'});

 const correctionOldSubmission=await seedMatchedSubmission(db,{deckId:correctionOld,kind:'uploaded',scope:'deck',scores:{visual:7,layout:8}});
 const correctionNewSubmission=await seedMatchedSubmission(db,{deckId:correctionNew,kind:'uploaded',scope:'deck',scores:{visual:8,layout:9}});
 const correctionBaseline=await seedJobRecord(db,{deckId:correctionNew,kind:'uploaded',scope:'deck',scores:{visual:7,layout:7},variant:'baseline'});
 await db.query("insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,'baseline')",[correctionNewSubmission.submissionId,correctionBaseline.recordId]);
 const oldDecision=await confirmCorrection(db,correctionOldSubmission,{visual:8,layout:9},'corr-old');
 const newDecision=await confirmCorrection(db,correctionNewSubmission,{visual:9,layout:9},'corr-new');
 await db.query("update ai_review_decisions set created_at=now()-interval '2 days' where id=$1",[oldDecision]);
 const badNoMatchSubmission=await seedUnmatchedSubmission(db,{deckId:badNoMatch,kind:'uploaded',scope:'deck',scores:{visual:8,layout:8}});
 const badNoMatchDecision=await insertDirectDecision(db,{deckId:badNoMatch,submission:badNoMatchSubmission,key:'bad-no-match'});
 const badScaleSubmission=await seedMatchedSubmission(db,{deckId:badScale,kind:'uploaded',scope:'deck',scores:{visual:8,layout:8}});
 const badScaleDecision=await insertDirectDecision(db,{deckId:badScale,submission:badScaleSubmission,decisionMaxScore:5,key:'bad-scale'});

 return {targetUpload,sameMeta,otherMeta,unknownMeta,pageDeck,incomplete,holdout,generatedContam,newDeck,targetGenerated,generatedSample,generatedHoldout,correctionOld,correctionNew,oldDecision,newDecision,badNoMatchDecision,badScaleDecision,newSubmission,generatedSubmission,generatedHoldoutSubmission};
}
async function exerciseFormalCorrection(db,ids){
 const scoreDeck=idForBucket('formal-score',1),sameDeck=idForBucket('formal-same',2),reasonDeck=idForBucket('formal-reason',3),missingDeck=idForBucket('formal-missing',4),extraDeck=idForBucket('formal-extra',1),classDeck=idForBucket('formal-class',2),opaqueDeck=idForBucket('formal-opaque',3),generatedLowDeck=idForBucket('formal-gen-low',4),generatedPassDeck=idForBucket('formal-gen-pass',1);
 await asService(db);
 await db.query('update workspaces set dedicated_judge_id=null where id=$1',[W1]);
 await insertDeck(db,scoreDeck,W1,{images:[{id:'fs1',name:'score.png'}]});
 await insertDeck(db,sameDeck,W1,{images:[{id:'fq1',name:'same.png'}]});
 await insertDeck(db,reasonDeck,W1,{images:[{id:'fr1',name:'reason.png'}]});
 await insertDeck(db,missingDeck,W1,{images:[{id:'fm1',name:'missing.png'}]});
 await insertDeck(db,extraDeck,W1,{images:[{id:'fe1',name:'extra.png'}]});
 await insertDeck(db,classDeck,W1,{images:[{id:'fc1',name:'class.png'}]});
 await insertDeck(db,opaqueDeck,W1,{images:[{id:'fo1',name:'opaque.png'}]});
 await insertDeck(db,generatedLowDeck,W1,{source:'generated',images:[{id:'fg1',name:'generated-low.png'}]});
 await insertDeck(db,generatedPassDeck,W1,{source:'generated',images:[{id:'fp1',name:'generated-pass.png'}]});
 await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[A,W1]);
 const seedFor=async(label,options)=>{try{return await seedMatchedSubmission(db,options)}catch(error){throw new Error(`${label} (${options.deckId}): ${error.message}`)}};
 const scoreSubmission=await seedFor('score',{deckId:scoreDeck,kind:'uploaded',scope:'deck',scores:{visual:4,layout:5}});
 const sameSubmission=await seedFor('same',{deckId:sameDeck,kind:'uploaded',scope:'deck',scores:{visual:7,layout:6}});
 const reasonSubmission=await seedFor('reason',{deckId:reasonDeck,kind:'uploaded',scope:'deck',scores:{visual:6,layout:6}});
 const missingSubmission=await seedFor('missing',{deckId:missingDeck,kind:'uploaded',scope:'deck',scores:{visual:8,layout:7}});
 const extraSubmission=await seedFor('extra',{deckId:extraDeck,kind:'uploaded',scope:'deck',scores:{visual:5,layout:5}});
 const classSubmission=await seedFor('class',{deckId:classDeck,kind:'uploaded',scope:'deck',scores:{visual:6,layout:6}});
 const opaqueSubmission=await seedFor('opaque',{deckId:opaqueDeck,kind:'uploaded',scope:'deck',scores:{visual:null,layout:null}});
 const generatedLowSubmission=await seedFor('generated-low',{deckId:generatedLowDeck,kind:'generated',scope:'deck',scores:{visual:4,layout:3,match:4}});
 const generatedPassSubmission=await seedFor('generated-pass',{deckId:generatedPassDeck,kind:'generated',scope:'deck',scores:{visual:9,layout:9,match:9}});

 const scoreDecision=await confirmFormal(db,scoreSubmission,{formalScores:{visual:6,layout:5},idempotencyKey:'formal-score-only'});
 assert.equal(scoreDecision.status,'corrected','a current-dimension numeric difference alone must mark corrected');
 assert.equal(scoreDecision.idempotent,false);
 const scoreDecisionAgain=await confirmFormal(db,scoreSubmission,{formalScores:{visual:6,layout:5},idempotencyKey:'formal-score-only'});
 assert.equal(scoreDecisionAgain.idempotent,true);
 assert.equal(scoreDecisionAgain.status,'corrected','idempotent response must expose the stored decision status');

 const sameDecision=await confirmFormal(db,sameSubmission,{formalScores:{visual:7,layout:6},classification:' 模型分类 ',reasons:[],idempotencyKey:'formal-same-normalized'});
 assert.equal(sameDecision.status,'confirmed','same numeric scores plus normalized equal classification must stay confirmed');
 const reasonDecision=await confirmFormal(db,reasonSubmission,{formalScores:{visual:6,layout:6},reasons:['hierarchy'],idempotencyKey:'formal-reasons-only'});
 assert.equal(reasonDecision.status,'confirmed','optional human reasons alone must not imply a numeric correction');
 const extraDecision=await confirmFormal(db,extraSubmission,{formalScores:{visual:5,layout:5,match:10},idempotencyKey:'formal-extra-dimension'});
 assert.equal(extraDecision.status,'confirmed');
 assert.deepEqual(extraDecision.formalScores,{visual:5,layout:5},'extra dimensions must not enter the uploaded formal average');
 assert.equal(extraDecision.formalTotal,5);
 const classDecision=await confirmFormal(db,classSubmission,{formalScores:{visual:6,layout:6},classification:{label:' 新人工分类 '},idempotencyKey:'formal-class-diff'});
 assert.equal(classDecision.status,'corrected','a non-empty normalized classification label difference must mark corrected');
 const opaqueDecision=await confirmFormal(db,opaqueSubmission,{formalScores:{visual:5,layout:5},idempotencyKey:'formal-opaque-ai'});
 assert.equal(opaqueDecision.status,'confirmed','non-numeric AI scores must not fabricate a correction');

 await asService(db);
 await assert.rejects(db.query('select ai_review_confirm($1,$2,$3::jsonb)',[W1,A,JSON.stringify({submissionId:missingSubmission.submissionId,aiRecordId:missingSubmission.recordId,formalScores:{visual:9},idempotencyKey:'formal-missing'})]),/全部维度/);
 assert.equal((await db.query('select count(*)::int n from ai_review_decisions where submission_id=$1',[missingSubmission.submissionId])).rows[0].n,0);

 const generatedLow=await confirmFormal(db,generatedLowSubmission,{formalScores:{visual:4,layout:3,match:4},idempotencyKey:'formal-generated-low'});
 assert.equal(generatedLow.formalTotal,3.7);assert.equal(generatedLow.scoreRatio,.37);assert.equal(generatedLow.thresholdMet,false);
 const generatedPass=await confirmFormal(db,generatedPassSubmission,{formalScores:{visual:9,layout:9,match:9},idempotencyKey:'formal-generated-pass'});
 assert.equal(generatedPass.formalTotal,9);assert.equal(generatedPass.scoreRatio,.9);assert.equal(generatedPass.thresholdMet,true);

 const calibration=await calibrate(db,{deckId:ids.targetUpload,scope:'deck',limit:12});
 assert.ok(calibration.corrections.some(correction=>correction.correctionId===scoreDecision.decisionId&&correction.status==='corrected'),'score-only correction must be returned by calibration');
 assert.ok(calibration.corrections.some(correction=>correction.correctionId===classDecision.decisionId&&correction.status==='corrected'),'classification-only correction must be returned by calibration');
 assert.ok(!calibration.corrections.some(correction=>correction.correctionId===sameDecision.decisionId),'confirmed same-score decisions must stay out of correction samples');
}

(async()=>{
 const fresh=await bootstrap(7);
 try{
  await fresh.exec(formalCorrectionDdl);
  const freshRpc=(await fresh.query("select to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)') is not null as present,to_regprocedure('public.ai_review_training_bundle(uuid,uuid,integer)') is not null as legacy_present,exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_confirm(uuid,uuid,jsonb)') and position('FORMAL_CORRECTION_V13' in p.prosrc)>0) as formal_v13")).rows[0];
  assert.deepEqual(freshRpc,{present:true,legacy_present:true,formal_v13:true});
 }finally{await fresh.close();}

 const db=await bootstrap(6);
 try{
  const before=(await db.query("select to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)') is not null as present")).rows[0];
  assert.equal(before.present,false,'07 must be absent on an old 01->06 database');
  await db.exec(calibrationDdl);
  const afterUpgrade=(await db.query("select to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)') is not null as present,to_regprocedure('public.ai_review_training_bundle(uuid,uuid,integer)') is not null as legacy_present")).rows[0];
  assert.deepEqual(afterUpgrade,{present:true,legacy_present:true});
  await db.exec(formalCorrectionDdl);

  const ids=await seedFixture(db);
  const acl=(await db.query("select has_function_privilege('authenticated','public.ai_review_calibration(uuid,uuid,text,integer,text,text)','EXECUTE') as authenticated_ok,has_function_privilege('anon','public.ai_review_calibration(uuid,uuid,text,integer,text,text)','EXECUTE') as anon_ok,has_function_privilege('service_role','public.ai_review_calibration(uuid,uuid,text,integer,text,text)','EXECUTE') as service_ok")).rows[0];
  assert.deepEqual(acl,{authenticated_ok:false,anon_ok:false,service_ok:true});

  const deckResult=await calibrate(db,{deckId:ids.targetUpload,scope:'deck',limit:6});
  assert.equal(deckResult.kind,'uploaded');
  assert.equal(deckResult.selectedSamples,deckResult.samples.length);
  const legacySame=deckResult.samples.find(sample=>sample.deckId===ids.sameMeta&&sample.sampleType==='legacy_review');
  assert.ok(legacySame,'legacy complete upload sample must be selected');
  assert.equal(legacySame.previewPath,W1+'/'+ids.sameMeta+'/same.png','previewPath must come from decks.data.images[].path, not the frontend');
  assert.ok(deckResult.samples.some(sample=>sample.deckId===ids.newDeck&&sample.sampleType==='human_submission'),'current human submission must be selected');
  assert.ok(!deckResult.samples.some(sample=>sample.deckId===ids.incomplete),'incomplete legacy scores must be excluded');
  assert.ok(!deckResult.samples.some(sample=>sample.deckId===ids.targetUpload),'target deck must be excluded');
  assert.ok(!deckResult.samples.some(sample=>sample.deckId===ids.holdout),'holdout deck must be excluded');
  assert.ok(!deckResult.samples.some(sample=>sample.deckId===ids.generatedContam),'generated decks must not become uploaded history');
  assert.ok(deckResult.samples.every(sample=>sample.track==='uploaded'&&sample.split==='calibration'),'uploaded track must be isolated');
  assert.ok(deckResult.samples.every(sample=>sample.previewPath.startsWith(`${W1}/`)&&!sample.previewPath.includes('://')),'previewPath must be a workspace storage path');
  assert.ok(deckResult.samples.every(sample=>typeof sample.maxScore==='number'&&typeof sample.scores.visual==='number'&&typeof sample.scores.layout==='number'),'samples must expose complete scalar-scored legacy/current data');
  assert.ok(deckResult.samples.every(sample=>sample.sampleId!==undefined),'sample ids must be stable and present');

  const priority=await calibrate(db,{deckId:ids.targetUpload,scope:'deck',limit:1});
  assert.equal(priority.samples.length,1);
  assert.notEqual(priority.samples[0].deckId,ids.otherMeta,'same classification/style must outrank unrelated metadata');
  assert.notEqual(priority.samples[0].deckId,ids.unknownMeta,'newer null metadata must not outrank known matching metadata');
  assert.equal(priority.samples[0].sameClassification,true);
  assert.equal(priority.samples[0].sameStyle,true);

  const pagePriority=await calibrate(db,{deckId:ids.targetUpload,scope:'page',limit:1});
  assert.equal(pagePriority.samples.length,1);
  assert.equal(pagePriority.samples[0].scope,'page');
  assert.equal(pagePriority.samples[0].deckId,ids.pageDeck,'exact page scope must outrank deck scope');
  assert.equal(pagePriority.scopeFallback,false);

  const fallback=await calibrate(db,{deckId:ids.targetGenerated,kind:'generated',scope:'page',limit:2});
  assert.ok(fallback.samples.length>0);
  assert.ok(fallback.samples.every(sample=>sample.track==='generated'&&sample.scope==='deck'&&sample.scopeFallback===true),'generated page request may only fall back within generated track and must expose actual scope');
  assert.ok(fallback.samples.some(sample=>sample.deckId===ids.generatedSample));
  assert.ok(!fallback.samples.some(sample=>sample.deckId===ids.generatedHoldout),'generated holdout must be excluded');
  assert.equal(fallback.scopeFallback,true);

  const corrections=await calibrate(db,{deckId:ids.targetUpload,scope:'deck',limit:1});
  assert.equal(corrections.selectedCorrections,1);
  assert.equal(corrections.corrections[0].correctionId,ids.newDecision,'latest formal correction must win');
  assert.equal(corrections.corrections[0].status,'corrected');
  assert.equal(corrections.corrections[0].scope,'deck');
  assert.equal(typeof corrections.corrections[0].maxScore,'number');
  assert.ok(corrections.corrections[0].previewPath.startsWith(`${W1}/`));
  assert.ok(!corrections.corrections.some(correction=>correction.correctionId===ids.oldDecision));
  const allCorrections=await calibrate(db,{deckId:ids.targetUpload,scope:'deck',limit:12});
  assert.ok(allCorrections.corrections.some(correction=>correction.correctionId===ids.newDecision));
  assert.ok(!allCorrections.corrections.some(correction=>correction.correctionId===ids.badNoMatchDecision),'a formal decision without a concrete submission/record match must be excluded');
  assert.ok(!allCorrections.corrections.some(correction=>correction.correctionId===ids.badScaleDecision),'decision, submission, and record maxScore must agree');

  await exerciseFormalCorrection(db,ids);

  await asAuthenticated(db);
  await assert.rejects(db.query('select ai_review_calibration($1,$2,$3,$4,$5,$6)',[W1,A,'uploaded',1,ids.targetUpload,'deck']),/permission denied/);
  await asAnon(db);
  await assert.rejects(db.query('select ai_review_calibration($1,$2,$3,$4,$5,$6)',[W1,A,'uploaded',1,ids.targetUpload,'deck']),/permission denied/);
  await asService(db);
  await assert.rejects(db.query('select ai_review_calibration($1,$2,$3,$4,$5,$6)',[W1,B,'uploaded',1,ids.targetUpload,'deck']),/仅专职评委/);
  await assert.rejects(db.query('select ai_review_calibration($1,$2,$3,$4,$5,$6)',[W1,C,'uploaded',1,ids.targetUpload,'deck']),/仅专职评委/);
  await assert.rejects(db.query('select ai_review_calibration($1,$2,$3,$4,$5,$6)',[W1,A,'generated',1,ids.targetUpload,'deck']),/目标作品不存在或不属于该轨道/);

  console.log('PASS: service-role-only calibration RPC, legacy upload scores, separate tracks, holdout/target exclusion, exact-scope priority, null-safe metadata ranking, same-track fallback, formal correction integrity, score-only corrected decisions, score-complete gates, weight thresholds, and 01->07 + 13 upgrade');
 }finally{await db.close();}
})().catch(error=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});
