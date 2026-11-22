-- JWT-only professional planning and generation; keep the existing stores and queue.
begin;

create function guanye_internal.single_judge_creation_actor(w uuid) returns uuid
language plpgsql stable security definer set search_path='' as $$
declare actor uuid := auth.uid();
begin
 if actor is null or not exists (
  select 1 from public.workspaces ws join public.members m on m.workspace_id=ws.id
  where ws.id=w and ws.single_judge_admin_id is not null
    and ws.dedicated_judge_id=actor and m.user_id=actor
 ) then raise exception '仅当前工作区专职评分师可操作' using errcode='42501'; end if;
 return actor;
end $$;

create function guanye_internal.single_judge_planning_path(w uuid, plan_id uuid, object_name text) returns text
language sql immutable strict set search_path='' as $$
 select case when split_part(object_name,'/',1)=w::text
  and split_part(object_name,'/',2)='ai-planning' and split_part(object_name,'/',3)=plan_id::text
  and array_length(string_to_array(object_name,'/'),1)=5
  and case split_part(object_name,'/',4)
   when 'content' then split_part(object_name,'/',5) ~ '^[0-9a-f]{32}\.(txt|md|csv|json|pdf|ppt|pptx)$'
   when 'style' then split_part(object_name,'/',5) ~ '^[0-9a-f]{32}\.(pdf|ppt|pptx)$'
   when 'reference' then split_part(object_name,'/',5) ~ '^[0-9a-f]{32}\.(png|jpg|jpeg|webp)$'
   when 'style-rendered' then split_part(object_name,'/',5) ~ '^[0-9a-f]{32}\.png$'
   else false end
  then split_part(object_name,'/',4) end
$$;

-- Same lock for pre-draft uploads and plan writes. The lock is also used to
-- serialize retries of creation for one plan, before the queue's unique check.
create function guanye_internal.single_judge_planning_lock(plan_id uuid) returns void
language sql volatile strict set search_path='' as $$
 select pg_advisory_xact_lock(hashtextextended('single-judge-planning:'||plan_id::text,0))
$$;

create function public.single_judge_planning_insert_allowed(p_bucket text,p_name text) returns boolean
language plpgsql volatile security definer set search_path='' as $$
declare
 parts text[] := string_to_array(p_name,'/');
 uuid_pattern constant text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
 w uuid; plan_id uuid; actor uuid;
begin
 if p_bucket is distinct from 'ppt-library' or array_length(parts,1) is distinct from 5
    or parts[1] !~ uuid_pattern or parts[3] !~ uuid_pattern then return false; end if;
 w=parts[1]::uuid; plan_id=parts[3]::uuid;
 if guanye_internal.single_judge_planning_path(w,plan_id,p_name) is null then return false; end if;
 begin
  actor=guanye_internal.single_judge_creation_actor(w);
 exception when insufficient_privilege then return false;
 end;
 perform guanye_internal.single_judge_planning_lock(plan_id);
 -- Take the row lock when there is a draft, also serializing with existing SQL writes.
 perform 1 from public.ai_planning_plans p where p.id=plan_id and p.workspace_id=w
  and p.created_by=actor and p.status='draft' for share;
 if not found and exists(select 1 from public.ai_planning_plans p where p.id=plan_id) then return false; end if;
 return not exists(select 1 from storage.objects o where o.bucket_id=p_bucket and o.name=p_name);
end $$;

create policy single_judge_planning_inputs_insert on storage.objects for insert to authenticated
 with check (public.single_judge_planning_insert_allowed(bucket_id,name));

-- Restrict M24 candidates further to preview objects that M29 can actually use.
create function guanye_internal.single_judge_planning_candidates(w uuid,p_limit integer) returns jsonb
language sql stable security definer set search_path='' as $$
 select coalesce(jsonb_agg(r.item||jsonb_build_object('imagePaths',coalesce((
  select jsonb_agg(p.path order by p.ord)
  from jsonb_array_elements_text(r.item->'imagePaths') with ordinality p(path,ord)
  where guanye_internal.single_judge_generation_uploaded_reference_ok(w,r.item->>'deckId',p.path)
 ),'[]'::jsonb)) order by r.ord),'[]'::jsonb)
 from jsonb_array_elements(public.ai_planning_reference_candidates(w,p_limit)) with ordinality r(item,ord)
