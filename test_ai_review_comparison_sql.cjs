// npm install --prefix .test-output/sql @electric-sql/pglite
// Real PGlite fixture for the service-role-only ai_review_comparison(uuid) RPC.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');

const W1='11111111-1111-4111-8111-111111111111';
const W2='22222222-2222-4222-8222-222222222222';
const EMPTY_W='33333333-3333-4333-8333-333333333333';
const LEGACY_W='44444444-4444-4444-8444-444444444444';
const FORMAL_W='55555555-5555-4555-8555-555555555555';
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');
const migrations=[
 '20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning',
 '20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history',
 '20261121_07_review_calibration','20261121_08_review_comparison',
 '20261121_09_review_backfill','20261121_10_review_queue_listing',
 '20261121_11_planning_history','20261121_12_library_classification',
 '20261121_13_formal_correction','20261121_14_library_formal_scores'
].map(name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8'));
const comparisonDdl=migrations[7];
const formalCorrectionDdl=migrations[12];
const libraryFormalDdl=migrations[13];
const emptyResult={
 holdoutUnits:0,
 baseline:null,
 assisted:null,
 improvement:null,
 byKind:{
  uploaded:{holdoutUnits:0,baseline:null,assisted:null,improvement:null},
  generated:{holdoutUnits:0,baseline:null,assisted:null,improvement:null}
 },
 splitAlgorithm:'md5-first-byte-mod-5-v1',
 holdoutBucket:0
};

function closeTo(actual,expected,label){
 assert.equal(typeof actual,'number',`${label} must be numeric`);
 assert.ok(Math.abs(actual-expected)<1e-9,`${label}: expected ${expected}, got ${actual}`);
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
async function asOwner(db){await db.exec('reset role');}
async function asService(db){await db.exec('reset role');await db.exec('set role service_role');}
async function asAuthenticated(db){await db.exec('reset role');await db.exec('set role authenticated');}
async function asAnon(db){await db.exec('reset role');await db.exec('set role anon');}
async function comparison(db,w){return (await db.query('select public.ai_review_comparison($1) as value',[w])).rows[0].value;}

async function insertWorkspace(db,id,label){
 await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,$2,$3,encode(extensions.digest($4,'sha256'),'hex'))",[
  id,label,`${label}@example.test`,`invite-${label}`
 ]);
}
async function insertDeck(db,id,workspace,kind='uploaded',unitIds=['p1']){
 const images=unitIds.map(unitId=>typeof unitId==='object'&&unitId!==null?unitId:{
  id:unitId,name:`${unitId}.png`,path:`${workspace}/${id}/${unitId}.png`
 });
 const data={id,name:id,title:id,origin:kind==='generated'?'generated':'import',images};
 await db.query('insert into decks(id,workspace_id,data,source_kind) values($1,$2,$3::jsonb,$4)',[id,workspace,JSON.stringify(data),kind]);
}
async function insertRecord(db,{
 workspace=W1,deckId,kind='uploaded',scope='page',unitId,variant,bucket=0,split='holdout',
 deckVersion='deck-v1',rubricVersion='rubric-v1',maxScore=10,scores,
 jobVariant=variant,recordVariant=variant,matchVariant=null
}){
 const jobId=crypto.randomUUID(),recordId=crypto.randomUUID();
 await db.query(`insert into ai_review_jobs(
  id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,
  deck_version,rubric_version,max_score,status,attempts,judge_user_id
 ) values($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,$11,$12,'completed',1,$13)`,
 [jobId,workspace,deckId,kind,scope,unitId,jobVariant,bucket,split,deckVersion,rubricVersion,maxScore,A]);
 await db.query(`insert into ai_review_records(
  id,job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,
  deck_version,rubric_version,max_score,model_hint,ai_result,judge_user_id
 ) values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'comparison-test',$14::jsonb,$15)`,
 [recordId,jobId,workspace,deckId,kind,scope,unitId,recordVariant,bucket,split,deckVersion,rubricVersion,maxScore,
  JSON.stringify({scores}),A]);
 return {jobId,recordId,kind,scope,unitId,variant:recordVariant,deckVersion,rubricVersion,maxScore,matchVariant};
}
async function insertSubmission(db,{
 workspace=W1,deckId,kind='uploaded',scope='page',unitId,scores,maxScore=10,
 deckVersion='deck-v1',rubricVersion='rubric-v1',humanUserId=B
}){
 const submissionId=crypto.randomUUID();
 await db.query(`insert into ai_review_submissions(
  id,workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,
  deck_version,rubric_version,human_version,max_score,entry
 ) values($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,$11,$12::jsonb)`,
 [submissionId,workspace,deckId,kind,scope,unitId,humanUserId,deckVersion,rubricVersion,
  `human-${crypto.randomUUID()}`,maxScore,JSON.stringify({scores})]);
 return submissionId;
}
async function linkRecord(db,submissionId,record){
 await db.query('insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,$3)',[
  submissionId,record.recordId,record.matchVariant||record.variant
 ]);
}
async function seedPair(db,options){
 const submissionId=await insertSubmission(db,options);
 const baseline=await insertRecord(db,{...options,variant:'baseline',scores:options.baselineScores,...options.baselineRecord});
 const assisted=await insertRecord(db,{...options,variant:'assisted',scores:options.assistedScores,...options.assistedRecord});
 await linkRecord(db,submissionId,baseline);
 await linkRecord(db,submissionId,assisted);
 return submissionId;
}
async function seedOneSide(db,options,variant){
 const submissionId=await insertSubmission(db,options);
 const record=await insertRecord(db,{...options,variant,scores:variant==='baseline'?options.baselineScores:options.assistedScores,...options.record});
 await linkRecord(db,submissionId,record);
 return submissionId;
}
async function seedRecordPair(db,options){
 const baseline=await insertRecord(db,{...options,variant:'baseline',scores:options.baselineScores,...options.baselineRecord});
 const assisted=await insertRecord(db,{...options,variant:'assisted',scores:options.assistedScores,...options.assistedRecord});
 return {baseline,assisted};
}
async function insertLegacyReview(db,{workspace=W1,userId,deckId,data}){
 await db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4::jsonb)',[
  workspace,userId,deckId,JSON.stringify(data)
 ]);
}
async function currentVersions(db,workspace,deckId){
 return (await db.query(`select
  public.ai_review_deck_version($1,$2) as "deckVersion",
  public.ai_review_rubric_version($1) as "rubricVersion"`,[workspace,deckId])).rows[0];
}
async function sourceSnapshot(db){
 return (await db.query(`select
  (select count(*)::integer from public.decks) as decks_count,
  md5(coalesce((select jsonb_agg(to_jsonb(d) order by d.id) from public.decks d),'[]'::jsonb)::text) as decks_hash,
  (select count(*)::integer from public.reviews) as reviews_count,
  md5(coalesce((select jsonb_agg(to_jsonb(r) order by r.workspace_id,r.user_id,r.deck_id) from public.reviews r),'[]'::jsonb)::text) as reviews_hash,
  (select count(*)::integer from public.ai_review_jobs) as jobs_count,
  md5(coalesce((select jsonb_agg(to_jsonb(j) order by j.id) from public.ai_review_jobs j),'[]'::jsonb)::text) as jobs_hash,
  (select count(*)::integer from public.ai_review_records) as records_count,
  md5(coalesce((select jsonb_agg(to_jsonb(r) order by r.id) from public.ai_review_records r),'[]'::jsonb)::text) as records_hash,
  (select count(*)::integer from public.ai_review_submissions) as submissions_count,
  md5(coalesce((select jsonb_agg(to_jsonb(s) order by s.id) from public.ai_review_submissions s),'[]'::jsonb)::text) as submissions_hash,
  (select count(*)::integer from public.ai_review_matches) as matches_count,
  md5(coalesce((select jsonb_agg(to_jsonb(m) order by m.id) from public.ai_review_matches m),'[]'::jsonb)::text) as matches_hash`)).rows[0];
}

