// Offline PGlite coverage for migration 17's workspace detail batch.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict');

const W1='41000000-0000-4000-8000-000000000001';
const W2='41000000-0000-4000-8000-000000000002';
const A='42000000-0000-4000-8000-00000000000a';
const B='42000000-0000-4000-8000-00000000000b';
const J='42000000-0000-4000-8000-00000000000c';
const X='42000000-0000-4000-8000-00000000000d';
const D1='batch-deck-main',D2='batch-deck-other';
const SA1='43000000-0000-4000-8000-000000000001';
const SA2='43000000-0000-4000-8000-000000000002';
const SB='43000000-0000-4000-8000-000000000003';
const SI='43000000-0000-4000-8000-000000000004';
const SO='43000000-0000-4000-8000-000000000005';
const SX='43000000-0000-4000-8000-000000000006';
const J1='44000000-0000-4000-8000-000000000001';
const J2='44000000-0000-4000-8000-000000000002';
const J3='44000000-0000-4000-8000-000000000003';
const J5='44000000-0000-4000-8000-000000000005';
const R1='45000000-0000-4000-8000-000000000001';
const R2='45000000-0000-4000-8000-000000000002';
const R3='45000000-0000-4000-8000-000000000003';
const R5='45000000-0000-4000-8000-000000000005';
const DV='46000000-0000-4000-8000-000000000001';
const DI='46000000-0000-4000-8000-000000000002';
const DO='46000000-0000-4000-8000-000000000003';
const DB='46000000-0000-4000-8000-000000000004';
const DX='46000000-0000-4000-8000-000000000005';
const DM='46000000-0000-4000-8000-000000000006';
const EV='47000000-0000-4000-8000-000000000001';
const EI='47000000-0000-4000-8000-000000000002';
const EO='47000000-0000-4000-8000-000000000003';
const EB='47000000-0000-4000-8000-000000000004';
const EX='47000000-0000-4000-8000-000000000005';

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
  create function auth.uid() returns uuid language sql stable as $$\n   select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid\n  $$;
  create function auth.jwt() returns jsonb language sql stable as $$\n   select jsonb_build_object('email',current_setting('request.jwt.claim.email',true))\n  $$;
  create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint);
  create table storage.objects(id uuid,name text,bucket_id text);
  alter table storage.objects enable row level security;
  create function storage.foldername(text) returns text[] language sql as $$select string_to_array($1,'/')$$;
 `);
 const schema=fs.readFileSync('cloud/schema.sql','utf8');
 const marker='-- AI_REVIEW_FOUNDATION_BODY_START';
 const markerAt=schema.indexOf(marker);
 assert.ok(markerAt>1000,'legacy schema marker must exist');
 await db.exec(schema.slice(0,markerAt));
 const names=fs.readdirSync('cloud/migrations')
  .filter(name=>/^20261121_(0[1-9]|1[0-7])_.+\.sql$/.test(name)).sort();
 assert.equal(names.length,17);
 for(const name of names)await db.exec(fs.readFileSync('cloud/migrations/'+name,'utf8'));
 await db.exec('grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role; grant all on all tables in schema public,storage to service_role;');
 return db;
}
async function resetRole(db){await db.exec('reset role');}
async function call(db,actor=A,workspace=W1,deck=D1,ids=[SA1,SA2,SB,SI,SO,SX],role='service_role'){
 await resetRole(db);await db.exec(`set role ${role}`);
 try{return (await db.query('select public.ai_review_workspace_batch($1::uuid,$2::uuid,$3::text,$4::uuid[]) as value',[workspace,actor,deck,ids])).rows[0].value;}
 finally{await resetRole(db);}
}
const deckData=(id,unit)=>({id,name:id,title:id,images:[{id:unit,name:unit+'.png',path:W1+'/'+id+'/'+unit+'.png'}]});

(async()=>{
 const db=await bootstrap();
 try{
  await resetRole(db);
  await db.query('insert into auth.users(id) values ($1),($2),($3),($4)',[A,B,J,X]);
  await db.query("insert into workspaces(id,name,owner_email,invite_hash) values($1,'batch-one','one@example.test','i1'),($2,'batch-two','two@example.test','i2')",[W1,W2]);
  await db.query("insert into members(workspace_id,user_id,name,role) values($1,$2,'A','admin'),($1,$3,'B','reviewer'),($1,$4,'J','admin'),($5,$6,'X','admin')",[W1,A,B,J,W2,X]);
  await db.query('insert into decks(id,workspace_id,source_kind,data) values($1,$2,$3,$4::jsonb),($5,$6,$3,$7::jsonb)',[D1,W1,'uploaded',JSON.stringify(deckData(D1,'p1')),D2,W2,JSON.stringify(deckData(D2,'p1'))]);
  const inserted=await db.query(`select id from workspaces where id in ($1,$2)`,[W1,W2]);
  assert.equal(inserted.rows.length,2);

  const submission=(id,workspace,deck,actor,unit,version,scores,created)=>db.query(
   `insert into ai_review_submissions(id,workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,deck_version,rubric_version,human_version,max_score,entry,created_at)
    values($1,$2,$3,'uploaded','page',$4,1,$5,$6,'rubric-v1',$7,5,$8::jsonb,$9)`,
   [id,workspace,deck,unit,actor,version,id,JSON.stringify({scores}),created]
  );
  await submission(SA1,W1,D1,A,'p1','deck-v1',{visual:4,layout:4},'2026-11-21T00:00:01Z');
  await submission(SA2,W1,D1,A,'p1','deck-v1',{visual:4,layout:4},'2026-11-21T00:00:02Z');
  await submission(SB,W1,D1,B,'p1','deck-v1',{visual:4,layout:4},'2026-11-21T00:00:03Z');
  await submission(SI,W1,D1,A,'p2','deck-v1',{},'2026-11-21T00:00:04Z');
  await submission(SO,W1,D1,A,'p1','deck-v1',{visual:4,layout:4},'2026-11-21T00:00:05Z');
  await submission(SX,W2,D2,A,'p1','deck-v1',{visual:4,layout:4},'2026-11-21T00:00:06Z');

  const job=(id,workspace,deck,unit,version,variant='baseline')=>db.query(
   `insert into ai_review_jobs(id,workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score,status,judge_user_id)
    values($1,$2,$3,'uploaded','page',$4,1,$5,1,'calibration',$6,'rubric-v1',5,'completed',$7)`,
   [id,workspace,deck,unit,variant,version,J]
  );
  await job(J1,W1,D1,'p1','deck-v1');
  await job(J2,W1,D1,'p2','deck-v1');
  await job(J3,W1,D1,'p1','old-version');
  await job(J5,W2,D2,'p1','deck-v1');
  const record=(id,job,workspace,deck,unit,version)=>
   db.query(`insert into ai_review_records(id,job_id,workspace_id,deck_id,source_kind,scope,unit_id,variant,bucket,split,deck_version,rubric_version,max_score,model_hint,ai_result)
    values($1,$2,$3,$4,'uploaded','page',$5,'baseline',1,'calibration',$6,'rubric-v1',5,'gpt-test',$7::jsonb)`,
    [id,job,workspace,deck,unit,version,JSON.stringify({scores:{visual:4,layout:4},summary:'batch'})]);
  await record(R1,J1,W1,D1,'p1','deck-v1');
  await record(R2,J2,W1,D1,'p2','deck-v1');
  await record(R3,J3,W1,D1,'p1','old-version');
  await record(R5,J5,W2,D2,'p1','deck-v1');
  await db.query("insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,'baseline'),($3,$2,'baseline'),($4,$2,'baseline'),($5,$6,'baseline'),($7,$8,'baseline'),($9,$10,'baseline')",[SA1,R1,SA2,SB,SI,R2,SO,R3,SX,R5]);
  await db.query("insert into ai_review_matches(submission_id,record_id,variant) values($1,$2,'baseline')",[SA1,R5]);

  const decision=(id,workspace,deck,submission,record,key,created)=>db.query(
   `insert into ai_review_decisions(id,workspace_id,deck_id,submission_id,ai_record_id,judge_user_id,decision_key,status,reasons,formal_scores,formal_total,deck_version,rubric_version,max_score,score_ratio,summary,created_at)
    values($1,$2,$3,$4,$5,$6,$7,'confirmed','[]'::jsonb,'{"visual":4,"layout":4}'::jsonb,4,'deck-v1','rubric-v1',5,0.8,'old decision',$8)`,
   [id,workspace,deck,submission,record,J,key,created]
  );
  await decision(DV,W1,D1,SA1,R1,'dv','2026-11-21T00:10:01Z');
  await decision(DI,W1,D1,SI,R2,'di','2026-11-21T00:10:02Z');
  await decision(DO,W1,D1,SO,R3,'do','2026-11-21T00:10:03Z');
  await decision(DB,W1,D1,SB,R1,'db','2026-11-21T00:10:04Z');
  await decision(DX,W2,D2,SX,R5,'dx','2026-11-21T00:10:05Z');
  await decision(DM,W1,D1,SA1,R5,'dm','2026-11-21T00:10:06Z');
  const entry=(id,workspace,deck,decision,visible)=>
   db.query(`insert into ai_generated_entries(id,workspace_id,deck_id,decision_id,scope,unit_id,formal_scores,weights,human_threshold,library_visible,created_at)
    values($1,$2,$3,$4,'page','p1','{"visual":4,"layout":4}'::jsonb,'{"visual":50,"layout":50}'::jsonb,0.8,$5,now())`,[id,workspace,deck,decision,visible]);
  await entry(EV,W1,D1,DV,true);
  await entry(EI,W1,D1,DI,false);
  await entry(EO,W1,D1,DO,true);
  await entry(EB,W1,D1,DB,true);
  await entry(EX,W2,D2,DX,true);

  const meta=(await db.query(`select p.prosecdef,p.provolatile,p.proretset,pg_get_function_result(p.oid) as result_type,
   coalesce(array_to_string(p.proconfig,','),'') as config
   from pg_proc p where p.oid=to_regprocedure('public.ai_review_workspace_batch(uuid,uuid,text,uuid[])')`)).rows[0];
  assert.deepEqual(meta,{prosecdef:false,provolatile:'s',proretset:false,result_type:'jsonb',config:'search_path=""'});
  const acl=(await db.query(`select
   has_function_privilege('service_role','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','execute') as service,
   has_function_privilege('authenticated','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','execute') as authenticated,
   has_function_privilege('anon','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','execute') as anon`)).rows[0];
  assert.deepEqual(acl,{service:true,authenticated:false,anon:false});
  for(const role of ['authenticated','anon']){
   let denied=false;try{await call(db,A,W1,D1,[SA1],role);}catch(error){denied=/permission denied/.test(error.message);}
   assert.equal(denied,true,role+' must not execute the function');
  }

  const result=await call(db,A);
  assert.deepEqual(Object.keys(result).sort(),['decisions','records']);
  assert.deepEqual(result.records.map(row=>row.id),[R1],'complete actor records must be unique and current-version only');
  assert.equal(result.records[0].ai_result.scores.visual,4);
  assert.equal(result.records[0].submission_id,undefined,'record public shape is preserved by the Python projection');
  const decisions=Object.fromEntries(result.decisions.map(row=>[row.id,row]));
  assert.deepEqual(Object.keys(decisions).sort(),[DI,DM,DO,DV].sort(),'only this reviewer and workspace decisions are visible');
  assert.equal(decisions[DV]._library_visible,true);assert.equal(decisions[DV]._entry_id,EV);
  assert.equal(decisions[DI]._library_visible,false);assert.equal(decisions[DI]._entry_id,EI);
  assert.equal(decisions[DO]._library_visible,true,'old decisions remain visible');
  assert.equal(decisions[DM].ai_record_id,null,'cross-workspace linked record ID must not leak');
  assert.equal(decisions[DM]._library_visible,false);
  assert.equal(decisions[DB],undefined,'same-workspace other reviewer decisions stay isolated');
  assert.equal(decisions[DX],undefined,'cross-workspace decisions stay isolated');

  const other=await call(db,B,W1,D1,[SA1,SA2,SB,SI,SO]);
  const otherDecisions=Object.fromEntries(other.decisions.map(row=>[row.id,row]));
  assert.deepEqual(Object.keys(otherDecisions),[DB]);
  assert.deepEqual(other.records.map(row=>row.id),[R1]);

  let tooLarge=false;try{await call(db,A,W1,D1,Array.from({length:1001},()=>SA1));}catch(error){tooLarge=/提交批次过大/.test(error.message);}
  assert.equal(tooLarge,true,'the batch must reject more than 1000 IDs before scanning relations');
  let outsider=false;try{await call(db,X,W1,D1,[SA1]);}catch(error){outsider=/请先加入团队/.test(error.message);}
  assert.equal(outsider,true,'actor membership must be revalidated');
  console.log('PASS review workspace batch SQL');
 }finally{await db.close();}
})().catch(error=>{console.error(error.stack||error.message);process.exit(1);});
