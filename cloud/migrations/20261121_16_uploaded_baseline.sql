-- Uploaded works use baseline as the incumbent prediction unless the stable
-- md5 bucket 0 designates them as holdout, where baseline and assisted are both
-- evaluated. Generated works keep their existing assisted-first queue policy.
--
-- MLflow's upstream validate_evaluation_results treats candidate/challenger
-- metrics as the object under validation and baseline metrics as the reference.
-- For lower-is-better MAE, a challenger must be no worse than baseline minus the
-- configured margin. Source (master commit checked 2026-11-21):
-- https://github.com/mlflow/mlflow/blob/1ca7129977abce86ad69d1c108617adebbbaa1a8/mlflow/models/evaluation/validation.py#L239-L330
-- https://github.com/mlflow/mlflow/blob/1ca7129977abce86ad69d1c108617adebbbaa1a8/mlflow/models/evaluation/validation.py#L372-L428
--
-- This migration intentionally only replaces the queue writer. ai_review_backfill
-- already delegates to ai_review_prepare, so it needs no replacement: the new
-- routing applies there while ON CONFLICT preserves completed assisted evidence and
-- queued legacy work.
begin;

create or replace function public.ai_review_prepare(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare deck_row public.decks%rowtype; rubric_value jsonb; rubric_hash text; deck_hash text;
 scope_value text; unit_json jsonb; unit_key text; position_value integer; bucket_value integer; split_value text;
 variant_value text; variants text[]; job_ids jsonb='[]'::jsonb; max_value numeric;
begin
 -- UPLOADED_BASELINE_V16
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以准备 AI 预评'; end if;
 if jsonb_typeof(payload) is distinct from 'object' or length(coalesce(payload->>'deckId','')) not between 1 and 100 then raise exception '预评内容无效'; end if;
 select * into deck_row from public.decks where workspace_id=w and id=payload->>'deckId';
 if deck_row.id is null then raise exception '作品不存在'; end if;
 rubric_value=public.ai_review_rubric(w);rubric_hash=public.ai_review_json_sha(rubric_value);
 deck_hash=public.ai_review_deck_version(w,deck_row.id);max_value=(rubric_value->>'maxScore')::numeric;
 scope_value=coalesce(payload->>'scope','deck');
 if scope_value not in ('deck','page') then raise exception '预评粒度无效'; end if;
 bucket_value=mod(get_byte(decode(md5(deck_row.id),'hex'),0),5)::integer;
 split_value=case when bucket_value=0 then 'holdout' else 'calibration' end;
 variants=case
  when bucket_value=0 then array['baseline','assisted']
  when deck_row.source_kind='uploaded' then array['baseline']
  else array['assisted']
 end;
 if scope_value='deck' then
  foreach variant_value in array variants loop
   insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score)
   values(w,deck_row.id,deck_row.source_kind,'deck','',0,variant_value,bucket_value,split_value,deck_hash,rubric_hash,max_value)
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant) do nothing;
  end loop;
  select coalesce(jsonb_agg(jsonb_build_object('jobId',j.id,'deckId',j.deck_id,'sourceKind',j.source_kind,
   'scope',j.scope,'unitId',j.unit_id,'position',j.position,'variant',j.variant,'status',j.status,
   'bucket',j.bucket,'split',j.split,'deckVersion',j.deck_version,'rubricVersion',j.rubric_version) order by j.variant),'[]'::jsonb)
  into job_ids from public.ai_review_jobs j where j.workspace_id=w and j.deck_id=deck_row.id and j.scope='deck'
   and j.deck_version=deck_hash and j.rubric_version=rubric_hash;
 else
  if jsonb_typeof(payload->'unitIds') is distinct from 'array' or jsonb_array_length(payload->'unitIds') not between 1 and 600 then raise exception '请选择需要预评的页面'; end if;
  for unit_json in select value from jsonb_array_elements(payload->'unitIds') loop
   unit_key=case when jsonb_typeof(unit_json)='string' then trim(unit_json #>> '{}') else coalesce(unit_json->>'unitId','') end;
   select coalesce((image->>'position')::integer,ordinality::integer) into position_value
   from jsonb_array_elements(case when jsonb_typeof(deck_row.data->'images')='array' then deck_row.data->'images' else '[]'::jsonb end) with ordinality as x(image,ordinality)
   where image->>'id'=unit_key limit 1;
   if position_value is null then raise exception '预评页面不存在'; end if;
   foreach variant_value in array variants loop
    insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score)
    values(w,deck_row.id,deck_row.source_kind,'page',unit_key,position_value,variant_value,bucket_value,split_value,deck_hash,rubric_hash,max_value)
    on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant) do nothing;
   end loop;
  end loop;
  select coalesce(jsonb_agg(jsonb_build_object('jobId',j.id,'deckId',j.deck_id,'sourceKind',j.source_kind,
   'scope',j.scope,'unitId',j.unit_id,'position',j.position,'variant',j.variant,'status',j.status,
   'bucket',j.bucket,'split',j.split,'deckVersion',j.deck_version,'rubricVersion',j.rubric_version) order by j.position,j.variant),'[]'::jsonb)
  into job_ids from public.ai_review_jobs j where j.workspace_id=w and j.deck_id=deck_row.id and j.scope='page'
   and j.deck_version=deck_hash and j.rubric_version=rubric_hash
   and j.unit_id in (select case when jsonb_typeof(value)='string' then trim(value #>> '{}') else coalesce(value->>'unitId','') end from jsonb_array_elements(payload->'unitIds'));
 end if;
 return jsonb_build_object('ok',true,'deckId',deck_row.id,'sourceKind',deck_row.source_kind,'scope',scope_value,
  'deckVersion',deck_hash,'rubricVersion',rubric_hash,'jobs',job_ids,'aiVisible',false);
end $$;

revoke all on function public.ai_review_prepare(uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function public.ai_review_prepare(uuid,uuid,jsonb) to service_role;

commit;
