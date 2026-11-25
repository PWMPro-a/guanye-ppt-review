import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from cloud import manage

ROOT=Path(__file__).resolve().parent
TEST_REF='abcdefghijklmnopqrst'
OTHER_REF='zyxwvutsrqponmlkjihg'
PGLITE=ROOT/'.test-output'/'sql'/'node_modules'/'@electric-sql'/'pglite'
PGCRYPTO=PGLITE/'dist'/'contrib'/'pgcrypto.cjs'


class FakeApi:
    def __init__(self,base_installed,history_table_exists=None,history=None,probes=None,state=None):
        self.base_installed=base_installed
        self.history_table_exists=bool(history) if history_table_exists is None else history_table_exists
        self.history=dict(history or {})
        self.probes={f'm{index:02d}':True for index in range(1,len(manage.MIGRATION_SPECS)+1)}
        self.probes.update(probes or {})
        self.fail_version=None
        self.queries=[]
        self.payloads=[]
        if state is None:
            state={key:base_installed for key in ('workspaces','members','decks','reviews')}
        self.state=dict(state)

    def __call__(self,path,body=None,method=None):
        sql=body['query']
        self.queries.append(sql)
        self.payloads.append(dict(body))
        if 'as migration_history;' in sql:
            row=dict(self.state)
            row['migration_history']=self.history_table_exists
            return [row]
        if 'from guanye_internal.schema_migrations order by applied_at,version' in sql:
            return [{'version':version,'checksum':checksum} for version,checksum in self.history.items()]
        if ' as m01,' in sql:
            return [dict(self.probes)]
        if sql.startswith('do $guanye_migration$'):
            pairs=re.findall(r"\(\s*'([^']+)'\s*,\s*'([0-9a-f]{64})'\s*\)",sql)
            if self.fail_version and any(version==self.fail_version for version,_ in pairs):
                raise RuntimeError('simulated migration failure')
            for version,checksum in pairs:
                self.history[version]=checksum
                if version==manage.BASE_VERSION:
                    self.base_installed=True
                    self.state={key:True for key in ('workspaces','members','decks','reviews')}
            self.history_table_exists=True
            return []
        raise AssertionError('Unexpected SQL: '+sql[:200])


class ManageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.catalog=manage._migration_catalog()
        self.versions=[item['version'] for item in self.catalog]

    def test_checksum_is_utf8_lf_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            text='第一行\n第二行\n'
            lf=directory/'lf.sql'
            crlf=directory/'crlf.sql'
            lf.write_bytes(text.encode('utf-8'))
            crlf.write_bytes(text.replace('\n','\r\n').encode('utf-8'))
            self.assertEqual(manage._file_checksum(lf),manage._file_checksum(crlf))
            self.assertEqual(manage._text_checksum(text),manage._file_checksum(lf))

    def test_fresh_configure_path_applies_catalog_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            bootstrap=Path(temporary)/'bootstrap.sql'
            bootstrap.write_text("select 'bootstrap';",encoding='utf-8')
            api=FakeApi(base_installed=False)
            result=manage._ensure_database(api,TEST_REF,bootstrap_path=bootstrap,
                                           schema_path=manage.HERE/'schema.sql',catalog=self.catalog)
        self.assertEqual(result['applied'],self.versions)
        self.assertEqual(list(api.history),[manage.BASE_VERSION]+self.versions)
        atomic=[query for query in api.queries if query.startswith('do $guanye_migration$')]
        self.assertEqual(len(atomic),len(self.catalog)+1)
        self.assertIn(manage.BASE_VERSION,atomic[0])

    def test_existing_database_without_history_refuses_automatic_migration(self):
        api=FakeApi(base_installed=True,history_table_exists=False)
        with self.assertRaisesRegex(RuntimeError,'--upgrade-unmanaged-base'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog)
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_base_only_database_has_explicit_upgrade_path(self):
        api=FakeApi(base_installed=True,history_table_exists=False,
                    probes={'m02':False,'m03':False,'m04':False,'m05':False,'m06':False,'m07':False,'m08':False,'m09':False,'m10':False,'m11':False,'m12':False,'m13':False,'m14':False,'m15':False,'m16':False,'m17':False,'m18':False,'m19':False,'m20':False,'m21':False,'m22':False,'m23':False,'m24':False,'m25':False,'m26':False,'m27':False,'m28':False,'m29':False,'m30':False,'m31':False,'m32':False,'m33':False,'m34':False,'m35':False,'m36':False,'m37':False,'m38':False,'m39':False,'m40':False,'m41':False})
        result=manage._migrate_database(api,TEST_REF,catalog=self.catalog,upgrade_unmanaged_base=True)
        self.assertTrue(result['initialized_unmanaged_base'])
        self.assertEqual(result['applied'],self.versions)
        self.assertEqual(list(api.history),[manage.BASE_VERSION]+self.versions)
        self.assertFalse(any('-- AI reviews:' in query for query in api.queries[:2]))

    def test_base_only_upgrade_refuses_database_with_later_objects(self):
        api=FakeApi(base_installed=True,history_table_exists=False,
                    probes={'m03':True,'m04':False,'m05':False,'m06':False,'m07':False,'m08':False,'m09':False,'m10':False,'m11':False,'m12':False,'m13':False,'m14':False,'m15':False,'m16':False,'m17':False,'m18':False,'m19':False,'m20':False,'m21':False,'m22':False,'m23':False,'m24':False,'m25':False,'m26':False,'m27':False,'m28':False,'m29':False,'m30':False,'m31':False,'m32':False,'m33':False,'m34':False,'m35':False,'m36':False,'m37':False,'m38':False,'m39':False,'m40':False,'m41':False})
        with self.assertRaisesRegex(RuntimeError,'not base-only'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,upgrade_unmanaged_base=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_existing_database_adoption_records_after_key_object_probes_only(self):
        api=FakeApi(base_installed=True,history_table_exists=False)
        result=manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertTrue(result['adopted'])
        self.assertEqual(result['applied'],[])
        self.assertEqual(list(api.history),[manage.BASE_VERSION]+self.versions)
        atomic=[query for query in api.queries if query.startswith('do $guanye_migration$')]
        self.assertEqual(len(atomic),1)
        self.assertNotIn('-- AI reviews:',atomic[0])
        self.assertNotIn('create table if not exists public.ai_review_settings',atomic[0])

    def test_adoption_refuses_when_key_object_probes_fail(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m05':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_05_review_queue'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_calibration_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m07':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_07_review_calibration'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_comparison_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m08':False,'m09':False,'m10':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_08_review_comparison'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_backfill_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m09':False,'m10':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_09_review_backfill'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_queue_listing_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m10':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_10_review_queue_listing'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_planning_history_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m11':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_11_planning_history'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_library_classification_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m12':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_12_library_classification'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_formal_correction_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m13':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_13_formal_correction'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_library_formal_score_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m14':False,'m15':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_14_library_formal_scores'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_refuses_when_planning_original_references_probe_fails(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m15':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_15_planning_original_references'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_adoption_requires_uploaded_baseline_probe(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m16':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_16_uploaded_baseline'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})

    def test_adoption_requires_workspace_batch_probe(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m17':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_17_review_workspace_batch'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})

    def test_adoption_requires_classification_backfill_probe(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m18':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_18_review_backfill_classification'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})

    def test_adoption_requires_uploaded_source_provenance_probe(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m19':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_19_uploaded_source_provenance'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})

    def test_adoption_requires_uploaded_source_groups_probe(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m20':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_20_uploaded_source_groups'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})

    def test_adoption_requires_uploaded_comparison_source_dedup_probe(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m21':False})
        with self.assertRaisesRegex(RuntimeError,'20261121_21_uploaded_comparison_source_dedup'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})

    def test_m21_probe_is_oid_safe_and_checks_acl(self):
        api=FakeApi(base_installed=True,history_table_exists=False)
        manage._existing_schema_probes(api,TEST_REF)
        sql=api.queries[-1]
        self.assertIn("to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])')",sql)
        self.assertNotIn('::regprocedure',sql)
        self.assertIn("position('COMPARISON_SOURCE_DEDUP_V21' in p.prosrc)>0",sql)
        self.assertIn("not has_table_privilege('service_role',to_regclass('guanye_internal.uploaded_source_provenance'),'SELECT')",sql)

    def test_adoption_requires_single_judge_intake_probe(self):
        api=FakeApi(base_installed=True,history_table_exists=False,probes={'m35':False,'m36':False,'m37':False,'m38':False,'m39':False,'m40':False,'m41':False})
        with self.assertRaisesRegex(RuntimeError,'20261122_35_single_judge_intake'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog,adopt_existing=True)
        self.assertEqual(api.history,{})

    def test_m35_probe_checks_owner_read_claim_index_and_storage_delete_acl(self):
        api=FakeApi(base_installed=True,history_table_exists=False)
        manage._existing_schema_probes(api,TEST_REF)
        sql=api.queries[-1]
        self.assertIn("to_regclass('public.single_judge_intake')",sql)
        self.assertIn("polname='single_judge_intake_select_own'",sql)
        self.assertIn("position('submitted_by' in pg_get_expr(polqual,polrelid))",sql)
        self.assertIn("position('auth.uid()' in pg_get_expr(polqual,polrelid))",sql)
        self.assertIn("c.relname='single_judge_intake_claim_idx'",sql)
        self.assertIn("position('status' in pg_get_indexdef(i.indexrelid))",sql)
        self.assertIn("position('is distinct from ''judge''' in p.prosrc)",sql)
        self.assertIn("not has_table_privilege('authenticated',to_regclass('public.single_judge_intake'),'UPDATE')",sql)
        self.assertIn("polname='team_images_read'",sql)
        self.assertIn("position('single_judge_team_image_read_allowed' in pg_get_expr(polqual,polrelid))",sql)
        self.assertIn("position('is_member' in pg_get_expr(polqual,polrelid))=0",sql)
        self.assertIn("position('SINGLE_JUDGE_TEAM_IMAGE_READ_M35' in p.prosrc)",sql)
        self.assertIn("position('single_judge_admin_id is not null' in p.prosrc)",sql)
        self.assertIn("position('single_judge_access' in p.prosrc)",sql)
        self.assertIn("polname='single_judge_intake_storage_delete_orphan'",sql)
        self.assertIn("polcmd='d'",sql)
        self.assertIn("position('single_judge_intake_storage_delete_allowed' in pg_get_expr(polqual,polrelid))",sql)
        self.assertIn("position('ppt-library' in pg_get_expr(polqual,polrelid))",sql)
        self.assertIn("position('owner_id' in pg_get_expr(polqual,polrelid))",sql)
        self.assertIn("polname='single_judge_intake_storage_orphan_select'",sql)
        self.assertIn("to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)')",sql)
        self.assertIn("position('SINGLE_JUDGE_INTAKE_STORAGE_DELETE_M35' in p.prosrc)",sql)
        self.assertIn("position('object_path' in p.prosrc)",sql)
        self.assertIn("position('p_owner_id' in p.prosrc)",sql)
        self.assertIn("has_function_privilege('authenticated',to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)'),'EXECUTE')",sql)
        self.assertIn("not has_function_privilege('anon',to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)'),'EXECUTE')",sql)
        self.assertIn("not has_function_privilege('service_role',to_regprocedure('guanye_internal.single_judge_intake_storage_delete_allowed(text,text)'),'EXECUTE')",sql)

    def test_upgrade_from_penultimate_applies_only_tip(self):
        history={manage.BASE_VERSION:manage._schema_checksum(manage.HERE/'schema.sql')}
        history.update({item['version']:item['checksum'] for item in self.catalog[:-1]})
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        result=manage._migrate_database(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(result['applied'],[self.versions[-1]])
        self.assertEqual(list(api.history),[manage.BASE_VERSION]+self.versions)

    def test_history_must_be_an_ordered_prefix(self):
        base=manage._schema_checksum(manage.HERE/'schema.sql')
        api=FakeApi(base_installed=True,history_table_exists=True,
                    history={manage.BASE_VERSION:base,self.versions[5]:self.catalog[5]['checksum']})
        with self.assertRaisesRegex(RuntimeError,'ordered prefix'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog)
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_rerun_is_noop_and_checksum_drift_fails_closed(self):
        history={manage.BASE_VERSION:manage._schema_checksum(manage.HERE/'schema.sql')}
        history.update({item['version']:item['checksum'] for item in self.catalog})
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        result=manage._migrate_database(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(result['applied'],[])
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

        broken=dict(history)
        broken[self.versions[0]]='0'*64
        api=FakeApi(base_installed=True,history_table_exists=True,history=broken)
        with self.assertRaisesRegex(RuntimeError,'checksum changed'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog)
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_failed_migration_is_not_recorded_and_rerun_resumes(self):
        history={manage.BASE_VERSION:manage._schema_checksum(manage.HERE/'schema.sql')}
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        api.fail_version=self.versions[2]
        with self.assertRaisesRegex(RuntimeError,'simulated migration failure'):
            manage._migrate_database(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(list(api.history),[manage.BASE_VERSION,self.versions[0],self.versions[1]])
        api.fail_version=None
        result=manage._migrate_database(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(result['applied'],self.versions[2:])
        self.assertEqual(list(api.history),[manage.BASE_VERSION]+self.versions)

    def test_each_migration_is_wrapped_and_05_really_uses_the_wrapper(self):
        for item in self.catalog:
            query=manage._migration_sql(item['version'],item['checksum'],item['body'])
            self.assertTrue(query.startswith('do $guanye_migration$'))
            self.assertEqual(query.count('do $guanye_migration$'),1)
            self.assertNotRegex(item['body'],r'(?im)^\s*(begin|commit|rollback)\s*;\s*$')
        item=self.catalog[4]
        self.assertIn('create or replace function public.ai_review_finish_attempt',item['body'])
        self.assertNotIn('commit;',item['body'])

    @unittest.skipUnless(shutil.which('node') and PGLITE.exists() and PGCRYPTO.exists(),'PGlite is not installed')
    def test_isolated_pglite_executes_wrapped_current_catalog_and_rolls_back_failure(self):
        schema=(manage.HERE/'schema.sql').read_text(encoding='utf-8')
        marker='-- AI_REVIEW_FOUNDATION_BODY_START'
        legacy=schema[:schema.index(marker)]
        setup=r'''
create schema auth;
create schema storage;
create schema extensions;
create role anon;
create role authenticated;
create role service_role bypassrls;
grant usage on schema public to anon, authenticated;
alter default privileges in schema public grant all on tables to anon, authenticated;
create extension if not exists pgcrypto with schema extensions;
create table auth.users(id uuid primary key);
create function auth.uid() returns uuid language sql stable as $$
 select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid
$$;
create function auth.jwt() returns jsonb language sql stable as $$
 select jsonb_build_object('email',current_setting('request.jwt.claim.email',true))
$$;
create table storage.buckets(id text primary key,name text,public boolean,file_size_limit bigint);
create table storage.objects(id uuid,name text,bucket_id text,owner_id text);
alter table storage.objects enable row level security;
create function storage.foldername(text) returns text[] language sql as $$
 select string_to_array($1,'/')
$$;
grant usage on schema auth,storage,extensions to anon,authenticated,service_role;
grant select,insert,update,delete on all tables in schema storage to anon,authenticated;
grant all on all tables in schema storage to service_role;
'''
        failure=manage._migration_sql('atomic-failure','b'*64,
            'create table public.atomic_failure(id integer);\nselect 1/0;')
        script=r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {PGlite}=require(process.argv[2]);
const {pgcrypto}=require(process.argv[3]);
(async()=>{
 const db=new PGlite({extensions:{pgcrypto}});
 try{
  await db.exec(fs.readFileSync(process.argv[4],'utf8'));
  await db.exec(fs.readFileSync(process.argv[5],'utf8'));
  await db.exec(fs.readFileSync(process.argv[6],'utf8'));
  const probe=fs.readFileSync(process.argv[process.argv.length-2],'utf8');
  const legacyProbes=(await db.query(probe)).rows[0];
  assert.equal(legacyProbes.m28,false,'probe safe against original schema without AI tables');
  assert.equal(legacyProbes.m29,false,'reference probe safe without generation tables');
  assert.equal(legacyProbes.m30,false,'creation probe safe on an untouched library');
  assert.equal(legacyProbes.m31,false,'image runtime probe safe on an untouched library');
  assert.equal(legacyProbes.m32,false,'legacy backfill probe safe on an untouched library');
  assert.equal(legacyProbes.m33,false,'review queue probe safe on an untouched library');
  assert.equal(legacyProbes.m34,false,'owner activation probe safe on an untouched library');
  assert.equal(legacyProbes.m35,false,'intake probe safe on an untouched library');
  assert.equal(legacyProbes.m36,false,'PPT training must not be inferred on an untouched library');
  assert.equal(legacyProbes.m37,false,'cover training must not be inferred on an untouched library');
  assert.equal(legacyProbes.m38,false,'cover human follow-up must not be inferred on an untouched library');
  assert.equal(legacyProbes.m39,false,'cover progress must not be inferred on an untouched library');
  assert.equal(legacyProbes.m40,false,'cover visual reference must not be inferred on an untouched library');
  for(let index=7;index<process.argv.length-2;index++) await db.exec(fs.readFileSync(process.argv[index],'utf8'));
  const currentProbes=(await db.query(probe)).rows[0];
  assert.equal(currentProbes.m28,true,'admission privileges and triggers detected after M28');
  assert.equal(currentProbes.m29,true,'professional reference authorization detected after M29');
  assert.equal(currentProbes.m30,true,'professional creation detected after M30');
  assert.equal(currentProbes.m31,true,'professional image runtime detected after M31');
  assert.equal(currentProbes.m32,true,'professional legacy backfill detected after M32');
  assert.equal(currentProbes.m33,true,'professional review queue detected after M33');
  assert.equal(currentProbes.m34,true,'professional owner activation detected after M34');
  assert.equal(currentProbes.m35,true,'direct intake queue, owner read policy and claim index detected after M35');
  assert.equal(currentProbes.m36,true,'PPT training detected only after the migration');
  assert.equal(currentProbes.m37,true,'cover training detected only after the migration');
  assert.equal(currentProbes.m38,true,'cover human follow-up detected only after the migration');
  assert.equal(currentProbes.m39,true,'cover progress detected only after the migration');
  assert.equal(currentProbes.m40,true,'cover visual reference detected only after the migration');
  const failurePath=process.argv[process.argv.length-1];
  const expected=['20261121_01_ai_review_foundation','20261121_02_ai_reviews','20261121_03_ai_planning','20261121_04_ai_generation','20261121_05_review_queue','20261121_06_generation_history','20261121_07_review_calibration','20261121_08_review_comparison','20261121_09_review_backfill','20261121_10_review_queue_listing','20261121_11_planning_history','20261121_12_library_classification','20261121_13_formal_correction','20261121_14_library_formal_scores','20261121_15_planning_original_references','20261121_16_uploaded_baseline','20261121_17_review_workspace_batch','20261121_18_review_backfill_classification','20261121_19_uploaded_source_provenance','20261121_20_uploaded_source_groups','20261121_21_uploaded_comparison_source_dedup','20261122_22_single_judge_foundation','20261122_23_single_judge_original_archive','20261122_24_single_judge_submission','20261122_25_image_workers_default','20261122_26_single_judge_score_revisions','20261122_27_single_judge_review_workspace','20261122_28_single_judge_admission','20261122_29_single_judge_generation_references','20261122_30_single_judge_creation','20261122_31_single_judge_image_runtime','20261122_32_single_judge_legacy_backfill','20261122_33_single_judge_review_queue','20261122_34_single_judge_owner_activation','20261122_35_single_judge_intake','20261125_42_ppt_training_scope','20261123_38_cover_training','20261123_39_cover_after_human','20261124_40_cover_progress','20261124_41_cover_visual_reference'];
  const history=(await db.query('select version from guanye_internal.schema_migrations order by applied_at,version')).rows.map(row=>row.version);
  assert.deepEqual(history,expected);
  const objects=(await db.query(`select
   to_regclass('public.ai_review_jobs') is not null as review,
   to_regclass('public.ai_planning_plans') is not null as planning,
   to_regclass('public.ai_generation_jobs') is not null as generation,
   to_regprocedure('public.ai_generation_recent(uuid,uuid)') is not null as history_rpc,
   to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)') is not null as calibration_rpc,
   to_regprocedure('public.ai_review_comparison(uuid)') is not null as comparison_rpc,
   to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)') is not null as backfill_rpc,
  to_regprocedure('public.ai_review_queue_decks(uuid,text,text,integer)') is not null as queue_decks_rpc,
  to_regprocedure('public.ai_review_queue_counts(uuid)') is not null as queue_counts_rpc,
  to_regprocedure('public.ai_review_queue_jobs(uuid,text)') is not null as queue_jobs_rpc,
   to_regclass('public.ai_review_queue_decks_page_idx') is not null as queue_decks_index,
   to_regprocedure('public.ai_planning_list_my_plans(uuid,uuid,integer)') is not null as planning_history_rpc,
   to_regclass('public.ai_planning_plans_owner_history_idx') is not null as planning_history_index,
   exists(select 1 from pg_trigger where tgname='ai_review_enqueue_preview_after_deck' and tgrelid='public.decks'::regclass and not tgisinternal) as queue_trigger,
   has_function_privilege('service_role','public.ai_review_effective_meta(uuid,text)','EXECUTE') as effective_meta_service,
   not has_function_privilege('authenticated','public.ai_review_effective_meta(uuid,text)','EXECUTE') as effective_meta_authenticated_denied,
   not has_function_privilege('anon','public.ai_review_effective_meta(uuid,text)','EXECUTE') as effective_meta_anon_denied,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_effective_meta(uuid,text)') and position('LIBRARY_CLASSIFICATION_BACKFILL_V18' in p.prosrc)>0) as effective_meta_v18,
   has_function_privilege('service_role','public.ai_review_confirm(uuid,uuid,jsonb)','EXECUTE') as confirm_service,
   not has_function_privilege('authenticated','public.ai_review_confirm(uuid,uuid,jsonb)','EXECUTE') as confirm_authenticated_denied,
   not has_function_privilege('anon','public.ai_review_confirm(uuid,uuid,jsonb)','EXECUTE') as confirm_anon_denied,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_confirm(uuid,uuid,jsonb)') and position('FORMAL_CORRECTION_V13' in p.prosrc)>0) as confirm_v13,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.review_score_summary(uuid)') and position('LIBRARY_FORMAL_SQL_V14' in p.prosrc)>0) as summary_v14,
   has_function_privilege('authenticated','public.review_score_summary(uuid)','EXECUTE') as summary_authenticated,
   not has_function_privilege('anon','public.review_score_summary(uuid)','EXECUTE') as summary_anon_denied,
   to_regprocedure('public.ai_review_uploaded_formal_scores(uuid)') is not null as uploaded_formal_rpc,
   has_function_privilege('service_role','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE') as uploaded_formal_service,
   not has_function_privilege('authenticated','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE') as uploaded_formal_authenticated_denied,
   not has_function_privilege('anon','public.ai_review_uploaded_formal_scores(uuid)','EXECUTE') as uploaded_formal_anon_denied,
   to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)') is not null as planning_references_rpc,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)') and p.prosecdef) as planning_references_definer,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)') and position('PLANNING_ORIGINAL_REFERENCES_V15' in p.prosrc)>0) as planning_references_v15,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_planning_reference_candidates(uuid,integer)') and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value) where config.value in ('search_path=','search_path=""'))) as planning_references_search_path,
   has_function_privilege('service_role','public.ai_planning_reference_candidates(uuid,integer)','EXECUTE') as planning_references_service,
   not has_function_privilege('authenticated','public.ai_planning_reference_candidates(uuid,integer)','EXECUTE') as planning_references_authenticated_denied,
   not has_function_privilege('anon','public.ai_planning_reference_candidates(uuid,integer)','EXECUTE') as planning_references_anon_denied,
   to_regprocedure('public.ai_review_workspace_batch(uuid,uuid,text,uuid[])') is not null as workspace_batch_rpc,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_workspace_batch(uuid,uuid,text,uuid[])') and not p.prosecdef) as workspace_batch_invoker,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_workspace_batch(uuid,uuid,text,uuid[])') and position('REVIEW_WORKSPACE_BATCH_V17' in p.prosrc)>0) as workspace_batch_v17,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_workspace_batch(uuid,uuid,text,uuid[])') and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value) where config.value in ('search_path=','search_path=""'))) as workspace_batch_search_path,
   has_function_privilege('service_role','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','EXECUTE') as workspace_batch_service,
   not has_function_privilege('authenticated','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','EXECUTE') as workspace_batch_authenticated_denied,
   not has_function_privilege('anon','public.ai_review_workspace_batch(uuid,uuid,text,uuid[])','EXECUTE') as workspace_batch_anon_denied,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_backfill(uuid,uuid,text,integer,integer)') and position('REVIEW_BACKFILL_CLASSIFICATION_V18' in p.prosrc)>0) as backfill_v18,
   to_regprocedure('public.ai_review_effective_meta(uuid,text)') is not null as effective_meta_v18_rpc,
   has_function_privilege('service_role','public.ai_review_effective_meta(uuid,text)','EXECUTE') as effective_meta_v18_service,
   has_function_privilege('service_role','public.ai_review_backfill(uuid,uuid,text,integer,integer)','EXECUTE') as backfill_v18_service,
   to_regclass('guanye_internal.uploaded_source_provenance') is not null as provenance_table,
   exists(select 1 from pg_constraint c where c.conname='uploaded_source_provenance_deck_fk'
    and c.conrelid='guanye_internal.uploaded_source_provenance'::regclass
    and c.confrelid='public.decks'::regclass and c.condeferrable and c.condeferred and c.confdeltype='c') as provenance_fk,
   to_regclass('guanye_internal.uploaded_source_provenance_source_group_idx') is not null as provenance_source_group_index,
   to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)') is not null as provenance_rpc,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_import_review_deck(uuid,uuid,jsonb,text)')
    and p.prosecdef and position('UPLOADED_SOURCE_PROVENANCE_M19' in p.prosrc)>0
    and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value) where config.value in ('search_path=','search_path=""'))) as provenance_rpc_shape,
   has_function_privilege('service_role','public.ai_import_review_deck(uuid,uuid,jsonb,text)','EXECUTE') as provenance_service,
   not has_function_privilege('authenticated','public.ai_import_review_deck(uuid,uuid,jsonb,text)','EXECUTE') as provenance_authenticated_denied,
   not has_function_privilege('anon','public.ai_import_review_deck(uuid,uuid,jsonb,text)','EXECUTE') as provenance_anon_denied,
   not has_table_privilege('service_role','guanye_internal.uploaded_source_provenance','SELECT') as provenance_service_private,
   not has_table_privilege('authenticated','guanye_internal.uploaded_source_provenance','SELECT') as provenance_authenticated_private,
   not has_table_privilege('anon','guanye_internal.uploaded_source_provenance','SELECT') as provenance_anon_private,
   to_regprocedure('public.ai_review_source_group_decks(uuid,text)') is not null as provenance_split_rpc,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_source_group_decks(uuid,text)')
    and p.prosecdef and position('UPLOADED_SOURCE_SPLIT_M19' in p.prosrc)>0) as provenance_split_shape,
   has_function_privilege('service_role','public.ai_review_source_group_decks(uuid,text)','EXECUTE') as provenance_split_service,
   to_regprocedure('guanye_internal.ai_review_source_group_key(uuid,text,text)') is not null as source_group_key,
   not has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_source_group_key(uuid,text,text)'),'EXECUTE') as source_group_key_private,
   to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)') is not null as source_group_bucket,
   has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE') as source_group_service,
   not has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE') as source_group_authenticated_denied,
   not has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_source_group_bucket(uuid,text,text)'),'EXECUTE') as source_group_anon_denied,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_comparison(uuid)') and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0 and not p.prosecdef) as comparison_v20,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_calibration(uuid,uuid,text,integer,text,text)') and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0) as calibration_v20,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_prepare(uuid,uuid,jsonb)') and position('SOURCE_GROUP_ROUTING_V20' in p.prosrc)>0) as prepare_v20,
   to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])') is not null as comparison_dedup_helper,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])')
    and p.prosecdef and p.proretset and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value) where config.value in ('search_path=','search_path=""'))) as comparison_dedup_helper_shape,
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_comparison(uuid)')
    and not p.prosecdef and position('COMPARISON_SOURCE_DEDUP_V21' in p.prosrc)>0
    and exists(select 1 from unnest(coalesce(p.proconfig,array[]::text[])) as config(value) where config.value in ('search_path=','search_path=""'))) as comparison_v21,
   has_function_privilege('service_role',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE') as comparison_dedup_helper_service,
   not has_function_privilege('authenticated',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE') as comparison_dedup_helper_authenticated_denied,
   not has_function_privilege('anon',to_regprocedure('guanye_internal.ai_review_comparison_source_dedup(uuid,text[])'),'EXECUTE') as comparison_dedup_helper_anon_denied`)).rows[0];
  assert.equal(objects.review,true);assert.equal(objects.planning,true);assert.equal(objects.generation,true);
  assert.equal(objects.history_rpc,true);assert.equal(objects.calibration_rpc,true);assert.equal(objects.comparison_rpc,true);assert.equal(objects.backfill_rpc,true);assert.equal(objects.queue_decks_rpc,true);assert.equal(objects.queue_counts_rpc,true);assert.equal(objects.queue_jobs_rpc,true);assert.equal(objects.queue_decks_index,true);assert.equal(objects.planning_history_rpc,true);assert.equal(objects.planning_history_index,true);assert.equal(objects.queue_trigger,true);assert.equal(objects.effective_meta_service,true);assert.equal(objects.effective_meta_authenticated_denied,true);assert.equal(objects.effective_meta_anon_denied,true);assert.equal(objects.effective_meta_v18,true);assert.equal(objects.confirm_service,true);assert.equal(objects.confirm_authenticated_denied,true);assert.equal(objects.confirm_anon_denied,true);assert.equal(objects.confirm_v13,true);assert.equal(objects.summary_v14,true);assert.equal(objects.summary_authenticated,true);assert.equal(objects.summary_anon_denied,true);assert.equal(objects.uploaded_formal_rpc,true);assert.equal(objects.uploaded_formal_service,true);assert.equal(objects.uploaded_formal_authenticated_denied,true);assert.equal(objects.uploaded_formal_anon_denied,true);assert.equal(objects.planning_references_rpc,true);assert.equal(objects.planning_references_definer,true);assert.equal(objects.planning_references_v15,true,'V15 marker must be inside ai_planning_reference_candidates prosrc');assert.equal(objects.planning_references_search_path,true,'V15 function must pin search_path to the empty value');assert.equal(objects.planning_references_service,true);assert.equal(objects.planning_references_authenticated_denied,true);assert.equal(objects.planning_references_anon_denied,true);assert.equal(objects.workspace_batch_rpc,true);assert.equal(objects.workspace_batch_invoker,true);assert.equal(objects.workspace_batch_v17,true);assert.equal(objects.workspace_batch_search_path,true);assert.equal(objects.workspace_batch_service,true);assert.equal(objects.workspace_batch_authenticated_denied,true);assert.equal(objects.workspace_batch_anon_denied,true);assert.equal(objects.backfill_v18,true);assert.equal(objects.effective_meta_v18_rpc,true);assert.equal(objects.effective_meta_v18_service,true);assert.equal(objects.backfill_v18_service,true);
  assert.equal(objects.provenance_table,true);assert.equal(objects.provenance_fk,true);assert.equal(objects.provenance_source_group_index,true);assert.equal(objects.provenance_rpc,true);assert.equal(objects.provenance_rpc_shape,true);assert.equal(objects.provenance_service,true);assert.equal(objects.provenance_authenticated_denied,true);assert.equal(objects.provenance_anon_denied,true);assert.equal(objects.provenance_service_private,true);assert.equal(objects.provenance_authenticated_private,true);assert.equal(objects.provenance_anon_private,true);assert.equal(objects.provenance_split_rpc,true);assert.equal(objects.provenance_split_shape,true);assert.equal(objects.provenance_split_service,true);
  assert.equal(objects.source_group_key,true);assert.equal(objects.source_group_key_private,true);assert.equal(objects.source_group_bucket,true);assert.equal(objects.source_group_service,true);assert.equal(objects.source_group_authenticated_denied,true);assert.equal(objects.source_group_anon_denied,true);assert.equal(objects.comparison_v20,true);assert.equal(objects.calibration_v20,true);assert.equal(objects.prepare_v20,true);assert.equal(objects.comparison_dedup_helper,true);assert.equal(objects.comparison_dedup_helper_shape,true);assert.equal(objects.comparison_v21,true);assert.equal(objects.comparison_dedup_helper_service,true);assert.equal(objects.comparison_dedup_helper_authenticated_denied,true);assert.equal(objects.comparison_dedup_helper_anon_denied,true);
  for(const role of ['authenticated','anon']){
   await db.exec(`set role ${role}`);
   let readableRows=null;
   try{readableRows=(await db.query('select * from guanye_internal.schema_migrations')).rows;}catch(error){}
   assert.ok(readableRows===null||readableRows.length===0,`${role} must not read migration history`);
   let writeFailed=false;
   const forgedChecksum='c'.repeat(64);
   try{await db.query(`insert into guanye_internal.schema_migrations(version,checksum) values ('forged','${forgedChecksum}')`);}catch(error){writeFailed=true;}
   assert.equal(writeFailed,true,`${role} must not forge migration history`);
   await db.exec('reset role');
   const forged=(await db.query("select count(*)::int as count from guanye_internal.schema_migrations where version='forged'")).rows[0].count;
   assert.equal(forged,0,`${role} must not leave a forged history row`);
  }
  let failed=false;
  try{await db.exec(fs.readFileSync(failurePath,'utf8'));}catch(error){failed=true;assert.match(error.message,/division by zero/);}
  assert.equal(failed,true);
  const rollback=(await db.query("select to_regclass('public.atomic_failure') is not null as created, exists(select 1 from guanye_internal.schema_migrations where version='atomic-failure') as recorded")).rows[0];
  assert.equal(rollback.created,false);assert.equal(rollback.recorded,false);
  const uploadedBaseline = (await db.query(`select
   exists(select 1 from pg_proc p where p.oid=to_regprocedure('public.ai_review_prepare(uuid,uuid,jsonb)')
      and p.prosecdef and position('UPLOADED_BASELINE_V16' in p.prosrc)>0) as writer_marker,
   has_function_privilege('service_role','public.ai_review_prepare(uuid,uuid,jsonb)','execute') as service_granted,
   not has_function_privilege('authenticated','public.ai_review_prepare(uuid,uuid,jsonb)','execute') as authenticated_denied,
   not has_function_privilege('anon','public.ai_review_prepare(uuid,uuid,jsonb)','execute') as anon_denied`)).rows[0];
  assert.deepEqual(uploadedBaseline,{writer_marker:true,service_granted:true,authenticated_denied:true,anon_denied:true});
  await require(process.cwd()+'/test_single_judge_sql.cjs')(db);
  await require(process.cwd()+'/test_single_judge_submission_sql.cjs')(db);
  await require(process.cwd()+'/test_image_worker_capacity_sql.cjs')(db);
  await require(process.cwd()+'/test_single_judge_legacy_backfill_sql.cjs')(db);
  console.log('PASS wrapped migration catalog and atomic rollback');
 }finally{await db.close();}
})().catch(error=>{console.error(error.stack||error.message);process.exit(1);});
'''
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            files={}
            files['setup']=directory/'setup.sql';files['setup'].write_text(setup,encoding='utf-8')
            files['legacy']=directory/'legacy.sql';files['legacy'].write_text(legacy,encoding='utf-8')
            files['history']=directory/'history.sql';files['history'].write_text(manage.HISTORY_DDL,encoding='utf-8')
            for index,item in enumerate(self.catalog,1):
                path=directory/f'migration-{index:02d}.sql'
                path.write_text(manage._migration_sql(item['version'],item['checksum'],item['body']),encoding='utf-8')
                files[f'migration-{index:02d}']=path
            files['failure']=directory/'failure.sql';files['failure'].write_text(failure,encoding='utf-8')
            files['script']=directory/'pglite.cjs';files['script'].write_text(script,encoding='utf-8')
            arguments=[str(files['script']),str(PGLITE),str(PGCRYPTO),str(files['setup']),str(files['legacy']),str(files['history'])]
            arguments.extend(str(files[f'migration-{index:02d}']) for index in range(1,len(self.catalog)+1))
            probe_api=FakeApi(base_installed=True)
            manage._existing_schema_probes(probe_api,TEST_REF,read_only=True)
            files['probe']=directory/'probe.sql';files['probe'].write_text(probe_api.queries[0],encoding='utf-8')
            arguments.extend((str(files['probe']),str(files['failure'])))
            completed=subprocess.run([shutil.which('node')]+arguments,cwd=ROOT,text=True,
                                     encoding='utf-8',capture_output=True)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        self.assertIn('PASS wrapped migration catalog and atomic rollback',completed.stdout)


class MigrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.catalog=manage._migration_catalog()
        self.versions=[item['version'] for item in self.catalog]

    def _history_through(self,count):
        history={manage.BASE_VERSION:manage._schema_checksum(manage.HERE/'schema.sql')}
        history.update({item['version']:item['checksum'] for item in self.catalog[:count]})
        return history

    def test_migration_plan_payloads_are_read_only_selects(self):
        history=self._history_through(20)
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        plan=manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(plan['read_only'],True)
        self.assertTrue(api.payloads)
        for payload in api.payloads:
            self.assertEqual(set(payload),{'query','read_only'})
            self.assertIs(payload['read_only'],True)
            self.assertTrue(payload['query'].lstrip().lower().startswith('select'))
        self.assertFalse(any(query.lstrip().lower().startswith(('do ','insert ','update ','delete ','create ','alter ')) for query in api.queries))
        self.assertEqual(api.history,history)

    def test_read_only_adapter_rejects_non_select_sql(self):
        api=FakeApi(base_installed=True)
        with self.assertRaisesRegex(RuntimeError,'only permits metadata SELECT'):
            manage._read_only_query_rows(api,TEST_REF,'do $$ begin null; end $$;')
        self.assertEqual(api.queries,[])

    def test_plan_matches_actual_migrate_for_managed_prefix(self):
        history=self._history_through(len(self.catalog)-1)
        plan_api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        plan=manage._migration_plan(plan_api,TEST_REF,catalog=self.catalog)
        migrate_api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        result=manage._migrate_database(migrate_api,TEST_REF,catalog=self.catalog)
        self.assertEqual(plan['current_type'],'managed_prefix')
        self.assertEqual(plan['history_status'],'prefix')
        self.assertEqual(plan['recorded_versions'],list(history))
        self.assertEqual(plan['pending_versions'],[self.versions[-1]])
        self.assertEqual(plan['migration_path']['mode'],'managed_migrate')
        self.assertEqual(plan['migration_path']['command'],f'python -B cloud/manage.py migrate --project-ref {TEST_REF}')
        self.assertEqual(result['applied'],plan['pending_versions'])
        self.assertEqual(list(migrate_api.history),[manage.BASE_VERSION]+self.versions)

    def test_plan_classifies_unmanaged_base_and_preserves_upgrade_rule(self):
        probes={f'm{index:02d}':False for index in range(2,len(self.catalog)+1)}
        api=FakeApi(base_installed=True,history_table_exists=False,probes=probes)
        plan=manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(plan['current_type'],'unmanaged_base')
        self.assertEqual(plan['history_status'],'absent')
        self.assertEqual(plan['recorded_versions'],[])
        self.assertEqual(plan['pending_versions'],self.versions)
        self.assertEqual(plan['migration_path']['mode'],'upgrade_unmanaged_base')
        self.assertEqual(plan['migration_path']['command'],f'python -B cloud/manage.py migrate --project-ref {TEST_REF} --upgrade-unmanaged-base')
        self.assertFalse(plan['migration_path']['requires_manual_audit'])
        self.assertFalse(any(query.lstrip().lower().startswith(('do ','insert ','update ','delete ','create ','alter ')) for query in api.queries))

    def test_unmanaged_later_objects_require_manual_audit(self):
        api=FakeApi(base_installed=True,history_table_exists=False)
        plan=manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(plan['current_type'],'unmanaged_with_later_objects')
        self.assertEqual(plan['recorded_versions'],[])
        self.assertEqual(plan['pending_versions'],self.versions)
        self.assertEqual(plan['detected_later_versions'],self.versions[1:])
        self.assertEqual(plan['migration_path']['mode'],'manual_audit')
        self.assertIsNone(plan['migration_path']['command'])
        self.assertTrue(plan['migration_path']['requires_manual_audit'])
        self.assertFalse(any(query.lstrip().lower().startswith(('do ','insert ','update ','delete ','create ','alter ')) for query in api.queries))

    def test_complete_managed_history_has_no_pending_versions(self):
        history=self._history_through(len(self.catalog))
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        plan=manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertEqual(plan['current_type'],'managed_complete')
        self.assertEqual(plan['history_status'],'complete')
        self.assertEqual(plan['pending_versions'],[])
        self.assertEqual(plan['migration_path']['mode'],'none')
        self.assertIsNone(plan['migration_path']['command'])

    def test_missing_and_incomplete_base_are_rejected(self):
        missing=FakeApi(base_installed=False)
        with self.assertRaisesRegex(RuntimeError,'missing_base'):
            manage._migration_plan(missing,TEST_REF,catalog=self.catalog)
        self.assertTrue(all(payload.get('read_only') is True for payload in missing.payloads))

        incomplete=FakeApi(base_installed=True,state={'workspaces':True,'members':True,'decks':False,'reviews':False})
        with self.assertRaisesRegex(RuntimeError,'incomplete_base'):
            manage._migration_plan(incomplete,TEST_REF,catalog=self.catalog)
        self.assertTrue(all(payload.get('read_only') is True for payload in incomplete.payloads))

    def test_history_without_base_marker_is_rejected(self):
        history={self.versions[0]:self.catalog[0]['checksum']}
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        with self.assertRaisesRegex(RuntimeError,'no base marker'):
            manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_missing_number_and_out_of_order_history_are_rejected(self):
        history={manage.BASE_VERSION:manage._schema_checksum(manage.HERE/'schema.sql'),
                 self.versions[5]:self.catalog[5]['checksum']}
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        with self.assertRaisesRegex(RuntimeError,'ordered prefix'):
            manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_checksum_drift_is_rejected_before_writes(self):
        history=self._history_through(len(self.catalog))
        history[self.versions[0]]='0'*64
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        with self.assertRaisesRegex(RuntimeError,'checksum changed'):
            manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_unknown_history_is_rejected_before_writes(self):
        history=self._history_through(len(self.catalog))
        history['9999_unknown']='a'*64
        api=FakeApi(base_installed=True,history_table_exists=True,history=history)
        with self.assertRaisesRegex(RuntimeError,'unknown migration history'):
            manage._migration_plan(api,TEST_REF,catalog=self.catalog)
        self.assertFalse(any(query.startswith('do $guanye_migration$') for query in api.queries))

    def test_project_ref_validation_accepts_only_lowercase_alphanumeric_20(self):
        self.assertEqual(manage._validated_project_ref(TEST_REF),TEST_REF)
        self.assertEqual(manage._validated_project_ref('0123456789abcdefghij'),'0123456789abcdefghij')
        for invalid in ('short-ref','ABCDEFGHIJKLMNOPQRST','a'*19,'a'*21,'abcdefghijklmnopqrs-'):
            api=FakeApi(base_installed=True)
            with self.assertRaisesRegex(RuntimeError,'Invalid Supabase project ref'):
                manage._migration_plan(api,invalid,catalog=self.catalog)
            self.assertEqual(api.queries,[])
            with self.assertRaisesRegex(RuntimeError,'Invalid Supabase project ref'):
                manage._migrate_database(api,invalid,catalog=self.catalog)
            self.assertEqual(api.queries,[])

    def test_plan_and_migrate_explicit_ref_override_local_config_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            private=Path(temporary)/'private-deployment.json'
            original=json.dumps({'ref':OTHER_REF,'workspaceId':'local-config'},ensure_ascii=False).encode('utf-8')
            private.write_bytes(original)
            plan_result={'project_ref':TEST_REF,'current_type':'managed_complete','pending_versions':[]}
            migrate_result={'adopted':False,'initialized_unmanaged_base':False,'applied':[]}
            with mock.patch.object(manage,'PRIVATE',private), \
                 mock.patch.dict(os.environ,{'SUPABASE_ACCESS_TOKEN':'sbp_test'},clear=False), \
                 mock.patch.object(manage,'_migration_plan',return_value=plan_result) as plan, \
                 mock.patch.object(manage,'_migrate_database',return_value=migrate_result) as migrate, \
                 mock.patch.object(manage.urllib.request,'build_opener',return_value=mock.Mock()):
                plan_output=io.StringIO()
                with mock.patch('sys.argv',['manage.py','migration-plan','--project-ref',TEST_REF]):
                    with contextlib.redirect_stdout(plan_output):
                        manage.main()
                with mock.patch('sys.argv',['manage.py','migrate','--project-ref',TEST_REF]):
                    with contextlib.redirect_stdout(io.StringIO()):
                        manage.main()
            self.assertEqual(json.loads(plan_output.getvalue()),plan_result)
            plan.assert_called_once()
            migrate.assert_called_once()
            self.assertEqual(plan.call_args.args[1],TEST_REF)
            self.assertEqual(migrate.call_args.args[1],TEST_REF)
            self.assertEqual(private.read_bytes(),original)

    def test_cover_pilot_requires_an_explicit_distinct_production_project(self):
        for extra in ([], ['--production-project-ref', TEST_REF], ['--production-project-ref', 'bad-ref']):
            with self.subTest(extra=extra), mock.patch('sys.argv', ['manage.py', 'migrate',
                '--project-ref', TEST_REF, '--cover-pilot', *extra]), \
                 mock.patch.object(manage.urllib.request, 'build_opener') as opener, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    manage.main()
                self.assertEqual(caught.exception.code, 2)
                opener.assert_not_called()

    def test_cover_migration_requires_pilot_flag_and_preserves_it_in_next_command(self):
        prod = 'qrstabcdefghijklmnop'
        extra = ['--cover-pilot', '--production-project-ref', prod]
        plan_result = {'migration_path': {'command': f'python -B cloud/manage.py migrate --project-ref {TEST_REF}'}}
        migrate_result = {'adopted':False, 'initialized_unmanaged_base':False, 'applied':[]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(manage,'PRIVATE',Path(tmp)/'private-deployment.json'), \
             mock.patch.dict(os.environ,{'SUPABASE_ACCESS_TOKEN':'sbp_test'},clear=False), \
             mock.patch.object(manage,'_migrate_database',return_value=migrate_result) as migrate, \
             mock.patch.object(manage,'_migration_plan',return_value=plan_result) as plan, \
             mock.patch.object(manage.urllib.request,'build_opener',return_value=mock.Mock()):
            with mock.patch('sys.argv',['manage.py','migrate','--project-ref',TEST_REF]), \
                 contextlib.redirect_stdout(io.StringIO()):
                manage.main()
            default_catalog=migrate.call_args.kwargs['catalog']
            self.assertNotIn(manage.COVER_PILOT_VERSION,[entry['version'] for entry in default_catalog])
            with mock.patch('sys.argv',['manage.py','migrate','--project-ref',TEST_REF,*extra]), \
                 contextlib.redirect_stdout(io.StringIO()):
                manage.main()
            pilot_catalog=migrate.call_args.kwargs['catalog']
            self.assertEqual(pilot_catalog[:-len(manage.COVER_PILOT_VERSIONS)],default_catalog)
            self.assertEqual(tuple(e['version'] for e in pilot_catalog[-len(manage.COVER_PILOT_VERSIONS):]),manage.COVER_PILOT_VERSIONS)
            with mock.patch('sys.argv',['manage.py','migration-plan','--project-ref',TEST_REF,*extra]), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                manage.main()
            self.assertEqual(plan.call_args.kwargs['catalog'],pilot_catalog)
            self.assertTrue(json.loads(output.getvalue())['migration_path']['command'].endswith(' '.join(extra)))

    def test_cli_management_api_proxy_requires_explicit_https_proxy(self):
        cases=((None,{}),('',{}),('   ',{}),('http://proxy.example:8080',{'https':'http://proxy.example:8080'}))
        for proxy_value,expected in cases:
            with tempfile.TemporaryDirectory() as temporary:
                private=Path(temporary)/'private-deployment.json'
                with mock.patch.object(manage,'PRIVATE',private),                      mock.patch.dict(os.environ,{'SUPABASE_ACCESS_TOKEN':'sbp_test'},clear=False),                      mock.patch.object(manage,'_migration_plan',return_value={'project_ref':TEST_REF}),                      mock.patch.object(manage.urllib.request,'build_opener',return_value=mock.Mock()),                      mock.patch.object(manage.urllib.request,'ProxyHandler',wraps=manage.urllib.request.ProxyHandler) as handler,                      mock.patch('sys.argv',['manage.py','migration-plan','--project-ref',TEST_REF]):
                    if proxy_value is None:
                        os.environ.pop('HTTPS_PROXY',None)
                    else:
                        os.environ['HTTPS_PROXY']=proxy_value
                    with contextlib.redirect_stdout(io.StringIO()):
                        manage.main()
                handler.assert_called_once_with(expected)

    def test_cli_project_ref_does_not_create_private_deployment_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            private=Path(temporary)/'private-deployment.json'
            result={'project_ref':TEST_REF,'current_type':'managed_complete'}
            with mock.patch.object(manage,'PRIVATE',private), \
                 mock.patch.dict(os.environ,{'SUPABASE_ACCESS_TOKEN':'sbp_test'},clear=False), \
                 mock.patch.object(manage,'_migration_plan',return_value=result) as plan, \
                 mock.patch.object(manage.urllib.request,'build_opener',return_value=mock.Mock()), \
                 mock.patch('sys.argv',['manage.py','migration-plan','--project-ref',TEST_REF]):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):
                    manage.main()
            self.assertFalse(private.exists())
            plan.assert_called_once()
            self.assertEqual(plan.call_args.args[1],TEST_REF)
            self.assertEqual(json.loads(output.getvalue()),result)


if __name__=='__main__':
    unittest.main()
