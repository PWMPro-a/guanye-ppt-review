-- Install only. A verified operator must opt in each workspace after backup and rehearsal.
begin;

alter table public.workspaces add column single_judge_admin_id uuid references auth.users(id);
alter table public.workspaces add constraint workspaces_single_judge_distinct_accounts
 check (single_judge_admin_id is null or (dedicated_judge_id is not null and single_judge_admin_id <> dedicated_judge_id));

create function public.single_judge_activate(w uuid, judge_id uuid, admin_id uuid) returns void
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype;
begin
 select * into ws from public.workspaces where id=w for update;
 if not found or judge_id is null or admin_id is null or judge_id=admin_id
    or not exists(select 1 from public.members where workspace_id=w and user_id=judge_id and role='reviewer')
    or not exists(select 1 from public.members where workspace_id=w and user_id=admin_id and role='admin') then
  raise exception '必须核对两个不同的现有评分师和管理员账号' using errcode='22023';
 end if;
 if ws.single_judge_admin_id is not null and
    (ws.dedicated_judge_id is distinct from judge_id or ws.single_judge_admin_id is distinct from admin_id) then
  raise exception '已启用单评委模式，不能通过此入口重绑账号' using errcode='42501';
 end if;
 update public.workspaces set dedicated_judge_id=judge_id,single_judge_admin_id=admin_id where id=w;
end $$;
revoke all on function public.single_judge_activate(uuid,uuid,uuid) from public,anon,authenticated;
grant execute on function public.single_judge_activate(uuid,uuid,uuid) to service_role;

create or replace function public.is_member(w uuid) returns boolean
language sql stable security definer set search_path='' as $$
 select exists(select 1 from public.members m join public.workspaces ws on ws.id=m.workspace_id
  where m.workspace_id=w and m.user_id=auth.uid()
    and (ws.single_judge_admin_id is null or m.user_id in (ws.single_judge_admin_id,ws.dedicated_judge_id)))
$$;

