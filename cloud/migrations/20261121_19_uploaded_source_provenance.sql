-- UPLOADED_SOURCE_PROVENANCE_M19
-- Trusted provenance for the original bytes of one uploaded PPT/PPTX file.
-- This migration does not backfill old decks and does not change the current queue
-- or comparison split. It only records a server-computed source-group hash.
--
-- Split-design reference: scikit-learn f85f0584d5d7a66068bfe50b395ac855b2289dc2,
-- sklearn/model_selection/_split.py L2165-L2177: GroupShuffleSplit splits unique
-- groups first and maps sample rows back with np.isin, so a future split must keep
-- all decks sharing this hash on the same side.
--
-- Same-transaction/security references: PostgreSQL
-- 207b5293b8d9a217bb918b5b44774b3b4f1e2f89, src/backend/commands/tablecmds.c
-- L11498-L11503 and L14949-L14954 stores and enforces deferrable/initdeferred FK
-- state; src/backend/commands/functioncmds.c L1446-L1447 and L1504-L1511 stores
-- SECURITY DEFINER and SET search_path state. The deferred FK permits provenance
-- to be inserted before its deck in the same transaction, while SECURITY DEFINER
-- with an empty search_path keeps this API server-only and schema-qualified.
begin;

create schema if not exists guanye_internal;
revoke all on schema guanye_internal from public, anon, authenticated, service_role;

create table if not exists guanye_internal.uploaded_source_provenance (
 deck_id text primary key,
 workspace_id uuid not null,
 source_group_sha256 text not null,
 created_at timestamptz not null default now(),
 constraint uploaded_source_provenance_source_group_sha256_check
  check (source_group_sha256 ~ '^[0-9a-f]{64}$'),
 constraint uploaded_source_provenance_workspace_fk
  foreign key (workspace_id) references public.workspaces(id) on delete cascade
);

do $uploaded_source_provenance_fk$
begin
 if not exists (
  select 1 from pg_constraint
  where conname='uploaded_source_provenance_deck_fk'
    and conrelid='guanye_internal.uploaded_source_provenance'::regclass
 ) then
  alter table guanye_internal.uploaded_source_provenance
   add constraint uploaded_source_provenance_deck_fk
   foreign key (deck_id) references public.decks(id)
   on delete cascade deferrable initially deferred;
 end if;
end
$uploaded_source_provenance_fk$;

create index if not exists uploaded_source_provenance_source_group_idx
 on guanye_internal.uploaded_source_provenance(workspace_id,source_group_sha256,deck_id);

create or replace function guanye_internal.uploaded_source_provenance_workspace_guard()
returns trigger
language plpgsql security definer set search_path=''
as $$
declare target_workspace uuid;
begin
 select d.workspace_id into target_workspace
 from public.decks d
 where d.id=new.deck_id;
 if target_workspace is null then
  raise exception '来源记录缺少对应作品';
 end if;
 if target_workspace is distinct from new.workspace_id then
  raise exception '来源记录与作品工作区不一致';
 end if;
 return new;
end
$$;

drop trigger if exists uploaded_source_provenance_workspace_guard
 on guanye_internal.uploaded_source_provenance;
create constraint trigger uploaded_source_provenance_workspace_guard
after insert or update of deck_id,workspace_id
on guanye_internal.uploaded_source_provenance
deferrable initially deferred
for each row execute function guanye_internal.uploaded_source_provenance_workspace_guard();

alter table guanye_internal.uploaded_source_provenance enable row level security;
revoke all on table guanye_internal.uploaded_source_provenance
 from public, anon, authenticated, service_role;

-- The gateway supplies actor from its authenticated request context. This function
-- never trusts auth.uid(), a body workspace, a client hash, or a client source_kind.
create or replace function public.ai_import_review_deck(
 w uuid,
 actor uuid,
 deck jsonb,
 p_source_group_sha256 text
) returns void
language plpgsql security definer set search_path='' as $$
declare
 deck_id_value text;
