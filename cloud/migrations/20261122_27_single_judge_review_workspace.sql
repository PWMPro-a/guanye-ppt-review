-- SINGLE_JUDGE_REVIEW_WORKSPACE_M27
-- Narrow read-only facade for the activated single-judge workspace.
begin;

create or replace function public.single_judge_review_workspace(
 w uuid,
 p_kind text,
 p_deck_id text default null
) returns jsonb
language plpgsql
stable
security definer
set search_path=''
as $$
declare
 actor uuid=auth.uid();
 ws public.workspaces%rowtype;
 member_row public.members%rowtype;
 d public.decks%rowtype;
 selected_deck_id text;
 deck_json jsonb;
 review_json jsonb;
 submissions_json jsonb='[]'::jsonb;
 records_json jsonb='[]'::jsonb;
 decisions_json jsonb='[]'::jsonb;
 settings_json jsonb;
 workspace_settings jsonb='{}'::jsonb;
 queue_json jsonb;
 queues_json jsonb;
 queue_cursors_json jsonb;
 upload_queue jsonb;
 generated_queue jsonb;

 submission_ids uuid[]='{}'::uuid[];
 deck_version_value text='';
 rubric_version_value text='';
 pre_status text='not_queued';
 warnings jsonb='[]'::jsonb;
 job_status text;
 job_attempts integer;
 job_available_at timestamptz;
 warning_key text;
