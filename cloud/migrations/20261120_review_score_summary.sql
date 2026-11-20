-- Average complete reviews per person first, so a page-by-page review has one vote.
create or replace function public.review_score_summary(w uuid) returns jsonb
language sql stable security definer set search_path='' as $$
 with dims as (
   select dim->>'id' as id from public.workspaces ws cross join lateral jsonb_array_elements(ws.settings->'dimensions') dim
   where ws.id=w and public.is_member(w)
 ), personal as (
   select r.deck_id,r.user_id,avg(case when jsonb_typeof(v.score)='number' then (v.score::text)::numeric end) as mean
   from public.reviews r join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id
   cross join dims
   cross join lateral jsonb_array_elements(case when r.data->>'mode'='page' then d.data->'images' else '[{}]'::jsonb end) im
   cross join lateral (select case when r.data->>'mode'='page' then r.data#>array['pages',im->>'id','scores',dims.id] else r.data#>array['scores',dims.id] end as score) v
   where r.workspace_id=w
   group by r.deck_id,r.user_id having count(*)=count(*) filter(where jsonb_typeof(v.score)='number')
 ), totals as (select deck_id,count(*) as n,avg(mean) as mean from personal group by deck_id)
 select coalesce(jsonb_object_agg(deck_id,jsonb_build_object('count',n,'mean',mean)),'{}'::jsonb) from totals
$$;
revoke all on function public.review_score_summary(uuid) from public,anon;
grant execute on function public.review_score_summary(uuid) to authenticated;
