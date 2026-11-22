-- UPLOADED_SOURCE_GROUPS_V20
-- Route known uploaded sources by their M19 SHA256 group; unknown decks and the
-- generated track keep the existing MD5(deck id) first-byte five-bucket policy.
-- Group semantics follow scikit-learn GroupShuffleSplit at fixed commit
-- f85f0584d5d7a66068bfe50b395ac855b2289dc2: splits operate on groups, not
-- individual samples.
begin;

-- Fence writers while we compare the new source-group route with existing queue
-- and record rows. The locks are held until the function replacements commit.
lock table public.decks, guanye_internal.uploaded_source_provenance,
 public.ai_review_jobs, public.ai_review_records
 in share row exclusive mode;

grant usage on schema guanye_internal to service_role;

create or replace function guanye_internal.ai_review_source_group_key(
 w uuid,
 target_id text,
 p_source_kind text
) returns text
language sql stable security definer set search_path=''
as $m20_key$
 select case
  when p_source_kind='uploaded' and target_id is not null then
   coalesce((
    select 'source:'||p.source_group_sha256
    from guanye_internal.uploaded_source_provenance p
    join public.decks d
      on d.id=p.deck_id and d.workspace_id=p.workspace_id and d.source_kind='uploaded'
    where p.workspace_id=w and p.deck_id=target_id
    limit 1
   ),'deck:'||target_id)
  else 'deck:'||target_id
 end
$m20_key$;

create or replace function guanye_internal.ai_review_source_group_bucket(
 w uuid,
 target_id text,
 p_source_kind text
) returns integer
language sql stable security definer set search_path=''
as $m20_bucket$
 select case
  when p_source_kind='uploaded' then
   coalesce((
    select mod(get_byte(decode(p.source_group_sha256,'hex'),0),5)::integer
    from guanye_internal.uploaded_source_provenance p
    join public.decks d
      on d.id=p.deck_id and d.workspace_id=p.workspace_id and d.source_kind='uploaded'
    where p.workspace_id=w and p.deck_id=target_id
    limit 1
   ),mod(get_byte(decode(md5(target_id),'hex'),0),5)::integer)
  else mod(get_byte(decode(md5(target_id),'hex'),0),5)::integer
 end
$m20_bucket$;

create or replace function guanye_internal.ai_review_has_source_groups(w uuid)
returns boolean
language sql stable security definer set search_path=''
as $m20_known$
 select exists(
  select 1 from guanye_internal.uploaded_source_provenance p
  where p.workspace_id=w
 )
$m20_known$;

revoke all on function guanye_internal.ai_review_source_group_key(uuid,text,text)
 from public,anon,authenticated,service_role;
revoke all on function guanye_internal.ai_review_source_group_bucket(uuid,text,text)
 from public,anon,authenticated;
revoke all on function guanye_internal.ai_review_has_source_groups(uuid)
 from public,anon,authenticated;
grant execute on function guanye_internal.ai_review_source_group_bucket(uuid,text,text)
 to service_role;
grant execute on function guanye_internal.ai_review_has_source_groups(uuid)
 to service_role;

do $m20_guard$
begin
 if exists(
  select 1
  from public.ai_review_jobs j
  join guanye_internal.uploaded_source_provenance p
    on p.workspace_id=j.workspace_id and p.deck_id=j.deck_id
  join public.decks d
    on d.id=j.deck_id and d.workspace_id=j.workspace_id and d.source_kind='uploaded'
  where j.source_kind='uploaded'
   and (
    j.bucket is distinct from guanye_internal.ai_review_source_group_bucket(
     j.workspace_id,j.deck_id,'uploaded'
    )
    or j.split is distinct from case
     when guanye_internal.ai_review_source_group_bucket(j.workspace_id,j.deck_id,'uploaded')=0
      then 'holdout' else 'calibration' end
   )
 ) then
  raise exception 'M20 来源分桶冲突：ai_review_jobs 仍含 M19 来源的旧算法结果';
 end if;
 if exists(
  select 1
  from public.ai_review_records r
  join guanye_internal.uploaded_source_provenance p
    on p.workspace_id=r.workspace_id and p.deck_id=r.deck_id
  join public.decks d
    on d.id=r.deck_id and d.workspace_id=r.workspace_id and d.source_kind='uploaded'
  where r.source_kind='uploaded'
   and (
    r.bucket is distinct from guanye_internal.ai_review_source_group_bucket(
     r.workspace_id,r.deck_id,'uploaded'
    )
    or r.split is distinct from case
     when guanye_internal.ai_review_source_group_bucket(r.workspace_id,r.deck_id,'uploaded')=0
      then 'holdout' else 'calibration' end
   )
 ) then
  raise exception 'M20 来源分桶冲突：ai_review_records 仍含 M19 来源的旧算法结果';
 end if;
end $m20_guard$;

