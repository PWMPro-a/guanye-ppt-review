-- Allow all joined team members to manage shared standards and import works.
begin;
create or replace function public.save_review_state(w uuid, incoming jsonb) returns jsonb
language plpgsql security definer set search_path=public as $$
declare member members; current_settings jsonb; maximum numeric; item record; score record; dim jsonb; ids text[]='{}'; names text[]='{}'; changed boolean;
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
  for score in select key,value from jsonb_each(item.value->'scores') loop
   if jsonb_typeof(score.value)<>'number' then raise exception '分数超出范围'; end if;
   if (score.value::text)::numeric<0 or (score.value::text)::numeric>maximum then raise exception '分数超出范围'; end if;
  end loop;
 end loop;
 if changed then
  if (incoming->'maxScore')<>(current_settings->'maxScore') then
   update reviews r set data=jsonb_set(r.data,'{scores}',coalesce((select jsonb_object_agg(key,round((value::text)::numeric/(current_settings->>'maxScore')::numeric*maximum,2)) from jsonb_each(r.data->'scores')),'{}')) where r.workspace_id=w and r.user_id<>auth.uid();
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
drop policy if exists admin_images_insert on storage.objects;
drop policy if exists team_images_insert on storage.objects;
create policy team_images_insert on storage.objects for insert to authenticated with check(bucket_id='ppt-library' and public.is_member((storage.foldername(storage.objects.name))[1]::uuid));
commit;
