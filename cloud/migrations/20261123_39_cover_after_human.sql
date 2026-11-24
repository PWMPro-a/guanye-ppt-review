-- Isolated cover training: require human score before a candidate's Sol page review.
begin;

create table guanye_internal.cover_training_after_human (
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 page_id uuid not null references guanye_internal.cover_training_candidates(page_id) on delete cascade,
 deck_version text not null,
 rubric_version text not null,
 submission_id uuid not null unique references public.ai_review_submissions(id) on delete cascade,
 job_id uuid not null unique references public.ai_review_jobs(id) on delete cascade,
 primary key (page_id,deck_version,rubric_version)
);
revoke all on guanye_internal.cover_training_after_human from public,anon,authenticated,service_role;

-- First official candidate page decision queues exactly one calibration-only Sol job.
-- Later human revisions do not queue another call, and do not modify the first score.
create function guanye_internal.cover_training_enqueue_after_human() returns trigger
language plpgsql security definer set search_path='' as $$
declare s public.ai_review_submissions%rowtype; page uuid; target_job uuid;
begin
 select * into s from public.ai_review_submissions where id=new.submission_id;
 if s.scope is distinct from 'page' or s.source_kind is distinct from 'generated'
  or not exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=new.workspace_id and f.enabled)
  or not exists(select 1 from public.workspaces w where w.id=new.workspace_id and w.dedicated_judge_id=new.judge_user_id)
  or s.human_user_id is distinct from new.judge_user_id or s.max_score is distinct from 5::numeric
  or s.workspace_id is distinct from new.workspace_id or s.deck_id is distinct from new.deck_id
  or s.deck_version is distinct from public.ai_review_deck_version(new.workspace_id,new.deck_id)
  or s.rubric_version is distinct from public.ai_review_rubric_version(new.workspace_id)
  or not public.ai_review_scores_complete(new.workspace_id,new.deck_id,s.entry,5)
 then return new; end if;
 select p.id into page from guanye_internal.cover_training_candidates c
 join public.ai_generation_pages p on p.id=c.page_id and p.workspace_id=c.workspace_id
 join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=c.workspace_id
 join public.decks d on d.id=j.deck_id and d.workspace_id=j.workspace_id and d.ai_allowed
 where c.workspace_id=new.workspace_id and j.deck_id=s.deck_id and j.user_id=new.judge_user_id
  and p.id::text=s.unit_id and p.status='succeeded';
 if page is null then return new; end if;
 insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,
  variant,bucket,split,deck_version,rubric_version,max_score,judge_user_id)
 values(s.workspace_id,s.deck_id,'generated','page',s.unit_id,s.position,
  'assisted',1,'calibration',s.deck_version,s.rubric_version,s.max_score,s.human_user_id)
 on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant) do nothing
 returning id into target_job;
 if target_job is not null then
  insert into guanye_internal.cover_training_after_human(workspace_id,page_id,deck_version,rubric_version,submission_id,job_id)
  values(s.workspace_id,page,s.deck_version,s.rubric_version,s.id,target_job);
 end if;
 return new;
end $$;
revoke all on function guanye_internal.cover_training_enqueue_after_human() from public,anon,authenticated,service_role;
create trigger cover_training_enqueue_after_human after insert on public.ai_review_decisions
 for each row execute function guanye_internal.cover_training_enqueue_after_human();

create or replace function public.ai_review_enqueue_preview() returns trigger
language plpgsql security definer set search_path='' as $$
declare judge uuid;
begin
 if not new.ai_allowed then return new; end if;
 if new.source_kind='generated' and exists(
  select 1 from guanye_internal.cover_training_candidates c
  join public.ai_generation_pages p on p.id=c.page_id
  join public.ai_generation_jobs j on j.id=p.job_id
  where c.workspace_id=new.workspace_id and j.deck_id=new.id
 ) then return new; end if;
 if TG_OP='UPDATE' and old.data is not distinct from new.data and old.source_kind=new.source_kind and old.ai_allowed is not distinct from new.ai_allowed then return new; end if;
 if jsonb_typeof(new.data->'images') is distinct from 'array' then return new; end if;
 if not exists(select 1 from jsonb_array_elements(new.data->'images') as p(image)
     where nullif(p.image->>'id','') is not null and left(p.image->>'path',length(new.workspace_id::text)+1)=new.workspace_id::text||'/') then
  return new;
 end if;
 select dedicated_judge_id into judge from public.workspaces where id=new.workspace_id;
 if judge is not null then
  perform public.ai_review_prepare(new.workspace_id,judge,jsonb_build_object('deckId',new.id,'scope','deck'));
 end if;
 return new;
