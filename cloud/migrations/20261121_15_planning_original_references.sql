-- Planning references: uploaded originals only, with formal whole-deck scores overriding team means.
begin;

create or replace function public.ai_planning_reference_candidates(
 w uuid, max_items integer default 12
) returns jsonb
language sql stable security definer set search_path='' as $$
-- PLANNING_ORIGINAL_REFERENCES_V15
 with dimensions as (
  select ws.id as workspace_id,dim.value as dimension
  from public.workspaces ws
  cross join lateral jsonb_array_elements(coalesce(ws.settings->'dimensions','[]'::jsonb)) as dim(value)
  where ws.id=w
 ), score_rows as (
  select r.workspace_id,r.deck_id,r.user_id,
   case when coalesce(r.data->>'mode','deck')='page' then im->>'id' else '__deck__' end as unit_id,
   dm.dimension->>'id' as dimension_id,
   case when coalesce(r.data->>'mode','deck')='page' then
    case when jsonb_typeof(r.data#>array['pages',im->>'id','scores',dm.dimension->>'id'])='number'
     then (r.data#>array['pages',im->>'id','scores',dm.dimension->>'id'])::text::numeric else null end
   else
    case when jsonb_typeof(r.data#>array['scores',dm.dimension->>'id'])='number'
     then (r.data#>array['scores',dm.dimension->>'id'])::text::numeric else null end
   end as score
  from public.reviews r
  join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id and d.source_kind='uploaded'
  join dimensions dm on dm.workspace_id=r.workspace_id
  cross join lateral jsonb_array_elements(
   case when coalesce(r.data->>'mode','deck')='page'
    then case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end
    else '[{}]'::jsonb end
  ) im
 ), review_totals as (
  select deck_id,user_id,count(*)::integer as expected,count(score)::integer as scored,
   avg(score) as mean
  from score_rows
  group by deck_id,user_id
 ), complete_reviews as (
  select deck_id,user_id,mean from review_totals where expected>0 and scored=expected
 ), team_totals as (
  select deck_id,count(*)::integer as complete_review_count,round(avg(mean),2) as score
  from complete_reviews
  group by deck_id
 ), formal_decks as (
  select deck_id,max(score) as score
  from public.ai_review_uploaded_formal_scores(w)
  where scope='deck'
  group by deck_id
 ), uploaded_decks as (
  select d.id as deck_id,d.data,
   (get_byte(decode(md5(d.id),'hex'),0)%5)::integer as bucket
  from public.decks d
  where d.workspace_id=w and d.source_kind='uploaded'
 ), eligible as (
  select u.deck_id,u.data,u.bucket,
   coalesce(t.complete_review_count,0) as complete_review_count,
   case when f.deck_id is not null then f.score else t.score end as score,
   case when f.deck_id is not null then 'formal' else 'team' end as score_source
  from uploaded_decks u
  left join team_totals t on t.deck_id=u.deck_id
  left join formal_decks f on f.deck_id=u.deck_id
  where u.bucket<>0 and (f.deck_id is not null or t.deck_id is not null)
 ), ranked as (
  select * from eligible
  order by score desc nulls last,complete_review_count desc,deck_id
  limit least(greatest(coalesce(max_items,12),1),20)
 )
 select coalesce(jsonb_agg(jsonb_build_object(
  'deckId',deck_id,
  'title',coalesce(nullif(data->>'title',''),nullif(data->>'name',''),deck_id),
  'style',coalesce(data->>'style',''),
  'score',score,
  'completeReviewCount',complete_review_count,
  'pageCount',jsonb_array_length(case when jsonb_typeof(data->'images')='array' then data->'images' else '[]'::jsonb end),
  'bucket',bucket,
  'split','calibration',
  'sourceKind','uploaded',
  'scoreSource',score_source,
  'imagePaths',coalesce((
   select jsonb_agg(path order by page_number)
   from (
    select im.value->>'path' as path,im.page_number
    from jsonb_array_elements(case when jsonb_typeof(data->'images')='array' then data->'images' else '[]'::jsonb end)
     with ordinality as im(value,page_number)
    where nullif(im.value->>'path','') is not null
     and left(coalesce(im.value->>'path',''),length(w::text)+1)=w::text||'/'
     and strpos(coalesce(im.value->>'path',''),'..')=0
     and strpos(coalesce(im.value->>'path',''),chr(92))=0
    order by im.page_number
    limit 2
   ) paths
  ),'[]'::jsonb)
 ) order by score desc nulls last,complete_review_count desc,deck_id),'[]'::jsonb)
 from ranked;
$$;

revoke all on function public.ai_planning_reference_candidates(uuid,integer) from public,anon,authenticated;
grant execute on function public.ai_planning_reference_candidates(uuid,integer) to service_role;

commit;