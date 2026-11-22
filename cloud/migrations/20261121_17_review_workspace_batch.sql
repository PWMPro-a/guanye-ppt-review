-- REVIEW_WORKSPACE_BATCH_V17
-- Collapse the per-submission detail reads in the workspace review view into one
-- scalar JSONB response. The Python caller still loads submissions to preserve
-- the existing scoresComplete contract, then passes only those loaded IDs here.
--
-- The function is intentionally security invoker and executable only by
-- service_role. It is read-only (stable) and revalidates the workspace, actor,
-- deck and every relationship before returning predictions or decisions.
begin;

create or replace function public.ai_review_workspace_batch(
 w uuid,
 actor uuid,
 p_deck_id text,
 p_submission_ids uuid[] default '{}'::uuid[]
) returns jsonb
language plpgsql stable security invoker set search_path='' as $$
declare batch_count integer;
begin
 -- REVIEW_WORKSPACE_BATCH_V17
 if w is null or actor is null then raise exception '工作区或用户无效'; end if;
 if p_deck_id is null or length(p_deck_id) not between 1 and 100 then raise exception '作品标识无效'; end if;
 if p_submission_ids is null then p_submission_ids=array[]::uuid[]; end if;
 batch_count=coalesce(array_length(p_submission_ids,1),0);
 if batch_count>1000 then raise exception '提交批次过大'; end if;
 if not exists(
  select 1 from public.members m
  where m.workspace_id=w and m.user_id=actor
 ) then raise exception '请先加入团队'; end if;
 if not exists(
  select 1 from public.decks d
  where d.workspace_id=w and d.id=p_deck_id
 ) then raise exception '作品不存在'; end if;

 return (
  with requested as (
   select distinct ids.id
   from unnest(p_submission_ids) as ids(id)
  ), authorized as (
   select s.*
   from requested r
   join public.ai_review_submissions s on s.id=r.id
   where s.workspace_id=w
    and s.deck_id=p_deck_id
    and s.human_user_id=actor
  ), complete as (
   select s.*
   from authorized s
   where public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score)
  ), record_rows as (
   select distinct on (r.id)
    r.id,r.job_id,r.deck_id,r.scope,r.unit_id,r.variant,r.deck_version,r.rubric_version,
    r.model_hint,r.ai_result,r.created_at
   from complete s
   join public.ai_review_matches m
    on m.submission_id=s.id
   join public.ai_review_records r
    on r.id=m.record_id and r.variant=m.variant
   join public.ai_review_jobs j
    on j.id=r.job_id
   where r.workspace_id=w
    and j.workspace_id=w
    and r.deck_id=s.deck_id
    and j.deck_id=r.deck_id
    and r.source_kind=s.source_kind
    and j.source_kind=r.source_kind
    and j.variant=r.variant
    and r.scope=s.scope
    and r.unit_id=s.unit_id
    and j.scope=r.scope
    and j.unit_id=r.unit_id
    and r.deck_version=s.deck_version
    and r.rubric_version=s.rubric_version
    and j.deck_version=r.deck_version
    and j.rubric_version=r.rubric_version
   order by r.id,r.created_at desc
  ), decision_rows as (
   select d.*,e.id as entry_id,coalesce(e.library_visible,false) as library_visible
   from authorized s
   join public.ai_review_decisions d
    on d.submission_id=s.id
    and d.workspace_id=w
    and d.deck_id=s.deck_id
   left join lateral (
    select entry.id,entry.library_visible
    from public.ai_generated_entries entry
    where entry.workspace_id=w
     and entry.deck_id=d.deck_id
     and entry.decision_id=d.id
    order by entry.created_at desc,entry.id desc
    limit 1
   ) e on true
  )
  select jsonb_build_object(
   'records',coalesce((
    select jsonb_agg(jsonb_build_object(
     'id',rr.id,
     'job_id',rr.job_id,
     'deck_id',rr.deck_id,
     'scope',rr.scope,
     'unit_id',rr.unit_id,
     'variant',rr.variant,
     'deck_version',rr.deck_version,
     'rubric_version',rr.rubric_version,
     'model_hint',rr.model_hint,
     'ai_result',rr.ai_result
    ) order by rr.created_at desc,rr.id)
    from record_rows rr
   ),'[]'::jsonb),
   'decisions',coalesce((
    select jsonb_agg(jsonb_build_object(
     'id',dr.id,
     'submission_id',dr.submission_id,
     'ai_record_id',case when linked_record.id is null then null else dr.ai_record_id end,
     'status',dr.status,
     'classification',dr.classification,
     'reasons',dr.reasons,
     'reason_other',dr.reason_other,
     'formal_scores',dr.formal_scores,
     'formal_total',dr.formal_total,
     'score_ratio',dr.score_ratio,
     'expires_at',dr.expires_at,
     'summary',dr.summary,
     '_library_visible',dr.library_visible,
     '_entry_id',dr.entry_id
    ) order by dr.created_at desc,dr.id)
    from decision_rows dr
    left join public.ai_review_records linked_record
     on linked_record.id=dr.ai_record_id
     and linked_record.workspace_id=w
     and linked_record.deck_id=p_deck_id
   ),'[]'::jsonb)
  )
 );
end $$;

revoke all on function public.ai_review_workspace_batch(uuid,uuid,text,uuid[]) from public,anon,authenticated;
grant execute on function public.ai_review_workspace_batch(uuid,uuid,text,uuid[]) to service_role;

commit;