create or replace function public.get_ai_review_sample_snapshot(w uuid) returns jsonb
language plpgsql stable security invoker set search_path='' as $$
declare result jsonb;
begin
 -- SOURCE_GROUP_ROUTING_V20
 if not exists (select 1 from public.workspaces where id=w) then raise exception '工作区不存在'; end if;
 with
 workspace as (
  select ws.id,ws.settings,ws.dedicated_judge_id
  from public.workspaces ws where ws.id=w
 ),
 deck_rows as (
  select d.id,d.source_kind,d.data,
   guanye_internal.ai_review_source_group_bucket(w,d.id,d.source_kind) as bucket
  from public.decks d where d.workspace_id=w
 ),
 review_base as (
  select r.deck_id,r.user_id,
   case when r.data->>'mode'='page' then 'page' else 'deck' end as mode,
   r.data
  from public.reviews r where r.workspace_id=w
 ),
 review_units as (
  select rb.deck_id,rb.user_id,'deck'::text as scope,0::bigint as position,
   null::jsonb as image,rb.data as original_entry,
   case when jsonb_typeof(rb.data->'scores')='object' then rb.data->'scores' else '{}'::jsonb end as scores
  from review_base rb
  union all
  select rb.deck_id,rb.user_id,'page'::text as scope,im.ordinality::bigint as position,
   im.value as image,
   case when jsonb_typeof(rb.data->'pages'->(im.value->>'id'))='object' then rb.data->'pages'->(im.value->>'id') else '{}'::jsonb end as original_entry,
   case when jsonb_typeof((rb.data->'pages'->(im.value->>'id'))->'scores')='object'
    then (rb.data->'pages'->(im.value->>'id'))->'scores' else '{}'::jsonb end as scores
  from review_base rb
  join deck_rows d on d.id=rb.deck_id
  cross join lateral jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end)
   with ordinality as im(value,ordinality)
 ),
 unit_rows as (
  select u.*,
   exists(select 1 from jsonb_each(u.scores) s where jsonb_typeof(s.value)='number') as has_scores,
   coalesce((
    select bool_and((u.scores ? (dim->>'id')) and jsonb_typeof(u.scores->(dim->>'id'))='number')
    from jsonb_array_elements((select settings->'dimensions' from workspace)) dim
   ),false) as complete
  from review_units u
 ),
 scope_agg as (
  select u.deck_id,u.user_id,u.scope,
   jsonb_agg(jsonb_build_object(
    'scope',u.scope,'unitType',u.scope,
    'unitId',case when u.scope='page' then u.image->>'id' else null end,
    'position',u.position,
    'imageId',case when u.scope='page' then u.image->>'id' else null end,
    'imageName',case when u.scope='page' then u.image->>'name' else null end,
    'image',u.image,'originalEntry',u.original_entry,'scores',u.scores,
    'hasScores',u.has_scores,'complete',u.complete
   ) order by u.position) as units,
   jsonb_agg(jsonb_build_object(
    'scope',u.scope,'unitType',u.scope,
    'unitId',case when u.scope='page' then u.image->>'id' else null end,
    'position',u.position,
    'imageId',case when u.scope='page' then u.image->>'id' else null end,
    'imageName',case when u.scope='page' then u.image->>'name' else null end,
    'image',u.image,'originalEntry',u.original_entry,'scores',u.scores,
    'hasScores',u.has_scores,'complete',u.complete
   ) order by u.position) filter (where u.has_scores) as scored_units,
   bool_or(u.has_scores) as has_scores,
   coalesce(bool_and(u.complete),false) as complete
  from unit_rows u
  group by u.deck_id,u.user_id,u.scope
 ),
 review_rows as (
  select rb.deck_id,rb.user_id,rb.mode,rb.data,
   coalesce(p.units,'[]'::jsonb) as units,
   coalesce(p.has_scores,false) as has_scores,
   coalesce(p.complete,false) as complete,
   coalesce(p.has_scores,false) or coalesce(o.has_scores,false) as any_has_scores,
   coalesce(p.complete,false) or coalesce(o.complete,false) as any_complete,
   case when rb.mode='deck' then 'page' else 'deck' end as other_scope,
   coalesce(o.scored_units,'[]'::jsonb) as other_scope_units,
   coalesce(o.has_scores,false) as other_scope_has_scores,
   coalesce(o.complete,false) as other_scope_complete,
   case when not coalesce(p.has_scores,false) then 'unrated'
    when coalesce(p.complete,false) then 'complete' else 'partial' end as rating_status,
   case when not (coalesce(p.has_scores,false) or coalesce(o.has_scores,false)) then 'unrated'
    when coalesce(p.complete,false) or coalesce(o.complete,false) then 'complete'
    else 'partial' end as overall_rating_status
  from review_base rb
  left join scope_agg p on p.deck_id=rb.deck_id and p.user_id=rb.user_id and p.scope=rb.mode
  left join scope_agg o on o.deck_id=rb.deck_id and o.user_id=rb.user_id and o.scope<>rb.mode
 ),
 deck_agg as (
  select d.id,d.source_kind,d.data,d.bucket,
   jsonb_agg(jsonb_build_object(
    'userId',r.user_id,'mode',r.mode,'otherScope',r.other_scope,
    'ratingStatus',r.rating_status,'overallRatingStatus',r.overall_rating_status,
    'maxScore',(select settings->'maxScore' from workspace),
    'dimensions',(select settings->'dimensions' from workspace),
    'original',r.data,'hasScores',r.has_scores,'anyHasScores',r.any_has_scores,
    'complete',r.complete,'anyComplete',r.any_complete,'units',r.units,
    'otherScopeUnits',r.other_scope_units,'otherScopeHasScores',r.other_scope_has_scores,
    'otherScopeComplete',r.other_scope_complete
   ) order by r.user_id) filter (where r.user_id is not null) as reviews,
   count(*) filter (where r.any_has_scores)::int as rated_review_count,
   count(*) filter (where r.user_id is not null and not r.any_has_scores)::int as unrated_review_count,
   count(*) filter (where r.any_complete)::int as complete_review_count
  from deck_rows d
  left join review_rows r on r.deck_id=d.id
  group by d.id,d.source_kind,d.data,d.bucket
 ),
 deck_samples as (
  select *,
   case when rated_review_count=0 then 'unrated'
    when complete_review_count=rated_review_count then 'complete' else 'partial' end as rating_status
  from deck_agg
 )
 select jsonb_build_object(
  'schemaVersion',1,
  'workspaceId',(select id from workspace),
  'splitAlgorithm',case when guanye_internal.ai_review_has_source_groups(w)
   then 'source-sha256-or-md5-first-byte-mod-5-v1'
   else 'md5-first-byte-mod-5-v1' end,
  'holdoutBucket',0,
  'settings',(select settings from workspace),
  'dedicatedJudgeId',(select dedicated_judge_id from workspace),
  'deckCount',count(*),
  'ratedDeckCount',count(*) filter (where rated_review_count>0),
  'unratedDeckCount',count(*) filter (where rated_review_count=0),
  'calibrationDeckIds',coalesce(jsonb_agg(id order by id) filter (where bucket<>0),'[]'::jsonb),
  'holdoutDeckIds',coalesce(jsonb_agg(id order by id) filter (where bucket=0),'[]'::jsonb),
  'decks',coalesce(jsonb_agg(jsonb_build_object(
   'deckId',id,
   'split',case when bucket=0 then 'holdout' else 'calibration' end,
   'bucket',bucket,'sourceKind',source_kind,'originalDeck',data,
   'ratingStatus',rating_status,'ratedReviewCount',rated_review_count,
   'unratedReviewCount',unrated_review_count,'completeReviewCount',complete_review_count,
   'reviews',coalesce(reviews,'[]'::jsonb)
  ) order by id),'[]'::jsonb)
 ) into result
 from deck_samples;
 return result;
