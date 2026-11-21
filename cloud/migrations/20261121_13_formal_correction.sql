-- Formal correction: score differences against the matched AI record drive decision status.
begin;

create or replace function public.ai_review_confirm(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare submission_row public.ai_review_submissions%rowtype; record_row public.ai_review_records%rowtype; deck_row public.decks%rowtype;
 decision_row public.ai_review_decisions%rowtype; decision_key_value text; classification_value jsonb; reasons_value jsonb;
 classification_label text; classification_style text; ai_classification_label text; ai_classification_style text;
 formal_scores_value jsonb; formal_total_value numeric; score_record record; settings_row public.ai_review_settings%rowtype;
 source_value jsonb; reference_value jsonb; threshold_met_value boolean=false; status_value text='confirmed'; weighted_value numeric;
 score_ratio_value numeric=0; ai_score numeric;
begin
 -- FORMAL_CORRECTION_V13
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以确认正式评分'; end if;
 if jsonb_typeof(payload) is distinct from 'object' then raise exception '确认内容无效'; end if;
 select * into submission_row from public.ai_review_submissions where workspace_id=w and id=nullif(payload->>'submissionId','')::uuid;
 if submission_row.id is null then raise exception '人工提交不存在'; end if;
 if submission_row.deck_version<>public.ai_review_deck_version(w,submission_row.deck_id) then raise exception '作品版本已变化，请重新提交人工评分'; end if;
 if submission_row.rubric_version<>public.ai_review_rubric_version(w) then raise exception '评分标准已变化，请重新提交人工评分'; end if;
 if nullif(payload->>'aiRecordId','') is not null then
  select r.* into record_row from public.ai_review_records r join public.ai_review_matches m on m.record_id=r.id
   where m.submission_id=submission_row.id and r.id=(payload->>'aiRecordId')::uuid;
 else
  select r.* into record_row from public.ai_review_records r join public.ai_review_matches m on m.record_id=r.id
   where m.submission_id=submission_row.id order by case when m.variant='assisted' then 0 else 1 end,r.created_at desc limit 1;
 end if;
 select * into deck_row from public.decks where workspace_id=w and id=submission_row.deck_id;
 select * into settings_row from public.ai_review_settings where workspace_id=w;
 decision_key_value=nullif(payload->>'idempotencyKey','');
 if decision_key_value is null then
  decision_key_value='v1:'||public.ai_review_json_sha(jsonb_build_object('submissionId',submission_row.id,'recordId',record_row.id,
   'humanVersion',submission_row.human_version,'formalScores',payload->'formalScores','classification',payload->'classification'));
 end if;
 if length(decision_key_value)>128 then raise exception '幂等键过长'; end if;
 select * into decision_row from public.ai_review_decisions where workspace_id=w and decision_key=decision_key_value;
 if decision_row.id is not null then
  return jsonb_build_object('ok',true,'idempotent',true,'decisionId',decision_row.id,'status',decision_row.status,
   'formalTotal',decision_row.formal_total,'scoreRatio',decision_row.score_ratio,'expiresAt',decision_row.expires_at,
   'admitted',false,'aiVisible',true);
 end if;
 classification_value=case when payload ? 'classification' and jsonb_typeof(payload->'classification')<>'null' then payload->'classification' else null end;
 if classification_value is not null and jsonb_typeof(classification_value) not in ('string','object') then raise exception '正式分类无效'; end if;
 classification_label=case
  when jsonb_typeof(classification_value)='string' then nullif(btrim(classification_value #>> '{}'),'')
  when jsonb_typeof(classification_value)='object' then coalesce(
   nullif(btrim(classification_value->>'primary'),''),nullif(btrim(classification_value->>'label'),''),
   nullif(btrim(classification_value->>'name'),''),nullif(btrim(classification_value->>'category'),''))
  else null end;
 classification_style=case when jsonb_typeof(classification_value)='object' then nullif(btrim(classification_value->>'style'),'') else null end;
 ai_classification_label=case
  when jsonb_typeof(record_row.ai_result->'classification')='string' then nullif(btrim(record_row.ai_result->'classification' #>> '{}'),'')
  when jsonb_typeof(record_row.ai_result->'classification')='object' then coalesce(
   nullif(btrim(record_row.ai_result#>>'{classification,primary}'),''),nullif(btrim(record_row.ai_result#>>'{classification,label}'),''),
   nullif(btrim(record_row.ai_result#>>'{classification,name}'),''),nullif(btrim(record_row.ai_result#>>'{classification,category}'),''))
  else null end;
 ai_classification_style=case when jsonb_typeof(record_row.ai_result->'classification')='object'
  then nullif(btrim(record_row.ai_result#>>'{classification,style}'),'') else null end;
 reasons_value=case when payload ? 'reasons' and jsonb_typeof(payload->'reasons')<>'null' then payload->'reasons' else '[]'::jsonb end;
 formal_scores_value=payload->'formalScores';
 if jsonb_typeof(reasons_value)<>'array' or jsonb_typeof(formal_scores_value) is distinct from 'object' or formal_scores_value='{}'::jsonb then raise exception '正式评分必须由评委显式填写正式分，原因必须是数组'; end if;
 if submission_row.source_kind='generated' then
  formal_scores_value=jsonb_build_object('visual',formal_scores_value->'visual','layout',formal_scores_value->'layout','match',formal_scores_value->'match');
 else
  select coalesce(jsonb_object_agg(dimension->>'id',formal_scores_value->(dimension->>'id')),'{}'::jsonb)
  into formal_scores_value
  from public.workspaces ws
  cross join lateral jsonb_array_elements(case when jsonb_typeof(ws.settings->'dimensions')='array' then ws.settings->'dimensions' else '[]'::jsonb end) dimension
  where ws.id=w and nullif(btrim(dimension->>'id'),'') is not null;
 end if;
 if not public.ai_review_scores_complete(w,submission_row.deck_id,jsonb_build_object('scores',formal_scores_value),submission_row.max_score) then
  raise exception '正式评分必须覆盖当前评分标准的全部维度';
 end if;
 for score_record in select key,value from jsonb_each(formal_scores_value) loop
  if jsonb_typeof(score_record.value)<>'number' or (score_record.value::text)::numeric<0 or (score_record.value::text)::numeric>submission_row.max_score then raise exception '正式分超出范围'; end if;
 end loop;
 if submission_row.source_kind='generated' then
  weighted_value=round((coalesce((formal_scores_value->>'visual')::numeric,0)*settings_row.vision_weight+
   coalesce((formal_scores_value->>'layout')::numeric,0)*settings_row.layout_weight+
   coalesce((formal_scores_value->>'match')::numeric,0)*settings_row.match_weight)/100,2);
  formal_total_value=weighted_value;
 else
  formal_total_value=round((select avg((value::text)::numeric) from jsonb_each(formal_scores_value)),2);
 end if;
 score_ratio_value=round(formal_total_value/nullif(submission_row.max_score,0),6);
 threshold_met_value=score_ratio_value>=settings_row.human_threshold;
 if payload->>'status'='corrected' then status_value='corrected'; end if;
 if record_row.ai_result is not null then
  for score_record in select key,value from jsonb_each(formal_scores_value) loop
   if jsonb_typeof(record_row.ai_result->'scores'->score_record.key)='number' then
    ai_score=(record_row.ai_result->'scores'->>score_record.key)::numeric;
    if (score_record.value::text)::numeric<>ai_score then status_value='corrected'; end if;
   end if;
  end loop;
  if classification_label is not null and (ai_classification_label is null or classification_label<>ai_classification_label) then status_value='corrected'; end if;
  if classification_style is not null and (ai_classification_style is null or classification_style<>ai_classification_style) then status_value='corrected'; end if;
 end if;
 insert into public.ai_review_decisions(workspace_id,deck_id,submission_id,ai_record_id,judge_user_id,decision_key,status,
  classification,reasons,reason_other,formal_scores,formal_total,deck_version,rubric_version,max_score,score_ratio,expires_at,summary)
 values(w,deck_row.id,submission_row.id,record_row.id,actor,decision_key_value,status_value,classification_value,reasons_value,
  left(coalesce(payload->>'reasonOther',''),500),formal_scores_value,formal_total_value,submission_row.deck_version,submission_row.rubric_version,
  submission_row.max_score,score_ratio_value,now()+interval '7 days',left(coalesce(payload->>'summary',''),2000))
 returning * into decision_row;
 if submission_row.source_kind='generated' then
  source_value=coalesce(deck_row.data->'generation','{}'::jsonb);
  reference_value=coalesce(deck_row.data->'generation'->'references',deck_row.data->'references','[]'::jsonb);
  insert into public.ai_generated_entries(workspace_id,deck_id,decision_id,scope,unit_id,source,reference_refs,formal_scores,formal_total,weights,
   human_threshold,human_coverage,threshold_met,ai_self_learning,library_visible,library_label,score_ratio)
  values(w,deck_row.id,decision_row.id,submission_row.scope,submission_row.unit_id,source_value,reference_value,formal_scores_value,formal_total_value,
   jsonb_build_object('visual',settings_row.vision_weight,'layout',settings_row.layout_weight,'match',settings_row.match_weight),
   settings_row.human_threshold,0,threshold_met_value,true,false,'AI 自我学习生成',score_ratio_value) on conflict(decision_id) do nothing;
 end if;
 return jsonb_build_object('ok',true,'idempotent',false,'decisionId',decision_row.id,'status',status_value,
  'formalScores',formal_scores_value,'formalTotal',formal_total_value,'scoreRatio',score_ratio_value,
  'thresholdMet',threshold_met_value,'expiresAt',decision_row.expires_at,'admitted',false,'aiVisible',true,
  'message','正式评分已确认；需评委单独点击“收入素材库”');
end $$;

revoke all on function public.ai_review_confirm(uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.ai_review_confirm(uuid,uuid,jsonb) to service_role;

commit;