-- Read-only progress for the same authorized page, including saved admission and recoverable Sol failures.
create or replace function guanye_internal.cover_training_feedback(w uuid,p_page uuid,p_actor uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('human',coalesce(first.entry,s.entry)->'scores','recordId',r.id,'aiScores',r.ai_result->'scores',
  'aiReasons',r.ai_result->'reasons','reviewStatus',case when qj.status='failed' and qj.attempts<3
    and isfinite(qj.available_at) then 'retrying' else qj.status end,
  'decisionId',d.id,'formalScores',d.formal_scores,
  'libraryVisible',public.ai_review_page_library_visible(w,j.deck_id,p.id::text))
 from public.ai_generation_pages p join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=w
 join public.decks item on item.workspace_id=w and item.id=j.deck_id and item.ai_allowed
 join public.ai_review_submissions s on s.workspace_id=w and s.deck_id=j.deck_id and s.source_kind='generated'
  and s.human_user_id=p_actor and s.scope='page' and s.unit_id=p.id::text
  and s.deck_version=public.ai_review_deck_version(w,j.deck_id) and s.rubric_version=public.ai_review_rubric_version(w)
 left join guanye_internal.cover_training_after_human q on q.workspace_id=w and q.page_id=p.id
  and q.deck_version=s.deck_version and q.rubric_version=s.rubric_version
 left join public.ai_review_submissions first on first.id=q.submission_id and first.human_user_id=p_actor
 left join public.ai_review_jobs qj on qj.id=q.job_id and qj.workspace_id=w
 left join lateral (
  select r.* from public.ai_review_matches m join public.ai_review_records r on r.id=m.record_id
  where m.submission_id=coalesce(first.id,s.id) and m.variant=r.variant
   and (q.job_id is null or r.job_id=q.job_id) and r.workspace_id=w and r.deck_id=s.deck_id
   and r.scope=s.scope and r.unit_id=s.unit_id and r.deck_version=s.deck_version and r.rubric_version=s.rubric_version and r.max_score=s.max_score
  order by (r.variant='assisted') desc,r.created_at desc,r.id limit 1
 ) r on true
 left join lateral (
  select d.* from public.ai_review_decisions d where d.submission_id=s.id and d.workspace_id=w and d.deck_id=s.deck_id
   and d.judge_user_id=p_actor and d.deck_version=s.deck_version and d.rubric_version=s.rubric_version and d.max_score=s.max_score
  order by d.created_at desc,d.id limit 1
 ) d on true
 where p.id=p_page and p.workspace_id=w and p.status='succeeded' and j.user_id=p_actor
 order by s.created_at desc,s.id limit 1
$$;