create function public.single_judge_access(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select coalesce((select jsonb_build_object('mode','single-judge','role',
    case when dedicated_judge_id=auth.uid() then 'judge' else 'admin' end)
  from public.workspaces where id=w and single_judge_admin_id is not null and public.is_member(w)), 'null'::jsonb)
$$;
revoke all on function public.single_judge_access(uuid) from public,anon,authenticated;
grant execute on function public.single_judge_access(uuid) to authenticated;

-- Preserve legacy workspaces without accepting legacy writes in the configured workspace.
-- SQL RPC ownership remains unchanged; hidden implementations are not callable by browsers.
alter function public.join_workspace(uuid,text,text) set schema guanye_internal;
alter function public.set_dedicated_judge(uuid,uuid) set schema guanye_internal;
alter function public.save_review_state(uuid,jsonb) set schema guanye_internal;
alter function public.add_review_deck(uuid,jsonb) set schema guanye_internal;
alter function public.delete_review_deck(uuid,text,bigint) set schema guanye_internal;
revoke all on function guanye_internal.join_workspace(uuid,text,text),
 guanye_internal.set_dedicated_judge(uuid,uuid), guanye_internal.save_review_state(uuid,jsonb),
 guanye_internal.add_review_deck(uuid,jsonb), guanye_internal.delete_review_deck(uuid,text,bigint)
 from public,anon,authenticated;

create function guanye_internal.single_judge_require_legacy(w uuid) returns void
language plpgsql security definer set search_path='' as $$
declare mode_enabled boolean;
begin
 select single_judge_admin_id is not null into mode_enabled from public.workspaces where id=w for update;
 if mode_enabled then raise exception '单评委专业版已关闭旧版写入口' using errcode='42501'; end if;
end $$;
revoke all on function guanye_internal.single_judge_require_legacy(uuid) from public,anon,authenticated;

create function public.join_workspace(w uuid, display_name text, invitation text) returns void
language plpgsql security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_require_legacy(w);
 perform guanye_internal.join_workspace(w,display_name,invitation);
end $$;
create function public.set_dedicated_judge(w uuid, judge_user_id uuid) returns uuid
language plpgsql security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_require_legacy(w);
 return guanye_internal.set_dedicated_judge(w,judge_user_id);
end $$;
create function public.save_review_state(w uuid, incoming jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_require_legacy(w);
 return guanye_internal.save_review_state(w,incoming);
end $$;
create function public.add_review_deck(w uuid, deck jsonb) returns void
language plpgsql security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_require_legacy(w);
 perform guanye_internal.add_review_deck(w,deck);
end $$;
create function public.delete_review_deck(w uuid, target_id text, expected_revision bigint) returns bigint
language plpgsql security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_require_legacy(w);
 return guanye_internal.delete_review_deck(w,target_id,expected_revision);
end $$;
revoke all on function public.join_workspace(uuid,text,text), public.set_dedicated_judge(uuid,uuid),
 public.save_review_state(uuid,jsonb),public.add_review_deck(uuid,jsonb),public.delete_review_deck(uuid,text,bigint)
 from public,anon,authenticated;
grant execute on function public.join_workspace(uuid,text,text),public.set_dedicated_judge(uuid,uuid),
 public.save_review_state(uuid,jsonb),public.add_review_deck(uuid,jsonb),public.delete_review_deck(uuid,text,bigint)
 to authenticated;
grant execute on function public.set_dedicated_judge(uuid,uuid) to service_role;

-- Stop the old browser storage insertion path without changing prior reads or other workspaces.
create function public.review_image_upload_allowed(w uuid) returns boolean
language sql stable security definer set search_path='' as $$
 select public.is_member(w) and exists(select 1 from public.workspaces
  where id=w and single_judge_admin_id is null)
$$;
revoke all on function public.review_image_upload_allowed(uuid) from public,anon,authenticated;
grant execute on function public.review_image_upload_allowed(uuid) to authenticated;
alter policy team_images_insert on storage.objects with check (
 bucket_id='ppt-library' and public.review_image_upload_allowed(
 case when (storage.foldername(name))[1] ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      then (storage.foldername(name))[1]::uuid end));

create function guanye_internal.single_judge_guard_submission() returns trigger
language plpgsql security definer set search_path='' as $$
declare ws public.workspaces%rowtype;
begin
 select * into ws from public.workspaces where id=new.workspace_id for share;
 if ws.single_judge_admin_id is not null and
    (new.human_user_id is distinct from ws.dedicated_judge_id
     or nullif(current_setting('guanye.single_judge_bundle',true),'') is distinct from new.workspace_id::text) then
  raise exception '仅指定评分师可通过专业版提交评分' using errcode='42501';
 end if;
 return new;
end $$;
revoke all on function guanye_internal.single_judge_guard_submission() from public,anon,authenticated;
create trigger single_judge_guard_submission before insert on public.ai_review_submissions
 for each row execute function guanye_internal.single_judge_guard_submission();

-- The legacy worker may finish late; only the transaction that created the human submission
-- may attach an already-visible prediction in professional mode. Preserve all historical links.
create function guanye_internal.single_judge_freeze_matches() returns trigger
language plpgsql security definer set search_path='' as $$
declare scope_workspace uuid; same_transaction boolean;
begin
 select s.workspace_id, s.xmin=pg_current_xact_id()::xid
 into scope_workspace,same_transaction from public.ai_review_submissions s where s.id=new.submission_id;
 if exists(select 1 from public.workspaces where id=scope_workspace and single_judge_admin_id is not null) and
    (not same_transaction or nullif(current_setting('guanye.single_judge_bundle',true),'') is distinct from scope_workspace::text) then
  return null;
 end if;
 return new;
end $$;
revoke all on function guanye_internal.single_judge_freeze_matches() from public,anon,authenticated;
create trigger single_judge_freeze_matches before insert on public.ai_review_matches
 for each row execute function guanye_internal.single_judge_freeze_matches();

create function public.single_judge_submit_bundle(w uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid=auth.uid(); ws public.workspaces%rowtype; d public.decks%rowtype; unit jsonb;
 deck_unit jsonb; page_units jsonb='[]'::jsonb; seen text[]='{}'; unit_key text; page_position integer;
 maximum numeric; binding jsonb; deck_result jsonb; page_result jsonb; deck_hash text; rubric_hash text;
 submitted jsonb; submission public.ai_review_submissions%rowtype; dimensions jsonb; sanitized jsonb; decisions jsonb='[]'::jsonb;
begin
 select * into ws from public.workspaces where id=w for share;
 if actor is null or ws.single_judge_admin_id is null or actor is distinct from ws.dedicated_judge_id or not public.is_member(w) then
  raise exception '仅指定评分师可提交评分' using errcode='42501';
 end if;
 if jsonb_typeof(payload) is distinct from 'object' or jsonb_typeof(payload->'units') is distinct from 'array' then
  raise exception '评分提交格式无效' using errcode='22023';
 end if;
 if jsonb_array_length(payload->'units') not between 1 and 601 then raise exception '评分单元数量无效' using errcode='22023'; end if;
 select * into d from public.decks where workspace_id=w and id=payload->>'deckId' for update;
 if not found then raise exception '作品不存在' using errcode='22023'; end if;
 deck_hash=public.ai_review_deck_version(w,d.id); rubric_hash=public.ai_review_rubric_version(w);
 if payload->>'deckVersion' is distinct from deck_hash or payload->>'rubricVersion' is distinct from rubric_hash then
  raise exception '作品或评分标准版本已变化，请刷新后重新提交' using errcode='22023';
 end if;
 maximum=coalesce((ws.settings->>'maxScore')::numeric,5);
 dimensions=case when d.source_kind='generated' then '[{"id":"visual"},{"id":"layout"},{"id":"match"}]'::jsonb else ws.settings->'dimensions' end;
 for unit in select value from jsonb_array_elements(payload->'units') loop
  if not public.ai_review_scores_complete(w,d.id,unit,maximum) then raise exception '请填完所评分单元的全部评分项' using errcode='22023'; end if;
  if exists(select 1 from jsonb_object_keys(unit->'scores') k
      where not exists(select 1 from jsonb_array_elements(dimensions) dim where dim->>'id'=k)) then
   raise exception '评分项不属于当前评分标准' using errcode='22023';
  end if;
  select jsonb_object_agg(key,value) into sanitized from jsonb_each(unit)
   where key in ('scores','note','favorite','classificationSuggestion','classificationCorrection','reasons','reasonOther');
  if unit->>'scope'='deck' and deck_unit is null and coalesce(unit->>'unitId','')='' then
   deck_unit=sanitized;
  elsif unit->>'scope'='page' then
   unit_key=nullif(unit->>'unitId','');
   select case when (image.value->>'position') ~ '^[1-9][0-9]{0,5}$' then (image.value->>'position')::integer else image.ordinality::integer end
   into page_position from jsonb_array_elements(d.data->'images') with ordinality as image(value,ordinality) where image.value->>'id'=unit_key;
   if not found or unit_key=any(seen) then raise exception '评分页面不存在或重复' using errcode='22023'; end if;
   seen=array_append(seen,unit_key);
   page_units=page_units || jsonb_build_array(sanitized || jsonb_build_object('unitId',unit_key,'position',page_position));
  else raise exception '每次必须提交一份整套评分' using errcode='22023';
  end if;
 end loop;
 if deck_unit is null then raise exception '请提交整套评分' using errcode='22023'; end if;
 binding=jsonb_build_object('deckId',d.id,'deckVersion',deck_hash,'rubricVersion',rubric_hash);
 perform set_config('guanye.single_judge_bundle',w::text,true);
 deck_result=public.ai_review_submit(w,actor,binding||jsonb_build_object('scope','deck','units',jsonb_build_array(deck_unit)));
 if jsonb_array_length(page_units)>0 then
  page_result=public.ai_review_submit(w,actor,binding||jsonb_build_object('scope','page','units',page_units));
 end if;
 perform set_config('guanye.single_judge_bundle','',true);
 submitted=(deck_result->'submissionIds')||coalesce(page_result->'submissionIds','[]'::jsonb);
 for submission in select s.* from public.ai_review_submissions s
   where s.workspace_id=w and s.id in (select value::uuid from jsonb_array_elements_text(submitted))
   order by s.position,s.id loop
  decisions=decisions||jsonb_build_array(public.ai_review_confirm(w,actor,jsonb_build_object(
   'submissionId',submission.id, 'formalScores',submission.entry->'scores',
   'classification',coalesce(nullif(submission.entry->'classificationCorrection','""'::jsonb),
      nullif(submission.entry->'classificationSuggestion','""'::jsonb)),
   'reasons',coalesce(submission.entry->'reasons','[]'::jsonb),'reasonOther',submission.entry->>'reasonOther')));
 end loop;
 return binding||jsonb_build_object('ok',true,'submissionIds',submitted,
  'deck',deck_result,'pages',page_result,'decisions',decisions,'message','人工评分已保存');
end $$;
revoke all on function public.single_judge_submit_bundle(uuid,jsonb) from public,anon,authenticated;
grant execute on function public.single_judge_submit_bundle(uuid,jsonb) to authenticated;

commit;
