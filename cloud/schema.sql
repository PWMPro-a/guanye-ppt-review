-- Run once in a fresh Supabase project's SQL editor.
create extension if not exists pgcrypto with schema extensions;
create table if not exists public.workspaces (
 id uuid primary key, name text not null, owner_email text not null,
 invite_hash text not null,
 settings jsonb not null default '{"revision":0,"maxScore":5,"dimensions":[{"id":"visual","name":"视觉效果","description":"色彩、风格、图像与整体表现力"},{"id":"layout","name":"排版","description":"层级、对齐、留白与阅读秩序"}]}',
 check (jsonb_typeof(settings)='object')
);
create table if not exists public.members (
 workspace_id uuid references public.workspaces on delete cascade,
 user_id uuid references auth.users on delete cascade,
 name text not null check(length(name) between 1 and 40),
 role text not null check(role in ('admin','reviewer')),
 revision bigint not null default 0,
 primary key(workspace_id,user_id)
);
create table if not exists public.decks (
 id text primary key, workspace_id uuid not null references public.workspaces,
 data jsonb not null, created_at timestamptz not null default now()
);
create table if not exists public.reviews (
 workspace_id uuid not null references public.workspaces,
 user_id uuid not null references auth.users,
 deck_id text not null references public.decks,
 data jsonb not null,
 primary key(workspace_id,user_id,deck_id)
);
alter table public.workspaces enable row level security;
alter table public.members enable row level security;
alter table public.decks enable row level security;
alter table public.reviews enable row level security;
revoke all on public.workspaces,public.members,public.decks,public.reviews from anon,authenticated;
grant select on public.members,public.decks,public.reviews to authenticated;
grant all on public.workspaces,public.members,public.decks,public.reviews to service_role;
create or replace function public.is_member(w uuid) returns boolean language sql stable security definer set search_path=public as $$
 select exists(select 1 from members where workspace_id=w and user_id=auth.uid())
$$;
create policy members_read on public.members for select to authenticated using(public.is_member(workspace_id));
create policy decks_read on public.decks for select to authenticated using(public.is_member(workspace_id));
create policy reviews_read on public.reviews for select to authenticated using(public.is_member(workspace_id));
-- All writes pass through checked RPCs; no direct client write policies.
create or replace function public.join_workspace(w uuid, display_name text, invitation text) returns void
language plpgsql security definer set search_path=public,extensions as $$
declare workspace workspaces;
begin
 if auth.uid() is null then raise exception '请先登录'; end if;
 select * into workspace from workspaces where id=w;
 if workspace.id is null or invitation is null or workspace.invite_hash is distinct from encode(extensions.digest(invitation,'sha256'),'hex') then raise exception '邀请码不正确'; end if;
 if display_name is null or length(trim(display_name)) not between 1 and 40 then raise exception '姓名需要 1–40 个字符'; end if;
 insert into members(workspace_id,user_id,name,role) values(w,auth.uid(),trim(display_name),case when lower(auth.jwt()->>'email')=lower(workspace.owner_email) then 'admin' else 'reviewer' end)
 on conflict(workspace_id,user_id) do update set name=excluded.name;
end $$;
create or replace function public.get_review_state(w uuid) returns jsonb
language plpgsql security definer set search_path=public as $$
declare result jsonb;
begin
 if not is_member(w) then raise exception '请先使用邀请码加入团队'; end if;
 select settings into result from workspaces where id=w;
 return result || jsonb_build_object('revision',(select revision from members where workspace_id=w and user_id=auth.uid()),'configRevision',(result->>'revision')::bigint,
 'reviews',coalesce((select jsonb_object_agg(deck_id,data) from reviews where workspace_id=w and user_id=auth.uid()),'{}'::jsonb));
end $$;
create or replace function public.rescale_review_data(value jsonb, old_max numeric, new_max numeric) returns jsonb
language plpgsql immutable set search_path=public as $$
declare result jsonb=value; page record;
begin
 result=jsonb_set(result,'{scores}',coalesce((select jsonb_object_agg(key,round((v::text)::numeric/old_max*new_max,2)) from jsonb_each(value->'scores') as x(key,v)),'{}'));
 if value ? 'pages' then
  for page in select key,v from jsonb_each(value->'pages') as x(key,v) loop
   result=jsonb_set(result,array['pages',page.key,'scores'],coalesce((select jsonb_object_agg(key,round((v::text)::numeric/old_max*new_max,2)) from jsonb_each(page.v->'scores') as x(key,v)),'{}'));
  end loop;
 end if;
 return result;