$$;

create function public.single_judge_planning_references(w uuid,p_limit integer) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_creation_actor(w);
 if p_limit is null or p_limit not between 1 and 20 then
  raise exception '参考数量必须为 1–20' using errcode='22023'; end if;
 return guanye_internal.single_judge_planning_candidates(w,p_limit);
end $$;

-- Check structured provenance on both new and existing plans. Never read an
-- object from a caller-supplied path before classifying the exact plan prefix.
create function guanye_internal.single_judge_planning_inputs(w uuid,plan_id uuid,draft jsonb) returns void
language plpgsql stable security definer set search_path='' as $$
declare src jsonb; img jsonb; object_name text; input_role text; style_paths jsonb := '[]';
 source_bytes bigint := 0; seen_paths text[] := array[]::text[];
begin
 if jsonb_typeof(draft->'sourceManifest') is distinct from 'array' then
  raise exception '资料来源无效' using errcode='22023'; end if;
 for src in select value from jsonb_array_elements(draft->'sourceManifest') loop
  if jsonb_typeof(src) is distinct from 'object' then raise exception '资料来源无效' using errcode='22023'; end if;
  if src-array['name','role','mimeType','size','sha256','storagePath','visualInput','textChars','pages','truncated','renderedPagePaths'] <> '{}'::jsonb
    or jsonb_typeof(src->'name') is distinct from 'string' or length(src->>'name') not between 1 and 180
    or jsonb_typeof(src->'mimeType') is distinct from 'string' or length(src->>'mimeType') not between 1 and 150
    or jsonb_typeof(src->'size') is distinct from 'number' or coalesce(src->>'size','') !~ '^[1-9][0-9]{0,9}$'
    or coalesce(src->>'sha256','') !~ '^[0-9a-f]{64}$'
    or coalesce(src->>'role','') not in ('content','style','reference') then
   raise exception '资料来源无效' using errcode='22023'; end if;
  object_name=src->>'storagePath'; input_role=src->>'role';
  source_bytes=source_bytes+(src->>'size')::bigint;
  if (src->>'size')::bigint > (case when input_role='reference' then 8388608 else 12582912 end)
    or source_bytes>33554432 or object_name=any(seen_paths) then
   raise exception '资料大小超限或路径重复' using errcode='22023'; end if;
  seen_paths=array_append(seen_paths,object_name);
  if guanye_internal.single_judge_planning_path(w,plan_id,object_name) is distinct from input_role
    or not exists(select 1 from storage.objects o where o.bucket_id='ppt-library' and o.name=object_name) then
   raise exception '资料路径不属于当前策划' using errcode='22023'; end if;
  if input_role='reference' then style_paths=style_paths||jsonb_build_array(object_name); end if;
  if src ? 'renderedPagePaths' then
   if jsonb_typeof(src->'renderedPagePaths') is distinct from 'array' then
    raise exception '模板派生图无效' using errcode='22023'; end if;
   if jsonb_array_length(src->'renderedPagePaths')>6 then
    raise exception '每份模板最多 6 张派生图' using errcode='22023'; end if;
   for img in select value from jsonb_array_elements(src->'renderedPagePaths') loop
    object_name=img#>>'{}';
    if input_role<>'style' or jsonb_typeof(img) is distinct from 'string'
      or guanye_internal.single_judge_planning_path(w,plan_id,object_name) is distinct from 'style-rendered'
      or not exists(select 1 from storage.objects o where o.bucket_id='ppt-library' and o.name=object_name) then
     raise exception '模板派生图不属于当前策划' using errcode='22023'; end if;
    style_paths=style_paths||jsonb_build_array(object_name);
   end loop;
  end if;
 end loop;
 if draft ? 'generationStyleImages' and not style_paths @> (draft->'generationStyleImages') then
  raise exception '生成风格图必须来自当前策划资料' using errcode='22023'; end if;