begin
 -- UPLOADED_SOURCE_PROVENANCE_M19
 if w is null or actor is null then raise exception '工作区或用户无效'; end if;
 if p_source_group_sha256 is null
    or p_source_group_sha256 !~ '^[0-9a-f]{64}$' then
  raise exception '源文件哈希无效';
 end if;
 if jsonb_typeof(deck) is distinct from 'object' then raise exception '作品内容无效'; end if;
 if deck ? 'source_kind' and deck->>'source_kind' is distinct from 'uploaded' then
  raise exception '只允许新建 uploaded 作品';
 end if;
 deck := deck - 'source_kind';
 if deck ? 'source_group_sha256' or deck ? 'sourceGroupSha256' then
  raise exception '作品数据不得包含来源指纹';
 end if;
 deck_id_value := deck->>'id';
 if deck_id_value is null or length(deck_id_value) not between 1 and 100
    or jsonb_typeof(deck->'images') is distinct from 'array' then
  raise exception '作品图片无效';
 end if;
 if jsonb_array_length(deck->'images') not between 1 and 600 then
  raise exception '作品图片无效';
 end if;
 if exists(
  select 1
  from jsonb_array_elements(deck->'images') as item(image)
  where jsonb_typeof(item.image) is distinct from 'object'
   or nullif(item.image->>'id','') is null
   or length(item.image->>'id')>100
   or left(coalesce(item.image->>'path',''),length(w::text)+1)<>w::text||'/'
   or left(coalesce(item.image->>'thumbPath',''),length(w::text)+1)<>w::text||'/'
   or strpos(coalesce(item.image->>'path',''),'..')>0
   or strpos(coalesce(item.image->>'path',''),chr(92))>0
   or strpos(coalesce(item.image->>'thumbPath',''),'..')>0
   or strpos(coalesce(item.image->>'thumbPath',''),chr(92))>0
 ) then
  raise exception '作品预览路径无效';
 end if;

 perform 1 from public.workspaces where id=w for update;
 if not found then raise exception '工作区不存在'; end if;
 perform 1 from public.members
  where workspace_id=w and user_id=actor
  for share;
 if not found then raise exception '请先加入团队'; end if;
 if exists(select 1 from public.decks where id=deck_id_value) then
  raise exception '作品标识冲突';
 end if;

 -- Deferred FK: the provenance row is visible to any deck INSERT trigger before
 -- the deck row exists, and both writes commit or roll back as one statement.
 insert into guanye_internal.uploaded_source_provenance(
  deck_id,workspace_id,source_group_sha256
 ) values(deck_id_value,w,p_source_group_sha256);
 insert into public.decks(id,workspace_id,data,source_kind)
  values(deck_id_value,w,deck,'uploaded');
end
$$;

revoke all on function public.ai_import_review_deck(uuid,uuid,jsonb,text)
 from public, anon, authenticated, service_role;
grant execute on function public.ai_import_review_deck(uuid,uuid,jsonb,text)
 to service_role;

-- Service-only verification helper. It returns only sorted deck IDs for one
-- workspace and hash; neither the hash nor internal rows are exposed to clients.
create or replace function public.ai_review_source_group_decks(
 w uuid,
 p_source_group_sha256 text
) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 -- UPLOADED_SOURCE_SPLIT_M19
 if w is null then raise exception '工作区无效'; end if;
 if p_source_group_sha256 is null
    or p_source_group_sha256 !~ '^[0-9a-f]{64}$' then
  raise exception '源文件哈希无效';
 end if;
 return coalesce((
  select jsonb_agg(p.deck_id order by p.deck_id)
  from guanye_internal.uploaded_source_provenance p
  where p.workspace_id=w and p.source_group_sha256=p_source_group_sha256
 ),'[]'::jsonb);
end
$$;

revoke all on function public.ai_review_source_group_decks(uuid,text)
 from public, anon, authenticated, service_role;
grant execute on function public.ai_review_source_group_decks(uuid,text)
 to service_role;

do $uploaded_source_provenance_self_check$
declare
 table_oid oid := to_regclass('guanye_internal.uploaded_source_provenance');
 import_oid oid := to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)');
 split_oid oid := to_regprocedure('public.ai_review_source_group_decks(uuid,text)');
 guard_oid oid := to_regprocedure('guanye_internal.uploaded_source_provenance_workspace_guard()');