end $$;
create or replace function public.save_review_state(w uuid, incoming jsonb) returns jsonb
language plpgsql security definer set search_path=public as $$
declare member members; current_settings jsonb; maximum numeric; item record; score record; page record; dim jsonb; ids text[]='{}'; names text[]='{}'; changed boolean;
begin
 -- Lock workspace first for a consistent order across settings and reviewer writes.
 select ws.settings into current_settings from workspaces ws where id=w for update;
 select * into member from members where workspace_id=w and user_id=auth.uid() for update;
 if member.user_id is null then raise exception '请先加入团队'; end if;
 if (incoming->>'revision')::bigint is distinct from member.revision then raise exception '另一窗口更新了你的评分，请导出备份后刷新'; end if;
 if (incoming->>'configRevision')::bigint is distinct from (current_settings->>'revision')::bigint then raise exception '评分标准已更新，请先导出备份再刷新页面'; end if;
 maximum=(incoming->>'maxScore')::numeric;
 if jsonb_typeof(incoming->'maxScore') is distinct from 'number' then raise exception '满分应为 1–100'; end if;
 if maximum is null or maximum<1 or maximum>100 then raise exception '满分应为 1–100'; end if;
 if jsonb_typeof(incoming->'dimensions') is distinct from 'array' then raise exception '需要 1–12 个评分项'; end if;
 if jsonb_array_length(incoming->'dimensions') not between 1 and 12 then raise exception '需要 1–12 个评分项'; end if;
 for dim in select value from jsonb_array_elements(incoming->'dimensions') loop
  if length(coalesce(dim->>'id','')) not between 1 and 80 or length(trim(coalesce(dim->>'name',''))) not between 1 and 30 or dim->>'id'=any(ids) or dim->>'name'=any(names) then raise exception '评分项名称与标识必须有效且唯一'; end if;
  ids=array_append(ids,dim->>'id'); names=array_append(names,dim->>'name');
 end loop;
 changed=((incoming->'dimensions')<>(current_settings->'dimensions') or (incoming->'maxScore')<>(current_settings->'maxScore'));
 if jsonb_typeof(incoming->'reviews') is distinct from 'object' then raise exception '评分记录无效'; end if;
 for item in select key,value from jsonb_each(incoming->'reviews') loop
  if not exists(select 1 from decks where id=item.key and workspace_id=w) then raise exception '作品不存在'; end if;
  if length(coalesce(item.value->>'note',''))>10000 or jsonb_typeof(item.value->'scores') is distinct from 'object' then raise exception '评分记录无效'; end if;
  if (item.value ? 'note' and jsonb_typeof(item.value->'note')<>'string') or (item.value ? 'favorite' and jsonb_typeof(item.value->'favorite')<>'boolean') then raise exception '评分记录无效'; end if;
  if item.value ? 'mode' and coalesce(item.value->>'mode','') not in ('deck','page') then raise exception '评分模式无效'; end if;
  if item.value ? 'pages' then
   if jsonb_typeof(item.value->'pages') is distinct from 'object' then raise exception '逐页评分记录无效'; end if;
   if (select count(*) from jsonb_each(item.value->'pages'))>600 then raise exception '逐页评分记录无效'; end if;
   for page in select key,value from jsonb_each(item.value->'pages') loop
    if not exists(select 1 from decks d,jsonb_array_elements(d.data->'images') im where d.id=item.key and d.workspace_id=w and im->>'id'=page.key) then raise exception '评分页面不存在'; end if;
    if jsonb_typeof(page.value->'scores') is distinct from 'object' or length(coalesce(page.value->>'note',''))>10000 or (page.value ? 'note' and jsonb_typeof(page.value->'note')<>'string') then raise exception '逐页评分记录无效'; end if;
    for score in select key,value from jsonb_each(page.value->'scores') loop
     if jsonb_typeof(score.value)<>'number' then raise exception '分数超出范围'; end if;
     if (score.value::text)::numeric<0 or (score.value::text)::numeric>maximum then raise exception '分数超出范围'; end if;
    end loop;
   end loop;
  end if;
  for score in select key,value from jsonb_each(item.value->'scores') loop
   if jsonb_typeof(score.value)<>'number' then raise exception '分数超出范围'; end if;
   if (score.value::text)::numeric<0 or (score.value::text)::numeric>maximum then raise exception '分数超出范围'; end if;
  end loop;
 end loop;
 if changed then
  if (incoming->'maxScore')<>(current_settings->'maxScore') then
   update reviews r set data=public.rescale_review_data(r.data,(current_settings->>'maxScore')::numeric,maximum) where r.workspace_id=w and r.user_id<>auth.uid();
  end if;
  current_settings=jsonb_build_object('revision',(current_settings->>'revision')::bigint+1,'maxScore',maximum,'dimensions',incoming->'dimensions');
  update workspaces ws set settings=current_settings where id=w;
 end if;
 delete from reviews where workspace_id=w and user_id=auth.uid();
 insert into reviews(workspace_id,user_id,deck_id,data) select w,auth.uid(),key,value from jsonb_each(incoming->'reviews');
 update members set revision=revision+1 where workspace_id=w and user_id=auth.uid();
 return jsonb_build_object('revision',member.revision+1,'configRevision',(current_settings->>'revision')::bigint);
