-- SINGLE_JUDGE_GENERATION_REFERENCES_M29
-- Activated single-judge workspaces must prove every reference image is either:
-- 1) a stored uploaded-deck preview whose deck still allows AI, or
-- 2) a user-provided style input in the current confirmed planning version.
-- Legacy workspaces keep the exact M04 path-selection behavior.
begin;

create or replace function guanye_internal.single_judge_generation_uploaded_reference_ok(
 p_workspace uuid,
 p_deck_id text,
 p_path text
) returns boolean
language plpgsql
stable
security definer
set search_path=''
as $$
declare
 deck_row public.decks%rowtype;
 path_parts text[];
begin
 -- SINGLE_JUDGE_GENERATION_REFERENCES_M29
 if p_workspace is null or p_deck_id is null or p_path is null then return false; end if;
 if p_deck_id !~ '^[A-Za-z0-9_-]{1,100}$' then return false; end if;
 select * into deck_row
 from public.decks d
 where d.workspace_id=p_workspace and d.id=p_deck_id;
 if not found
    or deck_row.source_kind is distinct from 'uploaded'
    or deck_row.ai_allowed is distinct from true then
  return false;
 end if;
 if jsonb_typeof(deck_row.data->'images') is distinct from 'array' then return false; end if;

 -- Legacy importImages stores workspace/deck/<uuid>.<ext>; M24 stores
 -- workspace/deck/preview/<position>.png. Keep both, plus any direct image
 -- filename under the same deck, but never originals or planning/generated paths.
 path_parts=string_to_array(p_path,'/');
 if path_parts[1] is distinct from p_workspace::text
    or path_parts[2] is distinct from p_deck_id
    or array_length(path_parts,1) not between 3 and 4
    or (array_length(path_parts,1)=4 and path_parts[3] is distinct from 'preview')
    or path_parts[array_length(path_parts,1)] !~* '\.(png|jpe?g|webp|gif|bmp)$'
    or position('..' in p_path)>0
    or position(chr(92) in p_path)>0
    or position(chr(13) in p_path)>0
    or position(chr(10) in p_path)>0 then
  return false;
 end if;

 -- The path must be exactly one of this deck's image fields. Historical rows
 -- use path/imagePath/storagePath aliases, and thumbPath is not required.
 if not exists(
  select 1
  from jsonb_array_elements(deck_row.data->'images') image
  where coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath',''))=p_path
 ) then
  return false;
 end if;
 if not exists(
  select 1 from storage.objects o
  where o.bucket_id='ppt-library' and o.name=p_path
 ) then
  return false;
 end if;

 -- Reused paths are ambiguous. If any manual-only deck owns the same path,
 -- the reference fails closed even when an AI-allowed deck also lists it.
 if exists(
  select 1
  from public.decks other
  where other.workspace_id=p_workspace
   and other.ai_allowed is distinct from true
   and exists(
    select 1
    from jsonb_array_elements(
     case when jsonb_typeof(other.data->'images')='array'
      then other.data->'images' else '[]'::jsonb end
    ) image
    where coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath',''))=p_path
       or image->>'thumbPath'=p_path
   )
 ) then
  return false;
 end if;
 return true;
end $$;

create or replace function guanye_internal.single_judge_generation_style_reference_ok(
 p_workspace uuid,
 p_job public.ai_generation_jobs,
 p_path text
) returns boolean
language plpgsql
stable
security definer
set search_path=''
as $$
declare
 plan_uuid uuid;
 plan_row public.ai_planning_plans%rowtype;
 current_plan jsonb;
 judge_id uuid;
 version_confirmed_by uuid;
 path_parts text[];
