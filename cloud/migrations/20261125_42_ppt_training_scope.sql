-- PPT training scope: deck-wide, arbitrary single-page, and legacy cover.
-- Page labels fall back to the complete-deck label only when no real page label exists.
begin;

create table if not exists guanye_internal.ppt_training_labels (
 id bigint generated always as identity primary key,
 workspace_id uuid not null references public.workspaces(id) on delete cascade,
 deck_id text not null references public.decks(id) on delete cascade,
 page_id text,
 scope text not null check(scope in ('deck','page','cover')),
 label_scope text not null check(label_scope in ('human-deck','human-page','deck-proxy')),
 visual numeric not null check(visual>=0),
 layout numeric not null check(layout>=0),
 source_review_user_id uuid references auth.users(id) on delete set null,
 source_review_data jsonb not null default '{}'::jsonb,
 provenance jsonb not null default '{}'::jsonb,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 check((scope='deck' and page_id is null) or (scope in ('page','cover') and page_id is not null)),
 check((scope='deck' and label_scope='human-deck') or (scope='page' and label_scope in ('human-page','deck-proxy')) or (scope='cover' and label_scope='deck-proxy'))
);
create unique index if not exists ppt_training_labels_deck_unique on guanye_internal.ppt_training_labels(workspace_id,deck_id,scope) where scope='deck';
create unique index if not exists ppt_training_labels_page_unique on guanye_internal.ppt_training_labels(workspace_id,deck_id,page_id,scope) where scope in ('page','cover');
create index if not exists ppt_training_labels_lookup on guanye_internal.ppt_training_labels(workspace_id,scope,deck_id,page_id);
alter table guanye_internal.ppt_training_labels enable row level security;
revoke all on guanye_internal.ppt_training_labels from public,anon,authenticated,service_role;

