// npm install --prefix .test-output/sql @electric-sql/pglite
// Real PGlite checks for M01 -> M21 comparison source dedup.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');

const W='71000000-0000-4000-8000-000000000000';
const EMPTY_W='72000000-0000-4000-8000-000000000000';
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const SETTINGS={revision:0,maxScore:10,dimensions:[{id:'visual',name:'视觉'},{id:'layout',name:'排版'},{id:'match',name:'匹配'}]};
const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const legacySchema=schema.slice(0,schema.indexOf(marker));
assert.ok(legacySchema.length>1000,'legacy schema slice must exist');
const migrations=[
 '20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning',
 '20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history',
 '20261121_07_review_calibration','20261121_08_review_comparison','20261121_09_review_backfill',
 '20261121_10_review_queue_listing','20261121_11_planning_history','20261121_12_library_classification',
 '20261121_13_formal_correction','20261121_14_library_formal_scores','20261121_15_planning_original_references',
 '20261121_16_uploaded_baseline','20261121_17_review_workspace_batch','20261121_18_review_backfill_classification',
 '20261121_19_uploaded_source_provenance','20261121_20_uploaded_source_groups',
 '20261121_21_uploaded_comparison_source_dedup'
];
const migration=name=>fs.readFileSync(`cloud/migrations/${name}.sql`,'utf8');
const md5Bucket=id=>crypto.createHash('md5').update(id).digest()[0]%5;
const sourceBucket=bytes=>crypto.createHash('sha256').update(bytes).digest()[0]%5;
function idForBucket(prefix,bucket){
 for(let index=0;index<100000;index++){
  const id=`${prefix}-${index}`;
  if(md5Bucket(id)===bucket)return id;
 }
 throw new Error(`id bucket ${bucket} not found`);
}
function bytesForBucket(prefix,bucket){
 for(let index=0;index<100000;index++){
  const value=Buffer.from(`${prefix}-${index}`);
  if(sourceBucket(value)===bucket)return value;
 }
 throw new Error(`source bucket ${bucket} not found`);
}
function closeTo(actual,expected,label){
 assert.equal(typeof actual,'number',`${label} must be numeric`);
 assert.ok(Math.abs(actual-expected)<1e-9,`${label}: expected ${expected}, got ${actual}`);
}

