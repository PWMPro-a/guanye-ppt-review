-- Professional admission and aggregate comparison. No historical entries are rewritten.
begin;

-- Keep the legacy calculation, version/expiry checks and provenance write in one place.
-- Only the two guarded callers below can invoke it; move once on migration replay.
do $$
begin
 if to_regprocedure('guanye_internal.ai_review_admit(uuid,uuid,jsonb)') is null then
  alter function public.ai_review_admit(uuid,uuid,jsonb) set schema guanye_internal;
 end if;
end $$;
revoke all on function guanye_internal.ai_review_admit(uuid,uuid,jsonb)
 from public,anon,authenticated,service_role;

create or replace function public.ai_review_admit(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 perform guanye_internal.single_judge_require_legacy(w);
 return guanye_internal.ai_review_admit(w,actor,payload);
end $$;
revoke all on function public.ai_review_admit(uuid,uuid,jsonb) from public,anon,authenticated,service_role;
grant execute on function public.ai_review_admit(uuid,uuid,jsonb) to service_role;

-- now() is transaction start, not the order in which the bundle got its deck lock.
-- Stamp new professional generated submissions/decisions under that lock, including
-- multiple revisions in one transaction. Leave uploaded/legacy timestamps unchanged.
create or replace function guanye_internal.single_judge_order_generated_score() returns trigger
language plpgsql security definer set search_path='' as $$
declare active boolean; previous_at timestamptz;
begin
 select ws.single_judge_admin_id is not null into active
 from public.workspaces ws where ws.id=new.workspace_id for share;
 if not coalesce(active,false) then return new; end if;
 perform 1 from public.decks d
 where d.workspace_id=new.workspace_id and d.id=new.deck_id and d.source_kind='generated' for update;
 if not found then return new; end if;
 -- A fixed old snapshot cannot use a row-lock-only protocol to observe a concurrent revision.
 if current_setting('transaction_isolation') not in ('read committed','read uncommitted') then
  raise exception '请在 READ COMMITTED 事务中提交生成作品评分' using errcode='25000';
 end if;
 if tg_table_name='ai_review_submissions' then
  select max(s.created_at) into previous_at from public.ai_review_submissions s
  where s.workspace_id=new.workspace_id and s.deck_id=new.deck_id and s.human_user_id=new.human_user_id;
 else
  select max(d.created_at) into previous_at from public.ai_review_decisions d
  where d.workspace_id=new.workspace_id and d.deck_id=new.deck_id and d.submission_id=new.submission_id;
 end if;
 new.created_at=greatest(clock_timestamp(),previous_at+interval '1 microsecond');
 return new;
end $$;
revoke all on function guanye_internal.single_judge_order_generated_score()
 from public,anon,authenticated,service_role;
drop trigger if exists single_judge_order_generated_submission on public.ai_review_submissions;
create trigger single_judge_order_generated_submission before insert on public.ai_review_submissions
 for each row when (new.source_kind='generated') execute function guanye_internal.single_judge_order_generated_score();
drop trigger if exists single_judge_order_generated_decision on public.ai_review_decisions;
create trigger single_judge_order_generated_decision before insert on public.ai_review_decisions
 for each row execute function guanye_internal.single_judge_order_generated_score();

create or replace function public.single_judge_admit(w uuid,p_submission uuid,p_decision uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid=auth.uid(); ws public.workspaces%rowtype; deck_row public.decks%rowtype;
 submission_row public.ai_review_submissions%rowtype; decision_row public.ai_review_decisions%rowtype;
 entry_row public.ai_generated_entries%rowtype; threshold_value numeric;
begin
 select * into ws from public.workspaces where id=w for share;
 if actor is null or ws.single_judge_admin_id is null or ws.dedicated_judge_id is distinct from actor
    or not public.is_member(w) then
  raise exception '仅指定评分师可收入素材库' using errcode='42501';
 end if;
 if current_setting('transaction_isolation') not in ('read committed','read uncommitted') then
  raise exception '请在 READ COMMITTED 事务中确认入库' using errcode='25000';
 end if;
 select * into submission_row from public.ai_review_submissions s
 where s.workspace_id=w and s.id=p_submission and s.human_user_id=actor;
 if not found or submission_row.source_kind<>'generated' then
  raise exception '未找到可入库的生成作品评分' using errcode='22023';
 end if;
 -- Same workspace -> deck lock order and row as M26, before checking decisions or visibility.
 select * into deck_row from public.decks d
 where d.workspace_id=w and d.id=submission_row.deck_id and d.source_kind='generated' for update;
 if not found then raise exception '未找到可入库的生成作品' using errcode='22023'; end if;
 select * into decision_row from public.ai_review_decisions d
 where d.workspace_id=w and d.deck_id=deck_row.id and d.id=p_decision and d.submission_id=p_submission
   and d.judge_user_id=actor and d.deck_version=submission_row.deck_version and d.rubric_version=submission_row.rubric_version;
 if not found then raise exception '未找到对应的正式评分' using errcode='22023'; end if;

 -- Return the actual existing admission on repeated confirmation, even if it is now stale.
 -- On different IDs prefer the newest visible historical entry (the library also prefers newest).
 select * into entry_row from public.ai_generated_entries e
 where e.workspace_id=w and e.deck_id=deck_row.id and e.scope=submission_row.scope
   and e.unit_id=submission_row.unit_id and e.library_visible
 order by (e.decision_id=p_decision) desc,e.created_at desc,e.id desc limit 1;
 if entry_row.decision_id is distinct from p_decision then
  -- Rank by formal *submission* first. A later legacy confirm on an older submission
  -- cannot outrank a revision. Unresolvable historical timestamp ties fail closed.
  if exists (
   select 1 from public.ai_review_submissions s
   join public.ai_review_decisions d on d.submission_id=s.id and d.workspace_id=s.workspace_id
    and d.deck_id=s.deck_id and d.judge_user_id=actor and d.deck_version=s.deck_version and d.rubric_version=s.rubric_version
   where s.workspace_id=w and s.deck_id=deck_row.id and s.human_user_id=actor and s.source_kind='generated'
    and s.scope=submission_row.scope and s.unit_id=submission_row.unit_id
    and s.deck_version=submission_row.deck_version and s.rubric_version=submission_row.rubric_version
    and d.id<>p_decision and (s.created_at>submission_row.created_at or
     (s.created_at=submission_row.created_at and (s.id<>p_submission or d.created_at>=decision_row.created_at)))
  ) then raise exception '只能使用该评分单元的最新正式评分入库' using errcode='22023'; end if;
  if decision_row.formal_scores is distinct from submission_row.entry->'scores' then
   raise exception '正式分与评委提交的人分不一致' using errcode='22023';
  end if;
  -- Stabilize the rubric weights and threshold through the existing admission write.
  select s.human_threshold into threshold_value from public.ai_review_settings s where s.workspace_id=w for share;
  if not found then raise exception '未找到正式评分门槛' using errcode='22023'; end if;
  if entry_row.id is null then
   return guanye_internal.ai_review_admit(w,actor,jsonb_build_object('submissionId',p_submission,'decisionId',p_decision));
  end if;
  -- Another accepted decision for this unit is already visible. No new write is needed.
  if decision_row.expires_at<=now()
    or decision_row.deck_version is distinct from public.ai_review_deck_version(w,deck_row.id)
    or decision_row.rubric_version is distinct from public.ai_review_rubric_version(w)
    or decision_row.max_score<1 or decision_row.formal_total is null
    or round(decision_row.formal_total/decision_row.max_score,6)<threshold_value then
   raise exception '当前正式评分已失效或未达到入库门槛' using errcode='22023';
  end if;
 end if;
 return jsonb_build_object('ok',true,'idempotent',true,'admitted',true,'entryId',entry_row.id,
  'decisionId',entry_row.decision_id,'deckId',deck_row.id,'scope',entry_row.scope,'unitId',entry_row.unit_id,
  'scoreRatio',entry_row.score_ratio,'libraryLabel','AI 自我学习生成');
end $$;

create or replace function public.single_judge_comparison(w uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare access_info jsonb=public.single_judge_access(w);
begin
 if access_info->>'mode' is distinct from 'single-judge'
    or coalesce(access_info->>'role','') not in ('judge','admin') then
  raise exception '仅指定评分师或管理员可读取比较汇总' using errcode='42501';
 end if;
 return public.ai_review_comparison(w);
end $$;
revoke all on function public.single_judge_admit(uuid,uuid,uuid),public.single_judge_comparison(uuid)
 from public,anon,authenticated,service_role;
grant execute on function public.single_judge_admit(uuid,uuid,uuid),public.single_judge_comparison(uuid) to authenticated;

commit;