create or replace function public.single_judge_ppt_training_samples(w uuid,p_scope text,p_deck_id text default null,p_page_id text default null,p_limit integer default 200)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare result jsonb; limit_value integer:=greatest(1,least(coalesce(p_limit,200),2000));
begin
 if not exists(select 1 from public.members where workspace_id=w and user_id=auth.uid()) then raise exception '请先加入团队'; end if;
 if p_scope not in ('deck','page','cover') then raise exception '训练范围无效'; end if;
 if p_scope='deck' and p_page_id is not null then raise exception '整套训练不接受 pageId'; end if;
 if p_scope in ('page','cover') and p_deck_id is null then raise exception '单页训练需要 deckId'; end if;
 with deck_rows as (
  select d.id,d.data,
   coalesce((select r.data from public.reviews r where r.workspace_id=w and r.deck_id=d.id order by (r.data->>'updatedAt') desc nulls last,r.user_id limit 1),'{}'::jsonb) as review_data,
   (select r.user_id from public.reviews r where r.workspace_id=w and r.deck_id=d.id order by (r.data->>'updatedAt') desc nulls last,r.user_id limit 1) as review_user
  from public.decks d where d.workspace_id=w and d.source_kind='uploaded' and (p_deck_id is null or d.id=p_deck_id) order by d.id limit limit_value
 ), pages as (
  select d.id,d.data,d.review_data,d.review_user,im.item as image,im.ord::integer as position,coalesce(nullif(d.data->>'title',''),nullif(d.data->>'name',''),d.id) as title
  from deck_rows d cross join lateral jsonb_array_elements(coalesce(d.data->'images','[]'::jsonb)) with ordinality im(item,ord)
  where p_scope in ('page','cover') and (p_scope='page' or im.ord=1) and (p_page_id is null or im.item->>'id'=p_page_id)
 ), page_labels as (
  select p.*,l.label_scope as stored_label_scope,l.visual as stored_visual,l.layout as stored_layout,l.source_review_user_id as stored_source_user,l.source_review_data as stored_source_data,l.provenance as stored_provenance
  from pages p left join lateral (select x.* from guanye_internal.ppt_training_labels x where x.workspace_id=w and x.deck_id=p.id and x.page_id=p.image->>'id' and x.scope=p_scope limit 1) l on true
 ), deck_labels as (
  select d.*,l.label_scope as stored_label_scope,l.visual as stored_visual,l.layout as stored_layout,l.source_review_user_id as stored_source_user,l.source_review_data as stored_source_data,l.provenance as stored_provenance
  from deck_rows d left join lateral (select x.* from guanye_internal.ppt_training_labels x where x.workspace_id=w and x.deck_id=d.id and x.page_id is null and x.scope='deck' limit 1) l on true
 ), normalized_pages as (
  select p.*,case when p.stored_visual is not null and p.stored_layout is not null then jsonb_build_object('visual',p.stored_visual,'layout',p.stored_layout) when jsonb_typeof(p.review_data #> array['pages',p.image->>'id','scores'])='object' then p.review_data #> array['pages',p.image->>'id','scores'] else coalesce(p.review_data->'scores','{}'::jsonb) end as scores,
   case when p.stored_visual is not null and p.stored_layout is not null then p.stored_label_scope when jsonb_typeof(p.review_data #> array['pages',p.image->>'id','scores'])='object' then 'human-page' else 'deck-proxy' end as effective_label_scope
  from page_labels p
 ), normalized_decks as (
  select d.*,case when d.stored_visual is not null and d.stored_layout is not null then jsonb_build_object('visual',d.stored_visual,'layout',d.stored_layout) else coalesce(d.review_data->'scores','{}'::jsonb) end as scores,
   case when d.stored_visual is not null and d.stored_layout is not null then d.stored_label_scope else 'human-deck' end as effective_label_scope
  from deck_labels d
 )
 select coalesce(jsonb_agg(sample order by (sample->>'deckId'),coalesce((sample->>'position')::integer,0)),'[]'::jsonb) into result
 from (
  select jsonb_build_object('scope','deck','labelScope',d.effective_label_scope,'deckId',d.id,'pageId',null,'position',null,'title',coalesce(nullif(d.data->>'title',''),nullif(d.data->>'name',''),d.id),'images',coalesce(d.data->'images','[]'::jsonb),'scores',d.scores,'sourceReviewUserId',d.review_user,'sourceReview',d.review_data,'provenance',coalesce(d.data->'trainingImport','{}'::jsonb)) as sample from normalized_decks d where p_scope='deck'
  union all
  select jsonb_build_object('scope',p_scope,'labelScope',p.effective_label_scope,'deckId',p.id,'pageId',p.image->>'id','position',p.position,'title',p.title,'image',p.image,'images',jsonb_build_array(p.image),'scores',p.scores,'sourceReviewUserId',p.review_user,'sourceReview',p.review_data,'provenance',coalesce(p.data->'trainingImport','{}'::jsonb)) as sample from normalized_pages p
 ) samples;
 return result;
end $$;

create or replace function public.single_judge_ppt_training_counts(w uuid)
returns jsonb language sql stable security definer set search_path='' as $$
 with base as (
  select d.id,d.data,exists(select 1 from public.reviews r where r.workspace_id=w and r.deck_id=d.id and jsonb_typeof(r.data->'scores')='object') as scored,
   exists(select 1 from guanye_internal.ppt_training_labels l where l.workspace_id=w and l.deck_id=d.id and l.scope='deck') as stored_deck,
   (select count(*) from guanye_internal.ppt_training_labels l where l.workspace_id=w and l.deck_id=d.id and l.scope in ('page','cover')) as stored_pages
  from public.decks d where d.workspace_id=w and d.source_kind='uploaded' and exists(select 1 from public.members m where m.workspace_id=w and m.user_id=auth.uid())
 ), totals as (select count(*)::integer as decks,coalesce(sum(jsonb_array_length(coalesce(data->'images','[]'::jsonb))),0)::integer as pages,count(*) filter(where scored)::integer as scored_decks,coalesce(sum(stored_pages),0)::integer as stored_page_labels,count(*) filter(where stored_deck)::integer as stored_deck_labels from base)
 select jsonb_build_object('decks',decks,'pages',pages,'scoredDecks',scored_decks,'scope','deck/page/cover','labelScopes',jsonb_build_object('human-deck',stored_deck_labels,'human-page',stored_page_labels,'deck-proxy',greatest(pages-stored_page_labels,0))) from totals;
$$;

create or replace function public.single_judge_publish_training_library(w uuid,actor uuid,deck jsonb,review jsonb,p_source_group_sha256 text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare existing public.decks; existing_review jsonb;
begin
 if w is null or actor is null or p_source_group_sha256 is null or p_source_group_sha256 !~ '^[0-9a-f]{64}$' then raise exception '训练素材来源无效' using errcode='22023'; end if;
 if not exists(select 1 from public.members where workspace_id=w and user_id=actor) then raise exception '请先加入团队' using errcode='42501'; end if;
 if jsonb_typeof(deck) is distinct from 'object' or jsonb_typeof(deck->'images') is distinct from 'array' then raise exception '训练作品无效' using errcode='22023'; end if;
 if jsonb_array_length(deck->'images') not between 1 and 600 then raise exception '训练作品页数无效' using errcode='22023'; end if;
 if jsonb_typeof(review) is distinct from 'object' or jsonb_typeof(review->'scores') is distinct from 'object' then raise exception '训练评分无效' using errcode='22023'; end if;
 if exists(select 1 from jsonb_each(review->'scores') s where jsonb_typeof(s.value)<>'number' or (s.value::text)::numeric<0 or (s.value::text)::numeric>5) then raise exception '训练评分超出范围' using errcode='22023'; end if;
 select * into existing from public.decks where id=deck->>'id' for update;
 if found then
  if existing.workspace_id<>w or existing.data is distinct from deck then raise exception '训练作品标识已绑定其他素材' using errcode='22023'; end if;
  select r.data into existing_review from public.reviews r where r.workspace_id=w and r.user_id=actor and r.deck_id=existing.id;
  if existing_review is not distinct from review then return jsonb_build_object('ok',true,'deckId',existing.id,'idempotent',true); end if;
  insert into public.reviews(workspace_id,user_id,deck_id,data) values(w,actor,existing.id,review) on conflict(workspace_id,user_id,deck_id) do update set data=excluded.data;
  return jsonb_build_object('ok',true,'deckId',existing.id,'idempotent',false);
 end if;
 perform public.ai_import_review_deck(w,actor,deck,p_source_group_sha256);
 insert into public.reviews(workspace_id,user_id,deck_id,data) values(w,actor,deck->>'id',review);
 return jsonb_build_object('ok',true,'deckId',deck->>'id','idempotent',false);
end $$;

revoke all on function public.single_judge_ppt_training_samples(uuid,text,text,text,integer),public.single_judge_ppt_training_counts(uuid),public.single_judge_publish_training_library(uuid,uuid,jsonb,jsonb,text) from public,anon,authenticated,service_role;
grant execute on function public.single_judge_ppt_training_samples(uuid,text,text,text,integer),public.single_judge_ppt_training_counts(uuid) to authenticated;
grant execute on function public.single_judge_publish_training_library(uuid,uuid,jsonb,jsonb,text) to service_role;

commit;
