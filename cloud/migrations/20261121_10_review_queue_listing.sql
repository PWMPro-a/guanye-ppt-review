-- Exact queue listing in the database so PostgREST row limits cannot truncate counts or pages.
begin;

create index if not exists ai_review_queue_decks_page_idx
 on public.decks(workspace_id,source_kind,id);

create index if not exists ai_review_queue_jobs_all_order_idx
 on public.ai_review_jobs(workspace_id,created_at,id);

create index if not exists ai_review_queue_jobs_kind_order_idx
 on public.ai_review_jobs(workspace_id,source_kind,created_at,id);

create or replace function public.ai_review_queue_decks(
 w uuid,
 p_kind text,
 p_after text,
 p_limit integer
) returns jsonb
language plpgsql
stable
security invoker
set search_path=''
as $$
begin
 if w is null then raise exception '工作区不能为空'; end if;
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;
 if p_limit is null or p_limit<1 or p_limit>50 then raise exception '分页大小无效'; end if;
 -- Cursors are opaque deck ids: reject only the empty string and compare all other text verbatim.
 if p_after='' then raise exception '分页游标无效'; end if;

 return (
  with candidates as (
   select
    d.id,
    coalesce(nullif(d.data->>'title',''),nullif(d.data->>'name',''),d.id) as title,
    coalesce(nullif(d.data->>'name',''),d.id) as deck_name,
    d.source_kind
   from public.decks d
   where d.workspace_id=w
     and d.source_kind=p_kind
     and (p_after is null or d.id>p_after)
   order by d.id
   limit p_limit+1
  ),
  page as (
   select * from candidates order by id limit p_limit
  )
  select jsonb_build_object(
   'items',coalesce((
    select jsonb_agg(jsonb_build_object(
     'id',p.id,
     'title',p.title,
     'name',p.deck_name,
     'sourceKind',p.source_kind
    ) order by p.id)
    from page p
   ),'[]'::jsonb),
   'nextCursor',case
    when (select count(*) from candidates)>p_limit then (select p.id from page p order by p.id desc limit 1)
    else null
   end
  )
 );
end $$;

create or replace function public.ai_review_queue_counts(w uuid)
returns jsonb
language plpgsql
stable
security invoker
set search_path=''
as $$
declare result jsonb;
begin
 if w is null then raise exception '工作区不能为空'; end if;

 select jsonb_build_object(
  'uploaded',jsonb_build_object(
   'queued',count(*) filter (where source_kind='uploaded' and status='queued'),
   'running',count(*) filter (where source_kind='uploaded' and status='running'),
   'completed',count(*) filter (where source_kind='uploaded' and status='completed'),
   'failed',count(*) filter (where source_kind='uploaded' and status='failed')
  ),
  'generated',jsonb_build_object(
   'queued',count(*) filter (where source_kind='generated' and status='queued'),
   'running',count(*) filter (where source_kind='generated' and status='running'),
   'completed',count(*) filter (where source_kind='generated' and status='completed'),
   'failed',count(*) filter (where source_kind='generated' and status='failed')
  )
 ) into result
 from public.ai_review_jobs
 where workspace_id=w;
 return result;
end $$;

create or replace function public.ai_review_queue_jobs(w uuid,p_kind text)
returns jsonb
language plpgsql
stable
security invoker
set search_path=''
as $$
begin
 if w is null then raise exception '工作区不能为空'; end if;
 if p_kind is not null and p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;

 return coalesce((
  select jsonb_agg(jsonb_build_object(
   'id',j.id,
   'deck_id',j.deck_id,
   'source_kind',j.source_kind,
   'scope',j.scope,
   'unit_id',j.unit_id,
   'position',j.position,
   'variant',j.variant,
   'status',j.status,
   'attempts',j.attempts,
   'bucket',j.bucket,
   'split',j.split,
   'deck_version',j.deck_version,
   'rubric_version',j.rubric_version,
   'error',j.error
  ) order by j.created_at,j.id)
  from (
   select
    j.id,j.deck_id,j.source_kind,j.scope,j.unit_id,j.position,j.variant,j.status,
    j.attempts,j.bucket,j.split,j.deck_version,j.rubric_version,j.error,j.created_at
   from public.ai_review_jobs j
   where j.workspace_id=w
     and (p_kind is null or j.source_kind=p_kind)
   order by j.created_at,j.id
   limit 50
  ) j
 ),'[]'::jsonb);
end $$;

revoke all on function public.ai_review_queue_decks(uuid,text,text,integer) from public,anon,authenticated;
revoke all on function public.ai_review_queue_counts(uuid) from public,anon,authenticated;
revoke all on function public.ai_review_queue_jobs(uuid,text) from public,anon,authenticated;
grant execute on function public.ai_review_queue_decks(uuid,text,text,integer) to service_role;
grant execute on function public.ai_review_queue_counts(uuid) to service_role;
grant execute on function public.ai_review_queue_jobs(uuid,text) to service_role;

commit;