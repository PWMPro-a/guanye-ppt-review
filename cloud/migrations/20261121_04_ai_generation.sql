-- Windows image generation: durable queue, per-page leases, retries, and generated deck publishing.
begin;

create table if not exists public.ai_generation_jobs (
 id uuid primary key default gen_random_uuid(),
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 user_id uuid not null references auth.users(id) on delete cascade,
 plan_id text not null check(length(plan_id) between 1 and 200),
 deck_id text not null unique check(deck_id ~ '^[A-Za-z0-9_-]{1,100}$'),
 title text not null default '',
 style text not null default '',
 outline jsonb not null default '{}'::jsonb check(jsonb_typeof(outline)='object'),
 reference_data jsonb not null default '[]'::jsonb check(jsonb_typeof(reference_data)='array'),
 plan_snapshot jsonb not null check(jsonb_typeof(plan_snapshot)='object'),
 config jsonb not null default '{"workers":4}'::jsonb check(jsonb_typeof(config)='object'),
 status text not null default 'queued'
  check(status in ('queued','running','completed','partial','failed','cancelled')),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 completed_at timestamptz,
 unique(workspace_id,user_id,plan_id)
);

create table if not exists public.ai_generation_pages (
 id uuid primary key default gen_random_uuid(),
 job_id uuid not null references public.ai_generation_jobs(id) on delete cascade,
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 position integer not null check(position between 1 and 600),
 plan_page_id text,
 title text not null default '',
 outline jsonb not null default '{}'::jsonb check(jsonb_typeof(outline)='object'),
 description jsonb not null default '{}'::jsonb check(jsonb_typeof(description)='object'),
 prompt text not null default '',
 reference_data jsonb not null default '[]'::jsonb check(jsonb_typeof(reference_data)='array'),
 status text not null default 'queued'
  check(status in ('queued','running','succeeded','failed')),
 attempts integer not null default 0 check(attempts>=0),
 error text,
 storage_path text not null,
 content_type text not null default 'image/png',
 content_sha256 text,
 width integer,
 height integer,
 lease_owner text,
 lease_token uuid,
 lease_expires_at timestamptz,
 started_at timestamptz,
 finished_at timestamptz,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique(job_id,position)
);

create table if not exists public.ai_generation_settings (
 workspace_id uuid primary key references public.workspaces(id) on delete cascade,
 max_workers integer not null default 4 check(max_workers between 1 and 16),
 updated_by uuid references auth.users(id) on delete set null,
 updated_at timestamptz not null default now()
);

insert into public.ai_generation_settings(workspace_id,max_workers)
select id,4 from public.workspaces
on conflict(workspace_id) do nothing;

create or replace function public.ai_generation_ensure_settings() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 insert into public.ai_generation_settings(workspace_id,max_workers)
 values(new.id,4) on conflict(workspace_id) do nothing;
 return new;
end $$;
drop trigger if exists ai_generation_settings_after_workspace on public.workspaces;
create trigger ai_generation_settings_after_workspace after insert on public.workspaces
for each row execute function public.ai_generation_ensure_settings();

create unique index if not exists ai_generation_pages_plan_page_idx
 on public.ai_generation_pages(job_id,plan_page_id)
 where plan_page_id is not null and plan_page_id<>'';
create index if not exists ai_generation_pages_queue_idx
 on public.ai_generation_pages(workspace_id,status,lease_expires_at,position);
create index if not exists ai_generation_pages_job_idx
 on public.ai_generation_pages(job_id,position);
create index if not exists ai_generation_jobs_status_idx
 on public.ai_generation_jobs(workspace_id,status,created_at);

alter table public.ai_generation_settings enable row level security;
alter table public.ai_generation_jobs enable row level security;
alter table public.ai_generation_pages enable row level security;
revoke all on public.ai_generation_settings,public.ai_generation_jobs,public.ai_generation_pages from public,anon,authenticated;
grant all on public.ai_generation_settings,public.ai_generation_jobs,public.ai_generation_pages to service_role;