end $$;

revoke all on function public.get_ai_review_sample_snapshot(uuid)
 from public,anon,authenticated;
grant execute on function public.get_ai_review_sample_snapshot(uuid) to service_role;

create or replace function public.ai_review_calibration(
 w uuid,
 actor uuid,
 p_kind text,
 p_limit integer,
 p_deck_id text,
 p_scope text
) returns jsonb
language plpgsql security definer set search_path='' as $calibration$
declare
 target_meta jsonb;
 target_class text;
 target_style text;
 target_group_key text;
 max_value numeric;
 limit_value integer;
 result jsonb;
begin
 -- SOURCE_GROUP_ROUTING_V20
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '校准轨道无效'; end if;
 if p_scope is null or p_scope not in ('deck','page') then raise exception '校准粒度无效'; end if;
 if p_deck_id is null or length(p_deck_id) not between 1 and 100 then raise exception '目标作品无效'; end if;
 if actor is null or not exists(
  select 1 from public.workspaces ws where ws.id=w and ws.dedicated_judge_id=actor
 ) then raise exception '仅专职评委可以读取校准样本'; end if;
 if not exists(
  select 1 from public.decks d where d.workspace_id=w and d.id=p_deck_id and d.source_kind=p_kind
 ) then raise exception '目标作品不存在或不属于该轨道'; end if;

 select public.ai_review_effective_meta(w,d.id),coalesce((ws.settings->>'maxScore')::numeric,5),
  guanye_internal.ai_review_source_group_key(w,d.id,p_kind)
 into target_meta,max_value,target_group_key
 from public.decks d join public.workspaces ws on ws.id=d.workspace_id
 where d.workspace_id=w and d.id=p_deck_id and d.source_kind=p_kind;
 target_class=nullif(lower(btrim(coalesce(target_meta->>'effectiveClassification',''))),'');
 target_style=nullif(lower(btrim(coalesce(target_meta->>'effectiveStyle',''))),'');
 limit_value=greatest(1,least(coalesce(p_limit,6),12));

 with
 meta as (
  select d.id,d.source_kind,d.data,d.created_at,
   guanye_internal.ai_review_source_group_bucket(w,d.id,d.source_kind) as bucket,
   guanye_internal.ai_review_source_group_key(w,d.id,d.source_kind) as group_key,
   coalesce(
    nullif(m.meta->>'effectiveClassification',''),
    nullif(d.data->>'classification',''),nullif(d.data->>'category',''),nullif(d.data->>'primaryClassification',''),
    case when jsonb_typeof(d.data->'classification')='object' then
     coalesce(nullif(d.data#>>'{classification,label}',''),nullif(d.data#>>'{classification,primary}',''),
      nullif(d.data#>>'{classification,name}',''),nullif(d.data#>>'{classification,category}',''))
    end
   ) as classification_value,
   coalesce(
    nullif(m.meta->>'effectiveStyle',''),nullif(d.data->>'style',''),
    case when jsonb_typeof(d.data->'generation'->'style')='string' then d.data#>>'{generation,style}' end,
    case when jsonb_typeof(d.data->'generation'->'style')='object' then
     coalesce(nullif(d.data#>>'{generation,style,name}',''),nullif(d.data#>>'{generation,style,label}',''),
      nullif(d.data#>>'{generation,style,summary}',''))
    end
   ) as style_value,
   m.meta
  from public.decks d
  cross join lateral (select public.ai_review_effective_meta(w,d.id) as meta) m
  where d.workspace_id=w
 ),
 meta_keys as (
  select m.*,nullif(lower(btrim(classification_value)),'') as classification_key,
   nullif(lower(btrim(style_value)),'') as style_key
  from meta m
 ),
 image_rows as (
  select d.id as deck_id,
   coalesce(nullif(image->>'id',''),nullif(image->>'pageId',''),'page-'||ordinality::text) as unit_id,
   coalesce(nullif(image->>'name',''),nullif(image->>'label',''),nullif(image->>'title',''),'page-'||ordinality::text) as unit_name,
   coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')) as preview_path,
   ordinality::integer as position
  from meta_keys d
  cross join lateral jsonb_array_elements(
   case when jsonb_typeof(d.data->'images')='array' then d.data->'images'
    when jsonb_typeof(d.data->'generation'->'pages')='array' then d.data->'generation'->'pages'
    else '[]'::jsonb end
  ) with ordinality as image_row(image,ordinality)
  where coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')) is not null
   and left(coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')),length(w::text)+1)=w::text||'/'
   and strpos(coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')),'..')=0
   and strpos(coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')),chr(92))=0
 ),
 preview as (
  select deck_id,'page'::text as scope,unit_id,unit_name,preview_path,position from image_rows
  union all
  select deck_id,'deck'::text,'',unit_name,preview_path,position
  from (
   select distinct on (deck_id) deck_id,unit_id,unit_name,preview_path,position
   from image_rows order by deck_id,position,unit_id
  ) first_image
 ),
 legacy as (
  select 'legacy_review'::text as sample_type,
   ('legacy:'||r.user_id::text||':'||d.id||':'||p.scope||':'||p.unit_id)::text as sample_id,
   d.id as deck_id,d.source_kind as track,p.scope,p.unit_id,p.unit_name,p.preview_path,
   r.user_id as human_user_id,coalesce(entry->'scores','{}'::jsonb) as scores,max_value as max_score,
   'workspaces.settings.maxScore'::text as score_scale_source,d.created_at as sort_at,null::timestamptz as created_at,
   d.classification_value,d.style_value,d.classification_key,d.style_key,
   d.meta->>'classificationSource' as classification_source,d.meta->>'styleSource' as style_source
  from public.reviews r
  join meta_keys d on d.id=r.deck_id
  join preview p on p.deck_id=d.id
  cross join lateral (select case when p.scope='page' then r.data->'pages'->p.unit_id else r.data end as entry) scored
  where p_kind='uploaded' and r.workspace_id=w and d.source_kind='uploaded' and d.bucket<>0
   and d.id<>p_deck_id and d.group_key is distinct from target_group_key
   and jsonb_typeof(scored.entry)='object'
   and public.ai_review_scores_complete(w,d.id,scored.entry,max_value)
 ),
 sample_sources as (
  select distinct on (s.id)
   s.id as submission_id,s.deck_id,s.source_kind as track,s.scope,s.unit_id,s.human_user_id,s.entry,s.max_score,
   s.created_at as sort_at,s.created_at,
   d.classification_value,d.style_value,d.classification_key,d.style_key,
   d.meta->>'classificationSource' as classification_source,d.meta->>'styleSource' as style_source
  from public.ai_review_submissions s
  join public.ai_review_matches m on m.submission_id=s.id
  join public.ai_review_records r on r.id=m.record_id and r.workspace_id=w
  join public.ai_review_jobs j on j.id=r.job_id and j.workspace_id=w
  join meta_keys d on d.id=s.deck_id
  where s.workspace_id=w and s.source_kind=p_kind and r.source_kind=p_kind and j.source_kind=p_kind and d.source_kind=p_kind
   and m.variant=r.variant and j.split='calibration' and r.split='calibration'
   and j.bucket=d.bucket and r.bucket=d.bucket and d.bucket<>0
   and s.deck_id<>p_deck_id and d.group_key is distinct from target_group_key
   and s.deck_version=public.ai_review_deck_version(w,s.deck_id) and s.rubric_version=public.ai_review_rubric_version(w)
   and r.deck_version=s.deck_version and r.rubric_version=s.rubric_version and r.scope=s.scope and r.unit_id=s.unit_id
   and public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score)
  order by s.id,case when r.variant='assisted' then 0 else 1 end,r.created_at desc,r.id
 ),
 sample_pool as (
  select * from legacy
  union all
  select 'human_submission'::text,('submission:'||s.submission_id::text)::text,s.deck_id,s.track,p.scope,p.unit_id,p.unit_name,p.preview_path,
   s.human_user_id,coalesce(s.entry->'scores','{}'::jsonb),s.max_score,'ai_review_submissions.maxScore'::text,
   s.sort_at,s.created_at,s.classification_value,s.style_value,s.classification_key,s.style_key,s.classification_source,s.style_source
  from sample_sources s
  join preview p on p.deck_id=s.deck_id
   and ((p.scope='deck' and s.scope='deck') or (p.scope='page' and s.scope='page' and p.unit_id=s.unit_id))
 ),
 sample_ranked as (
  select s.*,coalesce(s.scope=p_scope,false) as exact_scope,
   coalesce(target_class is not null and s.classification_key=target_class,false) as same_classification,
   coalesce(target_style is not null and s.style_key=target_style,false) as same_style,
   row_number() over (order by coalesce(s.scope=p_scope,false) desc,
    coalesce(target_class is not null and s.classification_key=target_class,false) desc,
    coalesce(target_style is not null and s.style_key=target_style,false) desc,s.sort_at desc nulls last,s.sample_id) as sample_order
  from (
   select distinct on (deck_id,scope,unit_id,human_user_id) *
   from sample_pool
   order by deck_id,scope,unit_id,human_user_id,case when sample_type='human_submission' then 0 else 1 end,
    sort_at desc nulls last,sample_id
  ) s
 ),
 selected_samples as (select * from sample_ranked where sample_order<=limit_value),
 correction_sources as (
  select distinct on (d.deck_id,s.scope,s.unit_id)
   d.id::text as correction_id,d.deck_id,p_kind as track,s.scope,s.unit_id,d.status,d.classification,d.reasons,d.reason_other,
   d.formal_scores,d.formal_total,d.max_score,d.score_ratio,d.summary,d.created_at,
   dm.classification_value,dm.style_value,dm.classification_key,dm.style_key,
   dm.meta->>'classificationSource' as classification_source,dm.meta->>'styleSource' as style_source
  from public.ai_review_decisions d
  join public.ai_review_submissions s on s.id=d.submission_id and s.workspace_id=w
  join public.ai_review_records r on r.id=d.ai_record_id and r.workspace_id=w
  join public.ai_review_jobs j on j.id=r.job_id and j.workspace_id=w
  join meta_keys dm on dm.id=d.deck_id
  where d.workspace_id=w and d.judge_user_id=actor and d.status='corrected'
   and d.deck_id<>p_deck_id and dm.group_key is distinct from target_group_key
   and dm.source_kind=p_kind and s.source_kind=p_kind and r.source_kind=p_kind and j.source_kind=p_kind
   and j.split='calibration' and r.split='calibration' and j.bucket=dm.bucket and r.bucket=dm.bucket and dm.bucket<>0
   and d.rubric_version=public.ai_review_rubric_version(w) and d.deck_version=public.ai_review_deck_version(w,d.deck_id)
   and s.rubric_version=d.rubric_version and s.deck_version=d.deck_version and s.scope=r.scope and s.unit_id=r.unit_id
   and s.deck_id=d.deck_id and r.deck_id=d.deck_id and j.deck_id=d.deck_id
   and s.max_score=d.max_score and r.max_score=d.max_score and d.max_score=max_value
   and exists(select 1 from public.ai_review_matches m where m.submission_id=s.id and m.record_id=r.id and m.variant=r.variant)
  order by d.deck_id,s.scope,s.unit_id,d.created_at desc,d.id desc
 ),
 correction_ranked as (
  select c.*,p.scope as actual_scope,p.unit_id as actual_unit_id,p.unit_name,p.preview_path,
   coalesce(p.scope=p_scope,false) as exact_scope,
   coalesce(target_class is not null and c.classification_key=target_class,false) as same_classification,
   coalesce(target_style is not null and c.style_key=target_style,false) as same_style,
   row_number() over (order by coalesce(p.scope=p_scope,false) desc,
    coalesce(target_class is not null and c.classification_key=target_class,false) desc,
    coalesce(target_style is not null and c.style_key=target_style,false) desc,c.created_at desc nulls last,c.correction_id) as correction_order
  from correction_sources c
  join preview p on p.deck_id=c.deck_id
   and ((p.scope='deck' and c.scope='deck') or (p.scope='page' and c.scope='page' and p.unit_id=c.unit_id))
 ),
 selected_corrections as (select * from correction_ranked where correction_order<=limit_value)
 select jsonb_build_object(
  'samples',coalesce((select jsonb_agg(jsonb_build_object(
   'sampleId',s.sample_id,'sampleType',s.sample_type,'track',s.track,'split','calibration','deckId',s.deck_id,
   'scope',s.scope,'scopeFallback',not s.exact_scope,'unitId',nullif(s.unit_id,''),'unitName',s.unit_name,
   'previewPath',s.preview_path,'scores',s.scores,'maxScore',s.max_score,'scoreScaleSource',s.score_scale_source,
   'classification',s.classification_value,'style',s.style_value,'classificationSource',s.classification_source,
   'styleSource',s.style_source,'sameClassification',s.same_classification,'sameStyle',s.same_style,'createdAt',s.created_at
  ) order by s.sample_order) from selected_samples s),'[]'::jsonb),
  'corrections',coalesce((select jsonb_agg(jsonb_build_object(
   'correctionId',c.correction_id,'sampleType','judge_formal_decision','track',c.track,'split','calibration','deckId',c.deck_id,
   'scope',c.actual_scope,'scopeFallback',not c.exact_scope,'unitId',nullif(c.actual_unit_id,''),'unitName',c.unit_name,
   'previewPath',c.preview_path,'status',c.status,'formalScores',c.formal_scores,'maxScore',c.max_score,
   'formalTotal',c.formal_total,'scoreRatio',c.score_ratio,'classification',c.classification,'style',c.style_value,
   'reasons',c.reasons,'reasonOther',c.reason_other,'summary',c.summary,'sameClassification',c.same_classification,
   'sameStyle',c.same_style,'createdAt',c.created_at
  ) order by c.correction_order) from selected_corrections c),'[]'::jsonb),
  'selectedSamples',(select count(*) from selected_samples),
  'selectedCorrections',(select count(*) from selected_corrections),
  'kind',p_kind,'requestedScope',p_scope,
  'scopeFallback',coalesce((select bool_or(not exact_scope) from selected_samples),false),
  'correctionScopeFallback',coalesce((select bool_or(not exact_scope) from selected_corrections),false)
 ) into result;
 return result;
end
$calibration$;

revoke all on function public.ai_review_calibration(uuid,uuid,text,integer,text,text)
 from public,anon,authenticated;
grant execute on function public.ai_review_calibration(uuid,uuid,text,integer,text,text)
 to service_role;

create or replace function public.ai_review_comparison(w uuid)
returns jsonb
language sql
stable
security invoker
set search_path=''
as $comparison$
-- SOURCE_GROUP_ROUTING_V20
with
context as (
 select
  public.ai_review_rubric_version(w) as rubric_version,
  coalesce((select (ws.settings->>'maxScore')::numeric from public.workspaces ws where ws.id=w),5) as max_score
 where w is not null
),
formal_candidate_records as (
 select
  s.id as submission_id,r.variant,r.ai_result,s.entry,s.source_kind,s.max_score,
  row_number() over (partition by s.id,r.variant order by r.created_at desc,r.id desc) as candidate_rank
 from public.ai_review_records r
 join public.ai_review_matches m on m.record_id=r.id and m.variant=r.variant
 join public.ai_review_submissions s on s.id=m.submission_id
 join public.decks d on d.id=r.deck_id and d.workspace_id=w and d.source_kind=r.source_kind
 where w is not null
  and r.workspace_id=w and s.workspace_id=w
  and guanye_internal.ai_review_source_group_bucket(w,r.deck_id,r.source_kind)=0
  and r.bucket=0 and r.split='holdout' and r.variant in ('baseline','assisted')
  and r.deck_id=s.deck_id and r.scope=s.scope and r.unit_id=s.unit_id
  and r.source_kind=s.source_kind and r.deck_version=s.deck_version and r.rubric_version=s.rubric_version
  and abs(r.max_score-s.max_score)<=1e-9
),
formal_selected_records as (
 select * from formal_candidate_records where candidate_rank=1
),
formal_pairs as (
 select b.submission_id,b.entry,b.source_kind,b.max_score,
  b.ai_result as baseline_result,a.ai_result as assisted_result
 from formal_selected_records b
 join formal_selected_records a on a.submission_id=b.submission_id and a.variant='assisted'
 where b.variant='baseline' and b.max_score>0
),
holdout_record_decks as (
 select r.deck_id
 from public.ai_review_records r
 join public.decks d on d.id=r.deck_id and d.workspace_id=w and d.source_kind='uploaded'
 where w is not null and r.workspace_id=w
  and guanye_internal.ai_review_source_group_bucket(w,r.deck_id,'uploaded')=0
  and r.bucket=0 and r.split='holdout' and r.source_kind='uploaded'
  and r.variant in ('baseline','assisted')
 group by r.deck_id
 having count(distinct r.variant)=2
),
eligible_decks as (
 select d.id as deck_id,d.data,c.rubric_version,c.max_score
 from public.decks d
 join context c on true
 join holdout_record_decks h on h.deck_id=d.id
 where d.workspace_id=w and d.source_kind='uploaded'
  and guanye_internal.ai_review_source_group_bucket(w,d.id,'uploaded')=0
),
candidate_decks as (
 select d.*,public.ai_review_deck_version(w,d.deck_id) as deck_version from eligible_decks d
),
legacy_record_pairs as (
 select d.deck_id,d.deck_version,d.rubric_version,d.max_score,
  b.id as baseline_record_id,b.scope,b.unit_id,b.source_kind,
  b.ai_result as baseline_result,a.id as assisted_record_id,a.ai_result as assisted_result
 from candidate_decks d
 join public.ai_review_records b
  on b.workspace_id=w and b.deck_id=d.deck_id and b.source_kind='uploaded'
  and b.bucket=0 and b.split='holdout' and b.variant='baseline'
  and b.scope in ('deck','page') and b.deck_version=d.deck_version
  and b.rubric_version=d.rubric_version and b.max_score=d.max_score
 join public.ai_review_records a
  on a.workspace_id=w and a.deck_id=d.deck_id and a.source_kind='uploaded'
  and a.bucket=0 and a.split='holdout' and a.variant='assisted'
  and a.scope=b.scope and a.unit_id=b.unit_id and a.deck_version=d.deck_version
  and a.rubric_version=d.rubric_version and a.max_score=d.max_score
),
preview_units as (
 select distinct d.deck_id,image->>'id' as unit_id
 from candidate_decks d
 cross join lateral jsonb_array_elements(
  case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end
 ) image
 cross join lateral (
  select coalesce(nullif(image->>'path',''),nullif(image->>'imagePath',''),nullif(image->>'storagePath','')) as preview_path
 ) p
 where nullif(image->>'id','') is not null
  and p.preview_path is not null
  and left(p.preview_path,length(w::text)+1)=w::text||'/'
  and strpos(p.preview_path,'..')=0 and strpos(p.preview_path,chr(92))=0
),
legacy_slots as (
 select p.* from legacy_record_pairs p
 where (p.scope='deck' and p.unit_id='')
  or (p.scope='page' and exists(select 1 from preview_units u where u.deck_id=p.deck_id and u.unit_id=p.unit_id))
),
formal_current_pairs as (
 select distinct s.human_user_id,s.deck_id,s.scope,s.unit_id,s.deck_version,s.rubric_version
 from legacy_slots l
 join public.ai_review_matches mb on mb.record_id=l.baseline_record_id and mb.variant='baseline'
 join public.ai_review_submissions s
  on s.id=mb.submission_id and s.workspace_id=w and s.deck_id=l.deck_id and s.source_kind='uploaded'
  and s.scope=l.scope and s.unit_id=l.unit_id and s.deck_version=l.deck_version
  and s.rubric_version=l.rubric_version and abs(s.max_score-l.max_score)<=1e-9
 join public.ai_review_matches ma on ma.submission_id=s.id and ma.record_id=l.assisted_record_id and ma.variant='assisted'
 where exists(
  select 1 from jsonb_each(case when jsonb_typeof(s.entry->'scores')='object' then s.entry->'scores' else '{}'::jsonb end) h
  join jsonb_each(case when jsonb_typeof(l.baseline_result->'scores')='object' then l.baseline_result->'scores' else '{}'::jsonb end) b on b.key=h.key
  join jsonb_each(case when jsonb_typeof(l.assisted_result->'scores')='object' then l.assisted_result->'scores' else '{}'::jsonb end) a on a.key=h.key
  where jsonb_typeof(h.value)='number' and jsonb_typeof(b.value)='number' and jsonb_typeof(a.value)='number'
 )
),
legacy_sample_entries as (
 select ('legacy:'||r.user_id::text||':'||l.deck_id||':'||l.scope||':'||l.unit_id)::text as sample_key,
  'uploaded'::text as source_kind,l.deck_id,l.scope,l.unit_id,l.baseline_result,l.assisted_result,l.max_score,
  case when l.scope='page' then r.data->'pages'->l.unit_id else r.data end as entry
 from legacy_slots l
 join public.reviews r on r.workspace_id=w and r.deck_id=l.deck_id
 where not exists(
  select 1 from formal_current_pairs f where f.human_user_id=r.user_id
   and f.deck_id=l.deck_id and f.scope=l.scope and f.unit_id=l.unit_id
   and f.deck_version=l.deck_version and f.rubric_version=l.rubric_version
 )
),
legacy_samples as (
 select e.sample_key,e.source_kind,e.entry,e.baseline_result,e.assisted_result,e.max_score
 from legacy_sample_entries e
 where jsonb_typeof(e.entry)='object'
  and public.ai_review_scores_complete(w,e.deck_id,e.entry,e.max_score)
),
holdout_samples as (
 select ('submission:'||submission_id::text)::text as sample_key,source_kind,entry,
  baseline_result,assisted_result,max_score from formal_pairs
 union all
 select sample_key,source_kind,entry,baseline_result,assisted_result,max_score from legacy_samples
),
human_scores as (
 select s.sample_key,e.key as dimension,(e.value #>> '{}')::numeric as score
 from holdout_samples s
 cross join lateral jsonb_each(case when jsonb_typeof(s.entry->'scores')='object' then s.entry->'scores' else '{}'::jsonb end) e
 where jsonb_typeof(e.value)='number'
),
baseline_scores as (
 select s.sample_key,e.key as dimension,(e.value #>> '{}')::numeric as score
 from holdout_samples s
 cross join lateral jsonb_each(case when jsonb_typeof(s.baseline_result->'scores')='object' then s.baseline_result->'scores' else '{}'::jsonb end) e
 where jsonb_typeof(e.value)='number'
),
assisted_scores as (
 select s.sample_key,e.key as dimension,(e.value #>> '{}')::numeric as score
 from holdout_samples s
 cross join lateral jsonb_each(case when jsonb_typeof(s.assisted_result->'scores')='object' then s.assisted_result->'scores' else '{}'::jsonb end) e
 where jsonb_typeof(e.value)='number'
),
common_scores as (
 select h.sample_key,h.dimension,h.score as human_score,b.score as baseline_score,
  a.score as assisted_score,s.max_score
 from human_scores h
 join baseline_scores b on b.sample_key=h.sample_key and b.dimension=h.dimension
 join assisted_scores a on a.sample_key=h.sample_key and a.dimension=h.dimension
 join holdout_samples s on s.sample_key=h.sample_key
),
dimension_errors as (
 select sample_key,dimension,abs(baseline_score-human_score)/max_score as baseline_error,
  abs(assisted_score-human_score)/max_score as assisted_error
 from common_scores
),
sample_errors as (
 select d.sample_key,s.source_kind,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d join holdout_samples s on s.sample_key=d.sample_key
 group by d.sample_key,s.source_kind
),
overall_stats as (
 select count(*)::integer as unit_count,avg(baseline_mae) as baseline_mae,avg(assisted_mae) as assisted_mae
 from sample_errors
),
overall_dimension_values as (
 select dimension,avg(baseline_error) as baseline_mae,avg(assisted_error) as assisted_mae
 from dimension_errors group by dimension
),
overall_dimensions as (
 select coalesce(jsonb_object_agg(dimension,baseline_mae order by dimension) filter(where dimension is not null),'{}'::jsonb) as baseline_dimensions,
  coalesce(jsonb_object_agg(dimension,assisted_mae order by dimension) filter(where dimension is not null),'{}'::jsonb) as assisted_dimensions
 from overall_dimension_values
),
kinds as (
 select source_kind from (values ('uploaded'::text),('generated'::text)) k(source_kind)
),
kind_stats as (
 select k.source_kind,count(s.sample_key)::integer as unit_count,
  avg(s.baseline_mae) as baseline_mae,avg(s.assisted_mae) as assisted_mae
 from kinds k left join sample_errors s on s.source_kind=k.source_kind
 group by k.source_kind
),
kind_dimension_values as (
 select s.source_kind,d.dimension,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d join sample_errors s on s.sample_key=d.sample_key
 group by s.source_kind,d.dimension
),
kind_dimensions as (
 select k.source_kind,
  coalesce(jsonb_object_agg(v.dimension,v.baseline_mae order by v.dimension) filter(where v.dimension is not null),'{}'::jsonb) as baseline_dimensions,
  coalesce(jsonb_object_agg(v.dimension,v.assisted_mae order by v.dimension) filter(where v.dimension is not null),'{}'::jsonb) as assisted_dimensions
 from kinds k left join kind_dimension_values v on v.source_kind=k.source_kind
 group by k.source_kind
),
kind_json as (
 select s.source_kind,jsonb_build_object(
  'holdoutUnits',s.unit_count,
  'baseline',case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.baseline_mae,'dimensions',d.baseline_dimensions) end,
  'assisted',case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.assisted_mae,'dimensions',d.assisted_dimensions) end,
  'improvement',case when s.unit_count=0 then null else s.baseline_mae-s.assisted_mae end
 ) as value
 from kind_stats s join kind_dimensions d on d.source_kind=s.source_kind
),
by_kind as (
 select jsonb_build_object(
  'uploaded',(select value from kind_json where source_kind='uploaded'),
  'generated',(select value from kind_json where source_kind='generated')
 ) as value
),
overall_json as (
 select s.unit_count,
  case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.baseline_mae,'dimensions',d.baseline_dimensions) end as baseline,
  case when s.unit_count=0 then null else jsonb_build_object('count',s.unit_count,'mae',s.assisted_mae,'dimensions',d.assisted_dimensions) end as assisted,
  case when s.unit_count=0 then null else s.baseline_mae-s.assisted_mae end as improvement
 from overall_stats s cross join overall_dimensions d
)
select jsonb_build_object(
 'holdoutUnits',s.unit_count,'baseline',s.baseline,'assisted',s.assisted,'improvement',s.improvement,'byKind',b.value,
 'splitAlgorithm',case when guanye_internal.ai_review_has_source_groups(w)
  then 'source-sha256-or-md5-first-byte-mod-5-v1' else 'md5-first-byte-mod-5-v1' end,
 'holdoutBucket',0
)
from overall_json s cross join by_kind b
$comparison$;

revoke all on function public.ai_review_comparison(uuid) from public,anon,authenticated;
grant execute on function public.ai_review_comparison(uuid) to service_role;

create or replace function public.ai_planning_reference_candidates(
 w uuid, max_items integer default 12
) returns jsonb
language sql stable security definer set search_path='' as $$
-- SOURCE_GROUP_ROUTING_V20
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
   else case when jsonb_typeof(r.data#>array['scores',dm.dimension->>'id'])='number'
     then (r.data#>array['scores',dm.dimension->>'id'])::text::numeric else null end end as score
  from public.reviews r
  join public.decks d on d.workspace_id=r.workspace_id and d.id=r.deck_id and d.source_kind='uploaded'
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
  from public.ai_review_uploaded_formal_scores(w) where scope='deck' group by deck_id
 ), uploaded_decks as (
  select d.id as deck_id,d.data,
   guanye_internal.ai_review_source_group_bucket(w,d.id,'uploaded') as bucket
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

revoke all on function public.ai_planning_reference_candidates(uuid,integer)
 from public,anon,authenticated;
grant execute on function public.ai_planning_reference_candidates(uuid,integer)
 to service_role;

create or replace function public.ai_review_prepare(w uuid, actor uuid, payload jsonb) returns jsonb
language plpgsql security definer set search_path='' as $$
declare deck_row public.decks%rowtype; rubric_value jsonb; rubric_hash text; deck_hash text;
 scope_value text; unit_json jsonb; unit_key text; position_value integer; bucket_value integer; split_value text;
 variant_value text; variants text[]; job_ids jsonb='[]'::jsonb; max_value numeric;
begin
 -- SOURCE_GROUP_ROUTING_V20
 -- UPLOADED_BASELINE_V16
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then raise exception '仅专职评委可以准备 AI 预评'; end if;
 if jsonb_typeof(payload) is distinct from 'object' or length(coalesce(payload->>'deckId','')) not between 1 and 100 then raise exception '预评内容无效'; end if;
 select * into deck_row from public.decks where workspace_id=w and id=payload->>'deckId';
 if deck_row.id is null then raise exception '作品不存在'; end if;
 rubric_value=public.ai_review_rubric(w);rubric_hash=public.ai_review_json_sha(rubric_value);
 deck_hash=public.ai_review_deck_version(w,deck_row.id);max_value=(rubric_value->>'maxScore')::numeric;
 scope_value=coalesce(payload->>'scope','deck');
 if scope_value not in ('deck','page') then raise exception '预评粒度无效'; end if;
 bucket_value=guanye_internal.ai_review_source_group_bucket(w,deck_row.id,deck_row.source_kind);
 split_value=case when bucket_value=0 then 'holdout' else 'calibration' end;
 variants=case
  when bucket_value=0 then array['baseline','assisted']
  when deck_row.source_kind='uploaded' then array['baseline']
  else array['assisted']
 end;
 if scope_value='deck' then
  foreach variant_value in array variants loop
   insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score)
   values(w,deck_row.id,deck_row.source_kind,'deck','',0,variant_value,bucket_value,split_value,deck_hash,rubric_hash,max_value)
   on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant) do nothing;
  end loop;
  select coalesce(jsonb_agg(jsonb_build_object('jobId',j.id,'deckId',j.deck_id,'sourceKind',j.source_kind,
   'scope',j.scope,'unitId',j.unit_id,'position',j.position,'variant',j.variant,'status',j.status,
   'bucket',j.bucket,'split',j.split,'deckVersion',j.deck_version,'rubricVersion',j.rubric_version) order by j.variant),'[]'::jsonb)
  into job_ids from public.ai_review_jobs j where j.workspace_id=w and j.deck_id=deck_row.id and j.scope='deck'
   and j.deck_version=deck_hash and j.rubric_version=rubric_hash;
 else
  if jsonb_typeof(payload->'unitIds') is distinct from 'array' or jsonb_array_length(payload->'unitIds') not between 1 and 600 then raise exception '请选择需要预评的页面'; end if;
  for unit_json in select value from jsonb_array_elements(payload->'unitIds') loop
   unit_key=case when jsonb_typeof(unit_json)='string' then trim(unit_json #>> '{}') else coalesce(unit_json->>'unitId','') end;
   select coalesce((image->>'position')::integer,ordinality::integer) into position_value
   from jsonb_array_elements(case when jsonb_typeof(deck_row.data->'images')='array' then deck_row.data->'images' else '[]'::jsonb end) with ordinality as x(image,ordinality)
   where image->>'id'=unit_key limit 1;
   if position_value is null then raise exception '预评页面不存在'; end if;
   foreach variant_value in array variants loop
    insert into public.ai_review_jobs(workspace_id,deck_id,source_kind,scope,unit_id,position,variant,bucket,split,deck_version,rubric_version,max_score)
    values(w,deck_row.id,deck_row.source_kind,'page',unit_key,position_value,variant_value,bucket_value,split_value,deck_hash,rubric_hash,max_value)
    on conflict(workspace_id,deck_id,scope,unit_id,deck_version,rubric_version,variant) do nothing;
   end loop;
  end loop;
  select coalesce(jsonb_agg(jsonb_build_object('jobId',j.id,'deckId',j.deck_id,'sourceKind',j.source_kind,
   'scope',j.scope,'unitId',j.unit_id,'position',j.position,'variant',j.variant,'status',j.status,
   'bucket',j.bucket,'split',j.split,'deckVersion',j.deck_version,'rubricVersion',j.rubric_version) order by j.position,j.variant),'[]'::jsonb)
  into job_ids from public.ai_review_jobs j where j.workspace_id=w and j.deck_id=deck_row.id and j.scope='page'
   and j.deck_version=deck_hash and j.rubric_version=rubric_hash
   and j.unit_id in (select case when jsonb_typeof(value)='string' then trim(value #>> '{}') else coalesce(value->>'unitId','') end from jsonb_array_elements(payload->'unitIds'));
 end if;
 return jsonb_build_object('ok',true,'deckId',deck_row.id,'sourceKind',deck_row.source_kind,'scope',scope_value,
  'deckVersion',deck_hash,'rubricVersion',rubric_hash,'jobs',job_ids,'aiVisible',false);
end $$;

revoke all on function public.ai_review_prepare(uuid,uuid,jsonb)
 from public,anon,authenticated;
grant execute on function public.ai_review_prepare(uuid,uuid,jsonb) to service_role;

commit;
