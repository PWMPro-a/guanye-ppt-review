-- Bring newly previewable works into the existing, versioned review queues.
alter table public.ai_review_jobs add column if not exists available_at timestamptz not null default now();

create or replace function public.ai_review_enqueue_preview() returns trigger
language plpgsql security definer set search_path='' as $$
declare judge uuid;
begin
 if TG_OP='UPDATE' and old.data is not distinct from new.data and old.source_kind=new.source_kind then return new; end if;
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
revoke all on function public.ai_review_enqueue_preview() from public,anon,authenticated;
drop trigger if exists ai_review_enqueue_preview_after_deck on public.decks;
create trigger ai_review_enqueue_preview_after_deck after insert or update of data,source_kind on public.decks
for each row execute function public.ai_review_enqueue_preview();

create or replace function public.ai_review_claim_jobs(w uuid, actor uuid, p_kind text, p_limit integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare jobs_json jsonb;
begin
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以执行预评'; end if;
 update public.ai_review_jobs set status='failed',error='预评执行超时',available_at='infinity',updated_at=now()
 where workspace_id=w and source_kind=p_kind and status='running' and attempts>=3 and updated_at<now()-interval '15 minutes';
 with picked as (
  select j.id from public.ai_review_jobs j where j.workspace_id=w and j.source_kind=p_kind and j.attempts<3
   and ((j.status in ('queued','failed') and j.available_at<=now())
     or (j.status='running' and j.updated_at<now()-interval '15 minutes'))
  order by j.created_at,j.id for update skip locked limit greatest(1,least(coalesce(p_limit,1),20))
 ), changed as (
  update public.ai_review_jobs j set status='running',judge_user_id=actor,attempts=j.attempts+1,error=null,updated_at=now()
  from picked p where j.id=p.id returning j.*
 )
 select coalesce(jsonb_agg(jsonb_build_object('id',id,'deckId',deck_id,'sourceKind',source_kind,'scope',scope,
  'unitId',unit_id,'position',position,'variant',variant,'bucket',bucket,'split',split,'deckVersion',deck_version,
  'rubricVersion',rubric_version,'maxScore',max_score,'attempts',attempts) order by created_at,id),'[]'::jsonb)
 into jobs_json from changed;
 return jobs_json;
end $$;

create or replace function public.ai_review_finish_attempt(
 w uuid,actor uuid,target_job uuid,p_attempt integer,p_status text,p_result jsonb,p_error text,p_terminal boolean default false
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare current_job public.ai_review_jobs%rowtype; current_deck_version text; current_rubric_version text; outcome jsonb;
begin
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以完成预评'; end if;
 select * into current_job from public.ai_review_jobs where workspace_id=w and id=target_job for update;
 if not found or current_job.status<>'running' or current_job.attempts is distinct from p_attempt
    or current_job.judge_user_id is distinct from actor or current_job.updated_at<now()-interval '15 minutes' then
  return jsonb_build_object('ok',false,'status','lease_lost','aiVisible',false);
 end if;
 if p_status not in ('completed','failed') then raise exception '任务状态无效'; end if;
 current_deck_version=public.ai_review_deck_version(w,current_job.deck_id);
 current_rubric_version=public.ai_review_rubric_version(w);
 if current_deck_version is distinct from current_job.deck_version
    or current_rubric_version is distinct from current_job.rubric_version then
  update public.ai_review_jobs set status='failed',error='作品或评分标准版本已变化，预评作废',available_at='infinity'::timestamptz,updated_at=now()
  where id=target_job and status='running' and attempts=p_attempt and judge_user_id=actor;
  if not found then
   return jsonb_build_object('ok',false,'status','lease_lost','aiVisible',false);
  end if;
  return jsonb_build_object('ok',false,'status','version_changed','jobId',target_job,'aiVisible',false);
 end if;
 outcome=public.ai_review_finish_job(w,actor,target_job,p_status,p_result,p_error);
 if p_status='failed' then
  update public.ai_review_jobs set available_at=case when p_terminal or p_attempt>=3 then 'infinity'::timestamptz
      else now()+case when p_attempt=1 then interval '30 seconds' else interval '2 minutes' end end
  where id=target_job;
 end if;
 return outcome;
end $$;
revoke all on function public.ai_review_finish_attempt(uuid,uuid,uuid,integer,text,jsonb,text,boolean) from public,anon,authenticated;
grant execute on function public.ai_review_finish_attempt(uuid,uuid,uuid,integer,text,jsonb,text,boolean) to service_role;
revoke execute on function public.ai_review_finish_job(uuid,uuid,uuid,text,jsonb,text) from service_role;