create or replace function public.ai_generation_job_json(p_workspace uuid,p_job uuid)
returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object(
  'id',j.id,
  'planId',j.plan_id,
  'deckId',j.deck_id,
  'userId',j.user_id,
  'status',j.status,
  'title',j.title,
  'style',j.style,
  'outline',j.outline,
  'references',j.reference_data,
  'config',j.config,
  'createdAt',j.created_at,
  'updatedAt',j.updated_at,
  'completedAt',j.completed_at,
  'pages',coalesce((
   select jsonb_agg(page_rows.item order by page_rows.position)
   from (
    select p.position,jsonb_build_object(
     'id',p.id,
     'position',p.position,
     'planPageId',coalesce(p.plan_page_id,''),
     'title',p.title,
     'outline',p.outline,
     'description',p.description,
     'prompt',p.prompt,
     'references',p.reference_data,
     'status',p.status,
     'attempts',p.attempts,
     'error',p.error,
     'storagePath',p.storage_path,
     'contentType',p.content_type,
     'contentSha256',p.content_sha256,
     'width',p.width,
     'height',p.height,
     'image',case when p.storage_path is not null and p.status='succeeded' then jsonb_build_object(
       'id',p.id,
       'name','page-'||lpad(p.position::text,4,'0')||case when p.content_type='image/jpeg' then '.jpg' else '.png' end,
       'path',p.storage_path,
       'thumbPath',p.storage_path
     ) else null end
    ) item
    from public.ai_generation_pages p
    where p.workspace_id=j.workspace_id and p.job_id=j.id
   ) page_rows
  ),'[]'::jsonb)
 )
 from public.ai_generation_jobs j
 where j.workspace_id=p_workspace and j.id=p_job
$$;

create or replace function public.ai_generation_is_safe_reference_path(p_workspace uuid,p_path text)
returns boolean
language sql immutable set search_path='' as $$
 select coalesce(
  p_path is not null and length(p_path)<=500
  and p_path like p_workspace::text||'/%'
  and position('..' in p_path)=0
  and position(chr(92) in p_path)=0
  and p_path !~ E'[\r\n]'
 ,false)
$$;

create or replace function public.ai_generation_select_reference_path(
 p_workspace uuid,p_job uuid,p_page uuid
) returns text
language plpgsql stable security definer set search_path='' as $$
declare
 page_row public.ai_generation_pages%rowtype;
 job_row public.ai_generation_jobs%rowtype;
 selected_path text;
begin
 select * into page_row from public.ai_generation_pages
 where workspace_id=p_workspace and id=p_page and job_id=p_job;
 if page_row.id is null then raise exception '生成页面不存在'; end if;
 select * into job_row from public.ai_generation_jobs
 where workspace_id=p_workspace and id=p_job;
 if job_row.id is null then raise exception '生成任务不存在'; end if;

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
 return selected_path;
end $$;

create or replace function public.ai_generation_page_reference_path(
 p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid
) returns text
language plpgsql stable security definer set search_path='' as $$
declare page_row public.ai_generation_pages%rowtype;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select * into page_row from public.ai_generation_pages
 where workspace_id=p_workspace and id=p_page;
 if page_row.id is null then raise exception '生成页面不存在'; end if;
 if page_row.status<>'running' or page_row.lease_token is distinct from p_lease_token then
  raise exception '页面租约已失效';
 end if;
 if page_row.lease_expires_at is null or page_row.lease_expires_at<=now() then
  raise exception '页面租约已过期';
 end if;
 return public.ai_generation_select_reference_path(p_workspace,page_row.job_id,p_page);
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
 p_config jsonb default '{"workers":4}'::jsonb
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
  coalesce(p_config,'{"workers":4}'::jsonb),'queued'
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