end $$;

create function guanye_internal.single_judge_planning_validate(draft jsonb) returns void
language plpgsql immutable set search_path='' as $$
declare pg jsonb; item jsonb;
begin
 if draft is null or jsonb_typeof(draft) is distinct from 'object' or octet_length(draft::text)>500000 then
  raise exception '策划内容无效或超过 500 KB' using errcode='22023'; end if;
 if draft-array['schemaVersion','plannerModel','title','mode','pageCount','summary','contentStrategy',
   'style','pages','references','templateSample','generationBrief','assumptions','warnings','sourceManifest',
   'decisionBasis','designDirections','generationStyleImages'] <> '{}'::jsonb
    or draft->'schemaVersion' is distinct from '1'::jsonb
    or jsonb_typeof(draft->'title') is distinct from 'string'
    or jsonb_typeof(draft->'pageCount') is distinct from 'number'
    or jsonb_typeof(draft->'references') is distinct from 'array'
    or jsonb_typeof(draft->'sourceManifest') is distinct from 'array' then
  raise exception '策划字段无效' using errcode='22023'; end if;
 perform public.ai_planning_validate_draft(draft);
 for pg in select value from jsonb_array_elements(draft->'pages') loop
  if pg-array['index','id','title','narrativeRole','copy','layout','visualBrief','contentSources','styleReferenceDeckIds','contentType'] <> '{}'::jsonb
     or jsonb_typeof(pg->'title') is distinct from 'string' then raise exception '逐页策划字段无效' using errcode='22023'; end if;
  for item in select value from jsonb_array_elements(pg->'copy') loop
   if jsonb_typeof(item) is distinct from 'object' or item-array['kind','text'] <> '{}'::jsonb
      or jsonb_typeof(item->'text') is distinct from 'string' or length(item->>'text')>8000 then
    raise exception '逐页文案条目无效' using errcode='22023'; end if;
  end loop;
 end loop;
end $$;

-- Initial drafts can select M24 candidates by id, but cannot supply fake
-- metadata/paths. Store authoritative metadata, preserving selected rank/order.
create function guanye_internal.single_judge_planning_seed_refs(w uuid, refs jsonb) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare candidates jsonb; result jsonb := '[]'; r jsonb; match jsonb; rank integer := 0;
begin
 if refs='[]'::jsonb then return refs; end if;
 candidates=guanye_internal.single_judge_planning_candidates(w,20);
 for r in select value from jsonb_array_elements(refs) loop
  if jsonb_typeof(r) is distinct from 'object' or jsonb_typeof(r->'deckId') is distinct from 'string'
     or result @> jsonb_build_array(jsonb_build_object('deckId',r->'deckId')) then
   raise exception '参考作品无效或重复' using errcode='22023'; end if;
  select value into match from jsonb_array_elements(candidates) where value->>'deckId'=r->>'deckId';
  if not found or (r ? 'imagePaths' and (jsonb_typeof(r->'imagePaths') is distinct from 'array'
     or not (match->'imagePaths') @> (r->'imagePaths'))) then
   raise exception '参考作品不在当前工作区授权候选中' using errcode='22023'; end if;
  rank=rank+1;
  result=result||jsonb_build_array(match||jsonb_build_object('rank',rank));
 end loop;
 return result;
end $$;

create function guanye_internal.single_judge_planning_ref_ids(v jsonb,refs jsonb) returns jsonb
language sql immutable set search_path='' as $$
 select coalesce(jsonb_agg(chosen.id order by chosen.ord),'[]'::jsonb) from (
  select x.id,x.ord from jsonb_array_elements(case when jsonb_typeof(v)='array' then v else '[]'::jsonb end)
   with ordinality x(id,ord)
  where jsonb_typeof(x.id)='string' and refs @> jsonb_build_array(jsonb_build_object('deckId',x.id))
  order by x.ord limit 6
 ) chosen
