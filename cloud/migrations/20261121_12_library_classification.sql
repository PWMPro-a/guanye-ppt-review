-- Library classification: deck-level effective metadata from authoritative human evidence.
begin;

create or replace function public.ai_review_effective_meta(w uuid, target_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare
 deck_data jsonb;
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
 class_source text='none';
 style_source text='none';
begin
 -- LIBRARY_CLASSIFICATION_V12
 select data into deck_data from public.decks where workspace_id=w and id=target_id;
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

 if has_submission then
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

commit;
