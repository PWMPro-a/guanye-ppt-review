-- Pilot only: no workspace is enabled by this migration.
begin;
create table guanye_internal.cover_training_settings (
 workspace_id uuid primary key references public.workspaces(id) on delete cascade,
 enabled boolean not null default false,
 style_version bigint
);
create table guanye_internal.cover_training_styles (
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 version bigint not null check(version>0),
 markdown text not null check(length(markdown) between 1 and 12000),
 published_by uuid not null references auth.users(id),
 published_at timestamptz not null default now(),
 primary key(workspace_id,version)
);
create table guanye_internal.cover_training_candidates (
 page_id uuid primary key references public.ai_generation_pages(id) on delete cascade,
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 root_page_id uuid not null references public.ai_generation_pages(id) on delete cascade,
 parent_page_id uuid references public.ai_generation_pages(id) on delete cascade,
 ordinal smallint not null check(ordinal between 0 and 2),
 reference_deck_id text not null,
 reference_path text not null,
 request_key uuid,
 instructions jsonb,
 review_record_id uuid references public.ai_review_records(id) on delete set null,
 style_version bigint,
 unique(root_page_id,ordinal), unique(root_page_id,request_key)
);
create table guanye_internal.cover_training_choices (
 id bigint generated always as identity primary key,
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 root_page_id uuid not null references public.ai_generation_pages(id) on delete cascade,
 page_id uuid not null references public.ai_generation_pages(id) on delete cascade,
 decision_id uuid not null references public.ai_review_decisions(id) on delete cascade,
 actor uuid not null references auth.users(id),
 reason text not null default '',
 blind boolean not null default false,
 created_at timestamptz not null default now(),
 check(length(reason)<=700)
);
revoke all on guanye_internal.cover_training_settings, guanye_internal.cover_training_styles,
 guanye_internal.cover_training_candidates, guanye_internal.cover_training_choices from public,anon,authenticated,service_role;

-- Keep provenance separate from the confirmed payload; never trust a model-supplied version.
create table guanye_internal.cover_training_plan_context (
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 plan_id text not null,
 style_version bigint,
 choice_ids jsonb not null,
 primary key(workspace_id,plan_id)
);
revoke all on guanye_internal.cover_training_plan_context from public,anon,authenticated,service_role;

