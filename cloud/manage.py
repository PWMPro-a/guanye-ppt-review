"""Supabase Management API setup without browser automation.

Token: cloud/private-token.txt or SUPABASE_ACCESS_TOKEN. No secrets are printed.
Commands are incremental; a project is created only when explicitly requested.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
PRIVATE=HERE/'private-deployment.json'
SITE='https://pwmpro-a.github.io/guanye-ppt-review/'
PROJECT_REF_RE=re.compile(r'^[a-z0-9]{20}$')
MIGRATION_TABLE='guanye_internal.schema_migrations'
BASE_VERSION='0000_base_schema'
COVER_PILOT_VERSION='20261123_38_cover_training'
COVER_HUMAN_FIRST_VERSION='20261123_39_cover_after_human'
COVER_PROGRESS_VERSION='20261124_40_cover_progress'
COVER_VISUAL_VERSION='20261124_41_cover_visual_reference'
COVER_PILOT_VERSIONS=(COVER_PILOT_VERSION,COVER_HUMAN_FIRST_VERSION,COVER_PROGRESS_VERSION,COVER_VISUAL_VERSION)
MIGRATION_SPECS=(
    ('20261121_01_ai_review_foundation','20261121_01_ai_review_foundation.sql'),
    ('20261121_02_ai_reviews','20261121_02_ai_reviews.sql'),
    ('20261121_03_ai_planning','20261121_03_ai_planning.sql'),
    ('20261121_04_ai_generation','20261121_04_ai_generation.sql'),
    ('20261121_05_review_queue','20261121_05_review_queue.sql'),
    ('20261121_06_generation_history','20261121_06_generation_history.sql'),
    ('20261121_07_review_calibration','20261121_07_review_calibration.sql'),
    ('20261121_08_review_comparison','20261121_08_review_comparison.sql'),
    ('20261121_09_review_backfill','20261121_09_review_backfill.sql'),
    ('20261121_10_review_queue_listing','20261121_10_review_queue_listing.sql'),
    ('20261121_11_planning_history','20261121_11_planning_history.sql'),
    ('20261121_12_library_classification','20261121_12_library_classification.sql'),
    ('20261121_13_formal_correction','20261121_13_formal_correction.sql'),
    ('20261121_14_library_formal_scores','20261121_14_library_formal_scores.sql'),
    ('20261121_15_planning_original_references','20261121_15_planning_original_references.sql'),
    ('20261121_16_uploaded_baseline','20261121_16_uploaded_baseline.sql'),
    ('20261121_17_review_workspace_batch','20261121_17_review_workspace_batch.sql'),
    ('20261121_18_review_backfill_classification','20261121_18_review_backfill_classification.sql'),
    ('20261121_19_uploaded_source_provenance','20261121_19_uploaded_source_provenance.sql'),
    ('20261121_20_uploaded_source_groups','20261121_20_uploaded_source_groups.sql'),
    ('20261121_21_uploaded_comparison_source_dedup','20261121_21_uploaded_comparison_source_dedup.sql'),
    ('20261122_22_single_judge_foundation','20261122_22_single_judge_foundation.sql'),
    ('20261122_23_single_judge_original_archive','20261122_23_single_judge_original_archive.sql'),
    ('20261122_24_single_judge_submission','20261122_24_single_judge_submission.sql'),
    ('20261122_25_image_workers_default','20261122_25_image_workers_default.sql'),
    ('20261122_26_single_judge_score_revisions','20261122_26_single_judge_score_revisions.sql'),
    ('20261122_27_single_judge_review_workspace','20261122_27_single_judge_review_workspace.sql'),
    ('20261122_28_single_judge_admission','20261122_28_single_judge_admission.sql'),
    ('20261122_29_single_judge_generation_references','20261122_29_single_judge_generation_references.sql'),
    ('20261122_30_single_judge_creation','20261122_30_single_judge_creation.sql'),
    ('20261122_31_single_judge_image_runtime','20261122_31_single_judge_image_runtime.sql'),
    ('20261122_32_single_judge_legacy_backfill','20261122_32_single_judge_legacy_backfill.sql'),
    ('20261122_33_single_judge_review_queue','20261122_33_single_judge_review_queue.sql'),
    ('20261122_34_single_judge_owner_activation','20261122_34_single_judge_owner_activation.sql'),
    ('20261122_35_single_judge_intake','20261122_35_single_judge_intake.sql'),
    (COVER_PILOT_VERSION,'20261123_38_cover_training.sql'),
    (COVER_HUMAN_FIRST_VERSION,'20261123_39_cover_after_human.sql'),
    (COVER_PROGRESS_VERSION,'20261124_40_cover_progress.sql'),
    (COVER_VISUAL_VERSION,'20261124_41_cover_visual_reference.sql'),
)
HISTORY_DDL=f"""create schema if not exists guanye_internal;
revoke all on schema guanye_internal from public, anon, authenticated;
create table if not exists {MIGRATION_TABLE} (
 version text primary key,
 checksum text not null check (checksum ~ '^[0-9a-f]{{64}}$'),
 applied_at timestamptz not null default now()
);
alter table {MIGRATION_TABLE} enable row level security;
revoke all on {MIGRATION_TABLE} from public, anon, authenticated;"""


def _validated_project_ref(ref):
    if not isinstance(ref,str) or not PROJECT_REF_RE.fullmatch(ref):
        raise RuntimeError('Invalid Supabase project ref; expected exactly 20 lowercase letters or digits')
    return ref


def _sql_literal(value):
    return "'"+str(value).replace("'","''")+"'"


def _normalize_lf(text):
    return text.replace('\r\n','\n').replace('\r','\n')


def _text_checksum(text):
    return hashlib.sha256(_normalize_lf(text).encode('utf-8')).hexdigest()


def _file_checksum(path):
    return _text_checksum(Path(path).read_bytes().decode('utf-8'))


def _strip_outer_transaction(sql):
    text=_normalize_lf(sql).strip()
    begin=re.search(r'(?im)^[ \t]*begin[ \t]*;[ \t]*$',text)
    if begin:
        prefix=text[:begin.start()]
        if any(line.strip() and not line.lstrip().startswith('--') for line in prefix.splitlines()):
            raise RuntimeError('Migration transaction control must appear before SQL')
        text=text[:begin.start()]+text[begin.end():]
    commit=re.search(r'(?is)\n[ \t]*commit[ \t]*;[ \t]*\s*\Z',text)
    if commit:
        text=text[:commit.start()].rstrip()
    if re.search(r'(?im)^[ \t]*(begin|commit|rollback)[ \t]*;[ \t]*$',text):
        raise RuntimeError('Migration contains transaction control outside its outer wrapper')
    return text.strip()


def _atomic_sql(body):
    body=body.strip()
    if not body.endswith(';'):
        body+=';'
    for tag in ('$guanye_migration$','$guanye_migration_body$'):
        if tag in body:
            raise RuntimeError('Migration body uses a reserved dollar-quote tag')
    return f"""do $guanye_migration$
