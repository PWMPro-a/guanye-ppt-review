-- AI planning: bounded drafts, immutable confirmed versions, independent access,
-- calibration-only reference candidates, and a service-only generation read RPC.
begin;

create table if not exists public.ai_planning_plans (
 id uuid primary key,
 workspace_id uuid not null references public.workspaces(id) on delete restrict,
 created_by uuid not null references auth.users(id) on delete restrict,
 status text not null default 'draft' check (status in ('draft','confirmed')),
 revision bigint not null default 1 check (revision >= 1),
 draft jsonb not null check (jsonb_typeof(draft)='object'),
 current_version_id uuid,
 confirmed_at timestamptz,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique(id,workspace_id)
);

create table if not exists public.ai_planning_versions (
 id uuid primary key,
 plan_id uuid not null,
 workspace_id uuid not null,
 version integer not null check (version >= 1),
 confirmed_by uuid not null references auth.users(id) on delete restrict,
 payload jsonb not null check (jsonb_typeof(payload)='object'),
 content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
 confirmed_at timestamptz not null default now(),
 unique(plan_id,version),
 unique(id,workspace_id),
 foreign key(plan_id,workspace_id) references public.ai_planning_plans(id,workspace_id) on delete restrict
);

do $planning$
begin
 if not exists (
  select 1 from pg_constraint
  where conname='ai_planning_plans_current_version_fk'
    and conrelid='public.ai_planning_plans'::regclass
 ) then
  alter table public.ai_planning_plans
   add constraint ai_planning_plans_current_version_fk
   foreign key(current_version_id) references public.ai_planning_versions(id) on delete restrict
   deferrable initially deferred;
 end if;
end $planning$;

create table if not exists public.ai_planning_version_access (
 version_id uuid not null references public.ai_planning_versions(id) on delete restrict,
 user_id uuid not null references auth.users(id) on delete restrict,
 access_role text not null check (access_role in ('owner','viewer')),
 granted_by uuid not null references auth.users(id) on delete restrict,
 granted_at timestamptz not null default now(),
 primary key(version_id,user_id)
);

create table if not exists public.ai_planning_version_references (
 version_id uuid not null references public.ai_planning_versions(id) on delete restrict,
 deck_id text not null check (length(deck_id) between 1 and 200),
 reference_rank integer not null check (reference_rank >= 1),
 score numeric,
 deck_snapshot jsonb not null check (jsonb_typeof(deck_snapshot)='object'),
 image_paths jsonb not null default '[]'::jsonb check (jsonb_typeof(image_paths)='array'),
 primary key(version_id,deck_id)
);

create index if not exists ai_planning_plans_workspace_updated_idx
 on public.ai_planning_plans(workspace_id,updated_at desc);
create index if not exists ai_planning_versions_plan_idx
 on public.ai_planning_versions(plan_id,version desc);
create index if not exists ai_planning_access_user_idx
 on public.ai_planning_version_access(user_id,version_id);

alter table public.ai_planning_plans enable row level security;
alter table public.ai_planning_versions enable row level security;
alter table public.ai_planning_version_access enable row level security;
alter table public.ai_planning_version_references enable row level security;
revoke all on public.ai_planning_plans,public.ai_planning_versions,
 public.ai_planning_version_access,public.ai_planning_version_references
 from public,anon,authenticated;
grant all on public.ai_planning_plans,public.ai_planning_versions,
 public.ai_planning_version_access,public.ai_planning_version_references
 to service_role;