create function public.single_judge_cover_style(w uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare v_actor uuid:=guanye_internal.single_judge_creation_actor(w); result jsonb; examples jsonb;
begin
 select jsonb_build_object('version',s.version,'markdown',s.markdown) into result
 from guanye_internal.cover_training_settings f left join guanye_internal.cover_training_styles s
 on s.workspace_id=f.workspace_id and s.version=f.style_version where f.workspace_id=w and f.enabled;
 if not found then return null; end if;
 select coalesce(jsonb_agg(to_jsonb(ch)), '[]'::jsonb) into examples from (
  select last.id,last.reason,d.formal_scores as "formalScores",c.instructions
  from (select distinct on(root_page_id) * from guanye_internal.cover_training_choices where workspace_id=w
        order by root_page_id,id desc) last
  join guanye_internal.cover_training_candidates c on c.page_id=last.page_id
  join public.ai_review_decisions d on d.id=last.decision_id
  join public.decks deck on deck.workspace_id=w and deck.id=d.deck_id and deck.ai_allowed
  where last.actor=v_actor and d.rubric_version=public.ai_review_rubric_version(w)
    and d.deck_version=public.ai_review_deck_version(w,d.deck_id)
    and guanye_internal.single_judge_generation_uploaded_reference_ok(w,c.reference_deck_id,c.reference_path)
  order by last.id desc limit 4
 ) ch;
 return result||jsonb_build_object('examples',examples);
end $$;

-- The owner explicitly publishes a new document or re-selects a previous version.
create function public.single_judge_cover_save_plan(w uuid,p_plan_id uuid,p_draft jsonb,p_style bigint,p_choices jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare context jsonb; ids jsonb; result jsonb;
begin
 context=public.single_judge_cover_style(w);
 select coalesce(jsonb_agg(x->'id' order by (x->>'id')::bigint),'[]'::jsonb) into ids
 from jsonb_array_elements(context->'examples') x;
 if context is null or (context->>'version')::bigint is distinct from p_style or ids is distinct from p_choices
 then raise exception '团队规则或案例刚刚更新，请重新策划' using errcode='40001'; end if;
 result=public.single_judge_planning_save(w,p_plan_id,0,p_draft);
 insert into guanye_internal.cover_training_plan_context values(w,p_plan_id::text,p_style,ids);
 return result;
end $$;
revoke all on function public.single_judge_cover_save_plan(uuid,uuid,jsonb,bigint,jsonb) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_cover_save_plan(uuid,uuid,jsonb,bigint,jsonb) to authenticated;

create function public.single_judge_cover_publish_style(w uuid,p_markdown text,p_previous bigint) returns bigint
language plpgsql security definer set search_path='' as $$
declare actor uuid:=auth.uid(); next_version bigint;
begin
 if actor is null or not exists(select 1 from public.workspaces x where x.id=w and x.single_judge_admin_id=actor) then
  raise exception '仅专业版负责人可以发布风格' using errcode='42501'; end if;
 perform 1 from guanye_internal.cover_training_settings where workspace_id=w and enabled for update;
 if not found then raise exception '训练工作区未启用' using errcode='42501'; end if;
 if p_previous is not null then
  if p_markdown is not null or not exists(select 1 from guanye_internal.cover_training_styles where workspace_id=w and version=p_previous) then
   raise exception '未知的旧版本' using errcode='22023'; end if;
  next_version=p_previous;
 else
  if length(btrim(coalesce(p_markdown,''))) not between 1 and 12000 then raise exception '风格文档为空或太长' using errcode='22023'; end if;
  select coalesce(max(version),0)+1 into next_version from guanye_internal.cover_training_styles where workspace_id=w;
  insert into guanye_internal.cover_training_styles values(w,next_version,p_markdown,actor,now());
 end if;
 update guanye_internal.cover_training_settings set style_version=next_version where workspace_id=w;
 return next_version;
end $$;

alter function public.ai_generation_select_reference_path(uuid,uuid,uuid) rename to ai_generation_select_reference_path_before_cover;
create function public.ai_generation_select_reference_path(p_workspace uuid,p_job uuid,p_page uuid) returns text
language plpgsql stable security definer set search_path='' as $$
declare c guanye_internal.cover_training_candidates%rowtype;
begin
 select t.* into c from guanye_internal.cover_training_candidates t join public.ai_generation_pages p on p.id=t.page_id
 where t.workspace_id=p_workspace and t.page_id=p_page and p.job_id=p_job;
 if not found then return public.ai_generation_select_reference_path_before_cover(p_workspace,p_job,p_page); end if;
 if exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=p_workspace and f.enabled)
    and guanye_internal.single_judge_generation_uploaded_reference_ok(p_workspace,c.reference_deck_id,c.reference_path) then
  return c.reference_path;
 end if;
 return null;
end $$;

create function guanye_internal.cover_training_capture_root() returns trigger language plpgsql security definer set search_path='' as $$
declare j public.ai_generation_jobs%rowtype; ref_path text; ref_deck text; style_v bigint;
begin
 perform 1 from guanye_internal.cover_training_settings f where f.workspace_id=new.workspace_id and f.enabled;
 if not found then return new; end if;
 select * into j from public.ai_generation_jobs where id=new.job_id and workspace_id=new.workspace_id;
 if new.position<>1 or j.plan_snapshot->>'pageCount' is distinct from '1' or new.outline->>'narrativeRole' is distinct from 'cover'
    or j.config ? 'coverTrainingRoot' or not exists(select 1 from public.workspaces w
     where w.id=new.workspace_id and w.single_judge_admin_id is not null and w.dedicated_judge_id=j.user_id) then return new; end if;
 select style_version into style_v from guanye_internal.cover_training_plan_context
 where workspace_id=new.workspace_id and plan_id=j.plan_id;
 ref_path=nullif(current_setting('guanye.cover_reference_path',true),'');
 ref_deck=nullif(current_setting('guanye.cover_reference_deck',true),'');
 if ref_path is null or ref_deck is null then return new; end if;
 if not exists(select 1 from jsonb_array_elements(j.reference_data) r(value)
   where r.value->>'deckId'=ref_deck and (r.value->'imagePaths'->>0)=ref_path) or
   not guanye_internal.single_judge_generation_uploaded_reference_ok(new.workspace_id,ref_deck,ref_path) then
  raise exception '已选参考不属于确认策划或已失效' using errcode='42501'; end if;
 insert into guanye_internal.cover_training_candidates(page_id,workspace_id,root_page_id,ordinal,reference_deck_id,reference_path,style_version)
 values(new.id,new.workspace_id,new.id,0,ref_deck,ref_path,style_v);
 return new;
end $$;
create trigger cover_training_capture_root after insert on public.ai_generation_pages
 for each row execute function guanye_internal.cover_training_capture_root();

create function public.single_judge_cover_start(
 w uuid,p_plan_id text,p_deck_id text,p_plan jsonb,p_title text,p_style text,p_outline jsonb,p_references jsonb,p_pages jsonb,
 p_reference_deck text,p_reference_path text
) returns jsonb language plpgsql security definer set search_path='' as $$
declare actor uuid:=guanye_internal.single_judge_creation_actor(w); result jsonb;
begin
 if not exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=w and f.enabled) then
  raise exception '封面训练未启用' using errcode='42501'; end if;
 if p_plan->>'pageCount' is distinct from '1' or p_plan#>>'{pages,0,narrativeRole}' is distinct from 'cover'
    or p_pages#>>'{0,outline,narrativeRole}' is distinct from 'cover' or
    not exists(select 1 from jsonb_array_elements(p_references) r(value)
     where r.value->>'deckId'=p_reference_deck and (r.value->'imagePaths'->>0)=p_reference_path) or
    not guanye_internal.single_judge_generation_uploaded_reference_ok(w,p_reference_deck,p_reference_path) then
  raise exception '请选择确认版中的有效封面参考' using errcode='22023'; end if;
 perform set_config('guanye.cover_reference_path',p_reference_path,true);
 perform set_config('guanye.cover_reference_deck',p_reference_deck,true);
 result=public.single_judge_generation_create(w,p_plan_id,p_deck_id,p_plan,p_title,p_style,p_outline,p_references,p_pages);
 perform set_config('guanye.cover_reference_path','',true);
 perform set_config('guanye.cover_reference_deck','',true);
 if not exists(select 1 from guanye_internal.cover_training_candidates c join public.ai_generation_pages p on p.id=c.page_id
   where c.workspace_id=w and p.job_id=(result->>'id')::uuid and c.ordinal=0
    and c.reference_deck_id=p_reference_deck and c.reference_path=p_reference_path) then
  raise exception '该策划已有使用不同参考或非训练模式的任务' using errcode='23505'; end if;
 return result;