$$;

create function guanye_internal.single_judge_planning_write(
 w uuid, plan_id uuid, revision bigint, payload jsonb, confirm boolean
) returns jsonb language plpgsql security definer set search_path='' as $$
declare
 actor uuid := guanye_internal.single_judge_creation_actor(w);
 current_plan public.ai_planning_plans; draft jsonb := payload; refs jsonb;
 result jsonb;
begin
 if plan_id is null or revision is null or revision<0 then raise exception '策划草稿参数无效' using errcode='22023'; end if;
 perform guanye_internal.single_judge_planning_lock(plan_id);
 select * into current_plan from public.ai_planning_plans p where p.id=plan_id for update;
 if not found then
  if confirm or revision<>0 then raise exception '策划不存在或版本已变化' using errcode='22023'; end if;
  perform guanye_internal.single_judge_planning_validate(draft);
  refs=guanye_internal.single_judge_planning_seed_refs(w,draft->'references');
 else
  if current_plan.workspace_id<>w or current_plan.created_by<>actor then
   raise exception '策划不存在或不属于当前评分师' using errcode='42501'; end if;
  if current_plan.status<>'draft' then raise exception '已确认的策划不可修改' using errcode='22023'; end if;
  if current_plan.revision<>revision then raise exception '策划草稿版本已变化，请刷新后重试' using errcode='22023'; end if;
  draft=coalesce(draft,current_plan.draft);
  perform guanye_internal.single_judge_planning_validate(draft);
  if draft->'mode' is distinct from current_plan.draft->'mode' then
   raise exception '不可切换策划模式' using errcode='22023'; end if;
  -- Match the existing save/confirm sanitization: untrusted edits cannot replace
  -- source/reference provenance or the original planner identity.
  draft=(draft-array['generationStyleImages','plannerModel'])||jsonb_build_object('sourceManifest',current_plan.draft->'sourceManifest');
  if current_plan.draft ? 'generationStyleImages' then
   draft=draft||jsonb_build_object('generationStyleImages',current_plan.draft->'generationStyleImages'); end if;
  if current_plan.draft ? 'plannerModel' then
   draft=draft||jsonb_build_object('plannerModel',current_plan.draft->'plannerModel'); end if;
  refs=current_plan.draft->'references';
  if confirm then
   -- Recheck selection and paths at confirmation, without replacing the draft's
   -- stored metadata if only the scores/title have changed.
   perform guanye_internal.single_judge_planning_seed_refs(w,refs);
  end if;
 end if;
 draft=draft||jsonb_build_object('references',refs);
 draft=jsonb_set(draft,'{style,referenceDeckIds}',guanye_internal.single_judge_planning_ref_ids(draft#>'{style,referenceDeckIds}',refs));
 draft=jsonb_set(draft,'{pages}',(
  select jsonb_agg(pg.page||jsonb_build_object('index',pg.ord,'styleReferenceDeckIds',
   guanye_internal.single_judge_planning_ref_ids(pg.page->'styleReferenceDeckIds',refs)) order by pg.ord)
  from jsonb_array_elements(draft->'pages') with ordinality pg(page,ord)
 ));
 perform guanye_internal.single_judge_planning_validate(draft);
 perform guanye_internal.single_judge_planning_inputs(w,plan_id,draft);
 if confirm then return public.ai_planning_confirm(w,plan_id,actor,revision,draft); end if;
 result=public.ai_planning_save_draft(w,plan_id,actor,revision,draft);
 return result||jsonb_build_object('draft',draft);
end $$;

create function public.single_judge_planning_save(w uuid,p_plan_id uuid,p_revision bigint,p_draft jsonb) returns jsonb
language sql security definer set search_path='' as $$
 select guanye_internal.single_judge_planning_write(w,p_plan_id,p_revision,p_draft,false)
$$;

create function public.single_judge_planning_confirm(w uuid,p_plan_id uuid,p_revision bigint,p_draft jsonb) returns jsonb
language sql security definer set search_path='' as $$
 select guanye_internal.single_judge_planning_write(w,p_plan_id,p_revision,p_draft,true)
$$;

-- Hide legacy implementations; keep the public signatures for legacy clients.
alter function public.ai_planning_get_plan(uuid,uuid) set schema guanye_internal;
alter function public.ai_planning_list_my_plans(uuid,uuid,integer) set schema guanye_internal;
alter function public.ai_planning_grant_version_access(uuid,uuid,uuid) set schema guanye_internal;

create function public.single_judge_planning_get(w uuid,p_plan_id uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid := guanye_internal.single_judge_creation_actor(w);
begin
 if not exists(select 1 from public.ai_planning_plans p
  where p.id=p_plan_id and p.workspace_id=w and p.created_by=actor
   and (p.current_version_id is null or exists(select 1 from public.ai_planning_versions v
    where v.id=p.current_version_id and v.plan_id=p.id and v.workspace_id=p.workspace_id))) then
  raise exception '策划不存在或不属于当前评分师' using errcode='42501'; end if;
 return guanye_internal.ai_planning_get_plan(w,p_plan_id);
end $$;

create function public.single_judge_planning_history(w uuid,p_after uuid,p_limit integer) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_creation_actor(w);
 return guanye_internal.ai_planning_list_my_plans(w,p_after,p_limit);
end $$;

create function public.single_judge_planning_confirmed(w uuid,p_plan_id uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare actor uuid := guanye_internal.single_judge_creation_actor(w);
begin
 if not exists(
  select 1 from public.ai_planning_plans p join public.ai_planning_versions v
   on v.id=p.current_version_id and v.plan_id=p.id and v.workspace_id=p.workspace_id
  where p.id=p_plan_id and p.workspace_id=w and p.created_by=actor and p.status='confirmed' and v.confirmed_by=actor
 ) then raise exception '已确认策划不存在或不属于当前评分师' using errcode='42501'; end if;
 return public.ai_planning_read_confirmed_plan(p_plan_id);
end $$;

create function guanye_internal.single_judge_creation_active(w uuid) returns boolean
language sql stable set search_path='' as $$
 select exists(select 1 from public.workspaces where id=w and single_judge_admin_id is not null)
$$;

create function public.ai_planning_get_plan(w uuid,plan_id uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(w) then return public.single_judge_planning_get(w,plan_id); end if;
 return guanye_internal.ai_planning_get_plan(w,plan_id);
end $$;
create function public.ai_planning_list_my_plans(w uuid,p_after uuid,p_limit integer) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(w) then return public.single_judge_planning_history(w,p_after,p_limit); end if;
 return guanye_internal.ai_planning_list_my_plans(w,p_after,p_limit);
end $$;
create function public.ai_planning_grant_version_access(w uuid,version_id uuid,target_user_id uuid) returns void
language plpgsql security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(w) then
  raise exception '单评委专业版不支持共享策划版本' using errcode='42501'; end if;
 perform guanye_internal.ai_planning_grant_version_access(w,version_id,target_user_id);
end $$;

create function public.single_judge_generation_create(
 w uuid,p_plan_id text,p_deck_id text,p_plan jsonb,p_title text,p_style text,p_outline jsonb,p_references jsonb,p_pages jsonb
) returns jsonb language plpgsql security definer set search_path='' as $$
declare
 actor uuid := guanye_internal.single_judge_creation_actor(w);
 plan_uuid uuid; confirmed jsonb; pg jsonb; refs jsonb; prior public.ai_generation_jobs;
 page_outlines jsonb := '[]';
begin
 if coalesce(p_plan_id,'') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    or coalesce(p_deck_id,'') !~ '^generated-[0-9a-f]{24}$' then
  raise exception '生成计划或作品编号无效' using errcode='22023'; end if;
 plan_uuid=p_plan_id::uuid;
 perform guanye_internal.single_judge_planning_lock(plan_uuid);
 -- The old service create also takes a row SHARE lock before creating a job.
 perform 1 from public.ai_planning_plans p where p.id=plan_uuid and p.workspace_id=w
  and p.created_by=actor and p.status='confirmed' for update;
 if not found then raise exception '已确认策划不存在或不属于当前评分师' using errcode='42501'; end if;
 confirmed=public.single_judge_planning_confirmed(w,plan_uuid);
 if p_plan is distinct from confirmed then
  raise exception '生成快照与不可变确认版本不一致' using errcode='22023'; end if;
 refs=confirmed->'references';
 if jsonb_typeof(refs) is distinct from 'array' or jsonb_typeof(p_references) is distinct from 'array'
    or jsonb_typeof(p_outline) is distinct from 'object' or jsonb_typeof(p_pages) is distinct from 'array'
    or length(coalesce(p_title,'')) not between 1 and 200 or length(coalesce(p_style,'')) not between 1 and 12000
    or octet_length(p_outline::text)>1500000 or octet_length(p_pages::text)>2000000 then
  raise exception '生成参数无效' using errcode='22023'; end if;
 if jsonb_array_length(p_pages) not between 1 and 30
    or jsonb_array_length(p_pages)<>(confirmed->>'pageCount')::integer
    or jsonb_array_length(p_references)>12
    or p_outline-array['title','brief','pages','sourceManifest'] <> '{}'::jsonb
    or (p_outline ? 'title' and jsonb_typeof(p_outline->'title') is distinct from 'string')
    or (p_outline ? 'brief' and jsonb_typeof(p_outline->'brief') is distinct from 'string')
    or (p_outline ? 'sourceManifest' and p_outline->'sourceManifest' is distinct from confirmed->'sourceManifest')
    or exists(select 1 from jsonb_array_elements(p_references) r(item)
     where not exists(select 1 from jsonb_array_elements(refs) approved(item) where approved.item=r.item))
    or (select count(distinct r.item) from jsonb_array_elements(p_references) r(item))<>jsonb_array_length(p_references) then
  raise exception '生成参数超出确认版的页面或参考范围' using errcode='22023'; end if;
 for pg in select value from jsonb_array_elements(p_pages) loop
  if jsonb_typeof(pg) is distinct from 'object' or pg-array['planPageId','position','title','outline','description','prompt','references','contentType']<>'{}'::jsonb
     or jsonb_typeof(pg->'outline') is distinct from 'object'
     or (pg->'outline')-array['index','title','copy','layout','visualBrief','contentSources','styleReferenceDeckIds','narrativeRole'] <> '{}'::jsonb
     or (pg ? 'description' and (jsonb_typeof(pg->'description') is distinct from 'object'
         or (pg->'description')-'text' <> '{}'::jsonb or jsonb_typeof(pg#>'{description,text}') is distinct from 'string'))
     or (pg ? 'prompt' and jsonb_typeof(pg->'prompt') is distinct from 'string')
     or coalesce(pg->'references','[]'::jsonb) is distinct from guanye_internal.single_judge_planning_ref_ids(pg->'references',refs)
     or coalesce(pg#>'{outline,styleReferenceDeckIds}','[]'::jsonb) is distinct from guanye_internal.single_judge_planning_ref_ids(pg#>'{outline,styleReferenceDeckIds}',refs) then
   raise exception '生成页面字段无效或超出确认版参考范围' using errcode='22023'; end if;
  page_outlines=page_outlines||jsonb_build_array(pg->'outline');
 end loop;
 if p_outline ? 'pages' and p_outline->'pages' is distinct from page_outlines then
  raise exception '大纲必须与生成页面一致' using errcode='22023'; end if;
 -- Old trusted-service clients could already have enqueued a job for this plan.
 -- Never return a job with a different snapshot or unauthorized stored refs.
 select * into prior from public.ai_generation_jobs j
 where j.workspace_id=w and j.user_id=actor and j.plan_id=p_plan_id;
 if found then
  if prior.plan_snapshot is distinct from confirmed or exists(
   select 1 from jsonb_array_elements(prior.reference_data) r(item)
   where not exists(select 1 from jsonb_array_elements(refs) approved(item) where approved.item=r.item)
  ) then raise exception '现有生成任务与确认版不一致' using errcode='22023'; end if;
  return public.ai_generation_job_json(w,prior.id);
 end if;
 perform guanye_internal.single_judge_planning_inputs(w,plan_uuid,confirmed);
 -- Fail closed for older confirmed versions too. M29 additionally rechecks
 -- individual reference images when a worker consumes the queued job.
 perform guanye_internal.single_judge_planning_seed_refs(w,refs);
 return public.ai_generation_create(w,actor,p_plan_id,p_deck_id,p_plan,p_title,p_style,p_outline,p_references,p_pages,'{"workers":16}');
end $$;

alter function public.ai_generation_get(uuid,uuid,uuid) set schema guanye_internal;
alter function public.ai_generation_get_by_plan(uuid,uuid,text) set schema guanye_internal;
alter function public.ai_generation_recent(uuid,uuid) set schema guanye_internal;
alter function public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb) set schema guanye_internal;
alter function public.ai_generation_get_settings(uuid,uuid) set schema guanye_internal;
alter function public.ai_generation_set_workers(uuid,uuid,integer) set schema guanye_internal;

create function guanye_internal.single_judge_generation_actor(w uuid,actor uuid) returns void
language plpgsql stable set search_path='' as $$
begin
 if actor is distinct from guanye_internal.single_judge_creation_actor(w) then
  raise exception '操作人身份不匹配' using errcode='42501'; end if;
end $$;

create function public.ai_generation_get(p_workspace uuid,p_actor uuid,p_job uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(p_workspace) then
  perform guanye_internal.single_judge_generation_actor(p_workspace,p_actor);
  if not exists(select 1 from public.ai_generation_jobs j where j.workspace_id=p_workspace and j.user_id=p_actor and j.id=p_job) then
   raise exception '生成任务不存在或不属于当前评分师' using errcode='42501'; end if;
 end if;
 return guanye_internal.ai_generation_get(p_workspace,p_actor,p_job);
end $$;
create function public.ai_generation_get_by_plan(p_workspace uuid,p_actor uuid,p_plan_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(p_workspace) then
  perform guanye_internal.single_judge_generation_actor(p_workspace,p_actor); end if;
 return guanye_internal.ai_generation_get_by_plan(p_workspace,p_actor,p_plan_id);
end $$;
create function public.ai_generation_recent(p_workspace uuid,p_actor uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(p_workspace) then
  perform guanye_internal.single_judge_generation_actor(p_workspace,p_actor); end if;
 return guanye_internal.ai_generation_recent(p_workspace,p_actor);
end $$;
create function public.ai_generation_retry_pages(p_workspace uuid,p_actor uuid,p_job uuid,p_page_ids jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(p_workspace) then
  perform guanye_internal.single_judge_generation_actor(p_workspace,p_actor);
  if not exists(select 1 from public.ai_generation_jobs j where j.workspace_id=p_workspace and j.user_id=p_actor and j.id=p_job) then
   raise exception '生成任务不存在或不属于当前评分师' using errcode='42501'; end if;
 end if;
 return guanye_internal.ai_generation_retry_pages(p_workspace,p_actor,p_job,p_page_ids);
end $$;
create function public.ai_generation_get_settings(p_workspace uuid,p_actor uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(p_workspace) then
  perform guanye_internal.single_judge_generation_actor(p_workspace,p_actor); end if;
 return guanye_internal.ai_generation_get_settings(p_workspace,p_actor);
end $$;
create function public.ai_generation_set_workers(p_workspace uuid,p_actor uuid,p_max_workers integer) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if guanye_internal.single_judge_creation_active(p_workspace) then
  raise exception '单评委专业版不开放并发设置' using errcode='42501'; end if;
 return guanye_internal.ai_generation_set_workers(p_workspace,p_actor,p_max_workers);
end $$;

revoke all on function
 guanye_internal.single_judge_creation_actor(uuid),guanye_internal.single_judge_planning_path(uuid,uuid,text),
 guanye_internal.single_judge_planning_lock(uuid),guanye_internal.single_judge_planning_candidates(uuid,integer),
 guanye_internal.single_judge_planning_inputs(uuid,uuid,jsonb),guanye_internal.single_judge_planning_validate(jsonb),
 guanye_internal.single_judge_planning_seed_refs(uuid,jsonb),guanye_internal.single_judge_planning_ref_ids(jsonb,jsonb),
 guanye_internal.single_judge_planning_write(uuid,uuid,bigint,jsonb,boolean),guanye_internal.single_judge_creation_active(uuid),
 guanye_internal.single_judge_generation_actor(uuid,uuid),guanye_internal.ai_planning_get_plan(uuid,uuid),
 guanye_internal.ai_planning_list_my_plans(uuid,uuid,integer),guanye_internal.ai_planning_grant_version_access(uuid,uuid,uuid),
 guanye_internal.ai_generation_get(uuid,uuid,uuid),guanye_internal.ai_generation_get_by_plan(uuid,uuid,text),
 guanye_internal.ai_generation_recent(uuid,uuid),guanye_internal.ai_generation_retry_pages(uuid,uuid,uuid,jsonb),
 guanye_internal.ai_generation_get_settings(uuid,uuid),guanye_internal.ai_generation_set_workers(uuid,uuid,integer)
 from public,anon,authenticated,service_role;

revoke all on function
 public.single_judge_planning_insert_allowed(text,text),public.single_judge_planning_references(uuid,integer),
 public.single_judge_planning_save(uuid,uuid,bigint,jsonb),public.single_judge_planning_confirm(uuid,uuid,bigint,jsonb),
 public.single_judge_planning_get(uuid,uuid),public.single_judge_planning_history(uuid,uuid,integer),
 public.single_judge_planning_confirmed(uuid,uuid),public.single_judge_generation_create(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb),
 public.ai_planning_get_plan(uuid,uuid),public.ai_planning_list_my_plans(uuid,uuid,integer),public.ai_planning_grant_version_access(uuid,uuid,uuid),
 public.ai_generation_get(uuid,uuid,uuid),public.ai_generation_get_by_plan(uuid,uuid,text),public.ai_generation_recent(uuid,uuid),
 public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb),public.ai_generation_get_settings(uuid,uuid),public.ai_generation_set_workers(uuid,uuid,integer)
 from public,anon,authenticated,service_role;
grant execute on function
 public.single_judge_planning_insert_allowed(text,text),public.single_judge_planning_references(uuid,integer),
 public.single_judge_planning_save(uuid,uuid,bigint,jsonb),public.single_judge_planning_confirm(uuid,uuid,bigint,jsonb),
 public.single_judge_planning_get(uuid,uuid),public.single_judge_planning_history(uuid,uuid,integer),
 public.single_judge_planning_confirmed(uuid,uuid),public.single_judge_generation_create(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb),
 public.ai_planning_get_plan(uuid,uuid),public.ai_planning_list_my_plans(uuid,uuid,integer),public.ai_planning_grant_version_access(uuid,uuid,uuid),
 public.ai_generation_get(uuid,uuid,uuid),public.ai_generation_get_by_plan(uuid,uuid,text),public.ai_generation_recent(uuid,uuid),
 public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb),public.ai_generation_get_settings(uuid,uuid),public.ai_generation_set_workers(uuid,uuid,integer)
 to authenticated;
grant execute on function public.ai_generation_get(uuid,uuid,uuid),public.ai_generation_get_by_plan(uuid,uuid,text),
 public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb),public.ai_generation_get_settings(uuid,uuid),
 public.ai_generation_set_workers(uuid,uuid,integer) to service_role;

commit;
