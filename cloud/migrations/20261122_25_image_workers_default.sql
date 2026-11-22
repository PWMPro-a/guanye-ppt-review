-- Raise the shared GPT Image default without replacing an explicitly configured cap.
begin;

alter table public.ai_generation_settings alter column max_workers set default 16;
alter table public.ai_generation_jobs alter column config set default '{"workers":16}'::jsonb;
update public.ai_generation_settings
 set max_workers=16, updated_at=now()
 where max_workers=4 and updated_by is null;

create or replace function public.ai_generation_ensure_settings() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 insert into public.ai_generation_settings(workspace_id)
 values(new.id) on conflict(workspace_id) do nothing;
 return new;
end $$;

create or replace function public.ai_generation_create(
 p_workspace uuid,
 p_user uuid,
 p_plan_id text,
 p_deck_id text,
 p_plan jsonb,
 p_title text,
 p_style text,
 p_outline jsonb,
 p_references jsonb,
 p_pages jsonb,
 p_config jsonb default '{"workers":16}'::jsonb
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
 existing_id uuid;
 job_id_value uuid;
 page_json jsonb;
 page_id_value uuid;
 page_position integer=0;
 page_title text;
 page_outline jsonb;
 page_description jsonb;
 page_prompt text;
 page_plan_id text;
 page_refs jsonb;
 page_path text;
 plan_page_id_value text;
 page_content_type text;
 plan_uuid uuid;
 plan_row public.ai_planning_plans%rowtype;
 actor_role text;
begin
 if auth.uid() is not null and p_user is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if p_workspace is null or p_user is null then raise exception '工作区和用户不能为空'; end if;
 if p_plan_id !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' then
  raise exception '计划编号无效';
 end if;
 plan_uuid=p_plan_id::uuid;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_user) then
  raise exception '请先加入团队';
 end if;
 select m.role into actor_role from public.members m
 where m.workspace_id=p_workspace and m.user_id=p_user;
 select * into plan_row from public.ai_planning_plans
 where id=plan_uuid and workspace_id=p_workspace for share;
 if not found then raise exception '已确认策划不存在或不属于当前工作区'; end if;
 if plan_row.status<>'confirmed' or plan_row.current_version_id is null then
  raise exception '策划尚未确认，不能创建生成任务';
 end if;
 if plan_row.created_by<>p_user and actor_role<>'admin' and not exists(
  select 1 from public.ai_planning_version_access a
  where a.version_id=plan_row.current_version_id and a.user_id=p_user
 ) then
  raise exception '当前用户未获得该策划版本的生成授权';
 end if;
 if p_plan is distinct from public.ai_planning_read_confirmed_plan(plan_uuid) then
  raise exception '生成快照与不可变确认版本不一致';
 end if; if jsonb_typeof(p_plan) is distinct from 'object' then raise exception '生成计划无效'; end if;
 if jsonb_typeof(p_pages) is distinct from 'array' or jsonb_array_length(p_pages) not between 1 and 600 then
  raise exception '生成页面无效';
 end if;
 if jsonb_typeof(coalesce(p_outline,'{}'::jsonb)) is distinct from 'object' then raise exception '大纲无效'; end if;
 if jsonb_typeof(coalesce(p_references,'[]'::jsonb)) is distinct from 'array' then raise exception '参考物无效'; end if;
 if jsonb_typeof(coalesce(p_config,'{}'::jsonb)) is distinct from 'object' then raise exception '生成配置无效'; end if;

 select id into existing_id from public.ai_generation_jobs
 where workspace_id=p_workspace and user_id=p_user and plan_id=p_plan_id;
 if existing_id is not null then
  return public.ai_generation_job_json(p_workspace,existing_id);
 end if;

 if exists(select 1 from public.decks where id=p_deck_id) then
  raise exception '作品编号已被占用';
 end if;

 insert into public.ai_generation_jobs(
  workspace_id,user_id,plan_id,deck_id,title,style,outline,reference_data,plan_snapshot,config,status
 ) values(
  p_workspace,p_user,p_plan_id,p_deck_id,coalesce(p_title,''),coalesce(p_style,''),
  coalesce(p_outline,'{}'::jsonb),coalesce(p_references,'[]'::jsonb),p_plan,
  coalesce(p_config,'{"workers":16}'::jsonb),'queued'
 ) returning id into job_id_value;

 for page_json in select value from jsonb_array_elements(p_pages) loop
  page_position=page_position+1;
  if jsonb_typeof(page_json)='string' then
   page_json=jsonb_build_object('title',page_json #>> '{}','outline',jsonb_build_object('title',page_json #>> '{}'));
  end if;
  if jsonb_typeof(page_json) is distinct from 'object' then raise exception '第 % 页格式无效',page_position; end if;
  plan_page_id_value=coalesce(nullif(page_json->>'planPageId',''),nullif(page_json->>'plan_page_id',''),nullif(page_json->>'id',''));
  page_title=coalesce(nullif(page_json->>'title',''),nullif(page_json#>>'{outline,title}',''),'第 '||page_position||' 页');
  page_outline=case
   when jsonb_typeof(page_json->'outline')='object' then page_json->'outline'
   when jsonb_typeof(page_json->'outline_content')='object' then page_json->'outline_content'
   when jsonb_typeof(page_json->'outline')='string' then jsonb_build_object('title',page_title,'text',page_json->>'outline')
   else jsonb_build_object('title',page_title)
  end;
  page_prompt=coalesce(nullif(page_json->>'prompt',''),nullif(page_json->>'descriptionText',''),nullif(page_json->>'description_text',''),'');
  page_description=case
   when jsonb_typeof(page_json->'description')='object' then page_json->'description'
   when jsonb_typeof(page_json->'description_content')='object' then page_json->'description_content'
   when jsonb_typeof(page_json->'description')='string' then jsonb_build_object('text',page_json->>'description')
   when page_prompt<>'' then jsonb_build_object('text',page_prompt)
   else jsonb_build_object('text',page_title)
  end;
  page_refs=case when jsonb_typeof(page_json->'references')='array' then page_json->'references' else '[]'::jsonb end;
  page_content_type=coalesce(nullif(page_json->>'contentType',''),nullif(page_json->>'content_type',''),'image/png');
  if page_content_type not in ('image/png','image/jpeg','image/webp') then raise exception '第 % 页图片格式无效',page_position; end if;
  page_id_value=gen_random_uuid();
  page_path=p_workspace::text||'/'||p_deck_id||'/'||page_id_value::text||
   case page_content_type when 'image/jpeg' then '.jpg' when 'image/webp' then '.webp' else '.png' end;
  insert into public.ai_generation_pages(
   id,job_id,workspace_id,position,plan_page_id,title,outline,description,prompt,reference_data,storage_path,content_type
  ) values(
   page_id_value,job_id_value,p_workspace,page_position,nullif(plan_page_id_value,''),page_title,page_outline,page_description,
   page_prompt,page_refs,page_path,page_content_type
  );
 end loop;

 return public.ai_generation_job_json(p_workspace,job_id_value);
end $$;

create or replace function public.ai_generation_claim_pages(
 p_workspace uuid,
 p_actor uuid,
 p_worker_id text,
 p_limit integer default 16,
 p_lease_seconds integer default 1800
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
 result jsonb;
 lease_value integer;
 max_workers integer;
 active_count integer;
 available_slots integer;
 claim_limit integer;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 if length(trim(coalesce(p_worker_id,''))) not between 1 and 200 then raise exception '工作节点编号无效'; end if;
 insert into public.ai_generation_settings(workspace_id)
 values(p_workspace) on conflict(workspace_id) do nothing;
 -- Serialize claims on the workspace row so several worker processes cannot
 -- exceed the one workspace-wide concurrency limit.
 select s.max_workers into max_workers
 from public.ai_generation_settings s where s.workspace_id=p_workspace for update;
 select count(*) into active_count from public.ai_generation_pages
 where workspace_id=p_workspace and status='running'
  and lease_expires_at is not null and lease_expires_at>now();
 available_slots=greatest(0,max_workers-active_count);
 if available_slots=0 then return '[]'::jsonb; end if;
 claim_limit=least(greatest(1,least(coalesce(p_limit,16),32)),available_slots);
 lease_value=greatest(60,least(coalesce(p_lease_seconds,1800),7200));
 with picked as (
  select p.id
  from public.ai_generation_pages p
  join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=p.workspace_id
  where p.workspace_id=p_workspace
   and j.status in ('queued','running')
   and (p.status='queued' or (p.status='running' and (p.lease_expires_at is null or p.lease_expires_at<=now())))
  order by j.created_at,p.position
  for update of p skip locked
  limit claim_limit
 ), changed as (
  update public.ai_generation_pages p
  set status='running',lease_owner=trim(p_worker_id),lease_token=gen_random_uuid(),
      lease_expires_at=now()+make_interval(secs=>lease_value),attempts=p.attempts+1,
      started_at=coalesce(p.started_at,now()),updated_at=now(),error=null
  from picked x where p.id=x.id
  returning p.*
 )
 select coalesce(jsonb_agg(item ORDER BY job_created,page_position),'[]'::jsonb) into result
 from (
  select j.created_at job_created,p.position page_position,
   jsonb_build_object(
    'jobId',p.job_id,'deckId',j.deck_id,'planId',j.plan_id,'title',j.title,
    'style',j.style,'outline',j.outline,'references',j.reference_data,
    'pageId',p.id,'position',p.position,'planPageId',coalesce(p.plan_page_id,''),
    'pageTitle',p.title,'pageOutline',p.outline,'description',p.description,'prompt',p.prompt,
    'pageReferences',p.reference_data,'hasReferenceImage',public.ai_generation_select_reference_path(p_workspace,p.job_id,p.id) is not null,
    'storagePath',p.storage_path,'contentType',p.content_type,
    'attempts',p.attempts,'leaseToken',p.lease_token,
    'leaseExpiresAt',p.lease_expires_at
   ) item
  from changed p join public.ai_generation_jobs j on j.id=p.job_id
 ) rows;
 update public.ai_generation_jobs j set status='running',updated_at=now()
 where j.workspace_id=p_workspace and j.status='queued'
  and exists(select 1 from public.ai_generation_pages p where p.job_id=j.id and p.status='running');
 return result;
end $$;

commit;
