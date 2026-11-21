// npm install --prefix .test-output/sql @electric-sql/pglite
// Real SQL checks for uploaded source provenance and the M19 import RPC.
const {PGlite}=require('./.test-output/sql/node_modules/@electric-sql/pglite');
const {pgcrypto}=require('./.test-output/sql/node_modules/@electric-sql/pglite/dist/contrib/pgcrypto.cjs');
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto');

const W='31000000-0000-4000-8000-000000000000';
const W2='32000000-0000-4000-8000-000000000000';
const A='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const B='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const C='cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const schema=fs.readFileSync('cloud/schema.sql','utf8');
const migration=name=>fs.readFileSync(`cloud/migrations/20261121_${name}.sql`,'utf8');
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');

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
 await db.exec(schema);
 for(const name of [
  '02_ai_reviews','03_ai_planning','04_ai_generation','05_review_queue',
  '06_generation_history','07_review_calibration','08_review_comparison','09_review_backfill',
  '10_review_queue_listing','11_planning_history','12_library_classification',
  '13_formal_correction','14_library_formal_scores','15_planning_original_references',
  '16_uploaded_baseline','17_review_workspace_batch','18_review_backfill_classification'
 ]) await db.exec(migration(name));
 await db.exec(`
  grant usage on schema public,auth,storage,extensions to authenticated,anon,service_role;
  grant all on all tables in schema public,storage to service_role;
 `);
 return db;
}

async function reset(db){await db.exec('reset role');}
async function asService(db){await reset(db);await db.exec('set role service_role');}
async function loginAs(db,userId){await reset(db);await db.query("select set_config('request.jwt.claim.sub',$1,false)",[userId]);await db.exec('set role authenticated');}

function deck(w,id,label,extra={}){
 return {
  id,name:`作品 ${label}`,title:`作品 ${label}`,origin:'import',
  images:[{
   id:`${id}-page-1`,
   name:'0001.png',
   path:`${w}/${id}/page-1.png`,
   thumbPath:`${w}/${id}/page-1.png`,
  }],
  ...extra,
 };
}

async function importDeck(db,w,actor,value,hash){
 await asService(db);
 try{
  await db.query(
   'select public.ai_import_review_deck($1::uuid,$2::uuid,$3::jsonb,$4::text)',
   [w,actor,JSON.stringify(value),hash]
  );
 }finally{await reset(db);}
}

async function setupWorkspace(db){
 await db.query(
  "insert into workspaces(id,name,owner_email,invite_hash) values($1,'provenance test','owner@example.test','x')",
  [W]
 );
 await db.query(
  "insert into workspaces(id,name,owner_email,invite_hash) values($1,'second workspace','second@example.test','y')",
  [W2]
 );
 for(const [id,name,role] of [[A,'Admin','admin'],[B,'Reviewer','reviewer'],[C,'Outsider','reviewer']]){
  await db.query('insert into auth.users(id) values($1) on conflict(id) do nothing',[id]);
  if(id!==C) await db.query(
   'insert into members(workspace_id,user_id,name,role) values($1,$2,$3,$4)',
   [W,id,name,role]
  );
 }
}

