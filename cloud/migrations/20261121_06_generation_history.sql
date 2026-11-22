-- Persisted, owner-scoped generation history for the workbench.
begin;

create index if not exists ai_generation_jobs_recent_idx
 on public.ai_generation_jobs(workspace_id,user_id,created_at desc,id desc);

create or replace function public.ai_generation_recent(p_workspace uuid,p_actor uuid)
returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare result jsonb;
begin
 if auth.uid() is null or auth.uid() is distinct from p_actor then
  raise exception '操作人身份不匹配';
 end if;
 if not exists(
  select 1 from public.members
  where workspace_id=p_workspace and user_id=auth.uid()
 ) then
  raise exception '请先加入团队';
 end if;
 select jsonb_build_object(
  'jobs',coalesce(jsonb_agg(jsonb_build_object(
   'id',r.id,
   'planId',r.plan_id,
   'title',r.title,
   'status',r.status,
   'pageCount',r.page_count,
   'createdAt',r.created_at,
   'updatedAt',r.updated_at
  ) order by r.created_at desc,r.id desc),'[]'::jsonb)
 ) into result
 from (
  select j.id,j.plan_id,j.title,j.status,j.created_at,j.updated_at,
   (select count(*)::integer from public.ai_generation_pages p where p.workspace_id=j.workspace_id and p.job_id=j.id) as page_count
  from public.ai_generation_jobs j
  where j.workspace_id=p_workspace and j.user_id=auth.uid()
  order by j.created_at desc,j.id desc
  limit 30
 ) r;
 return result;
end $$;

revoke all on function public.ai_generation_recent(uuid,uuid) from public,anon,service_role;
grant execute on function public.ai_generation_recent(uuid,uuid) to authenticated;

commit;
