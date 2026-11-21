-- Owner-scoped planning history with stable (created_at,id) keyset pagination.
begin;

create index if not exists ai_planning_plans_owner_history_idx
 on public.ai_planning_plans(workspace_id,created_by,created_at desc,id desc);

create or replace function public.ai_planning_list_my_plans(
 w uuid,
 p_after uuid,
 p_limit integer
) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare
 actor_id uuid=auth.uid();
 after_created_at timestamptz;
 result_items jsonb;
 next_id uuid;
begin
 if actor_id is null then raise exception '请先登录'; end if;
 if w is null or not exists(
  select 1 from public.members
  where workspace_id=w and user_id=actor_id
 ) then raise exception '请先加入团队'; end if;
 if p_limit is null or p_limit not between 1 and 50 then
  raise exception '分页数量必须为 1–50';
 end if;
 if p_after is not null then
  select p.created_at into after_created_at
  from public.ai_planning_plans p
  where p.id=p_after and p.workspace_id=w and p.created_by=actor_id;
  if not found then raise exception '分页游标无效'; end if;
 end if;

 with page as (
  select p.id,p.created_at,p.draft->>'title' as title,p.draft->>'mode' as mode,
   case when jsonb_typeof(p.draft->'pages')='array'
    then jsonb_array_length(p.draft->'pages') else 0 end as page_count,
   p.status,p.revision
  from public.ai_planning_plans p
  where p.workspace_id=w and p.created_by=actor_id
   and (p_after is null or (p.created_at,p.id)<(after_created_at,p_after))
  order by p.created_at desc,p.id desc
  limit p_limit+1
 ), visible as (
  select * from page order by created_at desc,id desc limit p_limit
 )
 select
  coalesce(jsonb_agg(jsonb_build_object(
   'planId',v.id,
   'title',v.title,
   'mode',v.mode,
   'pageCount',v.page_count,
   'status',v.status,
   'revision',v.revision,
   'createdAt',v.created_at
  ) order by v.created_at desc,v.id desc),'[]'::jsonb),
  case when (select count(*) from page)>p_limit
   then (select v.id from visible v order by v.created_at asc,v.id asc limit 1)
   else null end
 into result_items,next_id
 from visible v;

 return jsonb_build_object('items',result_items,'nextCursor',next_id);
end $$;

revoke all on function public.ai_planning_list_my_plans(uuid,uuid,integer)
 from public,anon,authenticated,service_role;
grant execute on function public.ai_planning_list_my_plans(uuid,uuid,integer)
 to authenticated;

commit;