(async()=>{
 const db=await bootstrap(7);
 try{
  const absent=(await db.query("select to_regprocedure('public.ai_review_comparison(uuid)') is not null as present")).rows[0];
  assert.equal(absent.present,false,'08 must be absent on an old 01->07 database');
  await db.exec(comparisonDdl);
  const functionInfo=(await db.query(`select
   p.prosecdef as security_definer,
   coalesce(array_to_string(p.proconfig,','),'') as config
   from pg_proc p where p.oid=to_regprocedure('public.ai_review_comparison(uuid)')`)).rows[0];
  assert.equal(functionInfo.security_definer,false,'comparison RPC must be security invoker');
  assert.match(functionInfo.config,/search_path=/,'comparison RPC must pin an empty search_path');
  const acl=(await db.query(`select
   has_function_privilege('service_role','public.ai_review_comparison(uuid)','EXECUTE') as service_ok,
   has_function_privilege('authenticated','public.ai_review_comparison(uuid)','EXECUTE') as authenticated_ok,
   has_function_privilege('anon','public.ai_review_comparison(uuid)','EXECUTE') as anon_ok`)).rows[0];
  assert.deepEqual(acl,{service_ok:true,authenticated_ok:false,anon_ok:false});

  for(const userId of [A,B])await db.query('insert into auth.users(id) values($1)',[userId]);
  await insertWorkspace(db,W1,'comparison-main');
  await insertWorkspace(db,W2,'comparison-bulk');
  await insertWorkspace(db,EMPTY_W,'comparison-empty');
  await insertWorkspace(db,LEGACY_W,'comparison-legacy');
  await db.query('update workspaces set settings=$1::jsonb where id=$2',[
   JSON.stringify({revision:0,maxScore:10,dimensions:[
    {id:'visual',name:'视觉效果'},{id:'layout',name:'排版'},{id:'match',name:'匹配'}
   ]}),LEGACY_W
  ]);
  await insertDeck(db,'uploaded-deck',W1,'uploaded');
  await insertDeck(db,'generated-deck',W1,'generated');
  await insertDeck(db,'wrong-deck',W1,'uploaded');
  await insertDeck(db,'bulk-deck',W2,'uploaded');

  assert.deepEqual(await comparison(db,EMPTY_W),emptyResult,'empty workspace must return the exact empty shape');
  assert.deepEqual(await comparison(db,null),emptyResult,'null workspace must return the exact empty shape');

  await seedPair(db,{
   deckId:'uploaded-deck',kind:'uploaded',unitId:'uploaded-pair',
   scores:{visual:8,layout:6,match:4},
   baselineScores:{visual:7,layout:9},
   assistedScores:{visual:9,layout:7,match:1}
  });
  await seedPair(db,{
   deckId:'generated-deck',kind:'generated',unitId:'generated-pair',
   scores:{visual:8,layout:7,match:8},
   baselineScores:{visual:6,layout:4,match:6},
   assistedScores:{visual:10,layout:5,match:8}
  });

  await seedOneSide(db,{
   deckId:'uploaded-deck',kind:'uploaded',unitId:'baseline-only',
   scores:{visual:8,layout:6},baselineScores:{visual:7,layout:7},assistedScores:{visual:9,layout:9}
  },'baseline');
  await seedOneSide(db,{
   deckId:'uploaded-deck',kind:'uploaded',unitId:'assisted-only',
   scores:{visual:8,layout:6},baselineScores:{visual:7,layout:7},assistedScores:{visual:9,layout:9}
  },'assisted');
  await seedPair(db,{
   deckId:'uploaded-deck',kind:'uploaded',unitId:'no-intersection',
   scores:{visual:8,layout:6},baselineScores:{visual:7},assistedScores:{layout:5}
  });
  await seedPair(db,{
   deckId:'uploaded-deck',kind:'uploaded',unitId:'non-numeric',
   scores:{visual:8},baselineScores:{visual:'7'},assistedScores:{visual:9}
  });

  const invalidBindings=[
   {unitId:'wrong-deck',baselineRecord:{deckId:'wrong-deck'}},
   {unitId:'wrong-scope',baselineRecord:{scope:'deck'}},
   {unitId:'wrong-unit',baselineRecord:{unitId:'other-unit'}},
   {unitId:'wrong-source',baselineRecord:{kind:'generated'}},
   {unitId:'wrong-deck-version',baselineRecord:{deckVersion:'deck-v2'}},
   {unitId:'wrong-rubric-version',baselineRecord:{rubricVersion:'rubric-v2'}},
   {unitId:'wrong-max',baselineRecord:{maxScore:9}},
   {unitId:'wrong-match-variant',baselineRecord:{matchVariant:'assisted'}},
   {unitId:'wrong-bucket',baselineRecord:{bucket:1,split:'holdout'}},
   {unitId:'wrong-split',baselineRecord:{bucket:0,split:'calibration'}}
  ];
  for(const fixture of invalidBindings){
   await seedPair(db,{
    deckId:'uploaded-deck',kind:'uploaded',unitId:fixture.unitId,
    scores:{visual:8,layout:6},baselineScores:{visual:7,layout:7},assistedScores:{visual:9,layout:7},
    baselineRecord:fixture.baselineRecord
   });
  }

  await asAuthenticated(db);
  await assert.rejects(comparison(db,W1),/permission denied/i);
  await asAnon(db);
  await assert.rejects(comparison(db,W1),/permission denied/i);
  await asService(db);
  const beforeComparison=await sourceSnapshot(db);
  const result=await comparison(db,W1);
  const afterComparison=await sourceSnapshot(db);
  assert.deepEqual(afterComparison,beforeComparison,'comparison RPC must not mutate source tables');

  assert.equal(result.holdoutUnits,2);
  assert.equal(result.baseline.count,2);
  assert.equal(result.assisted.count,2);
  closeTo(result.baseline.mae,(0.2+0.2333333333333333)/2,'overall baseline MAE');
  closeTo(result.assisted.mae,(0.1+0.1333333333333333)/2,'overall assisted MAE');
  closeTo(result.improvement,0.1,'overall improvement');
  assert.deepEqual(Object.keys(result).sort(),['assisted','baseline','byKind','holdoutBucket','holdoutUnits','improvement','splitAlgorithm']);
  assert.deepEqual(Object.keys(result.baseline).sort(),['count','dimensions','mae']);
  assert.deepEqual(Object.keys(result.baseline.dimensions).sort(),['layout','match','visual']);
  assert.deepEqual(Object.keys(result.assisted.dimensions).sort(),['layout','match','visual']);
  closeTo(result.baseline.dimensions.visual,0.15,'overall visual baseline');
  closeTo(result.baseline.dimensions.layout,0.3,'overall layout baseline');
  closeTo(result.baseline.dimensions.match,0.2,'overall match baseline');
  closeTo(result.assisted.dimensions.visual,0.15,'overall visual assisted');
  closeTo(result.assisted.dimensions.layout,0.15,'overall layout assisted');
  closeTo(result.assisted.dimensions.match,0,'overall match assisted');

  const uploaded=result.byKind.uploaded,generated=result.byKind.generated;
  assert.equal(uploaded.holdoutUnits,1);
  closeTo(uploaded.baseline.mae,0.2,'uploaded baseline MAE');
  closeTo(uploaded.assisted.mae,0.1,'uploaded assisted MAE');
  closeTo(uploaded.improvement,0.1,'uploaded improvement');
  assert.deepEqual(Object.keys(uploaded.baseline.dimensions).sort(),['layout','visual']);
  assert.deepEqual(Object.keys(uploaded.assisted.dimensions).sort(),['layout','visual']);
  closeTo(uploaded.baseline.dimensions.visual,0.1,'uploaded visual baseline');
  closeTo(uploaded.baseline.dimensions.layout,0.3,'uploaded layout baseline');
  closeTo(uploaded.assisted.dimensions.visual,0.1,'uploaded visual assisted');
  closeTo(uploaded.assisted.dimensions.layout,0.1,'uploaded layout assisted');

  assert.equal(generated.holdoutUnits,1);
  closeTo(generated.baseline.mae,0.2333333333333333,'generated baseline MAE');
  closeTo(generated.assisted.mae,0.1333333333333333,'generated assisted MAE');
  closeTo(generated.improvement,0.1,'generated improvement');
  assert.deepEqual(Object.keys(generated.baseline.dimensions).sort(),['layout','match','visual']);
  assert.deepEqual(Object.keys(generated.assisted.dimensions).sort(),['layout','match','visual']);
  closeTo(generated.baseline.dimensions.match,0.2,'generated match baseline');
  closeTo(generated.assisted.dimensions.match,0,'generated match assisted');

  const serialized=JSON.stringify(result);
  assert.ok(!/submission|record|userId|user_id|scores/i.test(serialized),'aggregate must not expose samples, IDs, users, or score payloads');

  await asOwner(db);
  const legacyDecks=[
   ['legacy-b-18',[
    'legacy-page-real',
    {id:'legacy-page-no-path',name:'legacy-page-no-path.png',path:''},
    {id:'legacy-page-wrong-prefix',name:'legacy-page-wrong-prefix.png',path:`${W2}/legacy-b-18/legacy-page-wrong-prefix.png`}
   ],'uploaded'],
   ['legacy-dup-2',['dup-page-real'],'uploaded'],
   ['legacy-formal-incomplete-1',['legacy-formal-incomplete-page'],'uploaded'],
   ['legacy-deck-a',['legacy-nonholdout-deck'],'uploaded'],
   ['legacy-nonholdout-3',['legacy-nonholdout-record'],'uploaded'],
   ['legacy-oldversion-13',['legacy-old-deck-version'],'uploaded'],
   ['legacy-oldversion-24',['legacy-old-rubric-version'],'uploaded'],
   ['legacy-nopair-5',['legacy-no-pair-page'],'uploaded'],
   ['legacy-badcomplete-0',['legacy-bad-complete-page'],'uploaded'],
   ['legacy-badrange-12',['legacy-bad-range-page'],'uploaded'],
   ['legacy-wrongmax-21',['legacy-wrong-max-page'],'uploaded'],
   ['legacy-wrongtrack-0',['legacy-wrong-track-page'],'uploaded']
  ];
  for(const [deckId,unitIds,kind] of legacyDecks)await insertDeck(db,deckId,LEGACY_W,kind,unitIds);
  const bucketRows=(await db.query(`select id,mod(get_byte(decode(md5(id),'hex'),0),5)::integer as bucket
   from decks where id=any($1::text[])`,[legacyDecks.map(item=>item[0])])).rows;
  const buckets=Object.fromEntries(bucketRows.map(row=>[row.id,row.bucket]));
  assert.equal(buckets['legacy-deck-a'],3,'non-holdout deck fixture must be outside bucket 0');
  for(const [deckId] of legacyDecks.filter(item=>item[0]!=='legacy-deck-a')){
   assert.equal(buckets[deckId],0,`${deckId} must be a bucket-0 holdout fixture`);
  }

  const coreVersions=await currentVersions(db,LEGACY_W,'legacy-b-18');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-b-18',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:coreVersions.deckVersion,rubricVersion:coreVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:2,match:10},assistedScores:{visual:7,layout:5,match:7}
  });
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-b-18',kind:'uploaded',scope:'page',unitId:'legacy-page-real',
   deckVersion:coreVersions.deckVersion,rubricVersion:coreVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:7,layout:6,match:5},assistedScores:{visual:8,layout:7,match:6}
  });
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-b-18',kind:'uploaded',scope:'page',unitId:'legacy-page-ghost',
   deckVersion:coreVersions.deckVersion,rubricVersion:coreVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:0,layout:0,match:0},assistedScores:{visual:0,layout:0,match:0}
  });
  for(const unitId of ['legacy-page-no-path','legacy-page-wrong-prefix']){
   await seedRecordPair(db,{
    workspace:LEGACY_W,deckId:'legacy-b-18',kind:'uploaded',scope:'page',unitId,
    deckVersion:coreVersions.deckVersion,rubricVersion:coreVersions.rubricVersion,maxScore:10,
    baselineScores:{visual:0,layout:0,match:0},assistedScores:{visual:0,layout:0,match:0}
   });
  }
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-b-18',
   data:{
    scores:{visual:6,layout:4,match:8},
    pages:{
     'legacy-page-real':{scores:{visual:0}},
     'legacy-page-ghost':{scores:{visual:0}}
    }
   }
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:B,deckId:'legacy-b-18',
   data:{
    mode:'page',
    scores:{visual:0,layout:0},
    pages:{
     'legacy-page-real':{scores:{visual:9,layout:8,match:7}},
     'legacy-page-ghost':{scores:{visual:0,layout:0,match:0}},
     'legacy-page-no-path':{scores:{visual:0,layout:0,match:0}},
     'legacy-page-wrong-prefix':{scores:{visual:0,layout:0,match:0}}
    }
   }
  });

  const duplicateVersions=await currentVersions(db,LEGACY_W,'legacy-dup-2');
  const duplicatePair=await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-dup-2',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:duplicateVersions.deckVersion,rubricVersion:duplicateVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:9,layout:9,match:9},assistedScores:{visual:10,layout:10,match:10}
  });
  const duplicateSubmission=await insertSubmission(db,{
   workspace:LEGACY_W,deckId:'legacy-dup-2',kind:'uploaded',scope:'deck',unitId:'',
   humanUserId:A,scores:{visual:10,layout:10,match:10},maxScore:10,
   deckVersion:duplicateVersions.deckVersion,rubricVersion:duplicateVersions.rubricVersion
  });
  await linkRecord(db,duplicateSubmission,duplicatePair.baseline);
  await linkRecord(db,duplicateSubmission,duplicatePair.assisted);
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-dup-2',
   data:{scores:{visual:0,layout:0,match:0}}
  });

  const incompleteFormalVersions=await currentVersions(db,LEGACY_W,'legacy-formal-incomplete-1');
  const incompleteFormalPair=await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-formal-incomplete-1',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:incompleteFormalVersions.deckVersion,rubricVersion:incompleteFormalVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:9,layout:9,match:9},assistedScores:{visual:10,layout:10,match:10}
  });
  const incompleteFormalSubmission=await insertSubmission(db,{
   workspace:LEGACY_W,deckId:'legacy-formal-incomplete-1',kind:'uploaded',scope:'deck',unitId:'',
   humanUserId:A,scores:{},maxScore:10,
   deckVersion:incompleteFormalVersions.deckVersion,rubricVersion:incompleteFormalVersions.rubricVersion
  });
  await linkRecord(db,incompleteFormalSubmission,incompleteFormalPair.baseline);
  await linkRecord(db,incompleteFormalSubmission,incompleteFormalPair.assisted);
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-formal-incomplete-1',
   data:{scores:{visual:10,layout:10,match:10}}
  });

  const nonholdoutDeckVersions=await currentVersions(db,LEGACY_W,'legacy-deck-a');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-deck-a',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:nonholdoutDeckVersions.deckVersion,rubricVersion:nonholdoutDeckVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-deck-a',
   data:{scores:{visual:7,layout:7,match:7}}
  });

  const nonholdoutRecordVersions=await currentVersions(db,LEGACY_W,'legacy-nonholdout-3');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-nonholdout-3',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:nonholdoutRecordVersions.deckVersion,rubricVersion:nonholdoutRecordVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9},
   baselineRecord:{bucket:1,split:'calibration'},assistedRecord:{bucket:1,split:'calibration'}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-nonholdout-3',
   data:{scores:{visual:7,layout:7,match:7}}
  });

  const oldDeckVersions=await currentVersions(db,LEGACY_W,'legacy-oldversion-13');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-oldversion-13',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:'stale-deck-version',rubricVersion:oldDeckVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-oldversion-13',
   data:{scores:{visual:7,layout:7,match:7}}
  });

  const oldRubricVersions=await currentVersions(db,LEGACY_W,'legacy-oldversion-24');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-oldversion-24',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:oldRubricVersions.deckVersion,rubricVersion:'stale-rubric-version',maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-oldversion-24',
   data:{scores:{visual:7,layout:7,match:7}}
  });

  const noPairVersions=await currentVersions(db,LEGACY_W,'legacy-nopair-5');
  await insertRecord(db,{
   workspace:LEGACY_W,deckId:'legacy-nopair-5',kind:'uploaded',scope:'deck',unitId:'',
   variant:'baseline',deckVersion:noPairVersions.deckVersion,rubricVersion:noPairVersions.rubricVersion,
   maxScore:10,scores:{visual:8,layout:8,match:8}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-nopair-5',
   data:{scores:{visual:7,layout:7,match:7}}
  });

  const wrongTrackVersions=await currentVersions(db,LEGACY_W,'legacy-wrongtrack-0');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-wrongtrack-0',kind:'generated',scope:'deck',unitId:'',
   deckVersion:wrongTrackVersions.deckVersion,rubricVersion:wrongTrackVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-wrongtrack-0',
   data:{scores:{visual:7,layout:7,match:7}}
  });

  const badCompleteVersions=await currentVersions(db,LEGACY_W,'legacy-badcomplete-0');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-badcomplete-0',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:badCompleteVersions.deckVersion,rubricVersion:badCompleteVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-badcomplete-0',
   data:{scores:{visual:7,layout:7}}
  });

  const badRangeVersions=await currentVersions(db,LEGACY_W,'legacy-badrange-12');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-badrange-12',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:badRangeVersions.deckVersion,rubricVersion:badRangeVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-badrange-12',
   data:{scores:{visual:11,layout:7,match:7}}
  });

  const wrongMaxVersions=await currentVersions(db,LEGACY_W,'legacy-wrongmax-21');
  await seedRecordPair(db,{
   workspace:LEGACY_W,deckId:'legacy-wrongmax-21',kind:'uploaded',scope:'deck',unitId:'',
   deckVersion:wrongMaxVersions.deckVersion,rubricVersion:wrongMaxVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:9,layout:9,match:9},
   assistedRecord:{maxScore:9}
  });
  await insertLegacyReview(db,{
   workspace:LEGACY_W,userId:A,deckId:'legacy-wrongmax-21',
   data:{scores:{visual:7,layout:7,match:7}}
  });

  await asService(db);
  const beforeLegacy=await sourceSnapshot(db);
  const legacy=await comparison(db,LEGACY_W);
  const afterLegacy=await sourceSnapshot(db);
  assert.deepEqual(afterLegacy,beforeLegacy,'legacy comparison must be read-only');
  assert.equal(legacy.holdoutUnits,4,'two legacy reviewers, one formal duplicate, and one incomplete-formal fallback must be counted once each');
  assert.equal(legacy.baseline.count,4);
  assert.equal(legacy.assisted.count,4);
  closeTo(legacy.baseline.mae,0.15,'legacy overall baseline MAE');
  closeTo(legacy.assisted.mae,0.05,'legacy overall assisted MAE');
  closeTo(legacy.improvement,0.1,'legacy overall improvement');
  assert.deepEqual(Object.keys(legacy.baseline.dimensions).sort(),['layout','match','visual']);
  assert.deepEqual(Object.keys(legacy.assisted.dimensions).sort(),['layout','match','visual']);
  for(const dimension of ['visual','layout','match']){
   closeTo(legacy.baseline.dimensions[dimension],0.15,`legacy ${dimension} baseline`);
   closeTo(legacy.assisted.dimensions[dimension],0.05,`legacy ${dimension} assisted`);
  }
  assert.equal(legacy.byKind.uploaded.holdoutUnits,4);
  assert.equal(legacy.byKind.generated.holdoutUnits,0);
  assert.ok(!JSON.stringify(legacy).match(/legacy:|submission:|user_id|userId|scores/i),'legacy aggregate must stay anonymous');

  await asOwner(db);
  await db.query(`insert into ai_review_jobs(
   id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,
   deck_version,rubric_version,max_score,status,attempts,judge_user_id
  )
  select gen_random_uuid(),$1,'bulk-deck','uploaded','page','bulk-'||i,i,item.variant,0,'holdout',
   'deck-v1','rubric-v1',10,'completed',1,$2
  from generate_series(1,1001) i
  cross join (values ('baseline'::text),('assisted'::text)) item(variant)`,[W2,A]);
  await db.query(`insert into ai_review_records(
   id,job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,
   deck_version,rubric_version,max_score,model_hint,ai_result,judge_user_id
  )
  select gen_random_uuid(),j.id,j.workspace_id,j.deck_id,j.source_kind,j.scope,j.unit_id,j.variant,j.bucket,j.split,
   j.deck_version,j.rubric_version,j.max_score,'bulk-test',
   jsonb_build_object('scores',jsonb_build_object('visual',case when j.variant='baseline' then 0 else 5 end)),$2
  from ai_review_jobs j where j.workspace_id=$1 and j.deck_id='bulk-deck'`,[W2,A]);
  await db.query(`insert into ai_review_submissions(
   id,workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,
   deck_version,rubric_version,human_version,max_score,entry
  )
  select gen_random_uuid(),$1,'bulk-deck','uploaded','page','bulk-'||i,i,$2,
   'deck-v1','rubric-v1','bulk-human-'||i,10,jsonb_build_object('scores',jsonb_build_object('visual',10))
  from generate_series(1,1001) i`,[W2,A]);
  await db.query(`insert into ai_review_matches(submission_id,record_id,variant)
  select s.id,r.id,r.variant
  from ai_review_submissions s
  join ai_review_records r on r.workspace_id=s.workspace_id and r.unit_id=s.unit_id
  where s.workspace_id=$1 and s.deck_id='bulk-deck' and r.deck_id='bulk-deck'`,[W2]);

  const beforeBulk=await sourceSnapshot(db);
  const bulk=await comparison(db,W2);
  const afterBulk=await sourceSnapshot(db);
  assert.deepEqual(afterBulk,beforeBulk,'bulk comparison must be read-only');
  assert.equal(bulk.holdoutUnits,1001,'all 1001 paired holdout submissions must be included');
  assert.equal(bulk.baseline.count,1001);
  assert.equal(bulk.assisted.count,1001);
  closeTo(bulk.baseline.mae,1,'bulk baseline MAE');
  closeTo(bulk.assisted.mae,0.5,'bulk assisted MAE');
  closeTo(bulk.improvement,0.5,'bulk improvement');
  assert.equal(bulk.byKind.uploaded.holdoutUnits,1001);
  assert.equal(bulk.byKind.generated.holdoutUnits,0);


  await db.exec(formalCorrectionDdl);
  await db.exec(libraryFormalDdl);
  await insertWorkspace(db,FORMAL_W,'comparison-formal-projection');
  await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'专职评委','admin')",[FORMAL_W,A]);
  await db.query('update workspaces set dedicated_judge_id=$1,settings=$2::jsonb where id=$3',[A,JSON.stringify({revision:0,maxScore:10,dimensions:[
   {id:'visual',name:'视觉效果'},{id:'layout',name:'排版'},{id:'match',name:'匹配'}
  ]}),FORMAL_W]);
  await insertDeck(db,'formal-projection-deck',FORMAL_W,'uploaded',['formal-page']);
  const formalVersions=await currentVersions(db,FORMAL_W,'formal-projection-deck');
  const formalPair=await seedRecordPair(db,{
   workspace:FORMAL_W,deckId:'formal-projection-deck',kind:'uploaded',scope:'page',unitId:'formal-page',
   deckVersion:formalVersions.deckVersion,rubricVersion:formalVersions.rubricVersion,maxScore:10,
   baselineScores:{visual:9,layout:9,match:9},assistedScores:{visual:8,layout:8,match:8}
  });
  const blindSubmission=await insertSubmission(db,{
   workspace:FORMAL_W,deckId:'formal-projection-deck',kind:'uploaded',scope:'page',unitId:'formal-page',
   humanUserId:B,scores:{visual:2,layout:2,match:2},maxScore:10,
   deckVersion:formalVersions.deckVersion,rubricVersion:formalVersions.rubricVersion
  });
  await linkRecord(db,blindSubmission,formalPair.baseline);
  await linkRecord(db,blindSubmission,formalPair.assisted);
  const blindBefore=(await db.query('select entry from ai_review_submissions where id=$1',[blindSubmission])).rows[0].entry;
  const correction=(await db.query('select public.ai_review_confirm($1,$2,$3::jsonb) as value',[FORMAL_W,A,JSON.stringify({
   submissionId:blindSubmission,aiRecordId:formalPair.assisted.recordId,formalScores:{visual:10,layout:8,match:6},
   reasons:['visual_impact'],status:'confirmed'
  })])).rows[0].value;
  const decision=(await db.query('select status,formal_scores,formal_total from ai_review_decisions where workspace_id=$1 and submission_id=$2',
   [FORMAL_W,blindSubmission])).rows[0];
  assert.equal(correction.status,'corrected','formalize must mark score differences as corrected');
  assert.equal(decision.status,'corrected');
  assert.deepEqual(decision.formal_scores,{visual:10,layout:8,match:6});
  closeTo(Number(decision.formal_total),8,'formal corrected total');
  assert.notDeepEqual(blindBefore.scores,decision.formal_scores,'blind and formal scores must differ for the same scoring unit');

  await asService(db);
  const formalProjection=(await db.query('select deck_id,scope,unit_id,score from public.ai_review_uploaded_formal_scores($1)',[FORMAL_W])).rows;
  assert.equal(formalProjection.length,1,'library projection must return the corrected unit');
  assert.equal(formalProjection[0].deck_id,'formal-projection-deck');
  assert.equal(formalProjection[0].scope,'page');
  assert.equal(formalProjection[0].unit_id,'formal-page');
  closeTo(Number(formalProjection[0].score),8,'library projection must use formal_scores');

  await asAuthenticated(db);
  await db.query("select set_config('request.jwt.claim.sub',$1,false)",[A]);
  const formalSummary=(await db.query('select public.review_score_summary($1) as value',[FORMAL_W])).rows[0].value;
  assert.equal(formalSummary['formal-projection-deck'].count,0);
  assert.equal(formalSummary['formal-projection-deck'].mean,null);
  closeTo(Number(formalSummary['formal-projection-deck'].formalPageScores['formal-page']),8,'review summary projection');

  await asService(db);
  const formalComparison=await comparison(db,FORMAL_W);
  assert.equal(formalComparison.holdoutUnits,1,'same-unit blind/formal fixture must count once');
  closeTo(formalComparison.baseline.mae,0.7,'MAE must use blind submission baseline');
  closeTo(formalComparison.assisted.mae,0.6,'MAE must use blind submission assisted');
  closeTo(formalComparison.improvement,0.1,'MAE improvement must use blind scores');
  assert.ok(Math.abs(formalComparison.baseline.mae-1/6)>1e-9,'formal corrected scores must not enter MAE');
  console.log('PASS: invoker-only comparison RPC, role isolation, blind-vs-formal score separation, real 13/14 correction projection, private-path page binding, safe deduplication, read-only source tables, and 1001 SQL-fixture holdout units without truncation');
 }finally{await db.close();}
})().catch(error=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});