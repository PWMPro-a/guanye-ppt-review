-- UPLOADED_COMPARISON_SOURCE_DEDUP_M21
-- Deduplicate the accepted M20 uploaded holdout observations by trusted M19
-- source SHA256. Generated observations and decks without provenance keep the
-- existing per-deck treatment.
--
-- Group semantics follow scikit-learn GroupShuffleSplit at fixed commit
-- f85f0584d5d7a66068bfe50b395ac855b2289dc2: a split unit is the source group,
-- not an arbitrary row carrying the same original bytes. MAE remains the mean
-- of sample-level absolute errors after eligible samples are selected:
-- sklearn/metrics/_regression.py at the same commit, mean_absolute_error.
--
-- This migration only creates a private selection helper and replaces
-- public.ai_review_comparison(uuid). It does not rewrite decks, reviews,
-- submissions, matches, records, decisions, jobs or queue/storage state.
begin;

grant usage on schema guanye_internal to service_role;

create or replace function guanye_internal.ai_review_comparison_source_dedup(
 w uuid,
 p_candidate_deck_ids text[]
) returns table (
 deck_id text,
 source_known boolean,
 retained boolean
)
language sql
stable
security definer
set search_path=''
as $m21_dedup$
 with candidates as (
  select d.id,d.created_at,p.source_group_sha256
  from public.decks d
  left join guanye_internal.uploaded_source_provenance p
    on p.workspace_id=w and p.deck_id=d.id
  where w is not null
   and d.workspace_id=w
   and d.source_kind='uploaded'
   and d.id=any(coalesce(p_candidate_deck_ids,array[]::text[]))
 ),
 ranked_known as (
  select c.id,
   row_number() over (
    partition by c.source_group_sha256
    order by c.created_at,c.id collate "C"
   ) as representative_rank
  from candidates c
  where c.source_group_sha256 is not null
 )
 select c.id,
  c.source_group_sha256 is not null as source_known,
  case
   when c.source_group_sha256 is null then true
   else coalesce(r.representative_rank=1,false)
  end as retained
 from candidates c
 left join ranked_known r on r.id=c.id
 order by c.id collate "C"
$m21_dedup$;

revoke all on function guanye_internal.ai_review_comparison_source_dedup(uuid,text[])
 from public,anon,authenticated;
grant execute on function guanye_internal.ai_review_comparison_source_dedup(uuid,text[])
 to service_role;

create or replace function public.ai_review_comparison(w uuid)
returns jsonb
language sql
stable
security invoker
set search_path=''
as $comparison$
-- COMPARISON_SOURCE_DEDUP_V21
-- SOURCE_GROUP_ROUTING_V20 remains the M20 holdout route for this replacement.
with
context as (
 select
  public.ai_review_rubric_version(w) as rubric_version,
  coalesce((select (ws.settings->>'maxScore')::numeric from public.workspaces ws where ws.id=w),5) as max_score
 where w is not null
),
formal_candidate_records as (
 select
  s.id as submission_id,r.deck_id,r.variant,r.ai_result,s.entry,s.source_kind,s.max_score,
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
 select b.submission_id,b.deck_id,b.entry,b.source_kind,b.max_score,
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
 select e.sample_key,e.source_kind,e.deck_id,e.entry,e.baseline_result,e.assisted_result,e.max_score
 from legacy_sample_entries e
 where jsonb_typeof(e.entry)='object'
  and public.ai_review_scores_complete(w,e.deck_id,e.entry,e.max_score)
),
holdout_samples as (
 select ('submission:'||submission_id::text)::text as sample_key,deck_id,source_kind,entry,
  baseline_result,assisted_result,max_score from formal_pairs
 union all
 select sample_key,deck_id,source_kind,entry,baseline_result,assisted_result,max_score from legacy_samples
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
 select d.sample_key,s.source_kind,s.deck_id,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d join holdout_samples s on s.sample_key=d.sample_key
 group by d.sample_key,s.source_kind,s.deck_id
),
qualified_uploaded_decks as (
 select coalesce(array_agg(q.deck_id order by q.deck_id collate "C"),array[]::text[]) as deck_ids
 from (select distinct s.deck_id from sample_errors s where s.source_kind='uploaded') q
),
dedup_decks as (
 select h.deck_id,h.source_known,h.retained
 from qualified_uploaded_decks q
 cross join lateral guanye_internal.ai_review_comparison_source_dedup(w,q.deck_ids) h
),
kept_sample_errors as (
 select s.sample_key,s.source_kind,s.deck_id,s.baseline_mae,s.assisted_mae
 from sample_errors s
 where s.source_kind='generated'
  or exists(
   select 1 from dedup_decks d where d.deck_id=s.deck_id and d.retained
  )
),
source_dedup as (
 select jsonb_build_object(
  'knownGroups',count(*) filter(where d.source_known and d.retained)::integer,
  'unknownDecks',count(*) filter(where not d.source_known)::integer,
  'excludedDuplicateDecks',count(*) filter(where d.source_known and not d.retained)::integer
 ) as value
 from dedup_decks d
),
overall_stats as (
 select count(*)::integer as unit_count,avg(baseline_mae) as baseline_mae,avg(assisted_mae) as assisted_mae
 from kept_sample_errors
),
overall_dimension_values as (
 select d.dimension,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d
 join kept_sample_errors s on s.sample_key=d.sample_key
 group by d.dimension
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
 from kinds k left join kept_sample_errors s on s.source_kind=k.source_kind
 group by k.source_kind
),
kind_dimension_values as (
 select s.source_kind,d.dimension,avg(d.baseline_error) as baseline_mae,avg(d.assisted_error) as assisted_mae
 from dimension_errors d join kept_sample_errors s on s.sample_key=d.sample_key
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
  'uploaded',(select value from kind_json where source_kind='uploaded') ||
   jsonb_build_object('sourceDedup',(select value from source_dedup)),
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

do $m21_guard$
declare
 helper_oid oid;
 comparison_oid oid;
 provenance_oid oid;
begin
 helper_oid:=to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])');
 comparison_oid:=to_regprocedure('public.ai_review_comparison(uuid)');
 provenance_oid:=to_regclass('guanye_internal.uploaded_source_provenance');
 if helper_oid is null then raise exception 'M21 comparison dedup helper missing'; end if;
 if not exists(
  select 1 from pg_proc p
  where p.oid=helper_oid and p.prosecdef and p.proretset
   and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
              where config.value in ('search_path=','search_path=""'))
 ) then raise exception 'M21 comparison dedup helper security posture invalid'; end if;
 if comparison_oid is null or not exists(
  select 1 from pg_proc p
  where p.oid=comparison_oid and not p.prosecdef
   and position('COMPARISON_SOURCE_DEDUP_V21' in p.prosrc)>0
   and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
              where config.value in ('search_path=','search_path=""'))
 ) then raise exception 'M21 comparison security posture invalid'; end if;
 if provenance_oid is null
    or has_table_privilege('service_role',provenance_oid,'SELECT') then
  raise exception 'M21 must not grant direct provenance reads';
 end if;
 if not has_function_privilege('service_role',helper_oid,'EXECUTE')
    or has_function_privilege('authenticated',helper_oid,'EXECUTE')
    or has_function_privilege('anon',helper_oid,'EXECUTE')
    or not has_function_privilege('service_role',comparison_oid,'EXECUTE')
    or has_function_privilege('authenticated',comparison_oid,'EXECUTE')
    or has_function_privilege('anon',comparison_oid,'EXECUTE') then
  raise exception 'M21 function ACL invalid';
 end if;
end
$m21_guard$;

commit;
