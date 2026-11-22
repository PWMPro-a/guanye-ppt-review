-- SINGLE_JUDGE_SCORE_REVISIONS_M26
-- User-authorized score revisions preserve every historical submission and decision.
-- Blind pairing remains bound to the first formal submission only.
begin;

-- Only the first formal submission for a professional unit may own blind-review matches.
-- Existing historical matches are never deleted or rewritten.
create or replace function guanye_internal.single_judge_freeze_matches() returns trigger
language plpgsql security definer set search_path='' as $$
declare scope_workspace uuid; current_submission public.ai_review_submissions%rowtype;
begin
 -- SINGLE_JUDGE_SCORE_REVISIONS_M26
 select s.workspace_id into scope_workspace
 from public.ai_review_submissions s where s.id=new.submission_id;

 if not exists(select 1 from public.workspaces where id=scope_workspace and single_judge_admin_id is not null) then
  return new;
 end if;
 -- The bundle-bound GUC is transaction-local; the legacy worker never receives it.
 if nullif(current_setting('guanye.single_judge_bundle',true),'') is distinct from scope_workspace::text then
  return null;
 end if;

 select * into current_submission from public.ai_review_submissions where id=new.submission_id;
 if current_submission.id is null then return null; end if;

 -- A later duplicate/revision must not attach a newly arrived AI prediction to the same submission.
 if exists(
  select 1 from public.ai_review_decisions d
  where d.workspace_id=scope_workspace and d.submission_id=new.submission_id
 ) then
  return null;
 end if;

 -- Even if the first formal submission had no prediction, later formal revisions never become blind pairs.
 if exists(
  select 1
  from public.ai_review_submissions s
  join public.ai_review_decisions d
   on d.workspace_id=s.workspace_id and d.submission_id=s.id
  where s.workspace_id=scope_workspace
   and s.id<>new.submission_id
   and s.deck_id=current_submission.deck_id
   and s.scope=current_submission.scope
   and s.unit_id=current_submission.unit_id
   and s.rubric_version=current_submission.rubric_version
 ) then
  return null;
 end if;

 return new;
end $$;
revoke all on function guanye_internal.single_judge_freeze_matches() from public,anon,authenticated;
drop trigger if exists single_judge_freeze_matches on public.ai_review_matches;
create trigger single_judge_freeze_matches before insert on public.ai_review_matches
 for each row execute function guanye_internal.single_judge_freeze_matches();