async function bootstrap(through){
 const db=new PGlite({extensions:{pgcrypto}});
 await db.exec(`
  create schema auth; create schema storage; create schema extensions;
  create role anon; create role authenticated; create role service_role bypassrls;
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
 for(let index=0;index<through;index++) await db.exec(migration(migrations[index]));
 await db.exec(`grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role;
  grant all on all tables in schema public,storage to service_role;`);
 return db;
}
async function reset(db){await db.exec('reset role');}
async function asService(db){await reset(db);await db.exec('set role service_role');}
async function asRole(db,role){await reset(db);await db.exec(`set role ${role}`);}
async function comparison(db,w){return (await db.query('select public.ai_review_comparison($1) as value',[w])).rows[0].value;}

async function insertWorkspace(db,id,label){
 await db.query("insert into workspaces(id,name,owner_email,invite_hash,settings) values($1,$2,$3,$4,$5::jsonb)",[
  id,label,`${label}@example.test`,`invite-${label}`,JSON.stringify(SETTINGS)
 ]);
}
async function addMembers(db){
 await db.query('insert into auth.users(id) values($1),($2)',[A,B]);
 await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'Judge','admin'),($1,$3,'Reviewer','reviewer')",[W,A,B]);
}
function deckData(w,id,units,source='uploaded',fakeHash=false){
 const images=units.map(unitId=>({
  id:unitId,name:`${unitId}.png`,path:`${w}/${id}/${unitId}.png`,thumbPath:`${w}/${id}/${unitId}.png`,position:units.indexOf(unitId)+1
 }));
 const value={id,name:id,title:id,origin:source==='generated'?'generated':'import',images};
 if(source==='generated')value.generation={outline:{title:id},style:{name:'AI'},pages:images.map(image=>({...image})),references:[]};
 if(fakeHash){
  value.source_group_sha256='f'.repeat(64);
  value.sourceGroupSha256='e'.repeat(64);
 }
 return value;
}
async function importDeck(db,w,id,hash,units){
 await asService(db);
 try{
  await db.query('select public.ai_import_review_deck($1::uuid,$2::uuid,$3::jsonb,$4::text)',[
   w,B,JSON.stringify(deckData(w,id,units)),hash,
  ]);
 }finally{await reset(db);}
}
async function insertDeck(db,id,source='uploaded',units=['p1'],fakeHash=false){
 await db.query('insert into decks(id,workspace_id,source_kind,data) values($1,$2,$3,$4::jsonb)',[
  id,W,source,JSON.stringify(deckData(W,id,units,source,fakeHash)),
 ]);
}
async function setDeckCreatedAt(db,id,value){await db.query('update decks set created_at=$1 where id=$2',[value,id]);}
async function versions(db,deckId){
 return (await db.query(`select public.ai_review_deck_version($1,$2) as "deckVersion",
  public.ai_review_rubric_version($1) as "rubricVersion"`,[W,deckId])).rows[0];
}
async function insertRecord(db,{deckId,kind='uploaded',scope='page',unitId,variant,scores,
 deckVersion,rubricVersion,maxScore=10,bucket=0,split='holdout'}){
 const jobId=crypto.randomUUID(),recordId=crypto.randomUUID();
 await db.query(`insert into ai_review_jobs(
  id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,
  deck_version,rubric_version,max_score,status,attempts,judge_user_id)
  values($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,$11,$12,'completed',1,$13)`,[
  jobId,W,deckId,kind,scope,unitId,variant,bucket,split,deckVersion,rubricVersion,maxScore,A
 ]);
 await db.query(`insert into ai_review_records(
  id,job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,
  deck_version,rubric_version,max_score,model_hint,ai_result,judge_user_id)
  values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'M21-test',$14::jsonb,$15)`,[
  recordId,jobId,W,deckId,kind,scope,unitId,variant,bucket,split,deckVersion,rubricVersion,maxScore,
  JSON.stringify({scores}),A
 ]);
 return {jobId,recordId,variant,kind,scope,unitId,deckVersion,rubricVersion,maxScore};
}
async function insertSubmission(db,{deckId,kind='uploaded',scope='page',unitId,human,v,humanUser=B}){
 const submissionId=crypto.randomUUID();
 await db.query(`insert into ai_review_submissions(
  id,workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,
  deck_version,rubric_version,human_version,max_score,entry)
  values($1,$2,$3,$4,$5,$6,0,$7,$8,$9,$10,10,$11::jsonb)`,[
  submissionId,W,deckId,kind,scope,unitId,humanUser,v.deckVersion,v.rubricVersion,
  `human-${crypto.randomUUID()}`,JSON.stringify({scores:human})
 ]);
 return submissionId;
}
async function linkRecord(db,submissionId,record){
 await db.query('insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,$3)',[
  submissionId,record.recordId,record.variant
 ]);
}
async function seedFormalPair(db,{deckId,kind='uploaded',unitId,human,baselineScores,assistedScores}){
 const v=await versions(db,deckId),submissionId=await insertSubmission(db,{deckId,kind,unitId,human,v});
 const baseline=await insertRecord(db,{deckId,kind,unitId,variant:'baseline',scores:baselineScores,deckVersion:v.deckVersion,rubricVersion:v.rubricVersion});
 const assisted=await insertRecord(db,{deckId,kind,unitId,variant:'assisted',scores:assistedScores,deckVersion:v.deckVersion,rubricVersion:v.rubricVersion});
 await linkRecord(db,submissionId,baseline);
 await linkRecord(db,submissionId,assisted);
 return submissionId;
}
async function seedOneSide(db,{deckId,kind='uploaded',unitId,human,baselineScores}){
 const v=await versions(db,deckId),submissionId=await insertSubmission(db,{deckId,kind,unitId,human,v});
 const baseline=await insertRecord(db,{deckId,kind,unitId,variant:'baseline',scores:baselineScores,deckVersion:v.deckVersion,rubricVersion:v.rubricVersion});
 await linkRecord(db,submissionId,baseline);
 return submissionId;
}
async function insertLegacyReview(db,{deckId,data,userId=B}){
 await db.query('insert into reviews(workspace_id,user_id,deck_id,data) values($1,$2,$3,$4::jsonb)',[
  W,userId,deckId,JSON.stringify(data)
 ]);
}
async function sourceSnapshot(db){
 await reset(db);
 const row=(await db.query(`select
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
  md5(coalesce((select jsonb_agg(to_jsonb(m) order by m.id) from public.ai_review_matches m),'[]'::jsonb)::text) as matches_hash,
  (select count(*)::integer from public.ai_review_decisions) as decisions_count,
  (select count(*)::integer from guanye_internal.uploaded_source_provenance) as provenance_count,
  md5(coalesce((select jsonb_agg(to_jsonb(p) order by p.deck_id) from guanye_internal.uploaded_source_provenance p),'[]'::jsonb)::text) as provenance_hash,
  md5(coalesce((select jsonb_agg(to_jsonb(o) order by o.id) from storage.objects o),'[]'::jsonb)::text) as storage_hash`)).rows[0];
 await reset(db);
 return row;
}

(async()=>{
 const db=await bootstrap(20);
 try{
  const absent=(await db.query("select to_regprocedure('public.ai_review_comparison(uuid)') is not null as comparison, to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])') is not null as dedup")).rows[0];
  assert.deepEqual(absent,{comparison:true,dedup:false},'M21 helper must be absent before M21');
  await insertWorkspace(db,W,'M21 comparison');
  await insertWorkspace(db,EMPTY_W,'M21 empty');
  await addMembers(db);

  const sourceA=bytesForBucket('m21-source-a',0),hashA=crypto.createHash('sha256').update(sourceA).digest('hex');
  const sourceB=bytesForBucket('m21-source-b',0),hashB=crypto.createHash('sha256').update(sourceB).digest('hex');
  assert.equal(sourceBucket(sourceA),0);assert.equal(sourceBucket(sourceB),0);
  const rep=idForBucket('m21-rep',0),dup=idForBucket('m21-dup',1);
  const noCommon=idForBucket('m21-nocommon',2),oneSide=idForBucket('m21-oneside',3);
  const tieA='dedup-tie-a',tieZ='dedup-tie-z';
  const unknownA=idForBucket('unknown-fake-a',0),unknownB=idForBucket('unknown-fake-b',0);
  const generated=idForBucket('generated-m21',0);
  assert.equal(md5Bucket(rep),0);assert.notEqual(md5Bucket(dup),0,'same-source IDs must exercise old holdout/calibration disagreement');
  assert.notEqual(md5Bucket(noCommon),0);assert.notEqual(md5Bucket(oneSide),0);

  // Trusted imports happen before a dedicated judge is assigned, so the M05
  // deck trigger does not create unrelated queue work for these fixtures.
  await importDeck(db,W,noCommon,hashA,['nocommon-p1']);
  await importDeck(db,W,oneSide,hashA,['oneside-p1']);
  await importDeck(db,W,rep,hashA,['rep-p1','rep-p2']);
  await importDeck(db,W,dup,hashA,['dup-p1','dup-p2','dup-p3']);
  await importDeck(db,W,tieA,hashB,['tie-a-p1']);
  await importDeck(db,W,tieZ,hashB,['tie-z-p1']);
  await insertDeck(db,unknownA,'uploaded',['unknown-a-p1'],true);
  await insertDeck(db,unknownB,'uploaded',['unknown-b-p1'],true);
  await insertDeck(db,generated,'generated',['generated-p1']);

  await setDeckCreatedAt(db,noCommon,'2026-01-01T00:00:00Z');
  await setDeckCreatedAt(db,oneSide,'2026-01-01T00:01:00Z');
  await setDeckCreatedAt(db,rep,'2026-01-02T00:00:00Z');
  await setDeckCreatedAt(db,dup,'2026-01-03T00:00:00Z');
  await setDeckCreatedAt(db,tieA,'2026-01-04T00:00:00Z');
  await setDeckCreatedAt(db,tieZ,'2026-01-04T00:00:00Z');
  await db.query('update workspaces set dedicated_judge_id=$1 where id=$2',[A,W]);

  // Earlier same-source deck with both variants but no common numeric dimension.
  await seedFormalPair(db,{deckId:noCommon,unitId:'nocommon-p1',human:{visual:5,layout:5,match:5},baselineScores:{visual:4},assistedScores:{layout:6}});
  // Earlier same-source deck with an unpaired variant.
  await seedOneSide(db,{deckId:oneSide,unitId:'oneside-p1',human:{visual:5,layout:5,match:5},baselineScores:{visual:4,layout:4,match:4}});

  // Representative: two qualified units. A same-user/page legacy review is
  // present but must not duplicate the formal sample.
  await seedFormalPair(db,{deckId:rep,unitId:'rep-p1',human:{visual:5,layout:5,match:5},baselineScores:{visual:4,layout:4,match:4},assistedScores:{visual:5,layout:5,match:5}});
  await seedFormalPair(db,{deckId:rep,unitId:'rep-p2',human:{visual:8,layout:8,match:8},baselineScores:{visual:7,layout:7,match:7},assistedScores:{visual:9,layout:9,match:9}});
  await insertLegacyReview(db,{deckId:rep,data:{mode:'page',scores:{visual:0,layout:0,match:0},pages:{'rep-p1':{scores:{visual:0,layout:0,match:0}}}}});

  // Later duplicate has deliberately terrible errors and extra pages.
  for(const unitId of ['dup-p1','dup-p2','dup-p3'])await seedFormalPair(db,{deckId:dup,unitId,human:{visual:0,layout:0,match:0},baselineScores:{visual:10,layout:10,match:10},assistedScores:{visual:10,layout:10,match:10}});

  // Equal timestamps: id COLLATE C keeps tie-a even though tie-z has better error.
  await seedFormalPair(db,{deckId:tieA,unitId:'tie-a-p1',human:{visual:0,layout:0,match:0},baselineScores:{visual:10,layout:10,match:10},assistedScores:{visual:0,layout:0,match:0}});
  await seedFormalPair(db,{deckId:tieZ,unitId:'tie-z-p1',human:{visual:5,layout:5,match:5},baselineScores:{visual:5,layout:5,match:5},assistedScores:{visual:5,layout:5,match:5}});

  await seedFormalPair(db,{deckId:unknownA,unitId:'unknown-a-p1',human:{visual:0,layout:0,match:0},baselineScores:{visual:2,layout:2,match:2},assistedScores:{visual:4,layout:4,match:4}});
  await seedFormalPair(db,{deckId:unknownB,unitId:'unknown-b-p1',human:{visual:10,layout:10,match:10},baselineScores:{visual:8,layout:8,match:8},assistedScores:{visual:6,layout:6,match:6}});
  await seedFormalPair(db,{deckId:generated,kind:'generated',unitId:'generated-p1',human:{visual:5,layout:5,match:5},baselineScores:{visual:3,layout:3,match:3},assistedScores:{visual:4,layout:4,match:4}});

  await asService(db);
  const m20=await comparison(db,W);
  await reset(db);
  assert.equal(m20.holdoutUnits,10,'M20 must count all uploaded repeats plus generated');
  assert.equal(m20.byKind.uploaded.holdoutUnits,9);
  assert.equal(m20.byKind.generated.holdoutUnits,1);
  assert.equal(m20.byKind.uploaded.sourceDedup,undefined,'M20 must not expose M21 counts');

  const dataBeforeM21=await sourceSnapshot(db);
  await db.exec(migration(migrations[20]));
  const dataAfterM21=await sourceSnapshot(db);
  assert.deepEqual(dataAfterM21,dataBeforeM21,'M21 migration must not rewrite source rows');

  const posture=(await db.query(`select
   not p.prosecdef as comparison_invoker,
   position('COMPARISON_SOURCE_DEDUP_V21' in p.prosrc)>0 as v21_marker,
   exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value) where config.value in ('search_path=','search_path=""')) as comparison_search_path,
   to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])') is not null as helper_rpc
   from pg_proc p where p.oid=to_regprocedure('public.ai_review_comparison(uuid)')`)).rows[0];
  assert.deepEqual(posture,{comparison_invoker:true,v21_marker:true,comparison_search_path:true,helper_rpc:true});

  const beforeComparison=await sourceSnapshot(db);
  await asService(db);
  const initial=await comparison(db,W);
  await reset(db);
  const afterComparison=await sourceSnapshot(db);
  assert.deepEqual(afterComparison,beforeComparison,'comparison RPC must be read-only');

  assert.equal(initial.holdoutUnits,6);
  assert.equal(initial.byKind.uploaded.holdoutUnits,5);
  assert.deepEqual(initial.byKind.uploaded.sourceDedup,{knownGroups:2,unknownDecks:2,excludedDuplicateDecks:2});
  closeTo(initial.byKind.uploaded.baseline.mae,0.32,'uploaded baseline MAE');
  closeTo(initial.byKind.uploaded.assisted.mae,0.18,'uploaded assisted MAE');
  closeTo(initial.byKind.uploaded.improvement,0.14,'uploaded improvement');
  assert.deepEqual(initial.byKind.generated,m20.byKind.generated,'generated statistics must not change');
  assert.equal(initial.byKind.generated.sourceDedup,undefined);
  closeTo(initial.baseline.mae,0.3,'global baseline MAE');
  closeTo(initial.assisted.mae,1/6,'global assisted MAE');
  closeTo(initial.improvement,2/15,'global improvement');

  const helperRows=(await db.query('select * from guanye_internal.ai_review_comparison_source_dedup($1,$2::text[]) order by deck_id collate "C"',[
   W,[rep,dup,tieA,tieZ,unknownA,unknownB],
  ])).rows;
  assert.deepEqual(Object.keys(helperRows[0]).sort(),['deck_id','retained','source_known']);
  const helperByDeck=Object.fromEntries(helperRows.map(row=>[row.deck_id,row]));
  assert.deepEqual(helperByDeck[rep],{deck_id:rep,source_known:true,retained:true});
  assert.deepEqual(helperByDeck[dup],{deck_id:dup,source_known:true,retained:false});
  assert.deepEqual(helperByDeck[tieA],{deck_id:tieA,source_known:true,retained:true});
  assert.deepEqual(helperByDeck[tieZ],{deck_id:tieZ,source_known:true,retained:false});
  assert.deepEqual(helperByDeck[unknownA],{deck_id:unknownA,source_known:false,retained:true});
  assert.deepEqual(helperByDeck[unknownB],{deck_id:unknownB,source_known:false,retained:true});
  assert.equal(JSON.stringify(helperRows).includes(hashA),false);
  assert.equal(JSON.stringify(helperRows).includes(hashB),false);
  assert.equal(JSON.stringify(helperRows).includes('source_group_sha256'),false);

  // A later same-source deck with different errors and extra pages may increase
  // only the duplicate counter; it must not alter representative metrics or units.
  const late=idForBucket('m21-late',4);
  await reset(db);
  await importDeck(db,W,late,hashA,['late-p1','late-p2','late-p3','late-p4']);
  await setDeckCreatedAt(db,late,'2026-01-06T00:00:00Z');
  for(const unitId of ['late-p1','late-p2','late-p3','late-p4'])await seedFormalPair(db,{deckId:late,unitId,human:{visual:0,layout:0,match:0},baselineScores:{visual:10,layout:10,match:10},assistedScores:{visual:0,layout:0,match:0}});
  const beforeLateComparison=await sourceSnapshot(db);
  await asService(db);
  const afterLate=await comparison(db,W);
  await reset(db);
  const afterLateComparison=await sourceSnapshot(db);
  assert.deepEqual(afterLateComparison,beforeLateComparison,'late-deck comparison must remain read-only');
  assert.equal(afterLate.holdoutUnits,initial.holdoutUnits);
  assert.deepEqual(afterLate.baseline,initial.baseline);
  assert.deepEqual(afterLate.assisted,initial.assisted);
  assert.equal(afterLate.improvement,initial.improvement);
  assert.equal(afterLate.byKind.uploaded.holdoutUnits,initial.byKind.uploaded.holdoutUnits);
  assert.deepEqual(afterLate.byKind.uploaded.baseline,initial.byKind.uploaded.baseline);
  assert.deepEqual(afterLate.byKind.uploaded.assisted,initial.byKind.uploaded.assisted);
  assert.deepEqual(afterLate.byKind.generated,initial.byKind.generated);
  assert.deepEqual(afterLate.byKind.uploaded.sourceDedup,{knownGroups:2,unknownDecks:2,excludedDuplicateDecks:3});

  const emptyExpected={
   holdoutUnits:0,baseline:null,assisted:null,improvement:null,
   byKind:{
    uploaded:{holdoutUnits:0,baseline:null,assisted:null,improvement:null,sourceDedup:{knownGroups:0,unknownDecks:0,excludedDuplicateDecks:0}},
    generated:{holdoutUnits:0,baseline:null,assisted:null,improvement:null}
   },
   splitAlgorithm:'md5-first-byte-mod-5-v1',holdoutBucket:0
  };
  await asService(db);
  assert.deepEqual(await comparison(db,EMPTY_W),emptyExpected);
  assert.equal(JSON.stringify(afterLate).includes(hashA),false,'source hash must not enter comparison JSON');
  assert.equal(JSON.stringify(afterLate).includes(hashB),false,'source hash must not enter comparison JSON');
  assert.equal(JSON.stringify(afterLate).includes('source_group_sha256'),false);
  assert.equal(JSON.stringify(afterLate).includes('sourceGroupSha256'),false);
  assert.ok(!/submission|record|userId|user_id|scores/i.test(JSON.stringify(afterLate)),'comparison JSON must remain aggregate-only');

  const acl=(await db.query(`select
   has_function_privilege('service_role',to_regprocedure('public.ai_review_comparison(uuid)'),'EXECUTE') as comparison_service,
   not has_function_privilege('authenticated',to_regprocedure('public.ai_review_comparison(uuid)'),'EXECUTE') as comparison_authenticated_denied,
   not has_function_privilege('anon',to_regprocedure('public.ai_review_comparison(uuid)'),'EXECUTE') as comparison_anon_denied,
   has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE') as helper_service,
   not has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE') as helper_authenticated_denied,
   not has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE') as helper_anon_denied,
   not has_table_privilege('service_role',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT') as provenance_service_private`)).rows[0];
  assert.deepEqual(acl,{comparison_service:true,comparison_authenticated_denied:true,comparison_anon_denied:true,helper_service:true,helper_authenticated_denied:true,helper_anon_denied:true,provenance_service_private:true});
  await assert.rejects(()=>db.query('select count(*) from guanye_internal.uploaded_source_provenance'),/permission denied/);

  for(const role of ['authenticated','anon']){
   await asRole(db,role);
   await assert.rejects(()=>comparison(db,W),/permission denied/i);
   await assert.rejects(()=>db.query('select * from guanye_internal.ai_review_comparison_source_dedup($1,$2::text[])',[W,[rep]]),/permission denied/i);
   await reset(db);
  }

  console.log('PASS comparison source dedup M01->M21');
 }finally{await db.close();}
})().catch(error=>{console.error(error.stack||error.message,error.where||'');process.exit(1);});