end $$;

-- Same M31 lease/capacity path for ordinary and pilot workers. Older workers must
-- never claim or send a pilot image to a legacy channel they were configured for.
create function guanye_internal.cover_training_claim_pages(
 p_workspace uuid,
 p_actor uuid,
 p_worker_id text,
 p_limit integer,
 p_lease_seconds integer,p_supports_cover boolean
) returns jsonb
language plpgsql security definer set search_path='' as $$
declare
 result jsonb;
 lease_value integer;
 max_workers integer;
 active_count integer;
 available_slots integer;
 claim_limit integer;
begin
 if auth.uid() is not null and p_actor is distinct from auth.uid() then raise exception '操作人身份不匹配'; end if;
 perform guanye_internal.single_judge_image_actor(p_workspace,p_actor);
 if not exists(select 1 from public.members where workspace_id=p_workspace and user_id=p_actor) then
  raise exception '请先加入团队';
 end if;
 if length(trim(coalesce(p_worker_id,''))) not between 1 and 200 then raise exception '工作节点编号无效'; end if;
 insert into public.ai_generation_settings(workspace_id)
 values(p_workspace) on conflict(workspace_id) do nothing;
 -- Serialize all training claims on the same project-wide channel lock.
 if p_supports_cover then perform pg_advisory_xact_lock(1735591798,20); end if;
 -- Serialize claims on the workspace row so several worker processes cannot
 -- exceed the one workspace-wide concurrency limit.
 select s.max_workers into max_workers
 from public.ai_generation_settings s where s.workspace_id=p_workspace for update;
 select count(*) into active_count from public.ai_generation_pages
 where workspace_id=p_workspace and status='running'
  and lease_expires_at is not null and lease_expires_at>now();
 available_slots=greatest(0,max_workers-active_count);
 if p_supports_cover and available_slots>0 then
  select count(*) into active_count from public.ai_generation_pages p
   join guanye_internal.cover_training_candidates c on c.page_id=p.id
   where p.status='running' and p.lease_expires_at>now();
  available_slots=least(available_slots,greatest(0,20-active_count));
 end if;
 if available_slots=0 then return '[]'::jsonb; end if;
 claim_limit=least(greatest(1,least(coalesce(p_limit,16),32)),available_slots);
 lease_value=greatest(60,least(coalesce(p_lease_seconds,1800),7200));
 with picked as (
  select p.id
  from public.ai_generation_pages p
  join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=p.workspace_id
  left join guanye_internal.cover_training_candidates c on c.page_id=p.id and c.workspace_id=p_workspace
  left join guanye_internal.cover_training_settings f on f.workspace_id=p_workspace
  where p.workspace_id=p_workspace
   and ((not p_supports_cover and c.page_id is null) or (p_supports_cover and c.page_id is not null and f.enabled))
   and j.status in ('queued','running')
   and (p.status='queued' or (p.status='running' and (p.lease_expires_at is null or p.lease_expires_at<=now())))
  order by j.created_at,p.position
  for update of p skip locked
  limit claim_limit
 ), changed as (
  update public.ai_generation_pages p
  set status='running',lease_owner=trim(p_worker_id),lease_token=gen_random_uuid(),
      lease_expires_at=now()+make_interval(secs=>lease_value),attempts=p.attempts+1,
      started_at=coalesce(p.started_at,now()),updated_at=now(),error=null
  from picked x where p.id=x.id
  returning p.*
 )
 select coalesce(jsonb_agg(item ORDER BY job_created,page_position),'[]'::jsonb) into result
 from (
  select j.created_at job_created,p.position page_position,
   jsonb_build_object(
    'jobId',p.job_id,'deckId',j.deck_id,'planId',j.plan_id,'title',j.title,
    'style',j.style,'outline',j.outline,'references',j.reference_data,
    'pageId',p.id,'position',p.position,'planPageId',coalesce(p.plan_page_id,''),
    'pageTitle',p.title,'pageOutline',p.outline,'description',p.description,'prompt',p.prompt,
    'pageReferences',p.reference_data,'hasReferenceImage',public.ai_generation_select_reference_path(p_workspace,p.job_id,p.id) is not null,
    'storagePath',p.storage_path,'contentType',p.content_type,
    'attempts',p.attempts,'leaseToken',p.lease_token,
    'leaseExpiresAt',p.lease_expires_at
   ) || case when c.page_id is null then '{}'::jsonb else '{"coverTraining":true}'::jsonb end item
  from changed p join public.ai_generation_jobs j on j.id=p.job_id
  left join guanye_internal.cover_training_candidates c on c.page_id=p.id and c.workspace_id=p_workspace
 ) rows;
 update public.ai_generation_jobs j set status='running',updated_at=now()
 where j.workspace_id=p_workspace and j.status='queued'
  and exists(select 1 from public.ai_generation_pages p where p.job_id=j.id and p.status='running');
 return result;
end $$;

create or replace function public.ai_generation_claim_pages(p_workspace uuid,p_actor uuid,p_worker_id text,p_limit integer default 16,p_lease_seconds integer default 1800)
returns jsonb language sql volatile security definer set search_path='' as $$
 select guanye_internal.cover_training_claim_pages(p_workspace,p_actor,p_worker_id,p_limit,p_lease_seconds,false)
