-- Browser intake queue: coworkers upload originals directly, the judge converts them locally.
begin;

create table public.single_judge_intake (
 id uuid primary key,
 workspace_id uuid not null references public.workspaces(id) on delete restrict,
 submitted_by uuid not null references auth.users(id) on delete restrict,
 title text not null,
 original_name text not null,
 object_path text not null unique,
 content_type text not null,
 size_bytes bigint not null,
 sha256 text not null,
 ai_allowed boolean not null default true,
 status text not null default 'queued',
 attempts integer not null default 0,
 lease_owner text,
 lease_token uuid,
 lease_expires_at timestamptz,
 deck_id text,
 error text,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 constraint single_judge_intake_title check (length(title) between 1 and 100),
 constraint single_judge_intake_name check (length(original_name) between 1 and 255),
 constraint single_judge_intake_mime check (content_type in ('application/pdf','application/vnd.ms-powerpoint','application/vnd.openxmlformats-officedocument.presentationml.presentation')),
 constraint single_judge_intake_file_type check (
  (original_name ~* '\.pdf$' and content_type='application/pdf')
  or (original_name ~* '\.ppt$' and content_type='application/vnd.ms-powerpoint')
  or (original_name ~* '\.pptx$' and content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation')),
 constraint single_judge_intake_size check (size_bytes between 1 and 26214400),
 constraint single_judge_intake_hash check (sha256 ~ '^[0-9a-f]{64}$'),
 constraint single_judge_intake_status check (status in ('queued','running','completed','failed'))
);
alter table public.single_judge_intake enable row level security;
revoke all on public.single_judge_intake from public,anon,authenticated,service_role;
grant insert,select on public.single_judge_intake to authenticated;

create function guanye_internal.single_judge_intake_storage_allowed(p_name text)
returns boolean language plpgsql stable security definer set search_path='' as $$
declare parts text[]; access_info jsonb;
begin
 parts=string_to_array(coalesce(p_name,''),'/');
 if array_length(parts,1) is distinct from 4 or parts[2] is distinct from 'intake'
    or parts[1] !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    or parts[3] !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    or parts[4] !~* '^[A-Za-z0-9][A-Za-z0-9._ -]{0,240}\.(pdf|ppt|pptx)$' then return false; end if;
 access_info=public.single_judge_access(parts[1]::uuid);
 return access_info is not null and access_info->>'role' in ('judge','admin','submitter');
end $$;
revoke all on function guanye_internal.single_judge_intake_storage_allowed(text) from public,anon,authenticated;
grant execute on function guanye_internal.single_judge_intake_storage_allowed(text) to authenticated;

create function public.single_judge_intake_insert_allowed(w uuid,p_id uuid,p_name text)
returns boolean language plpgsql stable security definer set search_path='' as $$
declare expected text;
begin
 if p_id is null or p_name is null then return false; end if;
 expected=w::text||'/intake/'||p_id::text||'/'||p_name;
 return guanye_internal.single_judge_intake_storage_allowed(expected);
end $$;
revoke all on function public.single_judge_intake_insert_allowed(uuid,uuid,text) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_intake_insert_allowed(uuid,uuid,text) to authenticated;

create policy single_judge_intake_insert on public.single_judge_intake
 for insert to authenticated
 with check (
  submitted_by=auth.uid() and status='queued' and ai_allowed is not null
  and object_path=workspace_id::text||'/intake/'||id::text||'/'||original_name
  and public.single_judge_intake_insert_allowed(workspace_id,id,original_name)
 );

create policy single_judge_intake_select_own on public.single_judge_intake
 for select to authenticated
 using (submitted_by=auth.uid());

create policy single_judge_intake_storage_insert on storage.objects
 for insert to authenticated
 with check (bucket_id='ppt-library' and guanye_internal.single_judge_intake_storage_allowed(name));

create function guanye_internal.single_judge_intake_storage_delete_allowed(p_name text,p_owner_id text)
returns boolean language plpgsql stable security definer set search_path='' as $$
declare parts text[]; access_info jsonb; actor uuid=auth.uid();
begin
 -- SINGLE_JUDGE_INTAKE_STORAGE_DELETE_M35
 if actor is null or p_owner_id is null or p_owner_id is distinct from actor::text then return false; end if;
 parts=string_to_array(coalesce(p_name,''),'/');
 if array_length(parts,1) is distinct from 4 or parts[2] is distinct from 'intake'
    or parts[1] !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    or parts[3] !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    or parts[4] !~* '^[A-Za-z0-9][A-Za-z0-9._ -]{0,240}\.(pdf|ppt|pptx)$' then return false; end if;
 access_info=public.single_judge_access(parts[1]::uuid);
 if access_info is null or access_info->>'role' not in ('judge','admin','submitter') then return false; end if;
 -- A registered intake owns its original for the whole queued/running/failed/completed lifecycle.
 if exists(select 1 from public.single_judge_intake i
   where i.workspace_id=parts[1]::uuid and (i.id=parts[3]::uuid or i.object_path=p_name)) then return false; end if;
 return true;
end $$;
revoke all on function guanye_internal.single_judge_intake_storage_delete_allowed(text,text) from public,anon,authenticated,service_role;
grant execute on function guanye_internal.single_judge_intake_storage_delete_allowed(text,text) to authenticated;

create policy single_judge_intake_storage_delete_orphan on storage.objects
 for delete to authenticated
 using (
  bucket_id='ppt-library'
  and guanye_internal.single_judge_intake_storage_delete_allowed(name,owner_id::text)
 );

-- PostgreSQL DELETE only sees rows admitted by SELECT RLS. This permissive policy is
-- intentionally the same helper: it exposes only the caller's own unregistered orphan.
create policy single_judge_intake_storage_orphan_select on storage.objects
 for select to authenticated
 using (
  bucket_id='ppt-library'
  and guanye_internal.single_judge_intake_storage_delete_allowed(name,owner_id::text)
 );

create function guanye_internal.single_judge_team_image_read_allowed(p_name text)
returns boolean language plpgsql stable security definer set search_path='' as $$
declare parts text[]; target_workspace_id uuid; professional boolean; access_info jsonb;
begin
 -- SINGLE_JUDGE_TEAM_IMAGE_READ_M35
 if p_name is null then return false; end if;
 parts=string_to_array(p_name,'/');
 if coalesce(array_length(parts,1),0)<1 or parts[1] is null
    or parts[1] !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then return false; end if;
 target_workspace_id=parts[1]::uuid;
 select ws.single_judge_admin_id is not null into professional
  from public.workspaces ws where ws.id=target_workspace_id;
 if professional is null then return false; end if;
 if professional then
  access_info=public.single_judge_access(target_workspace_id);
  return access_info is not null and access_info->>'role' in ('judge','admin');
 end if;
 return exists(select 1 from public.members m
  where m.workspace_id=target_workspace_id and m.user_id=auth.uid());
end $$;
revoke all on function guanye_internal.single_judge_team_image_read_allowed(text) from public,anon,authenticated;
grant execute on function guanye_internal.single_judge_team_image_read_allowed(text) to authenticated;

drop policy if exists team_images_read on storage.objects;
create policy team_images_read on storage.objects
 for select to authenticated
 using (bucket_id='ppt-library' and guanye_internal.single_judge_team_image_read_allowed(name));

create index single_judge_intake_claim_idx on public.single_judge_intake (workspace_id,created_at,id)
 where status in ('queued','running');

create function public.single_judge_intake_claim(w uuid,p_worker text,p_lease_seconds integer default 1800)
returns jsonb language plpgsql security definer set search_path='' as $$
declare item public.single_judge_intake%rowtype; token uuid=gen_random_uuid(); access_info jsonb;
begin
 -- SINGLE_JUDGE_INTAKE_CLAIM_M35
 access_info=public.single_judge_access(w);
 if access_info is null or access_info->>'role' is distinct from 'judge' then
  raise exception '仅评分师可以领取同事投递' using errcode='42501'; end if;
 if length(trim(coalesce(p_worker,''))) not between 1 and 200 then raise exception 'Worker 编号无效' using errcode='22023'; end if;
 if p_lease_seconds is null or p_lease_seconds not between 60 and 7200 then raise exception '租约时间无效' using errcode='22023'; end if;
 select * into item from public.single_judge_intake
  where workspace_id=w and status in ('queued','running')
    and (status='queued' or lease_expires_at is null or lease_expires_at<=now())
  order by created_at,id for update skip locked limit 1;
 if not found then return jsonb_build_object('ok',true,'task',null); end if;
 update public.single_judge_intake
  set status='running',attempts=attempts+1,lease_owner=trim(p_worker),lease_token=token,
      lease_expires_at=now()+make_interval(secs=>p_lease_seconds),updated_at=now(),error=null
  where id=item.id;
 return jsonb_build_object('ok',true,'task',jsonb_build_object(
  'id',item.id,'workspaceId',item.workspace_id,'title',item.title,'originalName',item.original_name,
  'objectPath',item.object_path,'contentType',item.content_type,'sizeBytes',item.size_bytes,
  'sha256',item.sha256,'aiAllowed',item.ai_allowed,'leaseToken',token));
end $$;
revoke all on function public.single_judge_intake_claim(uuid,text,integer) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_intake_claim(uuid,text,integer) to authenticated;

create function public.single_judge_intake_complete(w uuid,p_id uuid,p_lease uuid,p_deck_id text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare access_info jsonb;
begin
 -- SINGLE_JUDGE_INTAKE_COMPLETE_M35
 access_info=public.single_judge_access(w);
 if access_info is null or access_info->>'role' is distinct from 'judge' then raise exception '仅评分师可以完成同事投递' using errcode='42501'; end if;
 if p_deck_id is null or p_deck_id !~ '^import-[0-9a-f-]{36}$' then raise exception '作品编号无效' using errcode='22023'; end if;
 update public.single_judge_intake set status='completed',deck_id=p_deck_id,lease_owner=null,
  lease_token=null,lease_expires_at=null,updated_at=now(),error=null
  where workspace_id=w and id=p_id and status='running' and lease_token=p_lease;
 if not found then raise exception '投递租约已失效' using errcode='22023'; end if;
 return jsonb_build_object('ok',true,'intakeId',p_id,'status','completed','deckId',p_deck_id);
end $$;
revoke all on function public.single_judge_intake_complete(uuid,uuid,uuid,text) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_intake_complete(uuid,uuid,uuid,text) to authenticated;

create function public.single_judge_intake_fail(w uuid,p_id uuid,p_lease uuid,p_error text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare access_info jsonb;
begin
 -- SINGLE_JUDGE_INTAKE_FAIL_M35
 access_info=public.single_judge_access(w);
 if access_info is null or access_info->>'role' is distinct from 'judge' then raise exception '仅评分师可以写回同事投递' using errcode='42501'; end if;
 update public.single_judge_intake set status='failed',lease_owner=null,lease_token=null,
  lease_expires_at=null,updated_at=now(),error=left(coalesce(nullif(trim(p_error),''),'素材处理失败'),2000)
  where workspace_id=w and id=p_id and status='running' and lease_token=p_lease;
 if not found then raise exception '投递租约已失效' using errcode='22023'; end if;
 return jsonb_build_object('ok',true,'intakeId',p_id,'status','failed');
end $$;
revoke all on function public.single_judge_intake_fail(uuid,uuid,uuid,text) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_intake_fail(uuid,uuid,uuid,text) to authenticated;

commit;