create or replace function public.ai_generation_get(p_workspace uuid,p_actor uuid,p_job uuid)
returns jsonb
language plpgsql stable security definer set search_path='' as $$
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 if not exists(select 1 from public.ai_generation_jobs where workspace_id=p_workspace and id=p_job) then
  raise exception '生成任务不存在';
 end if;
 return public.ai_generation_job_json(p_workspace,p_job);
end $$;

create or replace function public.ai_generation_get_by_plan(p_workspace uuid,p_actor uuid,p_plan_id text)
returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare target uuid;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select id into target from public.ai_generation_jobs
 where workspace_id=p_workspace and plan_id=p_plan_id and user_id=p_actor
 order by created_at desc limit 1;
 if target is null then raise exception '生成任务不存在'; end if;
 return public.ai_generation_job_json(p_workspace,target);
end $$;

create or replace function public.ai_generation_get_settings(p_workspace uuid,p_actor uuid)
returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare row_value public.ai_generation_settings%rowtype;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select * into row_value from public.ai_generation_settings s where s.workspace_id=p_workspace;
 if row_value.workspace_id is null then raise exception '生成配置不存在'; end if;
 return jsonb_build_object('workspaceId',row_value.workspace_id,'maxWorkers',row_value.max_workers,'updatedAt',row_value.updated_at);
end $$;

create or replace function public.ai_generation_set_workers(p_workspace uuid,p_actor uuid,p_max_workers integer)
returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor and role='admin') then
  raise exception '仅管理员可以调整工作区生图并发';
 end if;
 if p_max_workers is null or p_max_workers not between 1 and 16 then raise exception '并发数必须在 1 到 16 之间'; end if;
 insert into public.ai_generation_settings(workspace_id,max_workers,updated_by,updated_at)
 values(p_workspace,p_max_workers,p_actor,now())
 on conflict(workspace_id) do update set max_workers=excluded.max_workers,updated_by=excluded.updated_by,updated_at=now();
 return public.ai_generation_get_settings(p_workspace,p_actor);