$$;
create function public.single_judge_cover_claim_pages(p_workspace uuid,p_actor uuid,p_worker_id text,p_limit integer,p_lease_seconds integer)
returns jsonb language sql volatile security definer set search_path='' as $$
 select guanye_internal.cover_training_claim_pages(p_workspace,p_actor,p_worker_id,p_limit,p_lease_seconds,true)
$$;
revoke all on function guanye_internal.cover_training_claim_pages(uuid,uuid,text,integer,integer,boolean),
 public.single_judge_cover_claim_pages(uuid,uuid,text,integer,integer) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_cover_claim_pages(uuid,uuid,text,integer,integer) to service_role;

-- Only the current judge's own, version-matched, page-level scores can unlock this action.
create function guanye_internal.cover_training_feedback(w uuid,p_page uuid,p_actor uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('human',s.entry->'scores','recordId',r.id,'aiScores',r.ai_result->'scores',
  'aiReasons',r.ai_result->'reasons','decisionId',d.id,'formalScores',d.formal_scores)
 from public.ai_generation_pages p join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=w
 join public.decks item on item.workspace_id=w and item.id=j.deck_id and item.ai_allowed
 join public.ai_review_submissions s on s.workspace_id=w and s.deck_id=j.deck_id and s.source_kind='generated'
  and s.human_user_id=p_actor and s.scope='page' and s.unit_id=p.id::text
  and s.deck_version=public.ai_review_deck_version(w,j.deck_id) and s.rubric_version=public.ai_review_rubric_version(w)
 left join lateral (
  select r.* from public.ai_review_matches m join public.ai_review_records r on r.id=m.record_id
  where m.submission_id=s.id and m.variant=r.variant and r.workspace_id=w and r.deck_id=s.deck_id
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

create function public.single_judge_cover_status(w uuid,p_job uuid) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare actor uuid:=guanye_internal.single_judge_creation_actor(w); root uuid; versions jsonb; choice uuid;
begin
 select c.root_page_id into root from guanye_internal.cover_training_candidates c join public.ai_generation_pages p on p.id=c.page_id
 join public.ai_generation_jobs j on j.id=p.job_id
 where c.workspace_id=w and p.job_id=p_job and j.user_id=actor;
 if not found then return null; end if;
 select coalesce(jsonb_agg(jsonb_build_object('pageId',p.id,'jobId',p.job_id,'deckId',j.deck_id,'ordinal',c.ordinal,'status',p.status,
  'image',case when p.status='succeeded' then jsonb_build_object('path',p.storage_path,'id',p.id) end,
  'feedback',guanye_internal.cover_training_feedback(w,p.id,actor)) order by c.ordinal),'[]'::jsonb) into versions
 from guanye_internal.cover_training_candidates c join public.ai_generation_pages p on p.id=c.page_id
 join public.ai_generation_jobs j on j.id=p.job_id and j.workspace_id=w
 where c.workspace_id=w and c.root_page_id=root;
 select k.page_id into choice from guanye_internal.cover_training_choices k where k.workspace_id=w and k.root_page_id=root order by k.id desc limit 1;
 return jsonb_build_object('rootPageId',root,'candidates',versions,'selectedPageId',choice,'enabled',
  exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=w and f.enabled));
end $$;

create function public.single_judge_cover_revise(w uuid,p_page uuid,p_record uuid,p_key uuid,p_instructions jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid:=guanye_internal.single_judge_creation_actor(w); c guanye_internal.cover_training_candidates%rowtype;
 p public.ai_generation_pages%rowtype; j public.ai_generation_jobs%rowtype;
 existing guanye_internal.cover_training_candidates%rowtype; feedback jsonb; next_id uuid:=gen_random_uuid();
 next_deck text; next_job uuid; next_page uuid; next_prompt text; instruction_key text;
begin
 if not exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=w and f.enabled) then
  raise exception '封面训练未启用' using errcode='42501'; end if;
 if p_key is null or p_record is null or jsonb_typeof(p_instructions) is distinct from 'object'
 or p_instructions-array['keep','change','doNotChange'] <> '{}'::jsonb then raise exception '改稿参数无效' using errcode='22023'; end if;
 foreach instruction_key in array array['keep','change','doNotChange'] loop
  if jsonb_typeof(p_instructions->instruction_key) is distinct from 'string' or length(btrim(p_instructions->>instruction_key)) not between 1 and 700 then
   raise exception '改稿约束无效' using errcode='22023'; end if;
 end loop;
 select * into c from guanye_internal.cover_training_candidates where workspace_id=w and page_id=p_page;
 if not found then raise exception '没有该封面候选' using errcode='42501'; end if;
 perform 1 from public.ai_generation_pages where id=c.root_page_id and workspace_id=w for update;
 select * into p from public.ai_generation_pages where workspace_id=w and id=p_page and status='succeeded';
 select * into j from public.ai_generation_jobs where id=p.job_id and workspace_id=w and user_id=actor;
 if not found then raise exception '只能改自己的已完成候选' using errcode='42501'; end if;
 select * into existing from guanye_internal.cover_training_candidates where root_page_id=c.root_page_id and request_key=p_key;
 if found then
  if existing.parent_page_id<>p_page or existing.review_record_id is distinct from p_record or existing.instructions is distinct from p_instructions then
   raise exception '请求标识已经用于另一份指令' using errcode='23505'; end if;
  return public.single_judge_cover_status(w,j.id);
 end if;
 if c.ordinal>=2 or exists(select 1 from guanye_internal.cover_training_candidates where root_page_id=c.root_page_id and ordinal>c.ordinal) then
  raise exception '只能从最新已完成版本改稿，每页最多改两版' using errcode='22023'; end if;
 feedback=guanye_internal.cover_training_feedback(w,p.id,actor);
 if feedback->>'recordId' is distinct from p_record::text then raise exception '先提交当前页人工分并取得对应 AI 评语' using errcode='42501'; end if;
 if public.ai_generation_select_reference_path(w,j.id,p.id) is distinct from c.reference_path then
  raise exception '原参考不再可用' using errcode='42501'; end if;
 next_deck='generated-'||left(replace(next_id::text,'-',''),24);
 -- Keep the confirmed snapshot and exact source reference, and use a new durable job (not a retry).
 select root.prompt into next_prompt from public.ai_generation_pages root where root.id=c.root_page_id;
 next_prompt=next_prompt||E'\n\n本次只重新生成本页。确认文案逐字不变，不复用参考图中文字。\n保留：'||(p_instructions->>'keep')
  ||E'\n修改：'||(p_instructions->>'change')||E'\n禁止改变：'||(p_instructions->>'doNotChange');
 insert into public.ai_generation_jobs(workspace_id,user_id,plan_id,deck_id,title,style,outline,reference_data,plan_snapshot,config)
 values(w,actor,next_id::text,next_deck,j.title,j.style,j.outline,j.reference_data,j.plan_snapshot,
  jsonb_build_object('workers',1,'coverTrainingRoot',c.root_page_id)) returning id into next_job;
 next_page=gen_random_uuid();
 insert into public.ai_generation_pages(id,job_id,workspace_id,position,plan_page_id,title,outline,description,prompt,reference_data,storage_path,content_type)
 values(next_page,next_job,w,1,p.plan_page_id,p.title,p.outline,jsonb_set(p.description,'{text}',to_jsonb(next_prompt)),next_prompt,
  p.reference_data,w::text||'/'||next_deck||'/'||next_page::text||'.png','image/png');
 insert into guanye_internal.cover_training_candidates values(next_page,w,c.root_page_id,p.id,c.ordinal+1,c.reference_deck_id,
  c.reference_path,p_key,p_instructions,p_record,c.style_version);
 return public.single_judge_cover_status(w,j.id);
end $$;

create function public.single_judge_cover_select(w uuid,p_page uuid,p_decision uuid,p_reason text,p_blind boolean) returns jsonb
language plpgsql security definer set search_path='' as $$
declare actor uuid:=guanye_internal.single_judge_creation_actor(w); c guanye_internal.cover_training_candidates%rowtype; j uuid; feedback jsonb;
begin
 if not exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=w and f.enabled) then
  raise exception '封面训练未启用' using errcode='42501'; end if;
 select * into c from guanye_internal.cover_training_candidates where workspace_id=w and page_id=p_page;
 feedback=guanye_internal.cover_training_feedback(w,p_page,actor);
 if c.page_id is null or p_decision is null or length(coalesce(p_reason,''))>700 or
    feedback->>'decisionId' is distinct from p_decision::text then
  raise exception '必须先正式确认该候选的逐页人工分' using errcode='42501'; end if;
 if coalesce((feedback#>>'{formalScores,match}')::numeric,0)<4 then
  raise exception '内容匹配分至少 4 分，且须人工核对后才可选用' using errcode='22023'; end if;
 select job_id into j from public.ai_generation_pages where id=p_page and workspace_id=w;
 perform 1 from public.ai_generation_pages where id=c.root_page_id and workspace_id=w for update;
 insert into guanye_internal.cover_training_choices(workspace_id,root_page_id,page_id,decision_id,actor,reason,blind)
 select w,c.root_page_id,p_page,p_decision,actor,btrim(coalesce(p_reason,'')),coalesce(p_blind,false)
 where not exists(select 1 from (select * from guanye_internal.cover_training_choices
  where root_page_id=c.root_page_id order by id desc limit 1) last
  where last.page_id=p_page and last.decision_id=p_decision and last.blind=coalesce(p_blind,false) and last.reason=btrim(coalesce(p_reason,'')));
 return public.single_judge_cover_status(w,j);
end $$;

revoke all on function public.single_judge_cover_start(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,text,text)
 from public,anon,authenticated,service_role;
grant execute on function public.single_judge_cover_start(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb,text,text) to authenticated;
revoke all on function public.ai_generation_select_reference_path_before_cover(uuid,uuid,uuid),
 public.ai_generation_select_reference_path(uuid,uuid,uuid),
 public.ai_generation_claim_pages(uuid,uuid,text,integer,integer),guanye_internal.cover_training_feedback(uuid,uuid,uuid),
 guanye_internal.cover_training_capture_root(), public.single_judge_cover_status(uuid,uuid),public.single_judge_cover_style(uuid),
 public.single_judge_cover_publish_style(uuid,text,bigint),public.single_judge_cover_revise(uuid,uuid,uuid,uuid,jsonb),
 public.single_judge_cover_select(uuid,uuid,uuid,text,boolean) from public,anon,authenticated,service_role;
grant execute on function public.ai_generation_claim_pages(uuid,uuid,text,integer,integer) to service_role;
grant execute on function public.single_judge_cover_status(uuid,uuid),public.single_judge_cover_style(uuid),
 public.single_judge_cover_publish_style(uuid,text,bigint),public.single_judge_cover_revise(uuid,uuid,uuid,uuid,jsonb),
 public.single_judge_cover_select(uuid,uuid,uuid,text,boolean) to authenticated;

-- Treat an unlabelled first preview as a cover proxy, never a labelled non-cover.
-- Legacy scores without explicit historical rubric/scale stay visible but cannot enter a formal split.
-- Isolated workspace operator curates source families before the first trial.
-- NULL explicitly excludes ambiguous provenance; an absent row uses the original digest.
create table guanye_internal.cover_training_source_review (
 workspace_id uuid not null references guanye_internal.cover_training_settings(workspace_id) on delete cascade,
 original_sha256 text not null check(original_sha256 ~ '^[0-9a-f]{64}$'),
 family_sha256 text check(family_sha256 ~ '^[0-9a-f]{64}$'),
 primary key(workspace_id,original_sha256)
);
revoke all on guanye_internal.cover_training_source_review from public,anon,authenticated,service_role;

create function guanye_internal.cover_training_inventory(w uuid,p_actor uuid,p_deck text default null)
returns table(id text, image_path text, source_group text, rubric text, human jsonb, eligible boolean,
              exclusion text, classification text, design_style text, original text, score_id uuid)
language sql stable security definer set search_path='' as $$
 with inventory as (
  select d.id, d.ai_allowed, coalesce(source_review.family_sha256,p.source_group_sha256) as source_group, r.rubric, r.score_id,
   coalesce(r.human,legacy.human) as human,
   coalesce(im->>'path',im->>'imagePath',im->>'storagePath') as image_path,
   coalesce(im->>'id',im->>'pageId','page-1') as unit_id,
   public.ai_review_effective_meta(w,d.id) as meta,
   case when im->>'pageType' is not null or im->>'narrativeRole' is not null then 'explicit' else 'first-preview-proxy' end as original,
   case when not d.ai_allowed then 'notAuthorized'
    when im is null or coalesce(im->>'pageType',im->>'narrativeRole','cover')<>'cover' then 'notCover'
    when p.source_group_sha256 is null then 'unknownSource'
    when source_review.original_sha256 is not null and source_review.family_sha256 is null then 'sourceReviewExcluded'
    when r.score_id is null and legacy.human is not null then 'legacyUnverifiedRubric'
    when r.score_id is null then 'noCurrentPageScore'
    when not guanye_internal.single_judge_generation_uploaded_reference_ok(w,d.id,coalesce(im->>'path',im->>'imagePath',im->>'storagePath'))
     then 'unavailablePreview'
    else null end as exclusion
  from public.decks d
  cross join lateral (select d.data#>'{images,0}' as im) f
  left join guanye_internal.uploaded_source_provenance p on p.deck_id=d.id and p.workspace_id=w
  left join guanye_internal.cover_training_source_review source_review
   on source_review.workspace_id=w and source_review.original_sha256=p.source_group_sha256
  left join lateral (
   select s.id as score_id,s.rubric_version as rubric,
    jsonb_build_object('visual',s.entry#>'{scores,visual}','layout',s.entry#>'{scores,layout}') as human
   from public.ai_review_submissions s
   where s.workspace_id=w and s.deck_id=d.id and s.source_kind='uploaded'
    and s.human_user_id=p_actor and s.scope='page' and s.unit_id=coalesce(im->>'id',im->>'pageId','page-1')
    and s.deck_version=public.ai_review_deck_version(w,d.id) and s.rubric_version=public.ai_review_rubric_version(w)
    and s.max_score=5 and public.ai_review_scores_complete(w,d.id,s.entry,5)
    and jsonb_typeof(s.entry#>'{scores,visual}')='number' and jsonb_typeof(s.entry#>'{scores,layout}')='number'
   order by s.created_at desc,s.id desc limit 1
  ) r on d.ai_allowed
  left join lateral (
   select v.data->'pages'->coalesce(im->>'id',im->>'pageId','page-1')->'scores' as human
   from public.reviews v where v.workspace_id=w and v.deck_id=d.id and v.user_id=p_actor
   limit 1
  ) legacy on d.ai_allowed
  where d.workspace_id=w and d.source_kind='uploaded' and (p_deck is null or d.id=p_deck)
 )
 select i.id,case when i.exclusion is null then i.image_path end,
  case when i.exclusion is distinct from 'sourceReviewExcluded' then i.source_group end,i.rubric,
  case when i.ai_allowed then i.human end,i.exclusion is null,i.exclusion,
  i.meta->>'effectiveClassification',i.meta->>'effectiveStyle',i.original,i.score_id
 from inventory i
$$;

create function public.single_judge_cover_inventory(w uuid,p_after text,p_limit integer) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare v_actor uuid:=guanye_internal.single_judge_creation_actor(w); result jsonb;
begin
 if not exists(select 1 from guanye_internal.cover_training_settings where workspace_id=w and enabled) then
  raise exception '封面训练未启用' using errcode='42501'; end if;
 select jsonb_build_object('rubricVersion',public.ai_review_rubric_version(w),'rows',coalesce(jsonb_agg(jsonb_build_object(
  'id',i.id,'scoreId',i.score_id,'sourceGroupSha256',i.source_group,'rubricVersion',i.rubric,'human',i.human,'scope','page',
  'pageType',case when i.exclusion is distinct from 'notCover' then 'cover' end,'aiAllowed',i.exclusion is distinct from 'notAuthorized',
  'maxScore',case when i.eligible then 5 end,'eligible',i.eligible,'exclusion',i.exclusion,'pageTypeEvidence',i.original,
  'pair',(select p.result||jsonb_build_object('pairId',p.id,'modelConfigId',p.model_config,'rubricVersion',i.rubric,
    'correctionId',c.id) from guanye_internal.cover_training_pairs p
   left join guanye_internal.cover_training_corrections c on c.pair_id=p.id
   where i.eligible and p.workspace_id=w and p.actor=v_actor and p.deck_id=i.id
     and p.snapshot->>'scoreId'=i.score_id::text order by p.created_at desc,p.id limit 1)
 ) order by i.id),'[]'::jsonb),'nextAfter',case when count(*)=p_limit then max(i.id) end) into result
 from (select * from guanye_internal.cover_training_inventory(w,v_actor) i where i.id>coalesce(p_after,'')
       order by i.id limit greatest(1,least(200,p_limit))) i;
 return result;
end $$;

create table guanye_internal.cover_training_pairs (
 id uuid primary key default gen_random_uuid(),
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 deck_id text not null,
 actor uuid not null references auth.users(id),
 model_config text not null,
 snapshot jsonb not null,
 result jsonb not null,
 created_at timestamptz not null default now()
);
create index cover_training_pairs_recent on guanye_internal.cover_training_pairs(workspace_id,actor,deck_id,created_at desc);
create function guanye_internal.cover_training_lock_source_review() returns trigger
language plpgsql security definer set search_path='' as $$
declare w uuid:=case when tg_op='DELETE' then old.workspace_id else new.workspace_id end;
begin
 if tg_op='UPDATE' and new.workspace_id<>old.workspace_id then
  raise exception '不能移动来源归组记录' using errcode='22023'; end if;
 perform 1 from guanye_internal.cover_training_settings f where f.workspace_id=w for update;
 -- A workspace teardown may cascade after removing the parent settings row.
 if found and exists(select 1 from guanye_internal.cover_training_pairs p where p.workspace_id=w) then
  raise exception '首次对照后不能更改来源分组；请新建隔离工作区' using errcode='22023'; end if;
 if tg_op='DELETE' then return old; end if;
 return new;
end $$;
create trigger cover_training_lock_source_review before insert or update or delete
 on guanye_internal.cover_training_source_review for each row execute function guanye_internal.cover_training_lock_source_review();
revoke all on function guanye_internal.cover_training_lock_source_review() from public,anon,authenticated,service_role;

create table guanye_internal.cover_training_corrections (
 id uuid primary key default gen_random_uuid(),
 pair_id uuid not null unique references guanye_internal.cover_training_pairs(id) on delete cascade,
 reason text not null check(length(reason)<=700),
 created_at timestamptz not null default now()
);
revoke all on guanye_internal.cover_training_pairs,guanye_internal.cover_training_corrections from public,anon,authenticated,service_role;

create function public.single_judge_cover_pair_context(w uuid,p_deck text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare v_actor uuid:=guanye_internal.single_judge_creation_actor(w); target record; examples jsonb; rules jsonb;
begin
 select jsonb_build_object('version',s.version,'markdown',s.markdown) into rules
 from guanye_internal.cover_training_settings f left join guanye_internal.cover_training_styles s
  on s.workspace_id=f.workspace_id and s.version=f.style_version where f.workspace_id=w and f.enabled;
 if not found then raise exception '封面训练未启用' using errcode='42501'; end if;
 select * into target from guanye_internal.cover_training_inventory(w,v_actor,p_deck) i where i.eligible;
 if not found then raise exception '封面不具备正式对照资格' using errcode='22023'; end if;
 select coalesce(jsonb_agg(to_jsonb(ch) order by ch.rank),'[]'::jsonb) into examples from (
  select distinct_cases.*,row_number() over(order by same_class desc,same_style desc,("correctionId" is not null) desc,id) as rank
  from (
   select distinct on(i.source_group) i.id,i.image_path as "imagePath",i.human,i.score_id as "scoreId",
    x.id as "correctionId",x.reason,x.baseline as "previousAI",
    coalesce(i.classification=target.classification,false) as same_class,coalesce(i.design_style=target.design_style,false) as same_style
   from guanye_internal.cover_training_inventory(w,v_actor) i
   left join lateral (
    select c.id,c.reason,p.result->'assisted' as baseline from guanye_internal.cover_training_corrections c
    join guanye_internal.cover_training_pairs p on p.id=c.pair_id and p.workspace_id=w and p.actor=v_actor and p.deck_id=i.id
    where p.snapshot->>'scoreId'=i.score_id::text and p.snapshot->>'rubricVersion'=i.rubric
    order by c.created_at desc,c.id desc limit 1
   ) x on true
   where i.eligible and i.source_group<>target.source_group and i.rubric=target.rubric
    and get_byte(decode(i.source_group,'hex'),0)%5<>0
   order by i.source_group,same_class desc,same_style desc,(x.id is not null) desc,i.id
  ) distinct_cases order by rank limit 3
 ) ch;
 return jsonb_build_object('id',target.id,'imagePath',target.image_path,'sourceGroupSha256',target.source_group,
  'scoreId',target.score_id,'rubricVersion',target.rubric,'human',target.human,'cases',examples,'rules',rules);
end $$;

create function public.single_judge_cover_pair_save(w uuid,p_deck text,p_context jsonb,p_model text,p_result jsonb) returns uuid
language plpgsql security definer set search_path='' as $$
declare v_actor uuid:=guanye_internal.single_judge_creation_actor(w); result_id uuid;
begin
 perform 1 from guanye_internal.cover_training_settings f where f.workspace_id=w for share;
 if p_model is distinct from 'gpt-6.2-sol:cover-rubric-v1' or p_result is null or octet_length(p_result::text)>16000
  or public.single_judge_cover_pair_context(w,p_deck) is distinct from p_context then
  raise exception '当前评分、授权、校准案例或规则版本已变化，不能入账' using errcode='22023'; end if;
 insert into guanye_internal.cover_training_pairs(workspace_id,deck_id,actor,model_config,snapshot,result)
 values(w,p_deck,v_actor,p_model,p_context,p_result) returning id into result_id;
 return result_id;
end $$;

create function public.single_judge_cover_correct(w uuid,p_pair uuid,p_reason text) returns uuid
language plpgsql security definer set search_path='' as $$
declare v_actor uuid:=guanye_internal.single_judge_creation_actor(w); pair guanye_internal.cover_training_pairs%rowtype; ctx jsonb; result_id uuid;
begin
 select * into pair from guanye_internal.cover_training_pairs where id=p_pair and workspace_id=w and actor=v_actor;
 if not found then raise exception '找不到本人对照记录' using errcode='42501'; end if;
 ctx=public.single_judge_cover_pair_context(w,pair.deck_id);
 if get_byte(decode(ctx->>'sourceGroupSha256','hex'),0)%5=0 or ctx->>'scoreId' is distinct from pair.snapshot->>'scoreId'
   or length(coalesce(p_reason,''))>700 then raise exception '仅当前口径校准组可确认纠错，留出组永不回流' using errcode='22023'; end if;
 insert into guanye_internal.cover_training_corrections(pair_id,reason) values(pair.id,btrim(coalesce(p_reason,'')))
 on conflict do nothing returning id into result_id;
 if result_id is null then select id into result_id from guanye_internal.cover_training_corrections where pair_id=pair.id; end if;
 return result_id;
end $$;
revoke all on function guanye_internal.cover_training_inventory(uuid,uuid,text),public.single_judge_cover_inventory(uuid,text,integer),
 public.single_judge_cover_pair_context(uuid,text),public.single_judge_cover_pair_save(uuid,text,jsonb,text,jsonb),
 public.single_judge_cover_correct(uuid,uuid,text) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_cover_inventory(uuid,text,integer),public.single_judge_cover_pair_context(uuid,text),
 public.single_judge_cover_pair_save(uuid,text,jsonb,text,jsonb),public.single_judge_cover_correct(uuid,uuid,text) to authenticated;
-- A pilot never re-enters the native retry queue, including through the old browser endpoint.
alter function public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb) rename to ai_generation_retry_pages_before_cover;
create function public.ai_generation_retry_pages(p_workspace uuid,p_actor uuid,p_job uuid,p_page_ids jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
begin
 if exists(select 1 from guanye_internal.cover_training_candidates c join public.ai_generation_pages p on p.id=c.page_id
   where c.workspace_id=p_workspace and p.job_id=p_job) then
  raise exception '封面训练任务不允许重发，请查看已有候选及渠道状态' using errcode='42501'; end if;
 return public.ai_generation_retry_pages_before_cover(p_workspace,p_actor,p_job,p_page_ids);
end $$;
revoke all on function public.ai_generation_retry_pages_before_cover(uuid,uuid,uuid,jsonb),
 public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb) from public,anon,authenticated,service_role;
grant execute on function public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb) to authenticated,service_role;

alter function public.single_judge_admit(uuid,uuid,uuid) rename to single_judge_admit_before_cover;
create function public.single_judge_admit(w uuid,p_submission uuid,p_decision uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare v_actor uuid:=guanye_internal.single_judge_creation_actor(w);
 c guanye_internal.cover_training_candidates%rowtype; chosen record;
begin
 select candidate.* into c from guanye_internal.cover_training_candidates candidate
 join public.ai_generation_pages p on p.id=candidate.page_id
 join public.ai_generation_jobs j on j.id=p.job_id and j.user_id=v_actor and j.workspace_id=w
 join public.ai_review_submissions s on s.id=p_submission and s.workspace_id=w and s.deck_id=j.deck_id
 where candidate.workspace_id=w;
 if found then
  perform 1 from public.ai_generation_pages where id=c.root_page_id and workspace_id=w for update;
  select k.page_id,k.decision_id into chosen from guanye_internal.cover_training_choices k
   where k.root_page_id=c.root_page_id and k.workspace_id=w order by k.id desc limit 1;
  if not found or chosen.page_id is distinct from c.page_id or chosen.decision_id is distinct from p_decision then
   raise exception '请先在封面训练面板核对内容并正式选用此版' using errcode='42501'; end if;
 end if;
 return public.single_judge_admit_before_cover(w,p_submission,p_decision);
end $$;
revoke all on function public.single_judge_admit_before_cover(uuid,uuid,uuid),public.single_judge_admit(uuid,uuid,uuid)
 from public,anon,authenticated,service_role;
grant execute on function public.single_judge_admit(uuid,uuid,uuid) to authenticated;
commit;
