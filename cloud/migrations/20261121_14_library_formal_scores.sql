-- Formal library scores: dedicated-judge uploads plus admitted generated entries.
begin;

create or replace function public.ai_review_uploaded_formal_scores(w uuid)
returns table(deck_id text,scope text,unit_id text,score numeric)
language sql stable security definer set search_path='' as $$
 with workspace_scale as (
   select coalesce((ws.settings->>'maxScore')::numeric,5) as max_score
   from public.workspaces ws where ws.id=w
 ), current_pages as (
   select distinct d.id as deck_id,image->>'id' as unit_id
   from public.decks d
   cross join lateral jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end) image
   where d.workspace_id=w
     and nullif(image->>'id','') is not null
     and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
     and strpos(coalesce(image->>'path',''),'..')=0
     and strpos(coalesce(image->>'path',''),chr(92))=0
 ), ranked as (
   -- LIBRARY_FORMAL_SQL_V14
   select distinct on (x.deck_id,s.scope,s.unit_id)
     x.deck_id,s.scope,s.unit_id,x.formal_total as score
   from public.workspaces ws
   join public.decks d on d.workspace_id=ws.id
   join public.ai_review_submissions s on s.workspace_id=ws.id and s.deck_id=d.id
   join public.ai_review_decisions x on x.workspace_id=ws.id and x.deck_id=d.id and x.submission_id=s.id
   cross join workspace_scale scale
   where ws.id=w
     and ws.dedicated_judge_id is not null
     and d.source_kind='uploaded'
     and x.judge_user_id=ws.dedicated_judge_id
     and x.status in ('confirmed','corrected')
     and x.formal_total is not null
     and x.deck_version=public.ai_review_deck_version(w,d.id)
     and x.rubric_version=public.ai_review_rubric_version(w)
     and x.max_score=scale.max_score
     and public.ai_review_scores_complete(w,d.id,jsonb_build_object('scores',x.formal_scores),x.max_score)
     and (s.scope='deck' or exists(
       select 1 from current_pages p where p.deck_id=d.id and p.unit_id=s.unit_id
     ))
   order by x.deck_id,s.scope,s.unit_id,x.created_at desc,x.id desc
 )
 select ranked.deck_id,ranked.scope,ranked.unit_id,ranked.score from ranked
$$;
revoke all on function public.ai_review_uploaded_formal_scores(uuid) from public,anon,authenticated;
grant execute on function public.ai_review_uploaded_formal_scores(uuid) to service_role;

create or replace function public.review_score_summary(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 -- LIBRARY_FORMAL_SQL_V14
 with dims as (
   select dim->>'id' as id from public.workspaces ws cross join lateral jsonb_array_elements(ws.settings->'dimensions') dim
   where ws.id=w and public.is_member(w)
 ), personal as (
   select r.deck_id,r.user_id,avg(case when jsonb_typeof(v.score)='number' then (v.score::text)::numeric end) as mean
   from public.reviews r join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id
   cross join dims
   cross join lateral jsonb_array_elements(case when r.data->>'mode'='page' then d.data->'images' else '[{}]'::jsonb end) im
   cross join lateral (select case when r.data->>'mode'='page' then r.data#>array['pages',im->>'id','scores',dims.id] else r.data#>array['scores',dims.id] end as score) v
   where r.workspace_id=w and public.ai_review_review_allowed(w,r.deck_id,r.data)
   group by r.deck_id,r.user_id having count(*)=count(*) filter(where jsonb_typeof(v.score)='number')
 ), totals as (select deck_id,count(*) as n,avg(mean) as mean from personal group by deck_id),
 workspace_scale as (
   select coalesce((ws.settings->>'maxScore')::numeric,5) as max_score
   from public.workspaces ws where ws.id=w and public.is_member(w)
 ), current_pages as (
   select distinct d.id as deck_id,image->>'id' as unit_id
   from public.decks d
   cross join lateral jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end) image
   where d.workspace_id=w and public.is_member(w)
     and nullif(image->>'id','') is not null
     and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
     and strpos(coalesce(image->>'path',''),'..')=0
     and strpos(coalesce(image->>'path',''),chr(92))=0
 ), uploaded_formal as (
   select * from public.ai_review_uploaded_formal_scores(w) where public.is_member(w)
 ), generated_formal as (
   select distinct on (e.deck_id,e.scope,e.unit_id)
     e.deck_id,e.scope,e.unit_id,
     round(x.formal_total/nullif(x.max_score,0)*scale.max_score,2) as score
   from public.ai_generated_entries e
   join public.decks d on d.workspace_id=e.workspace_id and d.id=e.deck_id
   join public.ai_review_decisions x on x.workspace_id=e.workspace_id and x.deck_id=e.deck_id and x.id=e.decision_id
   cross join workspace_scale scale
   where e.workspace_id=w and public.is_member(w)
     and d.source_kind='generated'
     and e.library_visible
     and x.status in ('confirmed','corrected')
     and x.formal_total is not null
     and x.max_score is not null and x.max_score>=1
     and (
       (e.scope='deck' and public.ai_review_deck_library_visible(w,e.deck_id))
       or (e.scope='page' and public.ai_review_page_library_visible(w,e.deck_id,e.unit_id)
         and exists(select 1 from current_pages p where p.deck_id=e.deck_id and p.unit_id=e.unit_id))
     )
   order by e.deck_id,e.scope,e.unit_id,e.created_at desc,e.id desc
 ), formal_scores as (
   select deck_id,
     max(score) filter (where scope='deck') as formal_score,
     jsonb_object_agg(unit_id,score) filter (where scope='page') as formal_page_scores
   from (select * from uploaded_formal union all select * from generated_formal) formal
   group by deck_id
 ), deck_rows as (
   select t.deck_id,t.n as count,t.mean,f.formal_score,f.formal_page_scores
   from totals t left join formal_scores f on f.deck_id=t.deck_id
   union
   select f.deck_id,0::bigint as count,null::numeric as mean,f.formal_score,f.formal_page_scores
   from formal_scores f where not exists(select 1 from totals t where t.deck_id=f.deck_id)
 )
 select case when public.is_member(w) then coalesce(jsonb_object_agg(deck_id,
   jsonb_build_object('count',count,'mean',mean)
   || case when formal_score is not null then jsonb_build_object('formalScore',formal_score) else '{}'::jsonb end
   || case when formal_page_scores is not null then jsonb_build_object('formalPageScores',formal_page_scores) else '{}'::jsonb end
 ),'{}'::jsonb) else '{}'::jsonb end
 from deck_rows
$$;
revoke all on function public.review_score_summary(uuid) from public,anon;
grant execute on function public.review_score_summary(uuid) to authenticated;

commit;
