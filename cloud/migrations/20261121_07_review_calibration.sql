-- AI review calibration: input-conditioned few-shot retrieval for each review track.
begin;

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
 max_value numeric;
 limit_value integer;
 result jsonb;
begin
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '校准轨道无效'; end if;
 if p_scope is null or p_scope not in ('deck','page') then raise exception '校准粒度无效'; end if;
 if p_deck_id is null or length(p_deck_id) not between 1 and 100 then raise exception '目标作品无效'; end if;
 if actor is null or not exists(
  select 1 from public.workspaces ws where ws.id=w and ws.dedicated_judge_id=actor
 ) then raise exception '仅专职评委可以读取校准样本'; end if;
 if not exists(
  select 1 from public.decks d where d.workspace_id=w and d.id=p_deck_id and d.source_kind=p_kind
 ) then raise exception '目标作品不存在或不属于该轨道'; end if;

 select public.ai_review_effective_meta(w,d.id),coalesce((ws.settings->>'maxScore')::numeric,5)
 into target_meta,max_value
 from public.decks d join public.workspaces ws on ws.id=d.workspace_id
 where d.workspace_id=w and d.id=p_deck_id and d.source_kind=p_kind;
 target_class=nullif(lower(btrim(coalesce(target_meta->>'effectiveClassification',''))),'');
 target_style=nullif(lower(btrim(coalesce(target_meta->>'effectiveStyle',''))),'');
 limit_value=greatest(1,least(coalesce(p_limit,6),12));

 with
 meta as (
  select d.id,d.source_kind,d.data,d.created_at,
   mod(get_byte(decode(md5(d.id),'hex'),0),5)::integer as bucket,
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
 -- Decks use data.images[] with id/name/path. pageId/label/imagePath/storagePath are read-only fallbacks for old drafts.
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
  where p_kind='uploaded' and r.workspace_id=w and d.source_kind='uploaded' and d.bucket<>0 and d.id<>p_deck_id
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
   and j.bucket<>0 and r.bucket<>0 and d.bucket<>0 and s.deck_id<>p_deck_id
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
  where d.workspace_id=w and d.judge_user_id=actor and d.status='corrected' and d.deck_id<>p_deck_id
   and dm.source_kind=p_kind and s.source_kind=p_kind and r.source_kind=p_kind and j.source_kind=p_kind
   and j.split='calibration' and r.split='calibration' and j.bucket<>0 and r.bucket<>0 and dm.bucket<>0
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

revoke all on function public.ai_review_calibration(uuid,uuid,text,integer,text,text) from public,anon,authenticated;
grant execute on function public.ai_review_calibration(uuid,uuid,text,integer,text,text) to service_role;

commit;