begin
 -- UPLOADED_SOURCE_PROVENANCE_SELF_CHECK_M19
 if table_oid is null then raise exception 'M19 provenance table missing'; end if;
 if not exists (
  select 1 from pg_class where oid=table_oid and relrowsecurity
 ) then raise exception 'M19 provenance RLS missing'; end if;
 if not exists (
  select 1 from pg_attribute
  where attrelid=table_oid and attname='source_group_sha256' and attnotnull
 ) then raise exception 'M19 source hash nullability is invalid'; end if;
 if not exists (
  select 1 from pg_constraint
  where conrelid=table_oid and contype='c'
    and conname='uploaded_source_provenance_source_group_sha256_check'
    and position('[0-9a-f]{64}' in pg_get_constraintdef(oid,true))>0
 ) then raise exception 'M19 source hash CHECK missing'; end if;
 if not exists (
  select 1 from pg_constraint
  where conrelid=table_oid and contype='f'
    and conname='uploaded_source_provenance_deck_fk'
    and confrelid='public.decks'::regclass
    and condeferrable and condeferred and confdeltype='c'
 ) then raise exception 'M19 deck FK is not deferred cascade'; end if;
 if not exists (
  select 1 from pg_constraint
  where conrelid=table_oid and contype='f'
    and conname='uploaded_source_provenance_workspace_fk'
 ) then raise exception 'M19 workspace FK missing'; end if;
 if to_regclass('guanye_internal.uploaded_source_provenance_source_group_idx') is null then
  raise exception 'M19 source group index missing';
 end if;
 if guard_oid is null or not exists (
  select 1 from pg_proc p
  where p.oid=guard_oid and p.prosecdef
    and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
               where config.value in ('search_path=','search_path=""'))
 ) then raise exception 'M19 workspace guard security posture invalid'; end if;
 if not exists (
  select 1 from pg_trigger t
  where t.tgname='uploaded_source_provenance_workspace_guard'
    and t.tgrelid=table_oid and t.tgdeferrable and t.tginitdeferred and not t.tgisinternal
 ) then raise exception 'M19 workspace guard trigger missing'; end if;
 if has_table_privilege('anon',table_oid,'SELECT')
    or has_table_privilege('authenticated',table_oid,'SELECT')
    or has_table_privilege('service_role',table_oid,'SELECT') then
  raise exception 'M19 provenance table is directly readable';
 end if;
 if has_schema_privilege('anon','guanye_internal','USAGE')
    or has_schema_privilege('authenticated','guanye_internal','USAGE')
    or has_schema_privilege('service_role','guanye_internal','USAGE') then
  raise exception 'M19 private schema is directly usable';
 end if;

 if import_oid is null or split_oid is null then raise exception 'M19 RPC missing'; end if;
 if not exists (
  select 1 from pg_proc p
  where p.oid=import_oid and p.prosecdef
    and position('UPLOADED_SOURCE_PROVENANCE_M19' in p.prosrc)>0
    and exists(
     select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
     where config.value in ('search_path=','search_path=""')
    )
 ) then raise exception 'M19 import RPC security posture invalid'; end if;
 if not exists (
  select 1 from pg_proc p
  where p.oid=split_oid and p.prosecdef
    and position('UPLOADED_SOURCE_SPLIT_M19' in p.prosrc)>0
    and exists(
     select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
     where config.value in ('search_path=','search_path=""')
    )
 ) then raise exception 'M19 split RPC security posture invalid'; end if;
 if not has_function_privilege('service_role',import_oid,'EXECUTE')
    or has_function_privilege('anon',import_oid,'EXECUTE')
    or has_function_privilege('authenticated',import_oid,'EXECUTE') then
  raise exception 'M19 import RPC ACL invalid';
 end if;
 if not has_function_privilege('service_role',split_oid,'EXECUTE')
    or has_function_privilege('anon',split_oid,'EXECUTE')
    or has_function_privilege('authenticated',split_oid,'EXECUTE') then
  raise exception 'M19 split RPC ACL invalid';
 end if;
 if to_regprocedure('public.add_review_deck(uuid,jsonb)') is null
    or not has_function_privilege('authenticated','public.add_review_deck(uuid,jsonb)','EXECUTE')
    or has_function_privilege('anon','public.add_review_deck(uuid,jsonb)','EXECUTE') then
  raise exception 'M19 legacy add_review_deck ACL changed';
 end if;
end
$uploaded_source_provenance_self_check$;

commit;
