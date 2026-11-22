-- REVIEW_BACKFILL_CLASSIFICATION_V18
-- Historical uploads can have valid page-level human ratings while their deck
-- has no human/existing classification. The v09 backfill correctly preserved
-- those page tasks, but page-only coverage never asked for the deck AI record
-- that supplies effective classification. This migration keeps page tasks and
-- adds only the missing deck work.
--
-- Batch behavior follows pgmq's read contract: a bounded, deterministic batch
-- under the caller's transaction, with an idempotent insert boundary. Source
-- convention checked 2026-11-21:
-- https://github.com/pgmq/pgmq/blob/main/pgmq-extension/sql/pgmq.sql#L375-L412
begin;

create or replace function public.ai_review_effective_meta(w uuid, target_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare
 deck_data jsonb;
 deck_kind text;
 deck_hash text;
 rubric_hash text;
 existing_class text;
 existing_style text;
 formal_class text;
 formal_style text;
 human_class text;
 human_style text;
 ai_class text;
 ai_style text;
 has_submission boolean=false;
 has_legacy_review_evidence boolean=false;
 class_source text='none';
 style_source text='none';
begin
 -- LIBRARY_CLASSIFICATION_BACKFILL_V18
 select data,source_kind into deck_data,deck_kind from public.decks where workspace_id=w and id=target_id;
 if deck_data is null then
  return jsonb_build_object('effectiveClassification',null,'effectiveStyle',null,
   'classificationSource','none','styleSource','none');
 end if;

 existing_class=coalesce(
  nullif(btrim(case
   when jsonb_typeof(deck_data->'classification')='string' then deck_data->>'classification'
   when jsonb_typeof(deck_data->'classification')='object' then coalesce(
    nullif(btrim(deck_data#>>'{classification,primary}'),''),
    nullif(btrim(deck_data#>>'{classification,label}'),''),
    nullif(btrim(deck_data#>>'{classification,name}'),''),
    nullif(btrim(deck_data#>>'{classification,category}'),''))
  end),''),
  nullif(btrim(deck_data->>'category'),''),
  nullif(btrim(deck_data->>'primaryClassification'),''));
 existing_style=nullif(btrim(deck_data->>'style'),'');
 deck_hash=public.ai_review_deck_version(w,target_id);
 rubric_hash=public.ai_review_rubric_version(w);

 with valid_formal as (
  select d.created_at,d.id,
   nullif(btrim(case
    when jsonb_typeof(d.classification)='string' then d.classification #>> '{}'
    when jsonb_typeof(d.classification)='object' then coalesce(
     nullif(btrim(d.classification->>'primary'),''),
     nullif(btrim(d.classification->>'label'),''),
     nullif(btrim(d.classification->>'name'),''),
     nullif(btrim(d.classification->>'category'),''))
   end),'') as class_value,
   nullif(btrim(case when jsonb_typeof(d.classification)='object' then d.classification->>'style' end),'') as style_value
  from public.ai_review_decisions d
  join public.ai_review_submissions s on s.id=d.submission_id
  join public.workspaces ws on ws.id=d.workspace_id
  where d.workspace_id=w and d.deck_id=target_id
   and d.judge_user_id=ws.dedicated_judge_id
   and d.status in ('confirmed','corrected')
   and s.workspace_id=w and s.deck_id=d.deck_id and s.scope='deck' and s.unit_id=''
   and d.deck_version=deck_hash and d.rubric_version=rubric_hash
   and s.deck_version=d.deck_version and s.rubric_version=d.rubric_version
   and d.max_score=coalesce((ws.settings->>'maxScore')::numeric,5)
   and public.ai_review_scores_complete(w,d.deck_id,jsonb_build_object('scores',d.formal_scores),d.max_score)
 )
 select
  (array_agg(class_value order by created_at desc,id desc) filter(where class_value is not null))[1],
  (array_agg(style_value order by created_at desc,id desc) filter(where style_value is not null))[1]
 into formal_class,formal_style
 from valid_formal;

 with valid_human as (
  select s.created_at,s.id,
   nullif(btrim(case
    when jsonb_typeof(s.entry->'classificationCorrection')='string' then s.entry->'classificationCorrection' #>> '{}'
    when jsonb_typeof(s.entry->'classificationCorrection')='object' then coalesce(
     nullif(btrim(s.entry#>>'{classificationCorrection,primary}'),''),
     nullif(btrim(s.entry#>>'{classificationCorrection,label}'),''),
     nullif(btrim(s.entry#>>'{classificationCorrection,name}'),''),
     nullif(btrim(s.entry#>>'{classificationCorrection,category}'),''))
   end),'') as class_value,
   nullif(btrim(case when jsonb_typeof(s.entry->'classificationCorrection')='object'
    then s.entry#>>'{classificationCorrection,style}' end),'') as style_value
  from public.ai_review_submissions s
  where s.workspace_id=w and s.deck_id=target_id and s.scope='deck' and s.unit_id=''
   and s.deck_version=deck_hash and s.rubric_version=rubric_hash
   and public.ai_review_scores_complete(w,s.deck_id,s.entry,s.max_score)
 )
 select exists(select 1 from valid_human),
  (array_agg(class_value order by created_at desc,id desc) filter(where class_value is not null))[1],
  (array_agg(style_value order by created_at desc,id desc) filter(where style_value is not null))[1]
 into has_submission,human_class,human_style
 from valid_human;

 -- v12 intentionally requires a valid whole-deck human submission before AI
 -- metadata is visible. For untouched historical uploads only, an existing
 -- valid review is the human evidence that requests the deck forecast. The
 -- fallback accepts both the original whole-deck reviews.scores shape and the
 -- existing page-level shape. Any modern submission keeps the normal blind
 -- gate, and generated/new works never enter this legacy branch.
 if deck_kind='uploaded' and not has_submission
    and not exists(select 1 from public.ai_review_submissions s where s.workspace_id=w and s.deck_id=target_id) then
  with current_rubric as (
   select coalesce((ws.settings->>'maxScore')::numeric,5) as max_score,
    case when jsonb_typeof(ws.settings->'dimensions')='array' then ws.settings->'dimensions' else '[]'::jsonb end as dimensions
   from public.workspaces ws
   where ws.id=w
  )
  select
   exists(
    select 1
    from public.reviews r
    cross join current_rubric rb
    where r.workspace_id=w and r.deck_id=target_id
     and public.ai_review_scores_complete(
      w,target_id,jsonb_build_object('scores',r.data->'scores'),rb.max_score
     )
   )
   or exists(
    select 1
    from public.reviews r
    cross join lateral jsonb_each(
     case when jsonb_typeof(r.data->'pages')='object' then r.data->'pages' else '{}'::jsonb end
    ) as page(unit_id,entry)
    cross join current_rubric rb
    where r.workspace_id=w and r.deck_id=target_id
     and exists(
      select 1
      from jsonb_array_elements(
       case when jsonb_typeof(deck_data->'images')='array' then deck_data->'images' else '[]'::jsonb end
      ) image
      where image->>'id'=page.unit_id
       and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
     )
     and jsonb_typeof(page.entry->'scores')='object'
     and coalesce((
      select bool_and(case
       when jsonb_typeof(score.value)='number' then (score.value #>> '{}')::numeric between 0 and rb.max_score
       else false
      end)
      from jsonb_each(case when jsonb_typeof(page.entry->'scores')='object' then page.entry->'scores' else '{}'::jsonb end) score
     ),false)
     and exists(
      select 1
      from jsonb_each(case when jsonb_typeof(page.entry->'scores')='object' then page.entry->'scores' else '{}'::jsonb end) score
      where exists(
       select 1 from jsonb_array_elements(rb.dimensions) dim where dim->>'id'=score.key
      )
     )
   )
  into has_legacy_review_evidence;
 end if;

 if has_submission or has_legacy_review_evidence then
  with valid_ai as (
   select r.created_at,r.id,case when r.variant='assisted' then 0 else 1 end as variant_order,
    nullif(btrim(case
     when jsonb_typeof(r.ai_result->'classification')='string' then r.ai_result->'classification' #>> '{}'
     when jsonb_typeof(r.ai_result->'classification')='object' then coalesce(
      nullif(btrim(r.ai_result#>>'{classification,primary}'),''),
      nullif(btrim(r.ai_result#>>'{classification,label}'),''),
      nullif(btrim(r.ai_result#>>'{classification,name}'),''),
      nullif(btrim(r.ai_result#>>'{classification,category}'),''))
    end),'') as class_value,
    nullif(btrim(case when jsonb_typeof(r.ai_result->'classification')='object'
     then r.ai_result#>>'{classification,style}' end),'') as style_value
   from public.ai_review_records r
   where r.workspace_id=w and r.deck_id=target_id and r.scope='deck' and r.unit_id=''
    and r.deck_version=deck_hash and r.rubric_version=rubric_hash
  )
  select
   (array_agg(class_value order by variant_order,created_at desc,id desc) filter(where class_value is not null))[1],
   (array_agg(style_value order by variant_order,created_at desc,id desc) filter(where style_value is not null))[1]
  into ai_class,ai_style
  from valid_ai;
 end if;

 if formal_class is not null then human_class=formal_class;class_source='human';
 elsif human_class is not null then class_source='human';
 elsif existing_class is not null then human_class=existing_class;class_source='existing';
 elsif ai_class is not null then human_class=ai_class;class_source='ai';
 end if;
 if formal_style is not null then human_style=formal_style;style_source='human';
 elsif human_style is not null then style_source='human';
 elsif existing_style is not null then human_style=existing_style;style_source='existing';
 elsif ai_style is not null then human_style=ai_style;style_source='ai';
 end if;
 return jsonb_build_object('effectiveClassification',human_class,'effectiveStyle',human_style,
  'classificationSource',class_source,'styleSource',style_source);
end $$;

revoke all on function public.ai_review_effective_meta(uuid,text) from public,anon,authenticated;
grant execute on function public.ai_review_effective_meta(uuid,text) to service_role;

create or replace function public.ai_review_backfill(
 w uuid,
 actor uuid,
 p_kind text,
 p_offset integer,
 p_limit integer
) returns jsonb
language plpgsql
security definer
set search_path=''
as $$
declare
 selected record;
 total_decks bigint;
 selected_decks integer=0;
 prepared_units integer=0;
 next_offset bigint;
 has_whole_rating boolean=false;
 page_ids text[]='{}'::text[];
 classification_meta jsonb;
begin
 -- REVIEW_BACKFILL_CLASSIFICATION_V18
 if p_kind is null or p_kind not in ('uploaded','generated') then raise exception '队列类型无效'; end if;
 if p_offset is null or p_offset<0 then raise exception '分页偏移无效'; end if;
 if p_limit is null or p_limit not between 1 and 20 then raise exception '分页大小无效'; end if;
 if not exists(select 1 from public.workspaces where id=w and dedicated_judge_id=actor) then
  raise exception '仅专职评委可以补档预评';
 end if;

 select count(*) into total_decks
 from public.decks d
 where d.workspace_id=w and d.source_kind=p_kind;

 for selected in
  select d.id,d.data,
   exists(
    select 1
    from jsonb_array_elements(case when jsonb_typeof(d.data->'images')='array' then d.data->'images' else '[]'::jsonb end) with ordinality as x(image,ordinality)
    where nullif(btrim(image->>'id'),'') is not null
     and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
   ) as private_preview
  from public.decks d
  where d.workspace_id=w and d.source_kind=p_kind
  order by d.id
  offset p_offset limit p_limit
 loop
  selected_decks=selected_decks+1;
  if not selected.private_preview then continue; end if;

  if p_kind='generated' then
   perform public.ai_review_prepare(w,actor,jsonb_build_object('deckId',selected.id,'scope','deck'));
   prepared_units=prepared_units+1;
   continue;
  end if;

  with rubric as (
   select
    coalesce((ws.settings->>'maxScore')::numeric,5) as max_score,
    case when jsonb_typeof(ws.settings->'dimensions')='array' then ws.settings->'dimensions' else '[]'::jsonb end as dimensions
   from public.workspaces ws
   where ws.id=w
  ),
  current_images as (
   select image->>'id' as unit_id,ordinality as image_order
   from jsonb_array_elements(case when jsonb_typeof(selected.data->'images')='array' then selected.data->'images' else '[]'::jsonb end) with ordinality as x(image,ordinality)
   where nullif(btrim(image->>'id'),'') is not null
    and left(coalesce(image->>'path',''),length(w::text)+1)=w::text||'/'
  ),
  current_unique as (
   select distinct on (unit_id) unit_id,image_order
   from current_images
   order by unit_id,image_order
  ),
  review_rows as (
   select case when jsonb_typeof(r.data)='object' then r.data else '{}'::jsonb end as data
   from public.reviews r
   where r.workspace_id=w and r.deck_id=selected.id
  ),
  review_data as (
   select r.data,
    case when jsonb_typeof(r.data->'pages')='object' then r.data->'pages' else '{}'::jsonb end as pages
   from review_rows r
  ),
  valid_reviews as (
   select r.data,r.pages,
    (jsonb_typeof(r.data->'scores')='object'
     and coalesce((
      select bool_and(case
       when jsonb_typeof(s.value)='number' then (s.value #>> '{}')::numeric between 0 and rb.max_score
       else false
      end)
      from jsonb_each(case when jsonb_typeof(r.data->'scores')='object' then r.data->'scores' else '{}'::jsonb end) s
     ),false)
     and exists(
      select 1
      from jsonb_each(case when jsonb_typeof(r.data->'scores')='object' then r.data->'scores' else '{}'::jsonb end) s
      where exists(
       select 1 from jsonb_array_elements(rb.dimensions) dim where dim->>'id'=s.key
      )
     )) as whole_valid
   from review_data r
   cross join rubric rb
  ),
  page_candidates as (
   select cu.unit_id,cu.image_order
   from current_unique cu
   cross join rubric rb
   where exists(
    select 1
    from valid_reviews vr
    cross join lateral jsonb_each(vr.pages) p(unit_id,entry)
    where p.unit_id=cu.unit_id
     and jsonb_typeof(p.entry->'scores')='object'
     and coalesce((
      select bool_and(case
       when jsonb_typeof(s.value)='number' then (s.value #>> '{}')::numeric between 0 and rb.max_score
       else false
      end)
      from jsonb_each(case when jsonb_typeof(p.entry->'scores')='object' then p.entry->'scores' else '{}'::jsonb end) s
     ),false)
     and exists(
      select 1
      from jsonb_each(case when jsonb_typeof(p.entry->'scores')='object' then p.entry->'scores' else '{}'::jsonb end) s
      where exists(
       select 1 from jsonb_array_elements(rb.dimensions) dim where dim->>'id'=s.key
      )
     )
   )
  )
  select
   exists(select 1 from valid_reviews where whole_valid),
   coalesce((select array_agg(unit_id order by image_order) from page_candidates),'{}'::text[])
  into has_whole_rating,page_ids;

  classification_meta=public.ai_review_effective_meta(w,selected.id);
  if has_whole_rating or coalesce(array_length(page_ids,1),0)=0
     or nullif(btrim(coalesce(classification_meta->>'effectiveClassification','')),'') is null then
   perform public.ai_review_prepare(w,actor,jsonb_build_object('deckId',selected.id,'scope','deck'));
   prepared_units=prepared_units+1;
  end if;
  if coalesce(array_length(page_ids,1),0)>0 then
   perform public.ai_review_prepare(w,actor,jsonb_build_object('deckId',selected.id,'scope','page','unitIds',to_jsonb(page_ids)));
   prepared_units=prepared_units+coalesce(array_length(page_ids,1),0);
  end if;
 end loop;

 next_offset=case when p_offset::bigint+p_limit<total_decks then p_offset::bigint+p_limit else null end;
 return jsonb_build_object(
  'ok',true,
  'kind',p_kind,
  'offset',p_offset,
  'nextOffset',next_offset,
  'decks',selected_decks,
  'preparedUnits',prepared_units,
  'submittedUnits',0,
  'totalDecks',total_decks
 );
end $$;

revoke all on function public.ai_review_backfill(uuid,uuid,text,integer,integer) from public,anon,authenticated;
grant execute on function public.ai_review_backfill(uuid,uuid,text,integer,integer) to service_role;

commit;