create or replace function public.ai_review_submit(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare member public.members%rowtype; deck_row public.decks%rowtype; workspace_settings jsonb; scope_value text; unit_json jsonb; latest_human_version text; latest_entry jsonb; revision_token text;
 entry jsonb; score_record record; unit_key text; unit_position integer; deck_hash text; rubric_hash text; human_hash text;
 existing_review jsonb; submission_uuid uuid; inserted_uuid uuid; submission_ids jsonb='[]'::jsonb;
 matched_baseline integer=0; matched_assisted integer=0; max_value numeric; note_value text; reason_other_value text;
begin
 perform set_config('guanye.ai_review_internal','on',true);
 if actor is null then raise exception '请先登录'; end if;
 select * into member from public.members where workspace_id=w and user_id=actor;
 if member.user_id is null then raise exception '请先加入团队'; end if;
 if jsonb_typeof(payload) is distinct from 'object' or length(coalesce(payload->>'deckId','')) not between 1 and 100 then raise exception '提交内容无效'; end if;
 select * into deck_row from public.decks where workspace_id=w and id=payload->>'deckId';
 if deck_row.id is null then raise exception '作品不存在'; end if;
 select settings into workspace_settings from public.workspaces where id=w;
 max_value=coalesce((workspace_settings->>'maxScore')::numeric,5);
 deck_hash=public.ai_review_deck_version(w,deck_row.id);rubric_hash=public.ai_review_rubric_version(w);
 if nullif(payload->>'deckVersion','') is not null and payload->>'deckVersion'<>deck_hash then raise exception '作品版本已变化，请刷新后重新提交'; end if;
 if nullif(payload->>'rubricVersion','') is not null and payload->>'rubricVersion'<>rubric_hash then raise exception '评分标准已变化，请刷新后重新提交'; end if;
 scope_value=coalesce(payload->>'scope','deck');
 if scope_value not in ('deck','page') then raise exception '评分范围无效'; end if;
 if jsonb_typeof(payload->'units') is distinct from 'array' or jsonb_array_length(payload->'units') not between 1 and 600 then raise exception '请选择至少一个评分单元'; end if;
 for unit_json in select value from jsonb_array_elements(payload->'units') loop
  if jsonb_typeof(unit_json) is distinct from 'object' then raise exception '提交内容无效'; end if;
  unit_key=case when scope_value='page' then coalesce(unit_json->>'unitId','') else '' end;
  unit_position=case when scope_value='page' then coalesce((unit_json->>'position')::integer,0) else 0 end;
  if scope_value='page' and not exists(select 1 from jsonb_array_elements(case when jsonb_typeof(deck_row.data->'images')='array' then deck_row.data->'images' else '[]'::jsonb end) image where image->>'id'=unit_key) then raise exception '评分页面不存在'; end if;
  if jsonb_typeof(unit_json->'scores') is distinct from 'object' then raise exception '分数格式无效'; end if;
  for score_record in select key,value from jsonb_each(unit_json->'scores') loop
   if jsonb_typeof(score_record.value)<>'number' or (score_record.value::text)::numeric<0 or (score_record.value::text)::numeric>max_value then raise exception '分数超出范围'; end if;
  end loop;
  note_value=coalesce(unit_json->>'note','');if length(note_value)>10000 then raise exception '备注过长'; end if;
  if unit_json ? 'favorite' and jsonb_typeof(unit_json->'favorite')<>'boolean' then raise exception '收藏状态无效'; end if;
  if unit_json ? 'classificationSuggestion' and jsonb_typeof(unit_json->'classificationSuggestion') not in ('string','object') then raise exception '分类建议无效'; end if;
  if unit_json ? 'classificationCorrection' and jsonb_typeof(unit_json->'classificationCorrection') not in ('string','object') then raise exception '分类纠偏无效'; end if;
  if unit_json ? 'reasons' then
   if jsonb_typeof(unit_json->'reasons')<>'array' or jsonb_array_length(unit_json->'reasons')>20 then raise exception '原因选择无效'; end if;
   for reason_other_value in select value #>> '{}' from jsonb_array_elements(unit_json->'reasons') loop
    if length(trim(reason_other_value)) not between 1 and 160 then raise exception '原因选择无效'; end if;
   end loop;
  end if;
  reason_other_value=coalesce(unit_json->>'reasonOther','');
  if length(reason_other_value)>500 then raise exception '其他原因过长'; end if;
  if (unit_json->'reasons') ? 'other' and length(trim(reason_other_value))=0 then raise exception '选择“其他”时请填写原因'; end if;  entry=unit_json-'unitId'-'position';
  human_hash=public.ai_review_json_sha(jsonb_build_object('deckVersion',deck_hash,'rubricVersion',rubric_hash,'scope',scope_value,'unitId',unit_key,'entry',entry));
  -- SINGLE_JUDGE_SCORE_REVISIONS_M26
  if nullif(current_setting('guanye.single_judge_revision',true),'') is not null
     and exists(select 1 from public.workspaces ws where ws.id=w and ws.single_judge_admin_id is not null) then
   revision_token=nullif(current_setting('guanye.single_judge_revision',true),'');
   select s.human_version,s.entry into latest_human_version,latest_entry
   from public.ai_review_submissions s
   where s.workspace_id=w and s.deck_id=deck_row.id and s.source_kind=deck_row.source_kind
    and s.deck_version=deck_hash and s.rubric_version=rubric_hash
    and s.scope=scope_value and s.unit_id=unit_key and s.human_user_id=actor
   order by s.created_at desc,s.id desc limit 1;
   if latest_human_version is not null and latest_entry is not distinct from entry then
    human_hash=latest_human_version;
   elsif latest_human_version is not null and revision_token is not null then
    human_hash=human_hash||':r:'||revision_token;
   end if;
  end if;
  if deck_row.source_kind='uploaded' then
   select data into existing_review from public.reviews where workspace_id=w and user_id=actor and deck_id=deck_row.id;
   if existing_review is null then existing_review=jsonb_build_object('scores','{}'::jsonb,'note','','favorite',false,'pages','{}'::jsonb); end if;
   if scope_value='page' then
    existing_review=existing_review||jsonb_build_object('mode','page');
    if jsonb_typeof(existing_review->'pages')<>'object' then existing_review=jsonb_set(existing_review,'{pages}','{}'::jsonb,true); end if;
    existing_review=jsonb_set(existing_review,array['pages',unit_key],coalesce(existing_review->'pages'->unit_key,'{}'::jsonb)||entry,true);
   else
    existing_review=existing_review||entry||jsonb_build_object('mode','deck');
   end if;
   insert into public.reviews(workspace_id,user_id,deck_id,data) values(w,actor,deck_row.id,existing_review)
   on conflict(workspace_id,user_id,deck_id) do update set data=excluded.data;
  end if;
  inserted_uuid=null;
  insert into public.ai_review_submissions(workspace_id,deck_id,source_kind,scope,unit_id,position,human_user_id,
   deck_version,rubric_version,human_version,max_score,entry)
  values(w,deck_row.id,deck_row.source_kind,scope_value,unit_key,unit_position,actor,deck_hash,rubric_hash,human_hash,max_value,entry)
  on conflict(workspace_id,deck_id,scope,unit_id,human_user_id,human_version) do nothing returning id into inserted_uuid;
  if inserted_uuid is null then
   select id into submission_uuid from public.ai_review_submissions where workspace_id=w and deck_id=deck_row.id
    and scope=scope_value and unit_id=unit_key and human_user_id=actor and human_version=human_hash;
  else
   submission_uuid=inserted_uuid;submission_ids=submission_ids||to_jsonb(submission_uuid);
   update public.members set revision=revision+1 where workspace_id=w and user_id=actor;
  end if;
  insert into public.ai_review_matches(submission_id,record_id,variant)
  select submission_uuid,r.id,r.variant from public.ai_review_records r
  where r.workspace_id=w and r.deck_id=deck_row.id and r.scope=scope_value and r.unit_id=unit_key
   and r.deck_version=deck_hash and r.rubric_version=rubric_hash
   and public.ai_review_scores_complete(w,deck_row.id,entry,max_value)
  on conflict(submission_id,record_id) do nothing;
  select count(*) filter(where variant='baseline'),count(*) filter(where variant='assisted') into matched_baseline,matched_assisted
  from public.ai_review_matches where submission_id=submission_uuid;
 end loop;
 perform set_config('guanye.ai_review_internal','off',true);
 return jsonb_build_object('ok',true,'deckId',deck_row.id,'sourceKind',deck_row.source_kind,'scope',scope_value,
  'deckVersion',deck_hash,'rubricVersion',rubric_hash,'submissionIds',submission_ids,
  'predictionAvailable',(matched_baseline+matched_assisted)>0,'matchedBaseline',matched_baseline,'matchedAssisted',matched_assisted,
  'aiVisible',(matched_baseline+matched_assisted)>0,'humanRevision',(select revision from public.members where workspace_id=w and user_id=actor),
  'message',case when (matched_baseline+matched_assisted)>0 then '人工评分已提交，已复用同版本同粒度预评' else '人工评分已提交，等待同版本同粒度预评' end);
end $$;
-- Preserve the existing RPC boundary.
revoke all on function public.ai_review_submit(uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.ai_review_submit(uuid,uuid,jsonb) to service_role;
create or replace function public.single_judge_submit_bundle(w uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid=auth.uid(); ws public.workspaces%rowtype; d public.decks%rowtype; unit jsonb;
 deck_unit jsonb; page_units jsonb='[]'::jsonb; seen text[]='{}'; unit_key text; page_position integer;
 maximum numeric; binding jsonb; deck_result jsonb; page_result jsonb; deck_hash text; rubric_hash text;
 submitted jsonb; submission public.ai_review_submissions%rowtype; dimensions jsonb; sanitized jsonb; decisions jsonb='[]'::jsonb; revision_token text;
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
 revision_token=replace(gen_random_uuid()::text,'-','');
 perform set_config('guanye.single_judge_bundle',w::text,true);
 perform set_config('guanye.single_judge_revision',revision_token,true);
 deck_result=public.ai_review_submit(w,actor,binding||jsonb_build_object('scope','deck','units',jsonb_build_array(deck_unit)));
 if jsonb_array_length(page_units)>0 then
  page_result=public.ai_review_submit(w,actor,binding||jsonb_build_object('scope','page','units',page_units));
 end if;
 perform set_config('guanye.single_judge_revision','',true);
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

create or replace function public.ai_review_comparison(w uuid)
returns jsonb
language sql
stable
security invoker
set search_path=''
as $comparison$
-- COMPARISON_SOURCE_DEDUP_V21
-- SOURCE_GROUP_ROUTING_V20 remains the M20 holdout route for this replacement.
with
context as (
 select
  public.ai_review_rubric_version(w) as rubric_version,
  coalesce((select (ws.settings->>'maxScore')::numeric from public.workspaces ws where ws.id=w),5) as max_score
 where w is not null
),
single_judge_first_submissions as (
 -- SINGLE_JUDGE_SCORE_REVISIONS_M26
 select distinct on (s.workspace_id,s.deck_id,s.scope,s.unit_id,s.rubric_version,s.human_user_id)
  s.id,s.workspace_id,s.deck_id,s.scope,s.unit_id,s.rubric_version,s.human_user_id
 from public.ai_review_submissions s
 join public.ai_review_decisions d
  on d.workspace_id=s.workspace_id and d.submission_id=s.id
  and d.status in ('confirmed','corrected')
 join public.workspaces ws on ws.id=s.workspace_id
 where s.workspace_id=w and ws.single_judge_admin_id is not null
 order by s.workspace_id,s.deck_id,s.scope,s.unit_id,s.rubric_version,s.human_user_id,s.created_at,s.id
),
formal_candidate_records as (
 select
  s.id as submission_id,r.deck_id,r.variant,r.ai_result,s.entry,s.source_kind,s.max_score,
  row_number() over (partition by s.id,r.variant order by r.created_at desc,r.id desc) as candidate_rank
 from public.ai_review_records r
 join public.ai_review_matches m on m.record_id=r.id and m.variant=r.variant
 join public.ai_review_submissions s on s.id=m.submission_id
 join public.decks d on d.id=r.deck_id and d.workspace_id=w and d.source_kind=r.source_kind
 join public.workspaces cw on cw.id=s.workspace_id
 left join single_judge_first_submissions f on f.id=s.id
 where w is not null
  and r.workspace_id=w and s.workspace_id=w
  and (cw.single_judge_admin_id is null or f.id is not null)
  and guanye_internal.ai_review_source_group_bucket(w,r.deck_id,r.source_kind)=0
  and r.bucket=0 and r.split='holdout' and r.variant in ('baseline','assisted')
  and r.deck_id=s.deck_id and r.scope=s.scope and r.unit_id=s.unit_id
  and r.source_kind=s.source_kind and r.deck_version=s.deck_version and r.rubric_version=s.rubric_version
  and abs(r.max_score-s.max_score)<=1e-9
),
formal_selected_records as (
 select * from formal_candidate_records where candidate_rank=1
),
formal_pairs as (
 select b.submission_id,b.deck_id,b.entry,b.source_kind,b.max_score,
  b.ai_result as baseline_result,a.ai_result as assisted_result
 from formal_selected_records b
 join formal_selected_records a on a.submission_id=b.submission_id and a.variant='assisted'
 where b.variant='baseline' and b.max_score>0
),
holdout_record_decks as (
 select r.deck_id
 from public.ai_review_records r
 join public.decks d on d.id=r.deck_id and d.workspace_id=w and d.source_kind='uploaded'
 where w is not null and r.workspace_id=w
  and guanye_internal.ai_review_source_group_bucket(w,r.deck_id,'uploaded')=0
  and r.bucket=0 and r.split='holdout' and r.source_kind='uploaded'
  and r.variant in ('baseline','assisted')
 group by r.deck_id
 having count(distinct r.variant)=2
),
eligible_decks as (
 select d.id as deck_id,d.data,c.rubric_version,c.max_score
 from public.decks d
 join context c on true
 join holdout_record_decks h on h.deck_id=d.id
 where d.workspace_id=w and d.source_kind='uploaded'
  and guanye_internal.ai_review_source_group_bucket(w,d.id,'uploaded')=0
),
candidate_decks as (
 select d.*,public.ai_review_deck_version(w,d.deck_id) as deck_version from eligible_decks d
),
legacy_record_pairs as (
 select d.deck_id,d.deck_version,d.rubric_version,d.max_score,
  b.id as baseline_record_id,b.scope,b.unit_id,b.source_kind,
  b.ai_result as baseline_result,a.id as assisted_record_id,a.ai_result as assisted_result
 from candidate_decks d
 join public.ai_review_records b
  on b.workspace_id=w and b.deck_id=d.deck_id and b.source_kind='uploaded'
  and b.bucket=0 and b.split='holdout' and b.variant='baseline'
  and b.scope in ('deck','page') and b.deck_version=d.deck_version
  and b.rubric_version=d.rubric_version and b.max_score=d.max_score
 join public.ai_review_records a
  on a.workspace_id=w and a.deck_id=d.deck_id and a.source_kind='uploaded'
  and a.bucket=0 and a.split='holdout' and a.variant='assisted'
  and a.scope=b.scope and a.unit_id=b.unit_id and a.deck_version=d.deck_version
  and a.rubric_version=d.rubric_version and a.max_score=d.max_score
),
preview_units as (
 select distinct d.deck_id,image->>'id' as unit_id
 from candidate_decks d
 cross join lateral jsonb_array_elements(
  case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end
 ) image
 cross join lateral (
  select coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')) as preview_path
 ) p
 where nullif(image->>'id','') is not null
  and p.preview_path is not null
  and left(p.preview_path,length(w::text)+1)=w::text||'/'
  and strpos(p.preview_path,'..')=0 and strpos(p.preview_path,chr(92))=0
),
legacy_slots as (
 select p.* from legacy_record_pairs p
 where (p.scope='deck' and p.unit_id='')
  or (p.scope='page' and exists(select 1 from preview_units u where u.deck_id=p.deck_id and u.unit_id=p.unit_id))
),
formal_current_pairs as (
 select distinct s.human_user_id,s.deck_id,s.scope,s.unit_id,s.deck_version,s.rubric_version
 from legacy_slots l
 join public.ai_review_matches mb on mb.record_id=l.baseline_record_id and mb.variant='baseline'
 join public.ai_review_submissions s
  on s.id=mb.submission_id and s.workspace_id=w and s.deck_id=l.deck_id and s.source_kind='uploaded'
  and s.scope=l.scope and s.unit_id=l.unit_id and s.deck_version=l.deck_version
  and s.rubric_version=l.rubric_version and abs(s.max_score-l.max_score)<=1e-9
 join public.ai_review_matches ma on ma.submission_id=s.id and ma.record_id=l.assisted_record_id and ma.variant='assisted'
 join public.workspaces cw on cw.id=s.workspace_id
 left join single_judge_first_submissions f on f.id=s.id
 where (cw.single_judge_admin_id is null or f.id is not null)
  and exists(
  select 1 from jsonb_each(case when jsonb_typeof(s.entry->'scores')='object' then s.entry->'scores' else '{}'::jsonb end) h
  join jsonb_each(case when jsonb_typeof(l.baseline_result->'scores')='object' then l.baseline_result->'scores' else '{}'::jsonb end) b on b.key=h.key
  join jsonb_each(case when jsonb_typeof(l.assisted_result->'scores')='object' then l.assisted_result->'scores' else '{}'::jsonb end) a on a.key=h.key
  where jsonb_typeof(h.value)='number' and jsonb_typeof(b.value)='number' and jsonb_typeof(a.value)='number'
 )
),
legacy_sample_entries as (
 select ('legacy:'||r.user_id::text||':'||l.deck_id||':'||l.scope||':'||l.unit_id)::text as sample_key,
  'uploaded'::text as source_kind,l.deck_id,l.scope,l.unit_id,l.baseline_result,l.assisted_result,l.max_score,
  case when l.scope='page' then r.data->'pages'->l.unit_id else r.data end as entry
 from legacy_slots l
 join public.reviews r on r.workspace_id=w and r.deck_id=l.deck_id
 where not exists(
  select 1 from formal_current_pairs f where f.human_user_id=r.user_id
   and f.deck_id=l.deck_id and f.scope=l.scope and f.unit_id=l.unit_id
   and f.deck_version=l.deck_version and f.rubric_version=l.rubric_version
 )
 and not exists(
  -- In single-judge mode, any first formal unit makes the mutable reviews row
  -- non-independent evidence even when no formal AI pair exists.
  select 1 from single_judge_first_submissions ff
  where ff.human_user_id=r.user_id and ff.deck_id=l.deck_id
   and ff.scope=l.scope and ff.unit_id=l.unit_id and ff.rubric_version=l.rubric_version
 )
),
legacy_samples as (
 select e.sample_key,e.source_kind,e.deck_id,e.entry,e.baseline_result,e.assisted_result,e.max_score
 from legacy_sample_entries e
 where jsonb_typeof(e.entry)='object'
  and public.ai_review_scores_complete(w,e.deck_id,e.entry,e.max_score)
),
holdout_samples as (
 select ('submission:'||submission_id::text)::text as sample_key,deck_id,source_kind,entry,
  baseline_result,assisted_result,max_score from formal_pairs
 union all
 select sample_key,deck_id,source_kind,entry,baseline_result,assisted_result,max_score from legacy_samples
),
human_scores as (
 select s.sample_key,e.key as dimension,(e.value #>> '{}')::numeric as score
 from holdout_samples s
 cross join lateral jsonb_each(case when jsonb_typeof(s.entry->'scores')='object' then s.entry->'scores' else '{}'::jsonb end) e
 where jsonb_typeof(e.value)='number'
),
baseline_scores as (
 select s.sample_key,e.key as dimension,(e.value #>> '{}')::numeric as score
 from holdout_samples s
 cross join lateral jsonb_each(case when jsonb_typeof(s.baseline_result->'scores')='object' then s.baseline_result->'scores' else '{}'::jsonb end) e
 where jsonb_typeof(e.value)='number'
),
assisted_scores as (
 select s.sample_key,e.key as dimension,(e.value #>> '{}')::numeric as score
 from holdout_samples s
 cross join lateral jsonb_each(case when jsonb_typeof(s.assisted_result->'scores')='object' then s.assisted_result->'scores' else '{}'::jsonb end) e
 where jsonb_typeof(e.value)='number'
),
common_scores as (
 select h.sample_key,h.dimension,h.score as human_score,b.score as baseline_score,
  a.score as assisted_score,s.max_score
 from human_scores h
 join baseline_scores b on b.sample_key=h.sample_key and b.dimension=h.dimension
 join assisted_scores a on a.sample_key=h.sample_key and a.dimension=h.dimension
 join holdout_samples s on s.sample_key=h.sample_key
),
dimension_errors as (
 select sample_key,dimension,abs(baseline_score-human_score)/max_score as baseline_error,
  abs(assisted_score-human_score)/max_score as assisted_error
 from common_scores
),
sample_errors as (
 select d.sample_key,s.source_kind,s.deck_id,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d join holdout_samples s on s.sample_key=d.sample_key
 group by d.sample_key,s.source_kind,s.deck_id
),
qualified_uploaded_decks as (
 select coalesce(array_agg(q.deck_id order by q.deck_id collate "C"),array[]::text[]) as deck_ids
 from (select distinct s.deck_id from sample_errors s where s.source_kind='uploaded') q
),
dedup_decks as (
 select h.deck_id,h.source_known,h.retained
 from qualified_uploaded_decks q
 cross join lateral guanye_internal.ai_review_comparison_source_dedup(w,q.deck_ids) h
),
kept_sample_errors as (
 select s.sample_key,s.source_kind,s.deck_id,s.baseline_mae,s.assisted_mae
 from sample_errors s
 where s.source_kind='generated'
  or exists(
   select 1 from dedup_decks d where d.deck_id=s.deck_id and d.retained
  )
),
source_dedup as (
 select jsonb_build_object(
  'knownGroups',count(*) filter(where d.source_known and d.retained)::integer,
  'unknownDecks',count(*) filter(where not d.source_known)::integer,
  'excludedDuplicateDecks',count(*) filter(where d.source_known and not d.retained)::integer
 ) as value
 from dedup_decks d
),
overall_stats as (
 select count(*)::integer as unit_count,avg(baseline_mae) as baseline_mae,avg(assisted_mae) as assisted_mae
 from kept_sample_errors
),
overall_dimension_values as (
 select d.dimension,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d
 join kept_sample_errors s on s.sample_key=d.sample_key
 group by d.dimension
),
overall_dimensions as (
 select coalesce(jsonb_object_agg(dimension,baseline_mae order by dimension) filter(where dimension is not null),'{}'::jsonb) as baseline_dimensions,
  coalesce(jsonb_object_agg(dimension,assisted_mae order by dimension) filter(where dimension is not null),'{}'::jsonb) as assisted_dimensions
 from overall_dimension_values
),
kinds as (
 select source_kind from (values ('uploaded'::text),('generated'::text)) k(source_kind)
),
kind_stats as (
 select k.source_kind,count(s.sample_key)::integer as unit_count,
  avg(s.baseline_mae) as baseline_mae,avg(s.assisted_mae) as assisted_mae
 from kinds k left join kept_sample_errors s on s.source_kind=k.source_kind
 group by k.source_kind
),
kind_dimension_values as (
 select s.source_kind,d.dimension,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d join kept_sample_errors s on s.sample_key=d.sample_key
 group by s.source_kind,d.dimension
),
kind_dimensions as (
 select k.source_kind,
  coalesce(jsonb_object_agg(v.dimension,v.baseline_mae order by v.dimension) filter(where v.dimension is not null),'{}'::jsonb) as baseline_dimensions,
  coalesce(jsonb_object_agg(v.dimension,v.assisted_mae order by v.dimension) filter(where v.dimension is not null),'{}'::jsonb) as assisted_dimensions
 from kinds k left join kind_dimension_values v on v.source_kind=k.source_kind
 group by k.source_kind
),
kind_json as (
 select s.source_kind,jsonb_build_object(
  'holdoutUnits',s.unit_count,
  'baseline',case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.baseline_mae,'dimensions',d.baseline_dimensions) end,
  'assisted',case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.assisted_mae,'dimensions',d.assisted_dimensions) end,
  'improvement',case when s.unit_count=0 then null else s.baseline_mae-s.assisted_mae end
 ) as value
 from kind_stats s join kind_dimensions d on d.source_kind=s.source_kind
),
by_kind as (
 select jsonb_build_object(
  'uploaded',(select value from kind_json where source_kind='uploaded') ||
   jsonb_build_object('sourceDedup',(select value from source_dedup)),
  'generated',(select value from kind_json where source_kind='generated')
 ) as value
),
overall_json as (
 select s.unit_count,
  case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.baseline_mae,'dimensions',d.baseline_dimensions) end as baseline,
  case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.assisted_mae,'dimensions',d.assisted_dimensions) end as assisted,
  case when s.unit_count=0 then null else s.baseline_mae-s.assisted_mae end as improvement
 from overall_stats s cross join overall_dimensions d
)
select jsonb_build_object(
 'holdoutUnits',s.unit_count,'baseline',s.baseline,'assisted',s.assisted,'improvement',s.improvement,'byKind',b.value,
 'splitAlgorithm',case when guanye_internal.ai_review_has_source_groups(w)
  then 'source-sha256-or-md5-first-byte-mod-5-v1' else 'md5-first-byte-mod-5-v1' end,
 'holdoutBucket',0
)
from overall_json s cross join by_kind b
$comparison$;
revoke all on function public.ai_review_comparison(uuid) from public,anon,authenticated;
grant execute on function public.ai_review_comparison(uuid) to service_role;
do $single_judge_score_revisions_self_check$
begin
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('guanye_internal.single_judge_freeze_matches()')
   and position('SINGLE_JUDGE_SCORE_REVISIONS_M26' in p.prosrc)>0
 ) then raise exception 'M26 match-freeze trigger marker missing'; end if;
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('public.ai_review_submit(uuid,uuid,jsonb)')
   and position('SINGLE_JUDGE_SCORE_REVISIONS_M26' in p.prosrc)>0
 ) then raise exception 'M26 revision hash marker missing'; end if;
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('public.single_judge_submit_bundle(uuid,jsonb)')
   and position('guanye.single_judge_revision' in p.prosrc)>0
 ) then raise exception 'M26 bundle revision marker missing'; end if;
 if not exists(
  select 1 from pg_trigger t
  where t.tgname='single_judge_freeze_matches'
   and t.tgrelid='public.ai_review_matches'::regclass and not t.tgisinternal
 ) then raise exception 'M26 match-freeze trigger missing'; end if;
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('public.ai_review_comparison(uuid)')
   and position('SINGLE_JUDGE_SCORE_REVISIONS_M26' in p.prosrc)>0
   and not p.prosecdef
 ) then raise exception 'M26 comparison canonical-pair filter missing'; end if;
end
$single_judge_score_revisions_self_check$;

commit;