begin
 execute $guanye_migration_body$
{body}
$guanye_migration_body$;
end
$guanye_migration$;"""


def _migration_sql(version,checksum,body):
    record=f"insert into {MIGRATION_TABLE}(version,checksum) values ({_sql_literal(version)}, {_sql_literal(checksum)});"
    return _atomic_sql(body.rstrip()+'\n'+record)


def _migration_catalog(directory=None):
    directory=HERE/'migrations' if directory is None else Path(directory)
    catalog=[]
    for version,filename in MIGRATION_SPECS:
        path=directory/filename
        text=_normalize_lf(path.read_bytes().decode('utf-8'))
        catalog.append({
            'version':version,
            'filename':filename,
            'checksum':_text_checksum(text),
            'body':_strip_outer_transaction(text),
        })
    return catalog


def _schema_checksum(path):
    return _file_checksum(path)


def _base_sql(schema_sql,bootstrap_sql,checksum):
    body=(HISTORY_DDL+'\n'+_normalize_lf(schema_sql).strip()+'\n'+_normalize_lf(bootstrap_sql).strip()+
          f"\ninsert into {MIGRATION_TABLE}(version,checksum) values ({_sql_literal(BASE_VERSION)}, {_sql_literal(checksum)});")
    return _atomic_sql(body)


def _adoption_sql(catalog,base_checksum):
    values=[(BASE_VERSION,base_checksum)]+[(item['version'],item['checksum']) for item in catalog]
    rows=',\n '.join(f"({_sql_literal(version)}, {_sql_literal(checksum)})" for version,checksum in values)
    return _atomic_sql(HISTORY_DDL+f"\ninsert into {MIGRATION_TABLE}(version,checksum) values\n {rows};")


def _coerce_query_rows(result):
    if result is None:
        return []
    if isinstance(result,dict):
        if isinstance(result.get('result'),list):
            result=result['result']
        else:
            result=[result]
    if not isinstance(result,list):
        raise RuntimeError('Unexpected Supabase database query response')
    return result


def _query_rows(api,ref,sql):
    ref=_validated_project_ref(ref)
    return _coerce_query_rows(api('/v1/projects/'+ref+'/database/query',{'query':sql}))


def _read_only_query_rows(api,ref,sql):
    ref=_validated_project_ref(ref)
    if not sql.lstrip().lower().startswith('select'):
        raise RuntimeError('Migration preflight only permits metadata SELECT queries')
    return _coerce_query_rows(api('/v1/projects/'+ref+'/database/query',{'query':sql,'read_only':True}))


def _query_one(api,ref,sql,read_only=False):
    rows=(_read_only_query_rows if read_only else _query_rows)(api,ref,sql)
    return rows[0] if rows else {}


def _database_state(api,ref,read_only=False):
    row=_query_one(api,ref,"""select
 to_regclass('public.workspaces') is not null as workspaces,
 to_regclass('public.members') is not null as members,
 to_regclass('public.decks') is not null as decks,
 to_regclass('public.reviews') is not null as reviews,
 to_regclass('guanye_internal.schema_migrations') is not null as migration_history;""",read_only=read_only)
    return {key:bool(row.get(key)) for key in ('workspaces','members','decks','reviews','migration_history')}


def _history_rows(api,ref,read_only=False):
    query=_read_only_query_rows if read_only else _query_rows
    rows=query(api,ref,f"select version,checksum from {MIGRATION_TABLE} order by version;")
    return {str(row['version']):str(row.get('checksum') or '') for row in rows}


def _existing_schema_probes(api,ref,read_only=False):
    return _query_one(api,ref,"""select
 (to_regprocedure('public.get_ai_review_sample_snapshot(uuid)') is not null
  and exists(select 1 from information_schema.columns where table_schema='public' and table_name='workspaces' and column_name='dedicated_judge_id')
  and exists(select 1 from information_schema.columns where table_schema='public' and table_name='decks' and column_name='source_kind')) as m01,
 (to_regclass('public.ai_review_settings') is not null
  and to_regclass('public.ai_review_jobs') is not null
  and to_regclass('public.ai_review_records') is not null
  and to_regprocedure('public.get_review_library(uuid)') is not null) as m02,
 (to_regclass('public.ai_planning_plans') is not null
  and to_regclass('public.ai_planning_versions') is not null
  and to_regprocedure('public.ai_planning_read_confirmed_plan(uuid)') is not null) as m03,
 (to_regclass('public.ai_generation_jobs') is not null
  and to_regclass('public.ai_generation_pages') is not null
  and to_regprocedure('public.ai_generation_claim_pages(uuid,uuid,text,integer,integer)') is not null) as m04,
 (exists(select 1 from information_schema.columns where table_schema='public' and table_name='ai_review_jobs' and column_name='available_at')
  and to_regprocedure('public.ai_review_finish_attempt(uuid,uuid,uuid,integer,text,jsonb,text,boolean)') is not null
  and exists(select 1 from pg_trigger where tgname='ai_review_enqueue_preview_after_deck' and tgrelid='public.decks'::regclass and not tgisinternal)) as m05,
 (to_regprocedure('public.ai_generation_recent(uuid,uuid)') is not null
  and to_regclass('public.ai_generation_jobs_recent_idx') is not null) as m06,
  (to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)') is not null
   and has_function_privilege('service_role','public.ai_review_calibration(uuid,uuid,text,integer,text,text)','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_calibration(uuid,uuid,text,integer,text,text)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_calibration(uuid,uuid,text,integer,text,text)','EXECUTE')) as m07,
 (to_regprocedure('public.ai_review_comparison(uuid)') is not null
  and has_function_privilege('service_role','public.ai_review_comparison(uuid)','EXECUTE')
  and not has_function_privilege('anon','public.ai_review_comparison(uuid)','EXECUTE')
  and not has_function_privilege('authenticated','public.ai_review_comparison(uuid)','EXECUTE')) as m08,
 (to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)') is not null
  and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)') and p.prosecdef)
  and has_function_privilege('service_role','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE')
  and not has_function_privilege('anon','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE')
  and not has_function_privilege('authenticated','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE')) as m09,
 (to_regprocedure('public.ai_review_queue_decks(uuid,text,text,integer)') is not null
  and to_regprocedure('public.ai_review_queue_counts(uuid)') is not null
  and to_regprocedure('public.ai_review_queue_jobs(uuid,text)') is not null
  and exists(select 1 from pg_proc p where p.oid in (
   to_regprocedure('public.ai_review_queue_decks(uuid,text,text,integer)'),
   to_regprocedure('public.ai_review_queue_counts(uuid)'),
   to_regprocedure('public.ai_review_queue_jobs(uuid,text)')) and not p.prosecdef)
  and has_function_privilege('service_role','public.ai_review_queue_decks(uuid,text,text,integer)','EXECUTE')
  and has_function_privilege('service_role','public.ai_review_queue_counts(uuid)','EXECUTE')
  and has_function_privilege('service_role','public.ai_review_queue_jobs(uuid,text)','EXECUTE')
  and not has_function_privilege('anon','public.ai_review_queue_decks(uuid,text,text,integer)','EXECUTE')
  and not has_function_privilege('anon','public.ai_review_queue_counts(uuid)','EXECUTE')
  and not has_function_privilege('anon','public.ai_review_queue_jobs(uuid,text)','EXECUTE')
  and not has_function_privilege('authenticated','public.ai_review_queue_decks(uuid,text,text,integer)','EXECUTE')
  and not has_function_privilege('authenticated','public.ai_review_queue_counts(uuid)','EXECUTE')
  and not has_function_privilege('authenticated','public.ai_review_queue_jobs(uuid,text)','EXECUTE')
  and to_regclass('public.ai_review_queue_decks_page_idx') is not null
  and to_regclass('public.ai_review_queue_jobs_all_order_idx') is not null
   and to_regclass('public.ai_review_queue_jobs_kind_order_idx') is not null) as m10,
  (to_regprocedure('public.ai_planning_list_my_plans(uuid,uuid,integer)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_planning_list_my_plans(uuid,uuid,integer)')
      and p.prosecdef
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value like 'search_path=%'))
   and has_function_privilege('authenticated','public.ai_planning_list_my_plans(uuid,uuid,integer)','EXECUTE')
   and not has_function_privilege('anon','public.ai_planning_list_my_plans(uuid,uuid,integer)','EXECUTE')
   and not has_function_privilege('service_role','public.ai_planning_list_my_plans(uuid,uuid,integer)','EXECUTE')
   and to_regclass('public.ai_planning_plans_owner_history_idx') is not null) as m11,
  (to_regprocedure('public.ai_review_effective_meta(uuid,text)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_effective_meta(uuid,text)')
      and p.prosecdef
      and (position('LIBRARY_CLASSIFICATION_V12' in p.prosrc)>0
           or position('LIBRARY_CLASSIFICATION_BACKFILL_V18' in p.prosrc)>0)
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value like 'search_path=%'))
   and has_function_privilege('service_role','public.ai_review_effective_meta(uuid,text)','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_effective_meta(uuid,text)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_effective_meta(uuid,text)','EXECUTE')) as m12,
  (to_regprocedure('public.ai_review_confirm(uuid,uuid,jsonb)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_confirm(uuid,uuid,jsonb)')
      and p.prosecdef
      and position('FORMAL_CORRECTION_V13' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value like 'search_path=%'))
   and has_function_privilege('service_role','public.ai_review_confirm(uuid,uuid,jsonb)','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_confirm(uuid,uuid,jsonb)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_confirm(uuid,uuid,jsonb)','EXECUTE')) as m13,
  (to_regprocedure('public.review_score_summary(uuid)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.review_score_summary(uuid)')
      and p.prosecdef
      and position('LIBRARY_FORMAL_SQL_V14' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value like 'search_path=%'))
   and has_function_privilege('authenticated','public.review_score_summary(uuid)','EXECUTE')
   and not has_function_privilege('anon','public.review_score_summary(uuid)','EXECUTE')
   and to_regprocedure('public.ai_review_uploaded_formal_scores(uuid)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_uploaded_formal_scores(uuid)')
      and p.prosecdef
      and position('LIBRARY_FORMAL_SQL_V14' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value like 'search_path=%'))
   and has_function_privilege('service_role','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE')) as m14,
  (to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)')
      and p.prosecdef
      and position('PLANNING_ORIGINAL_REFERENCES_V15' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and has_function_privilege('service_role','public.ai_planning_reference_candidates(uuid,integer)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_planning_reference_candidates(uuid,integer)','EXECUTE')
   and not has_function_privilege('anon','public.ai_planning_reference_candidates(uuid,integer)','EXECUTE')) as m15,
  (to_regprocedure('public.ai_review_prepare(uuid,uuid,jsonb)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_prepare(uuid,uuid,jsonb)')
      and p.prosecdef
      and position('UPLOADED_BASELINE_V16' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and has_function_privilege('service_role','public.ai_review_prepare(uuid,uuid,jsonb)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_prepare(uuid,uuid,jsonb)','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_prepare(uuid,uuid,jsonb)','EXECUTE')) as m16,
  (to_regprocedure('public.ai_review_workspace_batch(uuid,uuid,text,uuid[])') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_workspace_batch(uuid,uuid,text,uuid[])')
      and not p.prosecdef
      and position('REVIEW_WORKSPACE_BATCH_V17' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and has_function_privilege('service_role','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','EXECUTE')) as m17,
  (to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)')
      and p.prosecdef
      and position('REVIEW_BACKFILL_CLASSIFICATION_V18' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and to_regprocedure('public.ai_review_effective_meta(uuid,text)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_effective_meta(uuid,text)')
      and p.prosecdef
      and position('LIBRARY_CLASSIFICATION_BACKFILL_V18' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and has_function_privilege('service_role','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE')
   and has_function_privilege('service_role','public.ai_review_effective_meta(uuid,text)','EXECUTE')
   and not has_function_privilege('authenticated','public.ai_review_effective_meta(uuid,text)','EXECUTE')
   and not has_function_privilege('anon','public.ai_review_effective_meta(uuid,text)','EXECUTE')) as m18,
  (to_regclass('guanye_internal.uploaded_source_provenance') is not null
   and (select count(*) from information_schema.columns
    where table_schema='guanye_internal' and table_name='uploaded_source_provenance'
      and column_name in ('deck_id','workspace_id','source_group_sha256','created_at'))=4
   and exists(select 1 from pg_constraint c
    where c.conname='uploaded_source_provenance_deck_fk'
      and c.conrelid=to_regclass('guanye_internal.uploaded_source_provenance')
      and c.confrelid=to_regclass('public.decks')
      and c.contype='f' and c.condeferrable and c.condeferred and c.confdeltype='c')
   and to_regprocedure('guanye_internal.uploaded_source_provenance_workspace_guard()') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('guanye_internal.uploaded_source_provenance_workspace_guard()')
      and p.prosecdef
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and exists(select 1 from pg_trigger t
    where t.tgname='uploaded_source_provenance_workspace_guard'
      and t.tgrelid=to_regclass('guanye_internal.uploaded_source_provenance')
      and t.tgdeferrable and t.tginitdeferred and not t.tgisinternal)
   and to_regclass('guanye_internal.uploaded_source_provenance_source_group_idx') is not null
   and to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)')
      and p.prosecdef
      and position('UPLOADED_SOURCE_PROVENANCE_M19' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and has_function_privilege('service_role',to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)'),'EXECUTE')
   and not has_function_privilege('authenticated',to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)'),'EXECUTE')
   and not has_function_privilege('anon',to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)'),'EXECUTE')
   and to_regprocedure('public.ai_review_source_group_decks(uuid,text)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('public.ai_review_source_group_decks(uuid,text)')
      and p.prosecdef
      and position('UPLOADED_SOURCE_SPLIT_M19' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and has_function_privilege('service_role',to_regprocedure('public.ai_review_source_group_decks(uuid,text)'),'EXECUTE')
   and not has_function_privilege('authenticated',to_regprocedure('public.ai_review_source_group_decks(uuid,text)'),'EXECUTE')
   and not has_function_privilege('anon',to_regprocedure('public.ai_review_source_group_decks(uuid,text)'),'EXECUTE')
   and not has_table_privilege('service_role',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT')
   and not has_table_privilege('authenticated',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT')
   and not has_table_privilege('anon',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT')) as m19,
  (to_regprocedure('guanye_internal.ai_review_source_group_key(uuid,text,text)') is not null
   and to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)') is not null
   and to_regprocedure('guanye_internal.ai_review_has_source_groups(uuid)') is not null
   and exists(select 1 from pg_proc p
    where p.oid=to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)')
      and p.prosecdef
      and position('uploaded_source_provenance' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                 where config.value in ('search_path=','search_path=""')))
   and not has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_source_group_key(uuid,text,text)'),'EXECUTE')
   and has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE')
   and has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_has_source_groups(uuid)'),'EXECUTE')
   and not has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_source_group_key(uuid,text,text)'),'EXECUTE')
   and not has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE')
   and not has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_has_source_groups(uuid)'),'EXECUTE')
   and not has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_source_group_key(uuid,text,text)'),'EXECUTE')
   and not has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE')
   and not has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_has_source_groups(uuid)'),'EXECUTE')
   and has_schema_privilege('service_role','guanye_internal','USAGE')
   and not has_schema_privilege('authenticated','guanye_internal','USAGE')
   and not has_schema_privilege('anon','guanye_internal','USAGE')
   and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.get_ai_review_sample_snapshot(uuid)')
    and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0)
   and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)')
    and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0)
   and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_comparison(uuid)')
    and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0 and not p.prosecdef)
   and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)')
    and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0)
   and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_prepare(uuid,uuid,jsonb)')
    and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0)
   and has_function_privilege('service_role',to_regprocedure('public.get_ai_review_sample_snapshot(uuid)'),'EXECUTE')
   and has_function_privilege('service_role',to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)'),'EXECUTE')
   and has_function_privilege('service_role',to_regprocedure('public.ai_review_comparison(uuid)'),'EXECUTE')
   and has_function_privilege('service_role',to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)'),'EXECUTE')
   and has_function_privilege('service_role',to_regprocedure('public.ai_review_prepare(uuid,uuid,jsonb)'),'EXECUTE')
   and not has_function_privilege('authenticated',to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)'),'EXECUTE')
   and not has_function_privilege('anon',to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)'),'EXECUTE')) as m20,
   (to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])') is not null
    and exists(select 1 from pg_proc p
     where p.oid=to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])')
       and p.prosecdef and p.proretset
       and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                  where config.value in ('search_path=','search_path=""')))
    and to_regprocedure('public.ai_review_comparison(uuid)') is not null
    and exists(select 1 from pg_proc p
     where p.oid=to_regprocedure('public.ai_review_comparison(uuid)')
       and not p.prosecdef
       and position('COMPARISON_SOURCE_DEDUP_V21' in p.prosrc)>0
       and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value)
                  where config.value in ('search_path=','search_path=""')))
    and has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE')
    and not has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE')
    and has_function_privilege('service_role',to_regprocedure('public.ai_review_comparison(uuid)'),'EXECUTE')
    and not has_function_privilege('authenticated',to_regprocedure('public.ai_review_comparison(uuid)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.ai_review_comparison(uuid)'),'EXECUTE')
    and has_schema_privilege('service_role','guanye_internal','USAGE')
    and not has_table_privilege('service_role',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT')) as m21,
 (to_regprocedure('public.single_judge_activate(uuid,uuid,uuid)') is not null
    and to_regprocedure('public.single_judge_submit_bundle(uuid,jsonb)') is not null
    and to_regprocedure('public.single_judge_access(uuid)') is not null
    and exists(select 1 from pg_attribute where attrelid=to_regclass('public.workspaces')
      and attname='single_judge_admin_id' and not attisdropped)
    and exists(select 1 from pg_trigger where tgrelid=to_regclass('public.ai_review_submissions')
      and tgname='single_judge_guard_submission' and tgenabled='O')
    and exists(select 1 from pg_trigger where tgrelid=to_regclass('public.ai_review_matches')
      and tgname='single_judge_freeze_matches' and tgenabled='O')
    and not has_function_privilege('authenticated',to_regprocedure('public.single_judge_activate(uuid,uuid,uuid)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_submit_bundle(uuid,jsonb)'),'EXECUTE')
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_submit_bundle(uuid,jsonb)'),'EXECUTE')) as m22,
 (to_regclass('guanye_internal.single_judge_originals') is not null
    and to_regprocedure('public.single_judge_publish_import(uuid,uuid,jsonb,jsonb)') is not null
    and to_regprocedure('public.single_judge_original(uuid,text)') is not null
    and has_function_privilege('service_role',to_regprocedure('public.single_judge_publish_import(uuid,uuid,jsonb,jsonb)'),'EXECUTE')
    and not has_function_privilege('authenticated',to_regprocedure('public.single_judge_publish_import(uuid,uuid,jsonb,jsonb)'),'EXECUTE')
    and not has_table_privilege('authenticated',to_regclass('guanye_internal.single_judge_originals'),'SELECT')) as m23,
 (to_regprocedure('public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)') is not null
    and has_function_privilege('service_role',to_regprocedure('public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)'),'EXECUTE')
    and not has_function_privilege('authenticated',to_regprocedure('public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_publish_upload(uuid,uuid,jsonb,jsonb,boolean)'),'EXECUTE')
    and exists(select 1 from pg_attribute where attrelid=to_regclass('public.decks')
      and attname='ai_allowed' and attnotnull and not attisdropped)
    and exists(select 1 from pg_trigger where tgrelid=to_regclass('public.ai_review_jobs')
      and tgname='single_judge_manual_only_job_guard' and tgenabled='O')) as m24,
 (exists(select 1 from pg_attribute a join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum
      where a.attrelid=to_regclass('public.ai_generation_settings') and a.attname='max_workers'
      and pg_get_expr(d.adbin,d.adrelid)='16')
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_generation_claim_pages(uuid,uuid,text,integer,integer)')
      and position('p_limit integer DEFAULT 16' in pg_get_function_arguments(p.oid))>0)) as m25,
 (exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_submit_bundle(uuid,jsonb)')
      and position('guanye.single_judge_revision' in p.prosrc)>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.single_judge_freeze_matches()')
      and position('SINGLE_JUDGE_SCORE_REVISIONS_M26' in p.prosrc)>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_comparison(uuid)')
      and position('single_judge_first_submissions' in p.prosrc)>0)) as m26,
 (exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_review_workspace(uuid,text,text)')
      and p.prosecdef and position('SINGLE_JUDGE_REVIEW_WORKSPACE_M27' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as cfg(value)
        where cfg.value in ('search_path=','search_path=""')))
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_review_workspace(uuid,text,text)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_review_workspace(uuid,text,text)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.single_judge_review_workspace(uuid,text,text)'),'EXECUTE')) as m27,
 (exists(select 1 from pg_proc p where p.oid in (to_regprocedure('public.single_judge_admit(uuid,uuid,uuid)'),to_regprocedure('public.single_judge_admit_before_cover(uuid,uuid,uuid)'))
      and p.prosecdef and position('guanye_internal.ai_review_admit' in p.prosrc)>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_comparison(uuid)')
      and p.prosecdef and position('single_judge_access' in p.prosrc)>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_admit(uuid,uuid,jsonb)')
      and position('single_judge_require_legacy' in p.prosrc)>0)
    and to_regprocedure('guanye_internal.ai_review_admit(uuid,uuid,jsonb)') is not null
    and not has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_admit(uuid,uuid,jsonb)'),'EXECUTE')
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_admit(uuid,uuid,uuid)'),'EXECUTE')
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_comparison(uuid)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_admit(uuid,uuid,uuid)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_comparison(uuid)'),'EXECUTE')
    and exists(select 1 from pg_trigger where tgrelid=to_regclass('public.ai_review_submissions')
      and tgname='single_judge_order_generated_submission' and tgenabled='O')
    and exists(select 1 from pg_trigger where tgrelid=to_regclass('public.ai_review_decisions')
      and tgname='single_judge_order_generated_decision' and tgenabled='O')) as m28,
 (exists(select 1 from pg_proc p where p.oid in (to_regprocedure('public.ai_generation_select_reference_path(uuid,uuid,uuid)'),to_regprocedure('public.ai_generation_select_reference_path_before_cover(uuid,uuid,uuid)'))
      and p.prosecdef and position('SINGLE_JUDGE_GENERATION_REFERENCES_M29' in p.prosrc)>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.single_judge_generation_uploaded_reference_ok(uuid,text,text)')
      and p.prosecdef and position('SINGLE_JUDGE_GENERATION_REFERENCES_M29' in p.prosrc)>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.single_judge_generation_style_reference_ok(uuid,public.ai_generation_jobs,text)')
      and p.prosecdef and position('SINGLE_JUDGE_GENERATION_REFERENCES_M29' in p.prosrc)>0)
    and has_function_privilege('service_role',to_regprocedure('public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.ai_generation_select_reference_path(uuid,uuid,uuid)'),'EXECUTE')
    and not has_function_privilege('authenticated',to_regprocedure('public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.ai_generation_page_reference_path(uuid,uuid,uuid,uuid)'),'EXECUTE')) as m29,
 (not exists(
    select 1 from (values
      ('public.single_judge_planning_references(uuid,integer)'),
      ('public.single_judge_planning_save(uuid,uuid,bigint,jsonb)'),
      ('public.single_judge_planning_confirm(uuid,uuid,bigint,jsonb)'),
      ('public.single_judge_planning_get(uuid,uuid)'),
      ('public.single_judge_planning_history(uuid,uuid,integer)'),
      ('public.single_judge_planning_confirmed(uuid,uuid)'),
      ('public.single_judge_generation_create(uuid,text,text,jsonb,text,text,jsonb,jsonb,jsonb)')
    ) names(signature) left join pg_proc p on p.oid=to_regprocedure(names.signature)
    where p.oid is null or not p.prosecdef or not has_function_privilege('authenticated',p.oid,'EXECUTE')
      or has_function_privilege('anon',p.oid,'EXECUTE'))
    and exists(select 1 from pg_policy where polrelid=to_regclass('storage.objects')
      and polname='single_judge_planning_inputs_insert' and polcmd='a'
      and position('single_judge_planning_insert_allowed' in pg_get_expr(polwithcheck,polrelid))>0)
    and exists(select 1 from pg_proc p where p.oid in (to_regprocedure('public.ai_generation_retry_pages(uuid,uuid,uuid,jsonb)'),to_regprocedure('public.ai_generation_retry_pages_before_cover(uuid,uuid,uuid,jsonb)'))
      and position('single_judge_generation_actor' in p.prosrc)>0)) as m30,
 (exists(select 1 from pg_trigger t where t.tgrelid=to_regclass('public.workspaces')
      and t.tgname='single_judge_image_capacity_after_activation' and t.tgenabled='O')
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.single_judge_image_capacity_on_activation()')
      and p.prosecdef and position('SINGLE_JUDGE_IMAGE_CAPACITY_M31' in p.prosrc)>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text)')
      and p.prosecdef and position('SINGLE_JUDGE_IMAGE_FAIL_LEASE_M31' in p.prosrc)>0)
    and not has_function_privilege('authenticated',to_regprocedure('public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text)'),'EXECUTE')
    and has_function_privilege('service_role',to_regprocedure('public.ai_generation_page_fail(uuid,uuid,uuid,uuid,text)'),'EXECUTE')) as m31,
 (exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_backfill(uuid,text,integer,integer)')
      and p.prosecdef and position('SINGLE_JUDGE_LEGACY_BACKFILL_M32' in p.prosrc)>0)
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_backfill(uuid,text,integer,integer)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_backfill(uuid,text,integer,integer)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.single_judge_backfill(uuid,text,integer,integer)'),'EXECUTE')) as m32,
 (exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_review_queue(uuid,text,text,integer)')
      and p.prosecdef and position('SINGLE_JUDGE_REVIEW_QUEUE_M33' in p.prosrc)>0)
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_review_queue(uuid,text,text,integer)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_review_queue(uuid,text,text,integer)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.single_judge_review_queue(uuid,text,text,integer)'),'EXECUTE')) as m33,
 (exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_activate_owner(uuid,uuid)')
      and p.prosecdef and position('SINGLE_JUDGE_OWNER_ACTIVATION_M34' in p.prosrc)>0)
    and not has_function_privilege('authenticated',to_regprocedure('public.single_judge_activate_owner(uuid,uuid)'),'EXECUTE')
    and has_function_privilege('service_role',to_regprocedure('public.single_judge_activate_owner(uuid,uuid)'),'EXECUTE')) as m34,
 (to_regclass('public.single_judge_intake') is not null
    and exists(select 1 from pg_class where oid=to_regclass('public.single_judge_intake') and relrowsecurity)
    and has_table_privilege('authenticated',to_regclass('public.single_judge_intake'),'INSERT')
    and has_table_privilege('authenticated',to_regclass('public.single_judge_intake'),'SELECT')
    and not has_table_privilege('authenticated',to_regclass('public.single_judge_intake'),'UPDATE')
    and not has_table_privilege('authenticated',to_regclass('public.single_judge_intake'),'DELETE')
    and not has_table_privilege('anon',to_regclass('public.single_judge_intake'),'SELECT')
    and not has_table_privilege('service_role',to_regclass('public.single_judge_intake'),'SELECT')
    and exists(select 1 from pg_policy where polrelid=to_regclass('public.single_judge_intake')
      and polname='single_judge_intake_insert' and polcmd='a'
      and position('submitted_by' in pg_get_expr(polwithcheck,polrelid))>0
      and position('object_path' in pg_get_expr(polwithcheck,polrelid))>0
      and position('auth.uid()' in pg_get_expr(polwithcheck,polrelid))>0)
    and exists(select 1 from pg_policy where polrelid=to_regclass('public.single_judge_intake')
      and polname='single_judge_intake_select_own' and polcmd='r'
      and position('submitted_by' in pg_get_expr(polqual,polrelid))>0
      and position('auth.uid()' in pg_get_expr(polqual,polrelid))>0)
    and exists(select 1 from pg_policy where polrelid=to_regclass('storage.objects')
      and polname='single_judge_intake_storage_insert' and polcmd='a'
      and position('single_judge_intake_storage_allowed' in pg_get_expr(polwithcheck,polrelid))>0)
    and exists(select 1 from pg_policy where polrelid=to_regclass('storage.objects')
      and polname='single_judge_intake_storage_delete_orphan' and polcmd='d'
      and position('single_judge_intake_storage_delete_allowed' in pg_get_expr(polqual,polrelid))>0
      and position('bucket_id' in pg_get_expr(polqual,polrelid))>0
      and position('ppt-library' in pg_get_expr(polqual,polrelid))>0
      and position('owner_id' in pg_get_expr(polqual,polrelid))>0)
    and exists(select 1 from pg_policy where polrelid=to_regclass('storage.objects')
      and polname='single_judge_intake_storage_orphan_select' and polcmd='r'
      and position('single_judge_intake_storage_delete_allowed' in pg_get_expr(polqual,polrelid))>0
      and position('owner_id' in pg_get_expr(polqual,polrelid))>0)
    and exists(select 1 from pg_policy where polrelid=to_regclass('storage.objects')
      and polname='team_images_read' and polcmd='r'
      and position('single_judge_team_image_read_allowed' in pg_get_expr(polqual,polrelid))>0
      and position('ppt-library' in pg_get_expr(polqual,polrelid))>0
      and position('is_member' in pg_get_expr(polqual,polrelid))=0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.single_judge_team_image_read_allowed(text)')
      and p.prosecdef
      and position('SINGLE_JUDGE_TEAM_IMAGE_READ_M35' in p.prosrc)>0
      and position('single_judge_admin_id is not null' in p.prosrc)>0
      and position('single_judge_access' in p.prosrc)>0
      and position('''judge'',''admin''' in p.prosrc)>0
      and position('public.members' in p.prosrc)>0
      and position('auth.uid()' in p.prosrc)>0)
    and has_function_privilege('authenticated',to_regprocedure('guanye_internal.single_judge_team_image_read_allowed(text)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('guanye_internal.single_judge_team_image_read_allowed(text)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('guanye_internal.single_judge_team_image_read_allowed(text)'),'EXECUTE')
    and exists(select 1 from pg_index i join pg_class c on c.oid=i.indexrelid
      where i.indrelid=to_regclass('public.single_judge_intake') and c.relname='single_judge_intake_claim_idx'
      and i.indisvalid and position('workspace_id' in pg_get_indexdef(i.indexrelid))>0
      and position('created_at' in pg_get_indexdef(i.indexrelid))>0
      and position('status' in pg_get_indexdef(i.indexrelid))>0)
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.single_judge_intake_storage_allowed(text)')
      and p.prosecdef and position('submitter' in p.prosrc)>0)
    and has_function_privilege('authenticated',to_regprocedure('guanye_internal.single_judge_intake_storage_allowed(text)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('guanye_internal.single_judge_intake_storage_allowed(text)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('guanye_internal.single_judge_intake_storage_allowed(text)'),'EXECUTE')
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)')
      and p.prosecdef
      and position('SINGLE_JUDGE_INTAKE_STORAGE_DELETE_M35' in p.prosrc)>0
      and position('single_judge_access' in p.prosrc)>0
      and position('single_judge_intake' in p.prosrc)>0
      and position('object_path' in p.prosrc)>0
      and position('p_owner_id' in p.prosrc)>0
      and position('auth.uid()' in p.prosrc)>0
      and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as cfg(value)
        where cfg.value in ('search_path=','search_path=""')))
    and has_function_privilege('authenticated',to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)'),'EXECUTE')
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_intake_insert_allowed(uuid,uuid,text)') and p.prosecdef)
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_intake_insert_allowed(uuid,uuid,text)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_intake_insert_allowed(uuid,uuid,text)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.single_judge_intake_insert_allowed(uuid,uuid,text)'),'EXECUTE')
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_intake_claim(uuid,text,integer)')
      and p.prosecdef and position('SINGLE_JUDGE_INTAKE_CLAIM_M35' in p.prosrc)>0
      and position('is distinct from ''judge''' in p.prosrc)>0)
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_intake_claim(uuid,text,integer)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_intake_claim(uuid,text,integer)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.single_judge_intake_claim(uuid,text,integer)'),'EXECUTE')
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_intake_complete(uuid,uuid,uuid,text)')
      and p.prosecdef and position('SINGLE_JUDGE_INTAKE_COMPLETE_M35' in p.prosrc)>0
      and position('is distinct from ''judge''' in p.prosrc)>0)
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_intake_complete(uuid,uuid,uuid,text)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_intake_complete(uuid,uuid,uuid,text)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.single_judge_intake_complete(uuid,uuid,uuid,text)'),'EXECUTE')
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_intake_fail(uuid,uuid,uuid,text)')
      and p.prosecdef and position('SINGLE_JUDGE_INTAKE_FAIL_M35' in p.prosrc)>0
      and position('is distinct from ''judge''' in p.prosrc)>0)
    and has_function_privilege('authenticated',to_regprocedure('public.single_judge_intake_fail(uuid,uuid,uuid,text)'),'EXECUTE')
    and not has_function_privilege('anon',to_regprocedure('public.single_judge_intake_fail(uuid,uuid,uuid,text)'),'EXECUTE')
    and not has_function_privilege('service_role',to_regprocedure('public.single_judge_intake_fail(uuid,uuid,uuid,text)'),'EXECUTE')) as m35,
 (to_regclass('guanye_internal.cover_training_settings') is not null
    and to_regclass('guanye_internal.cover_training_candidates') is not null
    and to_regclass('guanye_internal.cover_training_pairs') is not null
    and to_regclass('guanye_internal.cover_training_corrections') is not null
    and to_regclass('guanye_internal.cover_training_plan_context') is not null
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_cover_revise(uuid,uuid,uuid,uuid,jsonb)')
      and p.prosecdef and has_function_privilege('authenticated',p.oid,'EXECUTE')
      and not has_function_privilege('anon',p.oid,'EXECUTE'))
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_cover_claim_pages(uuid,uuid,text,integer,integer)')
      and p.prosecdef and has_function_privilege('service_role',p.oid,'EXECUTE')
      and not has_function_privilege('authenticated',p.oid,'EXECUTE'))) as m36,
 (to_regclass('guanye_internal.cover_training_after_human') is not null
    and exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.single_judge_cover_review_claim(uuid,uuid,text,integer)')
      and p.prosecdef and has_function_privilege('service_role',p.oid,'EXECUTE')
      and not has_function_privilege('authenticated',p.oid,'EXECUTE'))
    and exists(select 1 from pg_trigger t where t.tgrelid=to_regclass('public.ai_review_decisions')
      and t.tgname='cover_training_enqueue_after_human' and not t.tgisinternal)) as m37,
 (exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.cover_training_feedback(uuid,uuid,uuid)')
    and p.prosecdef and position('libraryVisible' in p.prosrc)>0 and position('retrying' in p.prosrc)>0
    and not has_function_privilege('authenticated',p.oid,'EXECUTE'))) as m38;""",read_only=read_only)


def _all_base_tables(state):
    return all(state[key] for key in ('workspaces','members','decks','reviews'))


def _any_base_tables(state):
    return any(state[key] for key in ('workspaces','members','decks','reviews'))


def _validate_history_order(history,catalog):
    expected=[BASE_VERSION]+[item['version'] for item in catalog]
    actual=list(history)
    if actual!=expected[:len(actual)]:
        raise RuntimeError('Migration history must be an ordered prefix starting at '+BASE_VERSION+'; missing or out-of-order versions require manual audit')


def _require_complete_base(state):
    missing=[key for key in ('workspaces','members','decks','reviews') if not state[key]]
    if not missing:
        return
    current_type='missing_base' if not _any_base_tables(state) else 'incomplete_base'
    raise RuntimeError(f'Current type is {current_type}: base schema is missing or incomplete ({", ".join(missing)}); initialize a new database with configure instead of rerunning schema.sql')


def _validate_managed_history(history,catalog):
    known={item['version']:item for item in catalog}
    unknown=set(history)-{BASE_VERSION}-set(known)
    if unknown:
        raise RuntimeError('Database contains unknown migration history: '+', '.join(sorted(unknown)))
    _validate_history_order(history,catalog)
    for item in catalog:
        recorded=history.get(item['version'])
        if recorded and recorded!=item['checksum']:
            raise RuntimeError(f"Migration checksum changed after it was recorded: {item['version']}")
    return [item for item in catalog if item['version'] not in history]


def _later_schema_versions(checks,catalog):
    return [item['version'] for index,item in enumerate(catalog,1) if index>=2 and checks.get(f'm{index:02d}')]


def _adopt_existing(api,ref,catalog,schema_path):
    checks=_existing_schema_probes(api,ref)
    missing=[item['version'] for index,item in enumerate(catalog,1) if not checks.get(f'm{index:02d}')]
    if missing:
        raise RuntimeError('Existing database failed key-object probes; inspect the actual schema, constraints, functions and permissions, apply missing SQL manually, then retry. Missing probes: '+', '.join(missing))
    api('/v1/projects/'+ref+'/database/query',{'query':_adoption_sql(catalog,_schema_checksum(schema_path))})


def _upgrade_unmanaged_base(api,ref,schema_path,catalog):
    checks=_existing_schema_probes(api,ref)
    later=_later_schema_versions(checks,catalog)
    if later:
        raise RuntimeError('Database is not base-only; later migration objects were found ('+', '.join(later)+'). Audit it and use --adopt-existing after reviewing the full schema')
    api('/v1/projects/'+ref+'/database/query',{'query':_adoption_sql([], _schema_checksum(schema_path))})


def _migration_plan(api,ref,catalog=None,schema_path=None):
    ref=_validated_project_ref(ref)
    catalog=_migration_catalog() if catalog is None else list(catalog)
    state=_database_state(api,ref,read_only=True)
    _require_complete_base(state)
    history=_history_rows(api,ref,read_only=True) if state['migration_history'] else {}
    if not state['migration_history'] or BASE_VERSION not in history:
        if history:
            raise RuntimeError('Migration history exists but has no base marker; refusing automatic changes')
        checks=_existing_schema_probes(api,ref,read_only=True)
        later=_later_schema_versions(checks,catalog)
        pending=[item['version'] for item in catalog]
        if later:
            return {
                'project_ref':ref,
                'current_type':'unmanaged_with_later_objects',
                'recorded_versions':[],
                'pending_versions':pending,
                'history_status':'absent',
                'migration_table_exists':state['migration_history'],
                'detected_later_versions':later,
                'migration_path':{
                    'mode':'manual_audit',
                    'command':None,
                    'requires_manual_audit':True,
                    'notes':'Later migration objects exist without managed history. Audit the full schema, constraints, functions, triggers, permissions and RLS before considering migrate --adopt-existing; do not use --upgrade-unmanaged-base.',
                },
                'read_only':True,
            }
        return {
            'project_ref':ref,
            'current_type':'unmanaged_base',
            'recorded_versions':[],
            'pending_versions':pending,
            'history_status':'absent',
            'migration_table_exists':state['migration_history'],
            'migration_path':{
                'mode':'upgrade_unmanaged_base',
                'command':f'python -B cloud/manage.py migrate --project-ref {ref} --upgrade-unmanaged-base',
                'requires_manual_audit':False,
                'notes':'Base-only database with no 02 through 21 key objects. Back up first; this is the existing explicit upgrade path.',
            },
            'read_only':True,
        }
    pending=_validate_managed_history(history,catalog)
    pending_versions=[item['version'] for item in pending]
    if pending_versions:
        current_type='managed_prefix'
        mode='managed_migrate'
        command=f'python -B cloud/manage.py migrate --project-ref {ref}'
        notes='Managed history is an ordered checksum-verified prefix. migrate revalidates the same prefix before applying pending versions.'
        history_status='prefix'
    else:
        current_type='managed_complete'
        mode='none'
        command=None
        notes='Managed history is complete for the current migration catalog.'
        history_status='complete'
    return {
        'project_ref':ref,
        'current_type':current_type,
        'recorded_versions':list(history),
        'pending_versions':pending_versions,
        'history_status':history_status,
        'migration_table_exists':True,
        'migration_path':{
            'mode':mode,
            'command':command,
            'requires_manual_audit':False,
            'notes':notes,
        },
        'read_only':True,
    }


def _migrate_database(api,ref,catalog=None,schema_path=None,adopt_existing=False,upgrade_unmanaged_base=False):
    ref=_validated_project_ref(ref)
    catalog=_migration_catalog() if catalog is None else list(catalog)
    schema_path=HERE/'schema.sql' if schema_path is None else Path(schema_path)
    state=_database_state(api,ref)
    _require_complete_base(state)
    history=_history_rows(api,ref) if state['migration_history'] else {}
    initialized_unmanaged_base=False
    if not state['migration_history'] or BASE_VERSION not in history:
        if history:
            raise RuntimeError('Migration history exists but has no base marker; refusing automatic changes')
        if adopt_existing:
            _adopt_existing(api,ref,catalog,schema_path)
            return {'adopted':True,'initialized_unmanaged_base':False,'applied':[]}
        if not upgrade_unmanaged_base:
            raise RuntimeError('Initialized database has no managed migration history. Audit it, then run migrate --upgrade-unmanaged-base for a base-only database or migrate --adopt-existing after full manual review')
        _upgrade_unmanaged_base(api,ref,schema_path,catalog)
        history={BASE_VERSION:_schema_checksum(schema_path)}
        initialized_unmanaged_base=True
    pending=_validate_managed_history(history,catalog)
    for item in pending:
        api('/v1/projects/'+ref+'/database/query',{'query':_migration_sql(item['version'],item['checksum'],item['body'])})
    return {'adopted':False,'initialized_unmanaged_base':initialized_unmanaged_base,'applied':[item['version'] for item in pending]}


def _ensure_database(api,ref,bootstrap_path=None,schema_path=None,catalog=None):
    schema_path=HERE/'schema.sql' if schema_path is None else Path(schema_path)
    bootstrap_path=HERE/'bootstrap.sql' if bootstrap_path is None else Path(bootstrap_path)
    catalog=_migration_catalog() if catalog is None else list(catalog)
    state=_database_state(api,ref)
    if not _any_base_tables(state) and not state['migration_history']:
        if not bootstrap_path.exists():
            raise RuntimeError('cloud/bootstrap.sql is missing; prepare the private workspace before configuring a new database')
        schema_sql=schema_path.read_text(encoding='utf-8')
        bootstrap_sql=bootstrap_path.read_text(encoding='utf-8')
        api('/v1/projects/'+ref+'/database/query',{'query':_base_sql(schema_sql,bootstrap_sql,_schema_checksum(schema_path))})
    elif not _all_base_tables(state):
        raise RuntimeError('Base schema is incomplete; refusing to rerun schema.sql over a partially initialized database')
    return _migrate_database(api,ref,catalog=catalog,schema_path=schema_path,adopt_existing=False)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['inventory','create','status','configure','migration-plan','migrate','upload','pages'])
    parser.add_argument('--project-ref',help='Target project ref for migrate or migration-plan; omit to use an existing private-deployment.json')
    parser.add_argument('--cover-pilot',action='store_true',help='Include the cover training migration on a separate project')
    parser.add_argument('--production-project-ref',help='Explicit production project ref to exclude when using --cover-pilot')
    history_mode=parser.add_mutually_exclusive_group()
    history_mode.add_argument('--adopt-existing',action='store_true',help='After an operator audit, record history from key-object probes without rerunning migration SQL')
    history_mode.add_argument('--upgrade-unmanaged-base',action='store_true',help='Explicitly upgrade a base-only schema with no history by running 01 through 21')
    args=parser.parse_args()
    if (args.adopt_existing or args.upgrade_unmanaged_base) and args.command!='migrate':
        parser.error('history options are only valid with migrate')
    if args.project_ref and args.command not in ('migration-plan','migrate'):
        parser.error('--project-ref is only valid with migrate or migration-plan')
    if args.cover_pilot:
        if args.command not in ('migration-plan','migrate') or not args.project_ref or not args.production_project_ref:
            parser.error('--cover-pilot requires migration-plan or migrate with explicit --project-ref and --production-project-ref')
        try:
            target=_validated_project_ref(args.project_ref)
            production=_validated_project_ref(args.production_project_ref)
        except RuntimeError as exc:
            parser.error(str(exc))
        if target==production:
            parser.error('Cover training must use a different Supabase project from production')
    elif args.production_project_ref:
        parser.error('--production-project-ref is only valid with --cover-pilot')
    token=os.environ.get('SUPABASE_ACCESS_TOKEN')
    if not token:
        path=HERE/'private-token.txt'
        if not path.exists():
            raise SystemExit('Missing cloud/private-token.txt. Save the temporary Supabase access token there.')
        token=path.read_text(encoding='utf-8-sig').strip()
    if not token.startswith('sbp_'):
        raise SystemExit('Expected a Supabase Management API personal access token')
    proxy=os.environ.get('HTTPS_PROXY','').strip()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':proxy} if proxy else {}))
    def api(path,body=None,method=None):
        payload=None if body is None else json.dumps(body,ensure_ascii=False).encode()
        req=urllib.request.Request('https://api.supabase.com'+path,data=payload,method=method or ('GET' if body is None else 'POST'),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with opener.open(req,timeout=60) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Supabase API {exc.code}: {exc.read().decode()[:500]}') from None
    config=json.loads(PRIVATE.read_text(encoding='utf-8')) if PRIVATE.exists() else {}
    def checkpoint():PRIVATE.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    def release_catalog():
        return [item for item in _migration_catalog() if args.cover_pilot or item['version'] not in COVER_PILOT_VERSIONS]
    if args.command=='migration-plan':
        ref=args.project_ref or config.get('ref')
        if not ref:
            raise SystemExit('migration-plan requires --project-ref or an existing private-deployment.json with ref')
        ref=_validated_project_ref(ref)
        plan=_migration_plan(api,ref,catalog=release_catalog())
        if args.cover_pilot and plan['migration_path']['command']:
            plan['migration_path']['command']+=f' --cover-pilot --production-project-ref {production}'
        print(json.dumps(plan,ensure_ascii=False,indent=2))
        return
    if args.command=='migrate':
        ref=args.project_ref or config.get('ref')
        if not ref:
            raise SystemExit('migrate requires --project-ref or an existing private-deployment.json with ref')
        ref=_validated_project_ref(ref)
        result=_migrate_database(api,ref,catalog=release_catalog(),adopt_existing=args.adopt_existing,upgrade_unmanaged_base=args.upgrade_unmanaged_base)
        if result['adopted']:
            print('Recorded migration history from key-object probes only; this is not equivalence verification. Audit the full schema, constraints, functions and permissions first.')
        elif result['initialized_unmanaged_base']:
            print('Recorded base history for a base-only database, then applied migrations:',', '.join(result['applied']))
        elif result['applied']:
            print('Applied migrations:',', '.join(result['applied']))
        else:
            print('Database migrations already current.')
        return
    if args.command=='inventory':
        profile=api('/v1/profile')
        print(json.dumps({'profile':{k:profile.get(k) for k in ('id','primary_email')},'organizations':api('/v1/organizations'),'projects':[{k:p.get(k) for k in ('id','name','status','organization_id','region')} for p in api('/v1/projects')]},ensure_ascii=False))
        return
    if args.command=='create':
        if config.get('ref'):print('A project is already recorded; use status/configure.');return
        organizations=api('/v1/organizations')
        organization=next((o for o in organizations if o['name']=='观页评审'),None)
        if organization is None:
            organization=api('/v1/organizations',{'name':'观页评审'})
        slug=organization.get('slug') or organization['id']
        config['organization']=slug;checkpoint()
        existing=[p for p in api('/v1/projects') if p['name']=='guanye-ppt-review' and (p.get('organization_slug')==slug or p.get('organization_id')==organization.get('id'))]
        if existing:project=existing[0]
        else:
            config.setdefault('databasePassword',secrets.token_urlsafe(30));checkpoint()
            project=api('/v1/projects',{'name':'guanye-ppt-review','organization_slug':slug,'db_pass':config['databasePassword'],'region_selection':{'type':'smartGroup','code':'apac'},'auth_site_url':SITE})
        config['ref']=project['id'];config['url']='https://'+project['id']+'.supabase.co';checkpoint()
        print('Project recorded:',project['id'],project.get('status'));return
    if not config.get('ref'):raise SystemExit('Run create first')
    ref=config['ref']
    if args.command=='status':
        p=api('/v1/projects/'+ref);print(json.dumps({k:p.get(k) for k in ('id','name','status','region')},ensure_ascii=False));return
    if args.command=='configure':
        profile=api('/v1/profile')
        email=profile.get('primary_email') or profile.get('email')
        if not email:raise SystemExit('Owner email absent from Supabase profile; configure it explicitly before bootstrap')
        workspace=HERE/'private-workspace.json'
        if not workspace.exists():subprocess.run([sys.executable,str(HERE/'prepare_workspace.py'),'--owner-email',email],cwd=ROOT,check=True)
        config['workspaceId']=json.loads(workspace.read_text())['workspaceId'];checkpoint()
        result=_ensure_database(api,ref,catalog=release_catalog())
        config['schemaInstalled']=True;checkpoint()
        api('/v1/projects/'+ref+'/config/auth',{'site_url':SITE,'uri_allow_list':SITE,'password_min_length':8},method='PATCH')
        keys=api('/v1/projects/'+ref+'/api-keys?reveal=true')
        public=next((k for k in keys if k.get('type')=='publishable'),None) or next((k for k in keys if k.get('name')=='anon'),None)
        private=next((k for k in keys if k.get('name')=='service_role'),None) or next((k for k in keys if k.get('type')=='secret'),None)
        if not public or not private:raise SystemExit('Project keys are not available yet')
        config['anonKey']=public['api_key'];config['serviceKey']=private['api_key'];checkpoint()
        print(f"Schema, {len(result['applied'])} pending migration(s), workspace, auth URLs and project credentials configured.");return
    if args.command=='upload':
        env={**os.environ,'SUPABASE_URL':config['url'],'SUPABASE_SERVICE_ROLE_KEY':config['serviceKey'],'WORKSPACE_ID':config['workspaceId'],'HTTPS_PROXY':proxy}
        subprocess.run([sys.executable,str(HERE/'publish_library.py')],cwd=ROOT,env=env,check=True);return
    if args.command=='pages':
        for key,value in {'SUPABASE_URL':config['url'],'SUPABASE_ANON_KEY':config['anonKey'],'WORKSPACE_ID':config['workspaceId']}.items():
            result=subprocess.run(['gh','variable','set',key,'--repo','PWMPro-a/guanye-ppt-review'],input=value,text=True,shell=os.name=='nt',capture_output=True)
            if result.returncode:raise RuntimeError('Could not set GitHub variable '+key+': '+result.stderr[:300])
        subprocess.run(['gh','workflow','run','pages.yml','--repo','PWMPro-a/guanye-ppt-review'],shell=os.name=='nt',check=True)
        print('Pages deployment dispatched:',SITE)

if __name__=='__main__':
    try:main()
    except (RuntimeError,KeyError,urllib.error.URLError) as exc:raise SystemExit(str(exc))