end $$;
create or replace function public.ai_generation_claim_pages(
 p_workspace uuid,
 p_actor uuid,
 p_worker_id text,
 p_limit integer default 4,
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
 insert into public.ai_generation_settings(workspace_id,max_workers)
 values(p_workspace,4) on conflict(workspace_id) do nothing;
 -- Serialize claims on the workspace row so several worker processes cannot
 -- exceed the one workspace-wide concurrency limit.
 select s.max_workers into max_workers
 from public.ai_generation_settings s where s.workspace_id=p_workspace for update;
 select count(*) into active_count from public.ai_generation_pages
 where workspace_id=p_workspace and status='running'
  and lease_expires_at is not null and lease_expires_at>now();
 available_slots=greatest(0,max_workers-active_count);
 if available_slots=0 then return '[]'::jsonb; end if;
 claim_limit=least(greatest(1,least(coalesce(p_limit,4),32)),available_slots);
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
create or replace function public.ai_generation_heartbeat(
 p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid,p_lease_seconds integer default 1800
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare lease_value integer;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 lease_value=greatest(60,least(coalesce(p_lease_seconds,1800),7200));
 update public.ai_generation_pages
 set lease_expires_at=now()+make_interval(secs=>lease_value),updated_at=now()
 where workspace_id=p_workspace and id=p_page and status='running'
  and lease_token=p_lease_token and lease_expires_at>now();
 if not found then raise exception '页面租约已失效'; end if;
 return jsonb_build_object('ok',true,'leaseExpiresAt',now()+make_interval(secs=>lease_value));
end $$;

create or replace function public.ai_generation_page_upload_target(
 p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid
) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare row_value public.ai_generation_pages%rowtype;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select * into row_value from public.ai_generation_pages
 where workspace_id=p_workspace and id=p_page;
 if row_value.id is null then raise exception '生成页面不存在'; end if;
 if row_value.status='succeeded' then
  return jsonb_build_object('storagePath',row_value.storage_path,'contentType',row_value.content_type,'alreadySucceeded',true);
 end if;
 if row_value.status<>'running' or row_value.lease_token is distinct from p_lease_token then
  raise exception '页面租约已失效';
 end if;
 if row_value.lease_expires_at is null or row_value.lease_expires_at<=now() then
  raise exception '页面租约已过期';
 end if;
 return jsonb_build_object('storagePath',row_value.storage_path,'contentType',row_value.content_type,'alreadySucceeded',false);
end $$;

create or replace function public.ai_generation_publish_deck(p_workspace uuid,p_job uuid)
returns text
language plpgsql security definer set search_path='' as $$
declare
 job_row public.ai_generation_jobs%rowtype;
 page_count integer;
 success_count integer;
 images_value jsonb;
 pages_value jsonb;
 deck_value jsonb;
begin
 select * into job_row from public.ai_generation_jobs where workspace_id=p_workspace and id=p_job;
 if job_row.id is null then raise exception '生成任务不存在'; end if;
 select count(*),count(*) filter(where status='succeeded') into page_count,success_count
 from public.ai_generation_pages where workspace_id=p_workspace and job_id=p_job;
 if page_count=0 or success_count<>page_count then raise exception '页面尚未全部完成'; end if;

 select jsonb_agg(jsonb_build_object(
  'id',id,
  'name','page-'||lpad(position::text,4,'0')||case when content_type='image/jpeg' then '.jpg' else '.png' end,
  'path',storage_path,
  'thumbPath',storage_path
 ) order by position) into images_value
 from public.ai_generation_pages where workspace_id=p_workspace and job_id=p_job;

 select jsonb_agg(jsonb_build_object(
  'id',coalesce(nullif(plan_page_id,''),id::text),
  'pageId',coalesce(nullif(plan_page_id,''),id::text),
  'position',position,
  'title',title,
  'outline',outline,
  'description',description,
  'prompt',prompt,
  'references',reference_data,
  'imageId',id,
  'path',storage_path
 ) order by position) into pages_value
 from public.ai_generation_pages where workspace_id=p_workspace and job_id=p_job;

 deck_value=jsonb_build_object(
  'id',job_row.deck_id,
  'name',job_row.title,
  'title',job_row.title,
  'style',job_row.style,
  'origin','generated',
  'images',coalesce(images_value,'[]'::jsonb),
  'generation',jsonb_build_object(
   'outline',job_row.outline,
   'style',coalesce(job_row.plan_snapshot->'style',to_jsonb(job_row.style)),
   'pages',coalesce(pages_value,'[]'::jsonb),
   'references',job_row.reference_data
  ),
  'createdAt',job_row.created_at,
  'updatedAt',now()
 );
 insert into public.decks(id,workspace_id,data,source_kind) values(job_row.deck_id,p_workspace,deck_value,'generated')
 on conflict(id) do update set data=excluded.data
 where public.decks.workspace_id=p_workspace and public.decks.source_kind='generated';
 if not exists(select 1 from public.decks where id=job_row.deck_id and workspace_id=p_workspace and source_kind='generated') then
  raise exception '生成作品发布冲突';
 end if;
 return job_row.deck_id;
end $$;

create or replace function public.ai_generation_refresh_job_status(p_workspace uuid,p_job uuid)
returns text
language plpgsql security definer set search_path='' as $$
declare
 total_count integer;
 success_count integer;
 failed_count integer;
 running_count integer;
 next_status text;
begin
 select count(*),count(*) filter(where status='succeeded'),count(*) filter(where status='failed'),count(*) filter(where status='running')
 into total_count,success_count,failed_count,running_count
 from public.ai_generation_pages where workspace_id=p_workspace and job_id=p_job;
 if total_count=0 then raise exception '生成页面不存在'; end if;
 if success_count=total_count then
  perform public.ai_generation_publish_deck(p_workspace,p_job);
  next_status='completed';
 elsif failed_count=total_count then
  next_status='failed';
 elsif success_count+failed_count=total_count then
  next_status='partial';
 elsif running_count>0 then
  next_status='running';
 else
  next_status='queued';
 end if;
 update public.ai_generation_jobs
 set status=next_status,updated_at=now(),
     completed_at=case when next_status in ('completed','partial','failed') then coalesce(completed_at,now()) else null end
 where workspace_id=p_workspace and id=p_job;
 return next_status;
end $$;

create or replace function public.ai_generation_page_succeed(
 p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid,p_storage_path text,
 p_content_type text,p_content_sha256 text,p_width integer default null,p_height integer default null
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare row_value public.ai_generation_pages%rowtype;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select * into row_value from public.ai_generation_pages
 where workspace_id=p_workspace and id=p_page for update;
 if row_value.id is null then raise exception '生成页面不存在'; end if;
 if row_value.status='succeeded' then
  if p_content_sha256 is not null and row_value.content_sha256=p_content_sha256 then
   return public.ai_generation_job_json(p_workspace,row_value.job_id);
  end if;
  raise exception '页面已完成且内容不一致';
 end if;
 if row_value.status<>'running' or row_value.lease_token is distinct from p_lease_token then
  raise exception '页面租约已失效';
 end if;
 if row_value.lease_expires_at is null or row_value.lease_expires_at<=now() then
  raise exception '页面租约已过期';
 end if;
 if p_storage_path is distinct from row_value.storage_path then raise exception '图片存储路径不匹配'; end if;
 if p_content_type not in ('image/png','image/jpeg','image/webp') then raise exception '图片格式无效'; end if;
 if p_content_sha256 !~ '^[0-9a-f]{64}$' then raise exception '图片哈希无效'; end if;
 update public.ai_generation_pages
 set status='succeeded',content_sha256=p_content_sha256,content_type=p_content_type,
     width=p_width,height=p_height,error=null,finished_at=now(),updated_at=now(),
     lease_owner=null,lease_token=null,lease_expires_at=null
 where id=p_page;
 perform public.ai_generation_refresh_job_status(p_workspace,row_value.job_id);
 return public.ai_generation_job_json(p_workspace,row_value.job_id);
end $$;

create or replace function public.ai_generation_page_fail(
 p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid,p_error text
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare row_value public.ai_generation_pages%rowtype;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select * into row_value from public.ai_generation_pages
 where workspace_id=p_workspace and id=p_page for update;
 if row_value.id is null then raise exception '生成页面不存在'; end if;
 if row_value.status='succeeded' then return public.ai_generation_job_json(p_workspace,row_value.job_id); end if;
 if row_value.status<>'running' or row_value.lease_token is distinct from p_lease_token then
  raise exception '页面租约已失效';
 end if;
 update public.ai_generation_pages
 set status='failed',error=left(coalesce(nullif(trim(p_error),''),'页面生成失败'),2000),
     finished_at=now(),updated_at=now(),lease_owner=null,lease_token=null,lease_expires_at=null
 where id=p_page;
 perform public.ai_generation_refresh_job_status(p_workspace,row_value.job_id);
 return public.ai_generation_job_json(p_workspace,row_value.job_id);
end $$;

create or replace function public.ai_generation_retry_pages(
 p_workspace uuid,p_actor uuid,p_job uuid,p_page_ids jsonb
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
 job_row public.ai_generation_jobs%rowtype;
 requested_count integer;
 matched_count integer;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select * into job_row from public.ai_generation_jobs
 where workspace_id=p_workspace and id=p_job for update;
 if job_row.id is null then raise exception '生成任务不存在'; end if;
 if job_row.user_id<>p_actor and not exists(
  select 1 from public.members where workspace_id=p_workspace and user_id=p_actor and role='admin'
 ) then raise exception '仅任务发起人或管理员可以重试'; end if;
 if jsonb_typeof(p_page_ids) is distinct from 'array' or jsonb_array_length(p_page_ids) not between 1 and 600 then
  raise exception '请选择要重试的失败页面';
 end if;
 select count(distinct value #>> '{}') into requested_count from jsonb_array_elements(p_page_ids) where jsonb_typeof(value)='string';
 if requested_count<>jsonb_array_length(p_page_ids) then raise exception '重试页面列表无效'; end if;
 select count(*) into matched_count
 from public.ai_generation_pages
 where workspace_id=p_workspace and job_id=p_job and status='failed'
  and id::text in (select value #>> '{}' from jsonb_array_elements(p_page_ids));
 if matched_count<>requested_count then raise exception '仅可重试失败页面'; end if;
 update public.ai_generation_pages
 set status='queued',error=null,attempts=0,lease_owner=null,lease_token=null,lease_expires_at=null,
     started_at=null,finished_at=null,updated_at=now()
 where workspace_id=p_workspace and job_id=p_job and status='failed'
  and id::text in (select value #>> '{}' from jsonb_array_elements(p_page_ids));
 update public.ai_generation_jobs set status='queued',completed_at=null,updated_at=now() where id=p_job;
 return public.ai_generation_job_json(p_workspace,p_job);
end $$;

revoke all on function public.ai_generation_ensure_settings() from public,anon,authenticated;
revoke all on function public.ai_generation_is_safe_reference_path(uuid,text) from public,anon,authenticated;
revoke all on function public.ai_generation_select_reference_path(uuid,uuid,uuid) from public,anon,authenticated;
revoke all on function public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid) from public,anon,authenticated;
revoke all on function public.ai_generation_get_settings(uuid,uuid) from public,anon,authenticated;
revoke all on function public.ai_generation_set_workers(uuid,uuid,integer) from public,anon,authenticated;
revoke all on function public.ai_generation_job_json(uuid,uuid),
 public.ai_generation_create(uuid,uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,jsonb),
 public.ai_generation_get(uuid,uuid,uuid),public.ai_generation_get_by_plan(uuid,uuid,text),
 public.ai_generation_claim_pages(uuid,uuid,text,integer,integer),
 public.ai_generation_heartbeat(uuid,uuid,uuid,uuid,integer),
 public.ai_generation_page_upload_target(uuid,uuid,uuid,uuid),
 public.ai_generation_publish_deck(uuid,uuid),public.ai_generation_refresh_job_status(uuid,uuid),
 public.ai_generation_page_succeed(uuid,uuid,uuid,uuid,text,text,text,integer,integer),
 public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text),
 public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb)
from public,anon,authenticated;
grant execute on function public.ai_generation_get_settings(uuid,uuid) to service_role;
grant execute on function public.ai_generation_is_safe_reference_path(uuid,text),
 public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)
to service_role;
grant execute on function public.ai_generation_get(uuid,uuid,uuid),
 public.ai_generation_get_by_plan(uuid,uuid,text),
 public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb),
 public.ai_generation_get_settings(uuid,uuid),public.ai_generation_set_workers(uuid,uuid,integer)
to authenticated;
grant execute on function public.ai_generation_set_workers(uuid,uuid,integer) to service_role;
grant execute on function public.ai_generation_job_json(uuid,uuid),
 public.ai_generation_create(uuid,uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,jsonb),
 public.ai_generation_get(uuid,uuid,uuid),public.ai_generation_get_by_plan(uuid,uuid,text),
 public.ai_generation_claim_pages(uuid,uuid,text,integer,integer),
 public.ai_generation_heartbeat(uuid,uuid,uuid,uuid,integer),
 public.ai_generation_page_upload_target(uuid,uuid,uuid,uuid),
 public.ai_generation_publish_deck(uuid,uuid),public.ai_generation_refresh_job_status(uuid,uuid),
 public.ai_generation_page_succeed(uuid,uuid,uuid,uuid,text,text,text,integer,integer),
 public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text),
 public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb)
to service_role;

commit;