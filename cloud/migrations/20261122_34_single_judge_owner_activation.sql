-- One-account professional activation: the sole owner is both reviewer and admin.
begin;

alter table public.workspaces drop constraint if exists workspaces_single_judge_distinct_accounts;
alter table public.workspaces add constraint workspaces_single_judge_single_owner
 check (single_judge_admin_id is null or dedicated_judge_id is not null);

create function public.single_judge_activate_owner(w uuid, owner_id uuid)
returns void
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype;
begin
 -- SINGLE_JUDGE_OWNER_ACTIVATION_M34
 select * into ws from public.workspaces where id=w for update;
 if not found or owner_id is null
    or not exists(select 1 from public.members m where m.workspace_id=w and m.user_id=owner_id and m.role in ('reviewer','admin')) then
  raise exception '唯一评分师必须是当前工作区现有成员' using errcode='22023';
 end if;
 if ws.single_judge_admin_id is not null
    and (ws.dedicated_judge_id is distinct from owner_id or ws.single_judge_admin_id is distinct from owner_id) then
  raise exception '工作区已绑定其他单账号评分师，不能通过此入口重绑' using errcode='42501'; end if;
 update public.workspaces
  set dedicated_judge_id=owner_id,single_judge_admin_id=owner_id
  where id=w;
end $$;
revoke all on function public.single_judge_activate_owner(uuid,uuid)
 from public,anon,authenticated,service_role;
grant execute on function public.single_judge_activate_owner(uuid,uuid) to service_role;
commit;