async function main(){
 const db=await bootstrap();
 try{
  await setupWorkspace(db);
  // A deck imported before M19 must remain untagged: there is no inferred backfill.
  const legacy=deck(W,'p19-legacy-uploaded','legacy');
  await db.query(
   "insert into decks(id,workspace_id,source_kind,data) values($1,$2,'uploaded',$3::jsonb)",
   [legacy.id,W,JSON.stringify(legacy)]
  );
  await db.exec(migration('19_uploaded_source_provenance'));

  const objects=(await db.query(`select
   to_regclass('guanye_internal.uploaded_source_provenance') is not null as provenance_table,
   (select count(*) from information_schema.columns where table_schema='guanye_internal'
    and table_name='uploaded_source_provenance' and column_name in ('deck_id','workspace_id','source_group_sha256','created_at'))=4 as provenance_columns,
   exists(select 1 from pg_constraint c where c.conname='uploaded_source_provenance_deck_fk'
    and c.conrelid='guanye_internal.uploaded_source_provenance'::regclass
    and c.confrelid='public.decks'::regclass and c.condeferrable and c.condeferred and c.confdeltype='c') as provenance_fk,
   exists(select 1 from pg_trigger t where t.tgname='uploaded_source_provenance_workspace_guard'
    and t.tgrelid='guanye_internal.uploaded_source_provenance'::regclass
    and t.tgdeferrable and t.tginitdeferred and not t.tgisinternal) as workspace_guard,
   to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)') is not null as rpc,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)')
   and p.prosecdef and position('UPLOADED_SOURCE_PROVENANCE_M19' in p.prosrc)>0
    and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value) where config.value in ('search_path=','search_path=""'))) as rpc_shape,
   has_function_privilege('service_role','public.ai_import_review_deck(uuid,uuid,jsonb,text)','EXECUTE') as rpc_service,
   not has_function_privilege('authenticated','public.ai_import_review_deck(uuid,uuid,jsonb,text)','EXECUTE') as rpc_authenticated_denied,
   not has_function_privilege('anon','public.ai_import_review_deck(uuid,uuid,jsonb,text)','EXECUTE') as rpc_anon_denied,
   not has_table_privilege('service_role','guanye_internal.uploaded_source_provenance','SELECT') as table_service_private,
   not has_table_privilege('authenticated','guanye_internal.uploaded_source_provenance','SELECT') as table_authenticated_private,
   not has_table_privilege('anon','guanye_internal.uploaded_source_provenance','SELECT') as table_anon_private,
  to_regclass('guanye_internal.uploaded_source_provenance_source_group_idx') is not null as source_group_index,
  to_regprocedure('public.ai_review_source_group_decks(uuid,text)') is not null as split_rpc,
  has_function_privilege('service_role','public.ai_review_source_group_decks(uuid,text)','EXECUTE') as split_service,
  not has_function_privilege('authenticated','public.ai_review_source_group_decks(uuid,text)','EXECUTE') as split_authenticated_denied,
  not has_function_privilege('anon','public.ai_review_source_group_decks(uuid,text)','EXECUTE') as split_anon_denied,
   (select count(*)::int from guanye_internal.uploaded_source_provenance)=0 as no_backfill`)).rows[0];
  assert.deepEqual(objects,{
   provenance_table:true,provenance_columns:true,provenance_fk:true,workspace_guard:true,
   rpc:true,rpc_shape:true,rpc_service:true,rpc_authenticated_denied:true,rpc_anon_denied:true,
   table_service_private:true,table_authenticated_private:true,table_anon_private:true,
   source_group_index:true,split_rpc:true,split_service:true,split_authenticated_denied:true,split_anon_denied:true,no_backfill:true,
  });

  for(const role of ['authenticated','anon','service_role']){
   await reset(db);await db.exec(`set role ${role}`);
   let failed=false;
   try{
    await db.query("insert into guanye_internal.uploaded_source_provenance(deck_id,workspace_id,source_group_sha256) values('forged',$1,$2)",[W,'f'.repeat(64)]);
   }catch(error){failed=true;}
   assert.equal(failed,true,`${role} must not write the private provenance table`);
   await reset(db);
  }
  assert.equal((await db.query('select count(*)::int as n from guanye_internal.uploaded_source_provenance')).rows[0].n,0);

  const sameBytes=Buffer.from('same source bytes');
  const sameHash=sha(sameBytes);
  const first=deck(W,'p19-same-a','same A');
  const second=deck(W,'p19-same-b','same B');
  await importDeck(db,W,B,first,sameHash);
  await importDeck(db,W,B,second,sameHash);
  const different=deck(W,'p19-different','different');
  const differentHash=sha(Buffer.from('different source bytes'));
  await importDeck(db,W,B,different,differentHash);

  const hashes=(await db.query(
   "select deck_id,source_group_sha256 from guanye_internal.uploaded_source_provenance where deck_id=any($1::text[]) order by deck_id",
   [[first.id,second.id,different.id]]
  )).rows;
  assert.equal(hashes.length,3);
  const hashById=Object.fromEntries(hashes.map(row=>[row.deck_id,row.source_group_sha256]));
  assert.equal(hashById[first.id],sameHash);
  assert.equal(hashById[second.id],sameHash);
  assert.equal(hashById[different.id],differentHash);
  assert.notEqual(hashById[first.id],hashById[different.id]);
  await asService(db);
  const sourceGroup=(await db.query('select public.ai_review_source_group_decks($1,$2) as value',[W,sameHash])).rows[0].value;
  await reset(db);
  assert.deepEqual([...sourceGroup].sort(),[first.id,second.id].sort());
  assert.equal(JSON.stringify(sourceGroup).includes(sameHash),false,'source group RPC must not return the hash');

  const stored=(await db.query('select source_kind,data from decks where id=$1',[first.id])).rows[0];
  assert.equal(stored.source_kind,'uploaded');
  assert.deepEqual(stored.data,first);
  assert.equal(Object.hasOwn(stored.data,'source_group_sha256'),false);
  assert.equal(Object.hasOwn(stored.data,'sourceGroupSha256'),false);
  assert.deepEqual((await db.query('select data from decks where id=$1',['p19-legacy-uploaded'])).rows[0].data,legacy);
  assert.equal((await db.query("select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id='p19-legacy-uploaded'")).rows[0].n,0);

  for(const role of ['authenticated','anon']){
   await reset(db);await db.exec(`set role ${role}`);
   let failed=false;
   try{
    await db.query('select public.ai_import_review_deck($1::uuid,$2::uuid,$3::jsonb,$4::text)',[
     W,B,JSON.stringify(deck(W,`p19-denied-${role}`,'denied')),'a'.repeat(64),
    ]);
   }catch(error){failed=true;}
   assert.equal(failed,true,`${role} must not execute the provenance RPC`);
   let splitFailed=false;
   try{await db.query('select public.ai_review_source_group_decks($1,$2)',[W,sameHash]);}catch(error){splitFailed=true;}
   assert.equal(splitFailed,true,`${role} must not execute the source group RPC`);
   await reset(db);
  }

  await assert.rejects(()=>importDeck(db,W,C,deck(W,'p19-outsider','outsider'),'b'.repeat(64)),/请先加入团队/);
  assert.equal((await db.query("select count(*)::int as n from decks where id='p19-outsider'")).rows[0].n,0);
  assert.equal((await db.query("select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id='p19-outsider'")).rows[0].n,0);
  await assert.rejects(()=>importDeck(db,W,B,deck(W,'p19-upper','upper'),'A'.repeat(64)),/源文件哈希无效/);
  await assert.rejects(()=>importDeck(db,W,B,deck(W,'p19-injected','injected',{source_group_sha256:'f'.repeat(64)}),'c'.repeat(64)),/作品数据不得包含来源指纹/);
  await assert.rejects(()=>importDeck(db,W,B,deck(W,'p19-camel-injected','camel',{sourceGroupSha256:'f'.repeat(64)}),'c'.repeat(64)),/作品数据不得包含来源指纹/);
  await assert.rejects(()=>importDeck(db,W,B,deck(W,'p19-generated-forged','forged',{source_kind:'generated'}),'c'.repeat(64)),/只允许新建 uploaded 作品/);
  await assert.rejects(()=>importDeck(db,W,B,deck(W,'p19-cross-path','cross path',{images:[{
   id:'cross-path-page',name:'0001.png',path:`${W2}/p19-cross-path/page.png`,thumbPath:`${W2}/p19-cross-path/page.png`,
  }]}),'c'.repeat(64)),/作品预览路径无效/);
  await assert.rejects(()=>importDeck(db,W,B,deck(W,'p19-cross-thumb','cross thumb',{images:[{
   id:'cross-thumb-page',name:'0001.png',path:`${W}/p19-cross-thumb/page.png`,thumbPath:`${W2}/p19-cross-thumb/page.png`,
  }]}),'c'.repeat(64)),/作品预览路径无效/);
  for(const rejectedId of ['p19-injected','p19-camel-injected','p19-generated-forged','p19-cross-path','p19-cross-thumb']){
   assert.equal((await db.query('select count(*)::int as n from decks where id=$1',[rejectedId])).rows[0].n,0,`${rejectedId} must not create a deck`);
   assert.equal((await db.query('select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id=$1',[rejectedId])).rows[0].n,0,`${rejectedId} must not create provenance`);
  }

  await assert.rejects(()=>importDeck(db,W,B,first,sameHash),/作品标识冲突/);
  const conflict=(await db.query('select data from decks where id=$1',[first.id])).rows[0].data;
  assert.deepEqual(conflict,first);
  assert.equal((await db.query('select source_group_sha256 from guanye_internal.uploaded_source_provenance where deck_id=$1',[first.id])).rows[0].source_group_sha256,sameHash);

  // A deferred guard denies provenance/deck workspace mismatches at commit.
  await assert.rejects(()=>db.query(
   "insert into guanye_internal.uploaded_source_provenance(deck_id,workspace_id,source_group_sha256) values($1,$2,$3)",
   [legacy.id,W2,'d'.repeat(64)]
  ),/来源记录与作品工作区不一致/);
  assert.equal((await db.query('select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id=$1',[legacy.id])).rows[0].n,0);

  await db.exec(`
   create or replace function public.p19_before_deck_insert() returns trigger
   language plpgsql set search_path='' as $$
   begin
    if not exists(select 1 from guanye_internal.uploaded_source_provenance where deck_id=new.id) then
     raise exception 'provenance must be inserted before deck';
    end if;
    if new.id='p19-rollback-deck' then raise exception 'forced deck insert failure'; end if;
    return new;
   end $$;
   create trigger p19_before_deck_insert before insert on public.decks
   for each row when (new.id in ('p19-order-deck','p19-rollback-deck'))
   execute function public.p19_before_deck_insert();
  `);
  const ordered=deck(W,'p19-order-deck','ordered');
  await importDeck(db,W,B,ordered,'e'.repeat(64));
  assert.equal((await db.query("select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id='p19-order-deck'")).rows[0].n,1);
  await assert.rejects(()=>importDeck(db,W,B,deck(W,'p19-rollback-deck','rollback'),'f'.repeat(64)),/forced deck insert failure/);
  assert.equal((await db.query("select count(*)::int as n from decks where id='p19-rollback-deck'")).rows[0].n,0);
  assert.equal((await db.query("select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id='p19-rollback-deck'")).rows[0].n,0);

  // The old authenticated add_review_deck path still writes a merged deck and
  // deliberately records no provenance because its source group is unknown.
  const merged=deck(W,'p19-legacy-merged','merged');
  await loginAs(db,B);
  await db.query('select public.add_review_deck($1::uuid,$2::jsonb)',[W,JSON.stringify(merged)]);
  await reset(db);
  assert.deepEqual((await db.query('select data from decks where id=$1',[merged.id])).rows[0].data,merged);
  assert.equal((await db.query("select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id='p19-legacy-merged'")).rows[0].n,0);

  await db.query('delete from decks where id=$1',[first.id]);
  assert.equal((await db.query('select count(*)::int as n from guanye_internal.uploaded_source_provenance where deck_id=$1',[first.id])).rows[0].n,0);
  console.log('PASS uploaded source provenance M19');
 }finally{await db.close();}
}

main().catch(error=>{console.error(error.stack||error.message);process.exit(1);});
