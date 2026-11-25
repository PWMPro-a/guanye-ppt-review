-- Exact, page-aware PPT training references; no rank cutoff.
begin;

create function guanye_internal.planning_reference_candidates(
 w uuid, max_items integer, p_exact_deck_id text
) returns jsonb
language sql stable security definer set search_path='' as $$
-- PLANNING_ORIGINAL_REFERENCES_V15
-- SINGLE_JUDGE_AI_ONLY_PLANNING_M24
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
  else case when jsonb_typeof(r.data#>array['scores',dm.dimension->>'id'])='number'
    then (r.data#>array['scores',dm.dimension->>'id'])::text::numeric else null end end as score
 from public.reviews r
 join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id and d.source_kind='uploaded' and d.ai_allowed and (p_exact_deck_id is null or d.id=p_exact_deck_id)
 join dimensions dm on dm.workspace_id=r.workspace_id
 cross join lateral jsonb_array_elements(
  case when coalesce(r.data->>'mode','deck')='page'
   then case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end
   else '[{}]'::jsonb end
 ) im
), review_totals as (
 select deck_id,user_id,count(*)::integer as expected,count(score)::integer as scored,avg(score) as mean
 from score_rows group by deck_id,user_id
), complete_reviews as (
 select deck_id,user_id,mean from review_totals where expected>0 and scored=expected
), team_totals as (
 select deck_id,count(*)::integer as complete_review_count,round(avg(mean),2) as score
 from complete_reviews group by deck_id
), formal_decks as (
 select deck_id,max(score) as score
 from public.ai_review_uploaded_formal_scores(w) where scope='deck' and (p_exact_deck_id is null or deck_id=p_exact_deck_id) group by deck_id
), uploaded_decks as (
 select d.id as deck_id,d.data,
  guanye_internal.ai_review_source_group_bucket(w,d.id,'uploaded') as bucket
 from public.decks d
 where d.workspace_id=w and d.source_kind='uploaded' and d.ai_allowed and (p_exact_deck_id is null or d.id=p_exact_deck_id)
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
 'bucket',bucket,'split','calibration','sourceKind','uploaded','scoreSource',score_source,
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
   order by im.page_number limit 2
  ) paths
 ),'[]'::jsonb)
) order by score desc nulls last,complete_review_count desc,deck_id),'[]'::jsonb)
from ranked;
$$;

revoke all on function guanye_internal.planning_reference_candidates(uuid,integer,text) from public,anon,authenticated,service_role;

-- Keep the legacy top-N contract unchanged; only the pilot exact lookup skips ranking.
create or replace function public.ai_planning_reference_candidates(w uuid,max_items integer default 12)
returns jsonb language sql stable security definer set search_path='' as $$
 select guanye_internal.planning_reference_candidates(w,max_items,null);
$$;

create function public.single_judge_ppt_training_reference(w uuid,p_deck_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare candidate jsonb; family_sha text; family_bucket integer;
begin
 perform guanye_internal.single_judge_creation_actor(w);
 if not exists(select 1 from guanye_internal.cover_training_settings f where f.workspace_id=w and f.enabled) then
  raise exception 'PPT 训练未启用' using errcode='42501'; end if;
 if p_deck_id is null or p_deck_id !~ '^[A-Za-z0-9_-]{1,100}$' then
  raise exception '参考作品标识无效' using errcode='22023'; end if;
 candidate:=guanye_internal.planning_reference_candidates(w,1,p_deck_id)->0;
 if candidate is null or (candidate->>'score')::numeric not between 4 and 5
    or (candidate->>'pageCount')::integer < 2 then return null; end if;
 -- Reject ungrouped material and honor manually reviewed aliases and exclusions.
 select case when r.original_sha256 is not null then r.family_sha256 else p.source_group_sha256 end
  into family_sha from guanye_internal.uploaded_source_provenance p
  left join guanye_internal.cover_training_source_review r on r.workspace_id=w and r.original_sha256=p.source_group_sha256
  where p.workspace_id=w and p.deck_id=p_deck_id;
 if family_sha is null then return null; end if;
 family_bucket:=mod(get_byte(decode(family_sha,'hex'),0),5);
 if family_bucket=0 then return null; end if;
 return (select jsonb_build_object('deckId',p_deck_id,'bucket',family_bucket,'split','calibration',
   'pageCount',candidate->'pageCount','scoreSource',candidate->'scoreSource',
   'authorizedPagePaths',coalesce(jsonb_agg(case when paths.path ~* '\.(png|jpe?g|webp)$'
     and guanye_internal.single_judge_generation_uploaded_reference_ok(w,p_deck_id,paths.path)
     then paths.path else null end order by im.ord),'[]'::jsonb))
  from public.decks d cross join lateral jsonb_array_elements(d.data->'images') with ordinality im(img,ord)
  cross join lateral (select coalesce(nullif(im.img->>'path',''),nullif(im.img->>'imagePath',''),nullif(im.img->>'storagePath','')) as path) paths
  where d.workspace_id=w and d.id=p_deck_id);
end $$;
revoke all on function public.single_judge_ppt_training_reference(uuid,text) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_ppt_training_reference(uuid,text) to authenticated;
commit;