begin
 -- SINGLE_JUDGE_REVIEW_WORKSPACE_M27
 if actor is null then
  raise exception '请先登录' using errcode='42501';
 end if;
 if p_kind is null or p_kind not in ('uploaded','generated') then
  raise exception '队列类型无效' using errcode='22023';
 end if;

 select * into ws from public.workspaces where id=w;
 select * into member_row from public.members where workspace_id=w and user_id=actor;
 if ws.id is null or ws.single_judge_admin_id is null
    or ws.dedicated_judge_id is distinct from actor or member_row.user_id is null then
  raise exception '仅该工作区指定评分师可读取专业版评审' using errcode='42501';
 end if;

 if p_deck_id is not null and length(p_deck_id)>100 then
  raise exception '作品标识无效' using errcode='22023';
 end if;

 upload_queue=public.ai_review_queue_decks(w,'uploaded',null,8);
 generated_queue=public.ai_review_queue_decks(w,'generated',null,8);
 queues_json=jsonb_build_object(
  'uploaded',coalesce(upload_queue->'items','[]'::jsonb),
  'generated',coalesce(generated_queue->'items','[]'::jsonb)
 );
 queue_cursors_json=jsonb_build_object(
  'uploaded',upload_queue->'nextCursor',
  'generated',generated_queue->'nextCursor'
 );
 queue_json=public.ai_review_queue_counts(w);

 selected_deck_id=nullif(btrim(coalesce(p_deck_id,'')),'');
 if selected_deck_id is null then
  selected_deck_id=queues_json->p_kind->0->>'id';
 end if;

 if selected_deck_id is not null then
  select * into d from public.decks where workspace_id=w and id=selected_deck_id;
  if d.id is null then
   raise exception '作品不存在' using errcode='22023';
  end if;
  if d.source_kind is distinct from p_kind then
   raise exception '作品与队列类型不匹配' using errcode='22023';
  end if;
  deck_json=to_jsonb(d);
  deck_version_value=coalesce(public.ai_review_deck_version(w,d.id),'');
 else
  deck_json='null'::jsonb;
 end if;

 rubric_version_value=coalesce(public.ai_review_rubric_version(w),'');
 workspace_settings=coalesce(ws.settings,'{}'::jsonb);

 select jsonb_build_object(
  'visionWeight',s.vision_weight,
  'layoutWeight',s.layout_weight,
  'matchWeight',s.match_weight,
  'humanThreshold',s.human_threshold,
  'modelHint',s.model_hint,
  'reasonCatalog',s.reason_catalog
 ) into settings_json
 from public.ai_review_settings s where s.workspace_id=w;
 if settings_json is null then
  raise exception 'AI 评审配置尚未初始化' using errcode='22023';
 end if;

 if d.id is not null then
  select to_jsonb(r) into review_json from public.reviews r
   where r.workspace_id=w and r.user_id=actor and r.deck_id=d.id
   limit 1;

  select coalesce(array_agg(s.id order by s.created_at desc,s.id desc),'{}'::uuid[])
  into submission_ids
  from public.ai_review_submissions s
  where s.workspace_id=w and s.deck_id=d.id and s.human_user_id=actor
   and s.deck_version=deck_version_value and s.rubric_version=rubric_version_value;

  select coalesce(jsonb_agg(jsonb_build_object(
   'id',s.id,
   'deckId',s.deck_id,
   'scope',s.scope,
   'unitId',coalesce(s.unit_id,''),
   'position',s.position,
   'deckVersion',s.deck_version,
   'rubricVersion',s.rubric_version,
   'humanVersion',s.human_version,
   'entry',s.entry,
   'scoresComplete',public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score),
   'createdAt',s.created_at
  ) order by s.created_at desc,s.id desc),'[]'::jsonb)
  into submissions_json
  from public.ai_review_submissions s
  where s.workspace_id=w and s.deck_id=d.id and s.human_user_id=actor
   and s.deck_version=deck_version_value and s.rubric_version=rubric_version_value;

  -- ai_review_workspace_batch is service-role-only SECURITY INVOKER. Project the same
  -- frozen-match contract directly so this authenticated-only definer never needs to
  -- widen its execution role. Only matches already frozen to current-version submissions
  -- are visible; late records without a match are excluded.
  select coalesce(jsonb_agg(jsonb_build_object(
   'recordId',rr.id,
   'submissionId',rr.submission_id,
   'jobId',rr.job_id,
   'deckId',rr.deck_id,
   'scope',rr.scope,
   'unitId',coalesce(rr.unit_id,''),
   'variant',rr.variant,
   'deckVersion',rr.deck_version,
   'rubricVersion',rr.rubric_version,
   'modelHint',rr.model_hint,
   'result',coalesce(rr.ai_result,'{}'::jsonb)
  ) order by rr.created_at desc,rr.id),'[]'::jsonb)
  into records_json
  from (
   select distinct on (record.id)
    record.id,record.job_id,record.deck_id,record.scope,record.unit_id,record.variant,
    record.deck_version,record.rubric_version,record.model_hint,record.ai_result,record.created_at,
    matched.submission_id
   from public.ai_review_submissions sub
   join public.ai_review_matches matched on matched.submission_id=sub.id
   join public.ai_review_records record on record.id=matched.record_id and record.variant=matched.variant
   join public.ai_review_jobs job on job.id=record.job_id
   where sub.workspace_id=w and sub.deck_id=d.id and sub.human_user_id=actor
    and sub.deck_version=deck_version_value and sub.rubric_version=rubric_version_value
    and public.ai_review_scores_complete(w,sub.deck_id,sub.entry,sub.max_score)
    and record.workspace_id=w and job.workspace_id=w
    and record.deck_id=sub.deck_id and job.deck_id=record.deck_id
    and record.source_kind=sub.source_kind and job.source_kind=record.source_kind
    and job.variant=record.variant
    and record.scope=sub.scope and record.unit_id=sub.unit_id
    and job.scope=record.scope and job.unit_id=record.unit_id
    and record.deck_version=sub.deck_version and record.rubric_version=sub.rubric_version
    and job.deck_version=record.deck_version and job.rubric_version=record.rubric_version
    and abs(record.max_score-sub.max_score)<=1e-9
   order by record.id,record.created_at desc
  ) rr;

  select coalesce(jsonb_agg(jsonb_build_object(
   'decisionId',decision.id,
   'submissionId',decision.submission_id,
   'aiRecordId',decision.ai_record_id,
   'status',decision.status,
   'classification',decision.classification,
   'reasons',coalesce(decision.reasons,'[]'::jsonb),
   'reasonOther',coalesce(decision.reason_other,''),
   'formalScores',decision.formal_scores,
   'formalTotal',decision.formal_total,
   'scoreRatio',decision.score_ratio,
   'expiresAt',decision.expires_at,
   'libraryVisible',coalesce(entry.library_visible,false),
   'entryId',entry.id,
   'summary',coalesce(decision.summary,'')
  ) order by decision.created_at desc,decision.id),'[]'::jsonb)
  into decisions_json
  from public.ai_review_submissions sub
  join public.ai_review_decisions decision
   on decision.submission_id=sub.id and decision.workspace_id=w and decision.deck_id=sub.deck_id
  left join lateral (
   select generated.id,generated.library_visible
   from public.ai_generated_entries generated
   where generated.workspace_id=w and generated.deck_id=decision.deck_id and generated.decision_id=decision.id
   order by generated.created_at desc,generated.id desc
   limit 1
  ) entry on true
  where sub.workspace_id=w and sub.deck_id=d.id and sub.human_user_id=actor
   and sub.deck_version=deck_version_value and sub.rubric_version=rubric_version_value;

  select j.status,j.attempts,j.available_at
  into job_status,job_attempts,job_available_at
  from public.ai_review_jobs j
  where j.workspace_id=w and j.deck_id=d.id and j.source_kind=p_kind
   and j.scope='deck' and j.deck_version=deck_version_value and j.rubric_version=rubric_version_value
   and (p_kind<>'generated' or j.variant='assisted')
  order by (p_kind='uploaded' and j.variant='baseline') desc,j.updated_at desc,j.created_at desc
  limit 1;
  if found then
   if job_status in ('queued','running','completed') then
    pre_status=job_status;
   elsif job_status='failed' and job_attempts<3 and job_available_at is distinct from 'infinity'::timestamptz then
    pre_status='retry_scheduled';
   else
    pre_status='failed';
   end if;
  end if;

  if d.source_kind='generated' then
   if jsonb_typeof(d.data->'generation') is distinct from 'object' then
    warnings=jsonb_build_array('generated 作品缺少 decks.data.generation，已按常规 images 合同继续');
   else
    foreach warning_key in array array['outline','style','pages','references'] loop
     if not (d.data->'generation' ? warning_key) then
      warnings=warnings||jsonb_build_array('decks.data.generation 缺少 '||warning_key);
     end if;
    end loop;
   end if;
  end if;
 end if;

 return jsonb_build_object(
  'kind',p_kind,
  'deck',coalesce(deck_json,'null'::jsonb),
  'review',coalesce(review_json,'null'::jsonb),
  'submissions',submissions_json,
  'ai',records_json,
  'decisions',decisions_json,
  'settings',settings_json,
  'workspaceSettings',workspace_settings,
  'role',member_row.role,
  'isJudge',true,
  'singleJudge',true,
  'queue',queue_json,
  'queues',queues_json,
  'queueCursors',queue_cursors_json,
  'preEvaluationStatus',pre_status,
  'deckVersion',deck_version_value,
  'rubricVersion',rubric_version_value,
  'contractWarnings',warnings,
  'blind',jsonb_build_object(
   'aiVisible',jsonb_array_length(records_json)>0,
   'rule','仅与已提交人工记录同版本、同评分标准、同粒度的预评解锁'
  )
 );
end $$;

revoke all on function public.single_judge_review_workspace(uuid,text,text) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_review_workspace(uuid,text,text) to authenticated;

do $single_judge_review_workspace_self_check$
begin
 if not exists(
  select 1 from pg_proc p
  where p.oid=to_regprocedure('public.single_judge_review_workspace(uuid,text,text)')
   and p.prosecdef
   and position('SINGLE_JUDGE_REVIEW_WORKSPACE_M27' in p.prosrc)>0
   and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as c(value) where c.value in ('search_path=','search_path=""'))
 ) then raise exception 'M27 review workspace facade shape missing'; end if;
 if has_function_privilege('anon','public.single_judge_review_workspace(uuid,text,text)','EXECUTE')
    or has_function_privilege('service_role','public.single_judge_review_workspace(uuid,text,text)','EXECUTE')
    or not has_function_privilege('authenticated','public.single_judge_review_workspace(uuid,text,text)','EXECUTE') then
  raise exception 'M27 review workspace ACL invalid';
 end if;
end
$single_judge_review_workspace_self_check$;

commit;
