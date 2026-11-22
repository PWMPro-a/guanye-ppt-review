-- SINGLE_JUDGE_IMAGE_RUNTIME_M31
begin;

-- SINGLE_JUDGE_IMAGE_CAPACITY_M31: only active professional workspaces.
-- Keep the previous operator attribution; only capacity and change time move.
insert into public.ai_generation_settings as s(workspace_id,max_workers)
select w.id,16 from public.workspaces w where w.single_judge_admin_id is not null
on conflict(workspace_id) do update
 set max_workers=excluded.max_workers,updated_at=now()
 where s.max_workers is distinct from excluded.max_workers;

create function guanye_internal.single_judge_image_capacity_on_activation() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 -- SINGLE_JUDGE_IMAGE_CAPACITY_M31
 insert into public.ai_generation_settings as s(workspace_id,max_workers)
 values(new.id,16) on conflict(workspace_id) do update
  set max_workers=excluded.max_workers,updated_at=now()
  where s.max_workers is distinct from excluded.max_workers;
 return new;
end $$;
revoke all on function guanye_internal.single_judge_image_capacity_on_activation()
 from public,anon,authenticated,service_role;

-- M22's checked activation RPC sets single_judge_admin_id under a workspace
-- lock. Repair old explicit settings atomically with that activation, not on claim.
create trigger single_judge_image_capacity_after_activation
 after update of single_judge_admin_id on public.workspaces
 for each row when (new.single_judge_admin_id is not null)
 execute function guanye_internal.single_judge_image_capacity_on_activation();

-- SINGLE_JUDGE_IMAGE_ACTOR_M31: once a workspace is professional, the only
-- legal image actor is its dedicated judge. The owner may have either the
-- admin or reviewer member role; that role must not open or close the path.
create function guanye_internal.single_judge_image_actor(p_workspace uuid,p_actor uuid) returns boolean
language plpgsql volatile security definer set search_path='' as $$
declare
 professional boolean;
 judge_id uuid;
begin
 select w.single_judge_admin_id is not null,w.dedicated_judge_id into professional,judge_id
 from public.workspaces w where w.id=p_workspace for share;
 if professional then
  if p_actor is null or p_actor is distinct from judge_id or not exists (
   select 1 from public.members m where m.workspace_id=p_workspace and m.user_id=p_actor
  ) then
   raise exception '仅当前工作区专职评分师可操作生图任务' using errcode='42501';
  end if;
 end if;
 return coalesce(professional,false);
end $$;
revoke all on function guanye_internal.single_judge_image_actor(uuid,uuid)
 from public,anon,authenticated,service_role;

-- Keep the M25 claim body and return shape. Only the professional actor gate
-- moves ahead of the legacy membership gate.
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
 perform guanye_internal.single_judge_image_actor(p_workspace,p_actor);
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

create or replace function public.ai_generation_heartbeat(
 p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid,p_lease_seconds integer default 1800
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare lease_value integer;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 perform guanye_internal.single_judge_image_actor(p_workspace,p_actor);
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
language plpgsql volatile security definer set search_path='' as $$
declare row_value public.ai_generation_pages%rowtype;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 perform guanye_internal.single_judge_image_actor(p_workspace,p_actor);
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

create or replace function public.ai_generation_page_succeed(
 p_workspace uuid,p_actor uuid,p_page uuid,p_lease_token uuid,p_storage_path text,
 p_content_type text,p_content_sha256 text,p_width integer default null,p_height integer default null
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare row_value public.ai_generation_pages%rowtype;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 perform guanye_internal.single_judge_image_actor(p_workspace,p_actor);
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
declare
 row_value public.ai_generation_pages%rowtype;
 professional boolean;
begin
 -- SINGLE_JUDGE_IMAGE_FAIL_LEASE_M31
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 professional=guanye_internal.single_judge_image_actor(p_workspace,p_actor);
 if not professional and not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 select * into row_value from public.ai_generation_pages
 where workspace_id=p_workspace and id=p_page for update;
 if row_value.id is null then raise exception '生成页面不存在'; end if;
 if row_value.status='succeeded' then return public.ai_generation_job_json(p_workspace,row_value.job_id); end if;
 if row_value.status<>'running' or row_value.lease_token is distinct from p_lease_token
    or (professional and p_lease_token is null) then
  raise exception '页面租约已失效';
 end if;
 -- Use wall-clock time *after* the page lock, not a timestamp frozen at the
 -- start of a transaction or statement that may have waited for this page.
 if professional and (row_value.lease_expires_at is null or row_value.lease_expires_at<=clock_timestamp()) then
  raise exception '页面租约已过期';
 end if;
 update public.ai_generation_pages
 set status='failed',error=left(coalesce(nullif(trim(p_error),''),'页面生成失败'),2000),
     finished_at=now(),updated_at=now(),lease_owner=null,lease_token=null,lease_expires_at=null
 where id=p_page;
 perform public.ai_generation_refresh_job_status(p_workspace,row_value.job_id);
 return public.ai_generation_job_json(p_workspace,row_value.job_id);
end $$;
revoke all on function public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text)
 from public,anon,authenticated,service_role;
grant execute on function public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text) to service_role;

commit;