begin
 -- SINGLE_JUDGE_GENERATION_REFERENCES_M29
 if p_workspace is null or p_job.id is null or p_path is null
    or p_job.workspace_id is distinct from p_workspace then
  return false;
 end if;
 if p_job.plan_id !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' then
  return false;
 end if;

 -- Style inputs are reserved for the current professional judge using a plan
 -- that the judge personally created and confirmed. Legacy admin or version
 -- grants do not widen this read authority.
 select ws.dedicated_judge_id into judge_id
 from public.workspaces ws
 where ws.id=p_workspace and ws.single_judge_admin_id is not null;
 if not found or judge_id is distinct from p_job.user_id then return false; end if;

 plan_uuid=p_job.plan_id::uuid;
 select * into plan_row
 from public.ai_planning_plans p
 where p.id=plan_uuid and p.workspace_id=p_workspace;
 if not found
    or plan_row.status<>'confirmed'
    or plan_row.current_version_id is null
    or plan_row.created_by is distinct from p_job.user_id then
  return false;
 end if;
 select v.confirmed_by into version_confirmed_by
 from public.ai_planning_versions v
 where v.id=plan_row.current_version_id
   and v.plan_id=plan_uuid
   and v.workspace_id=p_workspace;
 if not found or version_confirmed_by is distinct from p_job.user_id then return false; end if;

 -- The generation job must still snapshot the current immutable plan version.
 begin
  current_plan=public.ai_planning_read_confirmed_plan(plan_uuid);
 exception when others then
  return false;
 end;
 if current_plan is null
    or current_plan->>'planId' is distinct from plan_uuid::text
    or current_plan->>'workspaceId' is distinct from p_workspace::text
    or current_plan->>'versionId' is distinct from plan_row.current_version_id::text
    or current_plan->>'confirmedBy' is distinct from p_job.user_id::text
    or p_job.plan_snapshot is distinct from current_plan then
  return false;
 end if;

 -- The path must be explicitly present in the current confirmed plan's style
 -- inputs, not inferred from a mutable job snapshot or path prefix alone.
 if not exists(
  select 1
  from jsonb_array_elements(
   case when jsonb_typeof(current_plan->'generationStyleImages')='array'
    then current_plan->'generationStyleImages' else '[]'::jsonb end
  ) value
  where value #>> '{}'=p_path
 ) then
  return false;
 end if;

 path_parts=string_to_array(p_path,'/');
 if array_length(path_parts,1) is distinct from 5
    or path_parts[1] is distinct from p_workspace::text
    or path_parts[2] is distinct from 'ai-planning'
    or path_parts[3] is distinct from plan_uuid::text
    or path_parts[4] not in ('reference','style-rendered')
    or path_parts[5] is null
    or path_parts[5]=''
    or position('..' in p_path)>0
    or position(chr(92) in p_path)>0
    or position(chr(13) in p_path)>0
    or position(chr(10) in p_path)>0 then
  return false;
 end if;
 if not exists(
  select 1 from storage.objects o
  where o.bucket_id='ppt-library' and o.name=p_path
 ) then
  return false;
 end if;
 return true;
end $$;
create or replace function public.ai_generation_select_reference_path(
 p_workspace uuid,p_job uuid,p_page uuid
) returns text
language plpgsql stable security definer set search_path='' as $$
declare
 page_row public.ai_generation_pages%rowtype;
 job_row public.ai_generation_jobs%rowtype;
 selected_path text;