end $$;
create or replace function public.add_review_deck(w uuid, deck jsonb) returns void
language plpgsql security definer set search_path=public as $$
declare previous public.decks;
begin
 perform 1 from workspaces where id=w for update;
 if not is_member(w) then raise exception '请先加入团队'; end if;
 if length(coalesce(deck->>'id','')) not between 1 and 100 or jsonb_typeof(deck->'images') is distinct from 'array' then raise exception '作品图片无效'; end if;
 if jsonb_array_length(deck->'images') not between 1 and 600 then raise exception '作品图片无效'; end if;
 select * into previous from decks where id=deck->>'id' for update;
 if previous.id is not null and (previous.workspace_id<>w or not ((deck->'images') @> (previous.data->'images'))) then raise exception '作品已被更新，请刷新作品库后重新追加'; end if;
 insert into decks(id,workspace_id,data) values(deck->>'id',w,deck) on conflict(id) do update set data=excluded.data where decks.workspace_id=w;
end $$;
revoke all on function public.join_workspace(uuid,text,text), public.get_review_state(uuid),public.save_review_state(uuid,jsonb),public.add_review_deck(uuid,jsonb),public.is_member(uuid) from public,anon;
grant execute on function public.join_workspace(uuid,text,text), public.get_review_state(uuid),public.save_review_state(uuid,jsonb),public.add_review_deck(uuid,jsonb),public.is_member(uuid) to authenticated;
insert into storage.buckets(id,name,public,file_size_limit) values('ppt-library','ppt-library',false,104857600) on conflict(id) do nothing;
create policy team_images_read on storage.objects for select to authenticated using(bucket_id='ppt-library' and public.is_member((storage.foldername(name))[1]::uuid));
create policy team_images_insert on storage.objects for insert to authenticated with check(bucket_id='ppt-library' and public.is_member((storage.foldername(storage.objects.name))[1]::uuid));
create or replace function public.delete_review_deck(w uuid, target_id text, expected_revision bigint) returns bigint
language plpgsql security definer set search_path='' as $$
declare current_revision bigint;
begin
 if not exists (select 1 from public.members where workspace_id=w and user_id=auth.uid() and role='admin') then raise exception '仅管理员可以删除作品'; end if;
 perform 1 from public.workspaces where id=w for update;
 select revision into current_revision from public.members where workspace_id=w and user_id=auth.uid() and role='admin' for update;
 if not found then raise exception '仅管理员可以删除作品'; end if;
 if current_revision is distinct from expected_revision then raise exception '评审记录已更新，请刷新后重试'; end if;
 perform 1 from public.decks where workspace_id=w and id=target_id for update;
 if not found then raise exception '作品不存在或已被删除'; end if;
 update public.members m set revision=m.revision+1 where m.workspace_id=w and exists (select 1 from public.reviews r where r.workspace_id=w and r.deck_id=target_id and r.user_id=m.user_id);
 delete from public.reviews where workspace_id=w and deck_id=target_id;
 delete from public.decks where workspace_id=w and id=target_id;
 return (select revision from public.members where workspace_id=w and user_id=auth.uid());
end $$;
revoke all on function public.delete_review_deck(uuid,text,bigint) from public,anon;
grant execute on function public.delete_review_deck(uuid,text,bigint) to authenticated;
-- Average complete reviews per person first, so a page-by-page review has one vote.
create or replace function public.review_score_summary(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 with dims as (
   select dim->>'id' as id from public.workspaces ws cross join lateral jsonb_array_elements(ws.settings->'dimensions') dim
   where ws.id=w and public.is_member(w)
 ), personal as (
   select r.deck_id,r.user_id,avg(case when jsonb_typeof(v.score)='number' then (v.score::text)::numeric end) as mean
   from public.reviews r join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id
   cross join dims
   cross join lateral jsonb_array_elements(case when r.data->>'mode'='page' then d.data->'images' else '[{}]'::jsonb end) im
   cross join lateral (select case when r.data->>'mode'='page' then r.data#>array['pages',im->>'id','scores',dims.id] else r.data#>array['scores',dims.id] end as score) v
   where r.workspace_id=w
   group by r.deck_id,r.user_id having count(*)=count(*) filter(where jsonb_typeof(v.score)='number')
 ), totals as (select deck_id,count(*) as n,avg(mean) as mean from personal group by deck_id)
 select coalesce(jsonb_object_agg(deck_id,jsonb_build_object('count',n,'mean',mean)),'{}'::jsonb) from totals
$$;
revoke all on function public.review_score_summary(uuid) from public,anon;
grant execute on function public.review_score_summary(uuid) to authenticated;