end $$;

-- Cancel only unsent legacy whole-deck predictions for existing cover candidates.
update public.ai_review_jobs t set status='failed', error='封面训练改为人工提交后逐页评审', available_at='infinity', updated_at=now()
where t.scope='deck' and t.status='queued' and t.attempts=0 and t.source_kind='generated'
 and exists(select 1 from guanye_internal.cover_training_candidates c
  join public.ai_generation_pages p on p.id=c.page_id join public.ai_generation_jobs j on j.id=p.job_id
  where c.workspace_id=t.workspace_id and j.deck_id=t.deck_id);

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
 -- Only a job tied to the first formal page decision may attach after submission.
 if exists(
  select 1 from guanye_internal.cover_training_after_human q
  join guanye_internal.cover_training_settings f on f.workspace_id=q.workspace_id and f.enabled
  join public.ai_review_records r on r.job_id=q.job_id and r.id=new.record_id
  join public.ai_review_submissions s on s.id=q.submission_id
  join public.decks deck on deck.workspace_id=q.workspace_id and deck.id=s.deck_id and deck.ai_allowed
  where q.workspace_id=scope_workspace and s.id=new.submission_id and r.variant=new.variant
   and r.workspace_id=q.workspace_id and r.deck_id=s.deck_id and r.scope='page' and r.unit_id=q.page_id::text
   and r.source_kind='generated' and r.deck_version=s.deck_version and r.rubric_version=s.rubric_version
   and r.max_score=s.max_score and r.judge_user_id=s.human_user_id
   and r.deck_version=public.ai_review_deck_version(q.workspace_id,r.deck_id)
   and r.rubric_version=public.ai_review_rubric_version(q.workspace_id)
 ) then return new; end if;
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

create or replace function guanye_internal.cover_training_feedback(w uuid,p_page uuid,p_actor uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('human',coalesce(first.entry,s.entry)->'scores','recordId',r.id,'aiScores',r.ai_result->'scores',
  'aiReasons',r.ai_result->'reasons','reviewStatus',qj.status,'decisionId',d.id,'formalScores',d.formal_scores)
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

-- The isolated worker never consumes the uploaded, legacy, or whole-deck queues.
create function public.single_judge_cover_review_claim(w uuid,actor uuid,p_kind text,p_limit integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare output jsonb;
begin
 if p_kind is distinct from 'generated' or not exists(
  select 1 from public.workspaces workspace join guanye_internal.cover_training_settings f
  on f.workspace_id=workspace.id and f.enabled where workspace.id=w and workspace.dedicated_judge_id=actor
 ) then return '[]'::jsonb; end if;
 update public.ai_review_jobs j set status='failed', error='封面 Sol 评审超时', available_at='infinity',updated_at=now()
 where j.workspace_id=w and j.status='running' and j.attempts>=3 and j.updated_at<now()-interval '15 minutes'
  and exists(select 1 from guanye_internal.cover_training_after_human q where q.workspace_id=w and q.job_id=j.id);
 with picked as (
  select j.id from guanye_internal.cover_training_after_human q
  join public.ai_review_jobs j on j.id=q.job_id and j.workspace_id=w and j.judge_user_id=actor
  join public.ai_review_submissions s on s.id=q.submission_id and s.human_user_id=actor
  join public.decks d on d.workspace_id=w and d.id=j.deck_id and d.ai_allowed
  where q.workspace_id=w and j.source_kind='generated' and j.scope='page' and j.attempts<3
   and ((j.status in ('queued','failed') and j.available_at<=now())
     or (j.status='running' and j.updated_at<now()-interval '15 minutes'))
  order by j.created_at,j.id for update of j skip locked limit 1
 ), changed as (
  update public.ai_review_jobs j set status='running',attempts=j.attempts+1,error=null,updated_at=now()
  from picked p where j.id=p.id returning j.*
 )
 select coalesce(jsonb_agg(jsonb_build_object('id',id,'deckId',deck_id,'sourceKind',source_kind,'scope',scope,
  'unitId',unit_id,'position',position,'variant',variant,'bucket',bucket,'split',split,'deckVersion',deck_version,
  'rubricVersion',rubric_version,'maxScore',max_score,'attempts',attempts)),'[]'::jsonb) into output from changed;
 return output;
end $$;
revoke all on function public.single_judge_cover_review_claim(uuid,uuid,text,integer) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_cover_review_claim(uuid,uuid,text,integer) to service_role;

commit;