create or replace function public.ai_planning_validate_draft(value jsonb) returns void
language plpgsql immutable set search_path=public,extensions as $$
declare page_count integer; item jsonb;
begin
 if value is null or jsonb_typeof(value) is distinct from 'object' then
  raise exception '策划内容必须是对象';
 end if;
 if coalesce(value->>'schemaVersion','')<>'1' then
  raise exception '策划版本格式不受支持';
 end if;
 if length(trim(coalesce(value->>'title',''))) not between 1 and 200 then
  raise exception '策划标题需要 1–200 个字符';
 end if;
 if coalesce(value->>'mode','') not in ('content_draft','template_sample') then
  raise exception '策划模式无效';
 end if;
 if jsonb_typeof(value->'style') is distinct from 'object' then
  raise exception '风格建议无效';
 end if;
 if jsonb_typeof(value->'pages') is distinct from 'array' then
  raise exception '逐页策划无效';
 end if;
 page_count=jsonb_array_length(value->'pages');
 if page_count not between 1 and 30 then
  raise exception '逐页策划需要 1–30 页';
 end if;
 if coalesce(value->>'pageCount','') !~ '^[0-9]+$' or (value->>'pageCount')::integer<>page_count then
  raise exception '页数必须与逐页策划一致';
 end if;
 for item in select p.item from jsonb_array_elements(value->'pages') as p(item) loop
  if jsonb_typeof(item) is distinct from 'object' then
   raise exception '逐页策划页必须是对象';
  end if;
  if length(trim(coalesce(item->>'title',''))) not between 1 and 200 then
   raise exception '每页都需要 1–200 个字符的标题';
  end if;
  if jsonb_typeof(item->'copy') is distinct from 'array' then
   raise exception '每页文案必须是数组';
  end if;
  if jsonb_array_length(item->'copy')>24 then
   raise exception '每页文案最多 24 条';
  end if;
 end loop;
 if jsonb_typeof(coalesce(value->'references','[]'::jsonb)) is distinct from 'array'
    or jsonb_array_length(coalesce(value->'references','[]'::jsonb))>12 then
  raise exception '参考作品无效';
 end if;
 if jsonb_typeof(coalesce(value->'sourceManifest','[]'::jsonb)) is distinct from 'array'
    or jsonb_array_length(coalesce(value->'sourceManifest','[]'::jsonb))>16 then
  raise exception '资料来源无效';
 end if;
 if value ? 'generationStyleImages' then
  if jsonb_typeof(value->'generationStyleImages') is distinct from 'array'
     or jsonb_array_length(value->'generationStyleImages')>12 then
   raise exception '生成用风格图无效';
  end if;
  for item in select g.item from jsonb_array_elements(value->'generationStyleImages') as g(item) loop
   if jsonb_typeof(item) is distinct from 'string'
      or length(trim(item#>>'{}')) not between 1 and 500 then
    raise exception '生成用风格图路径无效';
   end if;
  end loop;
 end if;
 if value ? 'templateSample' and jsonb_typeof(value->'templateSample')<>'null'
    and jsonb_typeof(value->'templateSample') is distinct from 'object' then
  raise exception '模板样板无效';
 end if;
end $$;

create or replace function public.ai_planning_save_draft(
 w uuid, plan_id uuid, actor_id uuid, expected_revision bigint, draft jsonb
) returns jsonb
language plpgsql security definer set search_path=public,extensions as $$
declare actor_role text; current_plan public.ai_planning_plans; next_revision bigint;
begin
 if actor_id is null then raise exception '缺少操作用户'; end if;
 select role into actor_role from public.members where workspace_id=w and user_id=actor_id;
 if actor_role is null then raise exception '请先加入团队'; end if;
 if plan_id is null or expected_revision is null or expected_revision<0 then
  raise exception '策划草稿参数无效';
 end if;
 perform public.ai_planning_validate_draft(draft);
 select * into current_plan from public.ai_planning_plans where id=plan_id for update;
 if not found then
  if expected_revision<>0 then raise exception '策划草稿版本已变化，请刷新后重试'; end if;
  insert into public.ai_planning_plans(id,workspace_id,created_by,status,revision,draft)
  values(plan_id,w,actor_id,'draft',1,draft);
  return jsonb_build_object('planId',plan_id,'status','draft','revision',1);
 end if;
 if current_plan.workspace_id<>w then raise exception '策划不属于当前工作区'; end if;
 if current_plan.created_by<>actor_id then raise exception '仅策划创建者可以修改草稿'; end if;
 if current_plan.status<>'draft' then raise exception '已确认的策划不可修改'; end if;
 if current_plan.revision<>expected_revision then raise exception '策划草稿已在其他窗口更新，请刷新后重试'; end if;
 next_revision=current_plan.revision+1;
 update public.ai_planning_plans as p
  set draft=ai_planning_save_draft.draft,revision=next_revision,updated_at=now()
  where p.id=ai_planning_save_draft.plan_id;
 return jsonb_build_object('planId',plan_id,'status','draft','revision',next_revision);
end $$;

create or replace function public.ai_planning_confirm(
 w uuid, plan_id uuid, actor_id uuid, expected_revision bigint, draft jsonb
) returns jsonb
language plpgsql security definer set search_path=public,extensions as $$
declare actor_role text; current_plan public.ai_planning_plans; next_version integer;
 new_version_id uuid; hash text; stamp timestamptz=now(); item jsonb;
begin
 if actor_id is null then raise exception '缺少操作用户'; end if;
 select role into actor_role from public.members where workspace_id=w and user_id=actor_id;
 if actor_role is null then raise exception '请先加入团队'; end if;
 perform public.ai_planning_validate_draft(draft);
 select * into current_plan from public.ai_planning_plans where id=plan_id for update;
 if not found or current_plan.workspace_id<>w then raise exception '策划不存在'; end if;
 if current_plan.created_by<>actor_id and actor_role<>'admin' then
  raise exception '仅策划创建者或管理员可以确认';
 end if;
 if current_plan.status<>'draft' then raise exception '策划已经确认，请创建新的策划版本'; end if;
 if current_plan.revision<>expected_revision then raise exception '策划草稿已更新，请刷新后再确认'; end if;
 select coalesce(max(v.version),0)+1 into next_version
  from public.ai_planning_versions v
  where v.plan_id=ai_planning_confirm.plan_id;
 new_version_id=gen_random_uuid();
 hash=encode(extensions.digest(convert_to(draft::text,'UTF8'),'sha256'),'hex');
 insert into public.ai_planning_versions(
  id,plan_id,workspace_id,version,confirmed_by,payload,content_hash,confirmed_at
 ) values(new_version_id,plan_id,w,next_version,actor_id,draft,hash,stamp);
 for item in select r.item from jsonb_array_elements(coalesce(draft->'references','[]'::jsonb)) as r(item) loop
  if length(coalesce(item->>'deckId','')) between 1 and 200 then
   insert into public.ai_planning_version_references(
    version_id,deck_id,reference_rank,score,deck_snapshot,image_paths
   ) values(
    new_version_id,item->>'deckId',
    greatest(coalesce((item->>'rank')::integer,1),1),
    case when jsonb_typeof(item->'score')='number' then (item->>'score')::numeric else null end,
    item,
    coalesce(item->'imagePaths','[]'::jsonb)
   ) on conflict(version_id,deck_id) do nothing;
  end if;
 end loop;
 insert into public.ai_planning_version_access(version_id,user_id,access_role,granted_by,granted_at)
  values(new_version_id,actor_id,'owner',actor_id,stamp)
  on conflict(version_id,user_id) do update set access_role='owner',granted_by=excluded.granted_by;
 update public.ai_planning_plans as p
  set status='confirmed',draft=ai_planning_confirm.draft,current_version_id=new_version_id,revision=p.revision+1,
      confirmed_at=stamp,updated_at=stamp
  where p.id=ai_planning_confirm.plan_id;
 return jsonb_build_object(
  'planId',plan_id,'versionId',new_version_id,'version',next_version,
  'contentHash',hash,'status','confirmed','confirmedAt',stamp,'immutable',true
 );
end $$;

create or replace function public.ai_planning_get_plan(w uuid, plan_id uuid) returns jsonb
language plpgsql security definer set search_path=public,extensions as $$
declare actor_role text; current_plan public.ai_planning_plans; current_version public.ai_planning_versions; result jsonb;
begin
 if auth.uid() is null then raise exception '请先登录'; end if;
 select role into actor_role from public.members where workspace_id=w and user_id=auth.uid();
 if actor_role is null then raise exception '请先加入团队'; end if;
 select * into current_plan from public.ai_planning_plans where id=plan_id and workspace_id=w;
 if not found then raise exception '策划不存在'; end if;
 if current_plan.status='draft' and current_plan.created_by<>auth.uid() and actor_role<>'admin' then
  raise exception '仅策划创建者或管理员可以读取草稿';
 end if;
 result=jsonb_build_object(
  'planId',current_plan.id,'workspaceId',current_plan.workspace_id,
  'createdBy',current_plan.created_by,'status',current_plan.status,
  'revision',current_plan.revision,'draft',current_plan.draft,
  'createdAt',current_plan.created_at,'updatedAt',current_plan.updated_at
 );
 if current_plan.current_version_id is not null then
  select * into current_version from public.ai_planning_versions where id=current_plan.current_version_id;
  if current_plan.created_by<>auth.uid() and actor_role<>'admin'
     and not exists(select 1 from public.ai_planning_version_access a
                    where a.version_id=current_version.id and a.user_id=auth.uid()) then
   raise exception '尚未获得该策划版本的独立授权';
  end if;
  result=result||jsonb_build_object('confirmed',jsonb_build_object(
   'versionId',current_version.id,'version',current_version.version,
   'contentHash',current_version.content_hash,'confirmedAt',current_version.confirmed_at,
   'payload',current_version.payload
  ));
 end if;
 return result;
end $$;

create or replace function public.ai_planning_grant_version_access(
 w uuid, version_id uuid, target_user_id uuid
) returns void
language plpgsql security definer set search_path=public,extensions as $$
declare actor_role text; version_workspace uuid; allowed boolean;
begin
 if auth.uid() is null then raise exception '请先登录'; end if;
 select role into actor_role from public.members where workspace_id=w and user_id=auth.uid();
 if actor_role is null then raise exception '请先加入团队'; end if;
 select workspace_id into version_workspace from public.ai_planning_versions where id=version_id;
 if version_workspace is null or version_workspace<>w then raise exception '策划版本不存在'; end if;
 select exists(select 1 from public.ai_planning_version_access a
               where a.version_id=ai_planning_grant_version_access.version_id and a.user_id=auth.uid() and a.access_role='owner')
  into allowed;
 if actor_role<>'admin' and not coalesce(allowed,false) then
  raise exception '仅版本所有者或管理员可以授权';
 end if;
 if not exists(select 1 from public.members where workspace_id=w and user_id=target_user_id) then
  raise exception '被授权人必须是同工作区成员';
 end if;
 insert into public.ai_planning_version_access(version_id,user_id,access_role,granted_by)
  values(version_id,target_user_id,'viewer',auth.uid())
  on conflict on constraint ai_planning_version_access_pkey do nothing;
end $$;

create or replace function public.ai_planning_reference_candidates(
 w uuid, max_items integer default 12
) returns jsonb
language sql stable security definer set search_path=public,extensions as $$
 with dimensions as (
  select ws.id as workspace_id,dim.value as dimension
  from public.workspaces ws
  cross join lateral jsonb_array_elements(coalesce(ws.settings->'dimensions','[]'::jsonb)) as dim(value)
  where ws.id=w
 ), score_rows as (
  select r.workspace_id,r.deck_id,r.user_id,
   case when coalesce(r.data->>'mode','deck')='page' then im->>'id' else '__deck__' end as unit_id,
   dm.dimension->>'id' as dimension_id,
   case when coalesce(r.data->>'mode','deck')='page' then
    case when jsonb_typeof(r.data#>array['pages',im->>'id','scores',dm.dimension->>'id'])='number'
     then (r.data#>array['pages',im->>'id','scores',dm.dimension->>'id'])::text::numeric else null end
   else
    case when jsonb_typeof(r.data#>array['scores',dm.dimension->>'id'])='number'
     then (r.data#>array['scores',dm.dimension->>'id'])::text::numeric else null end
   end as score
  from public.reviews r
  join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id
  join dimensions dm on dm.workspace_id=r.workspace_id
  cross join lateral jsonb_array_elements(
   case when coalesce(r.data->>'mode','deck')='page'
    then coalesce(d.data->'images','[]'::jsonb) else '[{}]'::jsonb end
  ) im
 ), review_totals as (
  select deck_id,user_id,count(*)::integer as expected,count(score)::integer as scored,
   avg(score) as mean
  from score_rows
  group by deck_id,user_id
 ), complete_reviews as (
  select deck_id,user_id,mean from review_totals where expected>0 and scored=expected
 ), ranked_base as (
  select d.id as deck_id,d.data,count(*)::integer as complete_review_count,
   round(avg(cr.mean),2) as score,
   (get_byte(decode(md5(d.id),'hex'),0)%5)::integer as bucket
  from complete_reviews cr
  join public.decks d on d.workspace_id=w and d.id=cr.deck_id
  group by d.id,d.data
 ), ranked as (
  select * from ranked_base
  where bucket<>0
  order by score desc nulls last,complete_review_count desc,deck_id
  limit least(greatest(coalesce(max_items,12),1),20)
 )
 select coalesce(jsonb_agg(jsonb_build_object(
  'deckId',deck_id,
  'title',coalesce(nullif(data->>'title',''),nullif(data->>'name',''),deck_id),
  'style',coalesce(data->>'style',''),
  'score',score,
  'completeReviewCount',complete_review_count,
  'pageCount',jsonb_array_length(coalesce(data->'images','[]'::jsonb)),
  'bucket',bucket,
  'split','calibration',
  'imagePaths',coalesce((
   select jsonb_agg(path) from (
    select im->>'path' as path
    from jsonb_array_elements(coalesce(data->'images','[]'::jsonb)) im
    where nullif(im->>'path','') is not null
    limit 2
   ) paths
  ),'[]'::jsonb)
 ) order by score desc nulls last,complete_review_count desc,deck_id),'[]'::jsonb)
 from ranked;
$$;

create or replace function public.ai_planning_read_confirmed_plan(p_plan_id uuid)
returns jsonb
language plpgsql stable security definer set search_path=public,extensions as $$
declare result jsonb;
begin
 select jsonb_build_object(
  'schemaVersion',1,
  'planId',p.id,
  'workspaceId',p.workspace_id,
  'versionId',v.id,
  'version',v.version,
  'status',p.status,
  'contentHash',v.content_hash,
  'confirmedBy',v.confirmed_by,
  'confirmedAt',v.confirmed_at,
  'title',v.payload->>'title',
  'mode',v.payload->>'mode',
  'pageCount',(v.payload->>'pageCount')::integer,
  'style',v.payload->'style',
  'pages',v.payload->'pages',
  'references',v.payload->'references',
  'generationStyleImages',coalesce(v.payload->'generationStyleImages','[]'::jsonb),
  'templateSample',v.payload->'templateSample',
  'generationBrief',v.payload->>'generationBrief',
  'assumptions',coalesce(v.payload->'assumptions','[]'::jsonb),
  'sourceManifest',coalesce(v.payload->'sourceManifest','[]'::jsonb)
 ) into result
 from public.ai_planning_plans p
 join public.ai_planning_versions v on v.id=p.current_version_id
 where p.id=p_plan_id and p.status='confirmed';
 if result is null then raise exception '已确认的策划版本不存在'; end if;
 return result;
end $$;

revoke all on function public.ai_planning_validate_draft(jsonb) from public,anon,authenticated;
revoke all on function public.ai_planning_save_draft(uuid,uuid,uuid,bigint,jsonb) from public,anon,authenticated;
revoke all on function public.ai_planning_confirm(uuid,uuid,uuid,bigint,jsonb) from public,anon,authenticated;
revoke all on function public.ai_planning_get_plan(uuid,uuid) from public,anon;
revoke all on function public.ai_planning_grant_version_access(uuid,uuid,uuid) from public,anon;
revoke all on function public.ai_planning_reference_candidates(uuid,integer) from public,anon,authenticated;
revoke all on function public.ai_planning_read_confirmed_plan(uuid) from public,anon,authenticated;
grant execute on function public.ai_planning_save_draft(uuid,uuid,uuid,bigint,jsonb) to service_role;
grant execute on function public.ai_planning_confirm(uuid,uuid,uuid,bigint,jsonb) to service_role;
grant execute on function public.ai_planning_get_plan(uuid,uuid) to authenticated;
grant execute on function public.ai_planning_grant_version_access(uuid,uuid,uuid) to authenticated;
grant execute on function public.ai_planning_reference_candidates(uuid,integer) to service_role;
grant execute on function public.ai_planning_read_confirmed_plan(uuid) to service_role;

create or replace function public.ai_planning_reject_confirmed_mutation() returns trigger
language plpgsql set search_path='' as $$
begin
 raise exception '已确认的策划版本不可修改或删除';
end $$;
revoke all on function public.ai_planning_reject_confirmed_mutation() from public,anon,authenticated;
drop trigger if exists ai_planning_versions_immutable on public.ai_planning_versions;
create trigger ai_planning_versions_immutable
 before update or delete on public.ai_planning_versions
 for each row execute function public.ai_planning_reject_confirmed_mutation();
drop trigger if exists ai_planning_references_immutable on public.ai_planning_version_references;
create trigger ai_planning_references_immutable
 before update or delete on public.ai_planning_version_references
 for each row execute function public.ai_planning_reject_confirmed_mutation();

commit;
