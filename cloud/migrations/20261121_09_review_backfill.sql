-- Backfill legacy review coverage through the existing prepare contract.
begin;

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
begin
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

  if has_whole_rating or coalesce(array_length(page_ids,1),0)=0 then
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