begin
 -- SINGLE_JUDGE_GENERATION_REFERENCES_M29
 select * into page_row from public.ai_generation_pages
 where workspace_id=p_workspace and id=p_page and job_id=p_job;
 if page_row.id is null then raise exception '生成页面不存在'; end if;
 select * into job_row from public.ai_generation_jobs
 where workspace_id=p_workspace and id=p_job;
 if job_row.id is null then raise exception '生成任务不存在'; end if;

 if exists(
  select 1 from public.workspaces ws
  where ws.id=p_workspace and ws.single_judge_admin_id is not null
 ) then
  select candidate.path into selected_path
  from (
   select
    case when jsonb_typeof(v.item)='string' then v.item #>> '{}' else v.item->>'path' end as path,
    1 as priority,
    v.ordinality as rank
   from jsonb_array_elements(coalesce(job_row.plan_snapshot->'generationStyleImages','[]'::jsonb))
    with ordinality as v(item,ordinality)
   where jsonb_typeof(v.item) in ('string','object')
    and guanye_internal.single_judge_generation_style_reference_ok(
     p_workspace,job_row,
     case when jsonb_typeof(v.item)='string' then v.item #>> '{}' else v.item->>'path' end
    )
   union all
   select im.path_text as path,2 as priority,im.ordinality as rank
   from jsonb_array_elements(coalesce(job_row.reference_data,'[]'::jsonb))
     with ordinality as r(item,ordinality)
   cross join lateral jsonb_array_elements_text(coalesce(r.item->'imagePaths','[]'::jsonb))
     with ordinality as im(path_text,ordinality)
   where guanye_internal.single_judge_generation_uploaded_reference_ok(
     p_workspace,r.item->>'deckId',im.path_text
    )
    and exists(
     select 1 from jsonb_array_elements_text(coalesce(page_row.outline->'styleReferenceDeckIds','[]'::jsonb)) sel
     where sel.value=r.item->>'deckId'
    )
   union all
   select im.path_text as path,3 as priority,im.ordinality as rank
   from jsonb_array_elements(coalesce(job_row.reference_data,'[]'::jsonb))
     with ordinality as r(item,ordinality)
   cross join lateral jsonb_array_elements_text(coalesce(r.item->'imagePaths','[]'::jsonb))
     with ordinality as im(path_text,ordinality)
   where guanye_internal.single_judge_generation_uploaded_reference_ok(
     p_workspace,r.item->>'deckId',im.path_text
    )
  ) candidate
  where public.ai_generation_is_safe_reference_path(p_workspace,candidate.path)
  order by candidate.priority,candidate.rank
  limit 1;
 else
  -- Exact M04 legacy branch for workspaces not activated as single-judge.
  select candidate.path into selected_path
  from (
   select
    case when jsonb_typeof(v.item)='string' then v.item #>> '{}' else v.item->>'path' end as path,
    1 as priority,
    v.ordinality as rank
   from jsonb_array_elements(coalesce(job_row.plan_snapshot->'generationStyleImages','[]'::jsonb))
    with ordinality as v(item,ordinality)
   union all
   select im.path_text as path,2 as priority,im.ordinality as rank
   from jsonb_array_elements(coalesce(job_row.reference_data,'[]'::jsonb))
     with ordinality as r(item,ordinality)
   cross join lateral jsonb_array_elements_text(coalesce(r.item->'imagePaths','[]'::jsonb))
     with ordinality as im(path_text,ordinality)
   where exists(
    select 1 from jsonb_array_elements_text(coalesce(page_row.outline->'styleReferenceDeckIds','[]'::jsonb)) sel
    where sel.value=r.item->>'deckId'
   )
   union all
   select im.path_text as path,3 as priority,im.ordinality as rank
   from jsonb_array_elements(coalesce(job_row.reference_data,'[]'::jsonb))
     with ordinality as r(item,ordinality)
   cross join lateral jsonb_array_elements_text(coalesce(r.item->'imagePaths','[]'::jsonb))
     with ordinality as im(path_text,ordinality)
  ) candidate
  where public.ai_generation_is_safe_reference_path(p_workspace,candidate.path)
  order by candidate.priority,candidate.rank
  limit 1;
 end if;
 return selected_path;
end $$;

revoke all on function guanye_internal.single_judge_generation_uploaded_reference_ok(uuid,text,text)
 from public,anon,authenticated,service_role;
revoke all on function guanye_internal.single_judge_generation_style_reference_ok(uuid,public.ai_generation_jobs,text)
 from public,anon,authenticated,service_role;
revoke all on function public.ai_generation_select_reference_path(uuid,uuid,uuid)
 from public,anon,authenticated,service_role;
revoke all on function public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)
 from public,anon,authenticated,service_role;
grant execute on function public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)
 to service_role;

do $single_judge_generation_references_self_check$
begin
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('guanye_internal.single_judge_generation_uploaded_reference_ok(uuid,text,text)')
   and p.prosecdef
   and position('SINGLE_JUDGE_GENERATION_REFERENCES_M29' in p.prosrc)>0
   and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) c(value) where c.value in ('search_path=','search_path=""'))
 ) then raise exception 'M29 uploaded reference guard missing'; end if;
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('guanye_internal.single_judge_generation_style_reference_ok(uuid,public.ai_generation_jobs,text)')
   and p.prosecdef
   and position('SINGLE_JUDGE_GENERATION_REFERENCES_M29' in p.prosrc)>0
   and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) c(value) where c.value in ('search_path=','search_path=""'))
 ) then raise exception 'M29 planning style guard missing'; end if;
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('public.ai_generation_select_reference_path(uuid,uuid,uuid)')
   and p.prosecdef
   and position('SINGLE_JUDGE_GENERATION_REFERENCES_M29' in p.prosrc)>0
   and position('Exact M04 legacy branch' in p.prosrc)>0
   and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) c(value) where c.value in ('search_path=','search_path=""'))
 ) then raise exception 'M29 reference selector wrapper missing'; end if;
 if has_function_privilege('authenticated','public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)','EXECUTE')
    or has_function_privilege('anon','public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)','EXECUTE')
    or not has_function_privilege('service_role','public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)','EXECUTE') then
  raise exception 'M29 reference read ACL invalid';
 end if;
 if has_function_privilege('service_role','public.ai_generation_select_reference_path(uuid,uuid,uuid)','EXECUTE') then
  raise exception 'M29 selector must remain internal';
 end if;
end
$single_judge_generation_references_self_check$;

commit;
