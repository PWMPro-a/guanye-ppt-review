from __future__ import annotations

import base64
import copy
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import cloud.ai_generation as generation
from cloud.windows_image_worker import (
    GatewayClient, LocalState, PreflightError, WindowsWorker, WorkerConfig, main, preflight,
)


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII='
)


class FakeRouter:
    def __init__(self):
        self.routes = {}

    def add(self, method, path, handler):
        self.routes[(method, path)] = handler


class FakeCtx:
    def __init__(self, *, workspace_id='11111111-1111-4111-8111-111111111111',
                 user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', role='reviewer'):
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.role = role
        self.token = 'test-token'
        self.body = {}
        self.query = {}
        self.calls = []
        self.stored = {}
        self.rpc_results = {}
        self.plan = None

    def rpc(self, name, params, privileged=False):
        self.calls.append(('rpc', name, params, privileged))
        result = self.rpc_results.get(name)
        if callable(result):
            return result(params)
        if result is not None:
            return result
        if name == 'ai_planning_get_plan':
            return self.plan
        raise AssertionError('unexpected RPC ' + name)

    def select(self, table, query, privileged=False):
        raise AssertionError('select is not expected in this test')

    def storage_get(self, path):
        value = self.stored.get(path)
        return value[0] if isinstance(value, tuple) else value

    def storage_sign(self, path, expires_in=3600):
        self.calls.append(('storage_sign', path, expires_in))
        return {'signedUrl': 'https://signed.example/' + path + '?expires=' + str(expires_in)}

    def storage_put(self, path, raw, content_type):
        self.calls.append(('storage_put', path, raw, content_type))
        self.stored[path] = (raw, content_type)

    def chat_json(self, messages):
        raise AssertionError('chat_json is not expected in this test')


def job_view(job_id='22222222-2222-4222-8222-222222222222', status='queued'):
    return {
        'id': job_id,
        'planId': 'plan-1',
        'deckId': 'generated-test',
        'status': status,
        'title': '测试生成',
        'style': '克制、清晰',
        'outline': {'title': '测试生成'},
        'references': [],
        'pages': [],
    }


class GatewayTestCase(unittest.TestCase):
    def test_configure_registers_exact_routes(self):
        router = FakeRouter()
        generation.configure(router)
        self.assertEqual({
            ('POST', '/api/ai/generation'),
            ('GET', '/api/ai/generation'),
            ('POST', '/api/ai/generation/retry'),
            ('POST', '/api/ai/generation/worker/claim'),
            ('POST', '/api/ai/generation/worker/heartbeat'),
            ('POST', '/api/ai/generation/worker/complete'),
            ('POST', '/api/ai/generation/worker/fail'),
            ('POST', '/api/ai/generation/worker/reference'),
            ('GET', '/api/ai/generation/artifact'),
            ('GET', '/api/ai/generation/settings'),
            ('POST', '/api/ai/generation/settings'),
            ('GET', '/api/ai/generation/health'),
        }, set(router.routes))

    def test_create_normalizes_confirmed_plan_and_calls_generation_rpc(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        plan_id = str(uuid.uuid4())
        ctx.body = {'plan_id': plan_id}
        payload_plan = {
            'schemaVersion': 1,
            'title': '品牌发布',
            'mode': 'content_draft',
            'pageCount': 2,
            'style': {
                'name': '克制深青',
                'summary': '白底、深青色、无渐变',
                'palette': ['#FFFFFF', '#164E5A'],
                'typography': {'heading': '思源宋体', 'body': '思源黑体', 'numbers': '等宽数字'},
                'layoutLanguage': '左对齐网格，大面积留白',
                'decoration': '细线',
                'avoid': ['渐变', '发光'],
            },
            'pages': [
                {'index': 1, 'title': '封面', 'copy': [{'kind': 'title', 'text': '品牌发布'}], 'layout': '居左', 'visualBrief': '一张克制的品牌封面'},
                {'index': 2, 'title': '结尾', 'copy': [{'kind': 'body', 'text': '谢谢观看'}], 'layout': '居中', 'visualBrief': '极简结尾页'},
            ],
            'references': [{'deckId': 'deck-ref', 'title': '参考作品', 'imagePaths': ['refs/ref.png']}],
            'generationBrief': '严格按已确认文案生成，不得改写。',
        }
        ctx.plan = {
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'createdBy': ctx.user_id,
            'status': 'confirmed',
            'confirmed': {
                'versionId': str(uuid.uuid4()),
                'version': 1,
                'contentHash': 'c' * 64,
                'payload': payload_plan,
            },
        }
        captured = {}

        def create(params):
            captured.update(params)
            return job_view()

        confirmed_snapshot = {
            **payload_plan,
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'versionId': ctx.plan['confirmed']['versionId'],
            'version': 1,
            'status': 'confirmed',
            'contentHash': 'c' * 64,
        }
        ctx.rpc_results['ai_planning_read_confirmed_plan'] = copy.deepcopy(confirmed_snapshot)
        ctx.rpc_results['ai_generation_create'] = create
        status, payload = router.routes[('POST', '/api/ai/generation')](ctx)
        self.assertEqual(status, 202)
        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(captured['p_title'], '品牌发布')
        self.assertIn('克制深青', captured['p_style'])
        self.assertIn('版式语言', captured['p_style'])
        self.assertEqual(len(captured['p_pages']), 2)
        self.assertEqual(captured['p_pages'][0]['planPageId'], 'page-0001')
        self.assertIn('可见逐字文案', captured['p_pages'][0]['description']['text'])
        self.assertIn('品牌发布', captured['p_pages'][0]['description']['text'])
        self.assertEqual(captured['p_pages'][0]['outline']['copy'][0]['text'], '品牌发布')
        self.assertEqual(captured['p_outline']['brief'], '严格按已确认文案生成，不得改写。')
        self.assertEqual(len(captured['p_outline']['pages']), 2)
        self.assertEqual(captured['p_config']['workers'], 4)
        self.assertTrue(captured['p_deck_id'].startswith('generated-'))
        planning_call = [call for call in ctx.calls if call[0] == 'rpc' and call[1] == 'ai_planning_get_plan'][0]
        self.assertEqual(planning_call[2], {'w': ctx.workspace_id, 'plan_id': plan_id})
        self.assertFalse(planning_call[3])
        generation_call = [call for call in ctx.calls if call[0] == 'rpc' and call[1] == 'ai_generation_create'][0]
        self.assertTrue(generation_call[3])
        immutable_call = [call for call in ctx.calls if call[0] == 'rpc' and call[1] == 'ai_planning_read_confirmed_plan'][0]
        self.assertEqual(immutable_call[2], {'p_plan_id': plan_id})
        self.assertTrue(immutable_call[3])
        self.assertNotIn('createdBy', confirmed_snapshot)
        self.assertEqual(captured['p_plan'], confirmed_snapshot)

    def test_confirmed_structured_page_preserves_verbatim_copy_and_roles(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        plan_id = str(uuid.uuid4())
        ctx.body = {'plan_id': plan_id}
        long_copy = '逐字证据' * 3001
        payload_plan = {
            'schemaVersion': 1,
            'title': '数据结论',
            'mode': 'content_draft',
            'pageCount': 1,
            'style': {
                'name': '克制深青',
                'summary': '白底、深青色、无渐变',
                'palette': ['#FFFFFF', '#164E5A'],
                'layoutLanguage': '左对齐网格，大面积留白',
                'avoid': ['渐变', '发光'],
            },
            'pages': [{
                'index': 1,
                'title': '数据结论',
                'narrativeRole': 'data',
                'copy': [
                    {'kind': 'title', 'text': '结论先行'},
                    {'kind': 'body', 'text': long_copy},
                ],
                'layout': '左栏结论，右栏横向条形图',
                'visualBrief': '按各项占比从高到低排序，使用横向条形图',
                'contentSources': ['内部材料'],
                'styleReferenceDeckIds': ['deck-ref'],
            }],
            'references': [{'deckId': 'deck-ref', 'title': '参考作品', 'imagePaths': ['refs/ref.png']}],
            'generationBrief': '严格按已确认文案生成，不得改写。',
        }
        ctx.plan = {
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'createdBy': ctx.user_id,
            'status': 'confirmed',
            'confirmed': {
                'versionId': str(uuid.uuid4()),
                'version': 1,
                'contentHash': 'd' * 64,
                'payload': payload_plan,
            },
        }
        ctx.rpc_results['ai_planning_read_confirmed_plan'] = {
            **payload_plan,
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'versionId': ctx.plan['confirmed']['versionId'],
            'version': 1,
            'status': 'confirmed',
            'contentHash': 'd' * 64,
        }
        captured = {}

        def create(params):
            captured.update(params)
            return job_view()

        ctx.rpc_results['ai_generation_create'] = create
        status, _ = router.routes[('POST', '/api/ai/generation')](ctx)
        self.assertEqual(status, 202)
        page = captured['p_pages'][0]
        description = page['description']
        page_text = description['text']
        self.assertEqual(page['outline']['narrativeRole'], 'data')
        self.assertEqual(page['outline']['copy'][1]['text'], long_copy)
        self.assertNotIn('数据结论', page_text)
        self.assertEqual(description['narrativeRole'], 'data')
        self.assertIn('可见逐字文案', page_text)
        self.assertIn('只供生图的页面说明', page_text)
        self.assertIn(long_copy, page_text)
        self.assertGreater(len(page_text), 12000)
        self.assertIn('叙事角色：data', page_text)
        self.assertIn('版式：左栏结论，右栏横向条形图', page_text)
        self.assertIn('视觉与图表：按各项占比从高到低排序，使用横向条形图', page_text)
        self.assertIn('不得把本段文字或任何技术标签画进画面', page_text)
        self.assertEqual(page['prompt'], page_text)

    def test_create_preserves_legacy_description_and_prompt_forms(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        plan_id = str(uuid.uuid4())
        ctx.body = {'plan_id': plan_id}
        payload_plan = {
            'title': '旧任务兼容',
            'mode': 'content_draft',
            'pageCount': 5,
            'style': {'name': '旧风格', 'summary': '沿用旧版'},
            'pages': [
                {
                    'title': '旧描述数组',
                    'copy': [{'kind': 'body', 'text': '结构化旧文案'}],
                    'description': {'text_content': ['旧文一', '旧文二']},
                },
                {
                    'title': '只有旧提示词',
                    'copy': [{'kind': 'body', 'text': '结构化提示词页'}],
                    'prompt': 'LEGACY PROMPT ONLY',
                },
                {
                    'title': '并列旧字段',
                    'description': {'text': 'LEGACY DESCRIPTION'},
                    'prompt': 'LEGACY PROMPT',
                },
                {'title': '旧页标题'},
                {
                    'title': '旧内部标题',
                    'copy': [{'kind': 'body', 'text': '仅确认正文'}],
                },
            ],
            'references': [],
            'generationBrief': '旧任务字段原样兼容。',
        }
        ctx.plan = {
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'createdBy': ctx.user_id,
            'status': 'confirmed',
            'confirmed': {
                'versionId': str(uuid.uuid4()),
                'version': 1,
                'contentHash': 'e' * 64,
                'payload': payload_plan,
            },
        }
        ctx.rpc_results['ai_planning_read_confirmed_plan'] = {
            **payload_plan,
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'versionId': ctx.plan['confirmed']['versionId'],
            'version': 1,
            'status': 'confirmed',
            'contentHash': 'e' * 64,
        }
        captured = {}

        def create(params):
            captured.update(params)
            return job_view()

        ctx.rpc_results['ai_generation_create'] = create
        status, _ = router.routes[('POST', '/api/ai/generation')](ctx)
        self.assertEqual(status, 202)
        pages = captured['p_pages']
        legacy_text = '旧文一' + chr(10) + '旧文二'
        self.assertEqual(pages[0]['description']['text'], legacy_text)
        self.assertEqual(pages[0]['prompt'], legacy_text)
        self.assertEqual(pages[1]['description']['text'], 'LEGACY PROMPT ONLY')
        self.assertEqual(pages[1]['prompt'], 'LEGACY PROMPT ONLY')
        self.assertEqual(pages[2]['description']['text'], 'LEGACY DESCRIPTION')
        self.assertEqual(pages[2]['prompt'], 'LEGACY PROMPT')
        self.assertIn('旧页标题', pages[3]['description']['text'])
        self.assertIn('仅确认正文', pages[4]['description']['text'])
        self.assertNotIn('旧内部标题', pages[4]['description']['text'])

    def test_schema_v1_conflicting_legacy_fields_do_not_override_confirmed_page(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        plan_id = str(uuid.uuid4())
        ctx.body = {'plan_id': plan_id}
        payload_plan = {
            'schemaVersion': 1,
            'title': '冲突测试',
            'mode': 'content_draft',
            'pageCount': 1,
            'outline': {
                'title': '冲突计划大纲',
                'brief': '冲突计划概述',
                'pages': [{'title': '冲突计划页', 'narrativeRole': 'cover'}],
            },
            'style': {'name': '确认风格', 'summary': '确认风格说明'},
            'pages': [{
                'index': 1,
                'title': '内部页标题',
                'narrativeRole': 'argument',
                'copy': [
                    {'kind': 'title', 'text': '确认标题'},
                    {'kind': 'body', 'text': '确认正文'},
                ],
                'layout': '确认版式',
                'visualBrief': '确认视觉',
                'contentSources': ['确认来源'],
                'styleReferenceDeckIds': ['confirmed-ref'],
                'description': {'text': '冲突描述'},
                'prompt': '冲突提示词',
                'outline': {
                    'title': '冲突大纲标题',
                    'narrativeRole': 'cover',
                    'copy': [{'kind': 'title', 'text': '冲突大纲文案'}],
                    'layout': '冲突大纲版式',
                    'visualBrief': '冲突大纲视觉',
                    'contentSources': ['冲突大纲来源'],
                    'styleReferenceDeckIds': ['conflict-ref'],
                },
                'outline_content': {
                    'title': '冲突大纲内容标题',
                    'narrativeRole': 'summary',
                    'copy': [{'kind': 'body', 'text': '冲突大纲内容文案'}],
                    'layout': '冲突大纲内容版式',
                },
            }],
            'references': [],
            'generationBrief': '确认约束',
        }
        ctx.plan = {
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'createdBy': ctx.user_id,
            'status': 'confirmed',
            'confirmed': {
                'versionId': str(uuid.uuid4()),
                'version': 1,
                'contentHash': 'f' * 64,
                'payload': payload_plan,
            },
        }
        ctx.rpc_results['ai_planning_read_confirmed_plan'] = {
            **payload_plan,
            'planId': plan_id,
            'workspaceId': ctx.workspace_id,
            'versionId': ctx.plan['confirmed']['versionId'],
            'version': 1,
            'status': 'confirmed',
            'contentHash': 'f' * 64,
        }
        captured = {}

        def create(params):
            captured.update(params)
            return job_view()

        ctx.rpc_results['ai_generation_create'] = create
        status, _ = router.routes[('POST', '/api/ai/generation')](ctx)
        self.assertEqual(status, 202)
        page = captured['p_pages'][0]
        page_text = page['description']['text']
        canonical_copy = [
            {'kind': 'title', 'text': '确认标题'},
            {'kind': 'body', 'text': '确认正文'},
        ]
        canonical_outline = {
            'index': 1,
            'title': '内部页标题',
            'copy': canonical_copy,
            'layout': '确认版式',
            'visualBrief': '确认视觉',
            'contentSources': ['确认来源'],
            'styleReferenceDeckIds': ['confirmed-ref'],
            'narrativeRole': 'argument',
        }
        self.assertEqual(page['outline'], canonical_outline)
        self.assertEqual(captured['p_outline'], {
            'title': '冲突测试',
            'brief': '确认约束',
            'pages': [canonical_outline],
        })
        outline_json = json.dumps(page['outline'], ensure_ascii=False)
        self.assertNotIn('冲突大纲', outline_json)
        self.assertNotIn('conflict-ref', outline_json)
        self.assertEqual(page['prompt'], page_text)
        self.assertIn('确认标题', page_text)
        self.assertIn('确认正文', page_text)
        self.assertIn('版式：确认版式', page_text)
        self.assertIn('视觉与图表：确认视觉', page_text)
        self.assertNotIn('冲突描述', page_text)
        self.assertNotIn('冲突提示词', page_text)
        self.assertNotIn('内部页标题', page_text)

    def test_page_body_over_exe_request_limit_is_rejected(self):
        page = {
            'title': '超长页面',
            'narrativeRole': 'data',
            'copy': [{'kind': 'body', 'text': 'x' * 80000}],
        }
        with patch.object(generation, 'EXE_MAX_REQUEST_BYTES', 64 * 1024):
            with self.assertRaisesRegex(ValueError, 'Windows Image2PPT EXE 单次请求上限'):
                generation._normalize_pages([page], '', '')

    def test_plan_workspace_and_confirmation_are_enforced(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        plan_id = str(uuid.uuid4())
        ctx.body = {'plan_id': plan_id}
        ctx.plan = {
            'planId': plan_id,
            'workspaceId': '99999999-9999-4999-8999-999999999999',
            'status': 'confirmed',
            'confirmed': {'payload': {'title': 'A', 'pages': [{'title': 'A'}], 'style': {}, 'pageCount': 1}},
        }
        with self.assertRaises(PermissionError):
            router.routes[('POST', '/api/ai/generation')](ctx)
        ctx.plan['workspaceId'] = ctx.workspace_id
        ctx.plan['status'] = 'draft'
        with self.assertRaises(ValueError):
            router.routes[('POST', '/api/ai/generation')](ctx)

    def test_complete_uploads_to_claimed_private_path_and_returns_job(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        page_id = str(uuid.uuid4())
        lease_token = str(uuid.uuid4())
        storage_path = f"{ctx.workspace_id}/generated-test/{page_id}.png"
        ctx.body = {
            'page_id': page_id,
            'lease_token': lease_token,
            'image_base64': base64.b64encode(PNG).decode('ascii'),
            'content_type': 'image/png',
            'content_sha256': __import__('hashlib').sha256(PNG).hexdigest(),
            'width': 1,
            'height': 1,
        }
        ctx.rpc_results['ai_generation_page_upload_target'] = {
            'storagePath': storage_path,
            'contentType': 'image/png',
        }
        ctx.rpc_results['ai_generation_page_succeed'] = job_view(status='completed')
        result = router.routes[('POST', '/api/ai/generation/worker/complete')](ctx)
        self.assertEqual(result['status'], 'completed')
        self.assertIn(storage_path, ctx.stored)
        self.assertEqual(ctx.stored[storage_path], (PNG, 'image/png'))
        succeed = [call for call in ctx.calls if call[0] == 'rpc' and call[1] == 'ai_generation_page_succeed'][0]
        self.assertEqual(succeed[2]['p_storage_path'], storage_path)
        self.assertEqual(succeed[2]['p_width'], 1)

    def test_artifact_path_reads_generated_storage_bytes(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        job_id = str(uuid.uuid4())
        page_id = str(uuid.uuid4())
        path = f"{ctx.workspace_id}/generated-test/{page_id}.png"
        ctx.stored[path] = (PNG, 'image/png')
        ctx.query = {'job_id': job_id, 'page_id': page_id}
        ctx.rpc_results['ai_generation_get'] = {
            'id': job_id, 'planId': str(uuid.uuid4()), 'status': 'completed', 'title': '测试', 'pages': [{
                'id': page_id, 'position': 1, 'status': 'succeeded', 'storagePath': path,
                'contentType': 'image/png', 'image': {'id': page_id, 'path': path, 'thumbPath': path},
            }],
        }
        artifact = router.routes[('GET', '/api/ai/generation/artifact')](ctx)
        self.assertEqual(artifact['path'], path)
        self.assertEqual(artifact['contentType'], 'image/png')
        self.assertEqual(base64.b64decode(artifact['dataBase64']), PNG)

    def test_workspace_worker_setting_is_exposed(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx(role='admin')
        ctx.rpc_results['ai_generation_get_settings'] = {'workspaceId': ctx.workspace_id, 'maxWorkers': 4}
        result = router.routes[('GET', '/api/ai/generation/settings')](ctx)
        self.assertEqual(result['maxWorkers'], 4)
        ctx.body = {'max_workers': 3}
        ctx.rpc_results['ai_generation_set_workers'] = {'workspaceId': ctx.workspace_id, 'maxWorkers': 3}
        result = router.routes[('POST', '/api/ai/generation/settings')](ctx)
        self.assertEqual(result['maxWorkers'], 3)
        call = [item for item in ctx.calls if item[0] == 'rpc' and item[1] == 'ai_generation_set_workers'][0]
        self.assertEqual(call[2]['p_max_workers'], 3)
    def test_complete_retry_after_object_write_timeout_is_idempotent(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        page_id = str(uuid.uuid4())
        lease_token = str(uuid.uuid4())
        storage_path = f"{ctx.workspace_id}/generated-test/{page_id}.png"
        ctx.stored[storage_path] = (PNG, 'image/png')
        ctx.body = {
            'page_id': page_id,
            'lease_token': lease_token,
            'image_base64': base64.b64encode(PNG).decode('ascii'),
            'content_type': 'image/png',
        }
        ctx.rpc_results['ai_generation_page_upload_target'] = {
            'storagePath': storage_path,
            'contentType': 'image/png',
        }
        completed = job_view(status='completed')
        completed['pages'] = [{
            'id': page_id, 'position': 1, 'status': 'succeeded', 'title': '封面',
            'storagePath': storage_path, 'contentType': 'image/png',
            'image': {'id': page_id, 'path': storage_path, 'name': 'page-0001.png', 'thumbPath': storage_path},
        }]
        ctx.rpc_results['ai_generation_page_succeed'] = completed
        result = router.routes[('POST', '/api/ai/generation/worker/complete')](ctx)
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(any(call[0] == 'storage_put' for call in ctx.calls))

    def test_complete_refuses_to_overwrite_different_existing_image(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        page_id = str(uuid.uuid4())
        lease_token = str(uuid.uuid4())
        storage_path = f"{ctx.workspace_id}/generated-test/{page_id}.png"
        ctx.stored[storage_path] = (b'not-the-same-image', 'image/png')
        ctx.body = {
            'page_id': page_id,
            'lease_token': lease_token,
            'image_base64': base64.b64encode(PNG).decode('ascii'),
            'content_type': 'image/png',
        }
        ctx.rpc_results['ai_generation_page_upload_target'] = {
            'storagePath': storage_path,
            'contentType': 'image/png',
        }
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            router.routes[('POST', '/api/ai/generation/worker/complete')](ctx)
        self.assertFalse(any(call[0] == 'storage_put' for call in ctx.calls))
        self.assertFalse(any(call[0] == 'rpc' and call[1] == 'ai_generation_page_succeed' for call in ctx.calls))

    def test_worker_reference_uses_lease_scoped_server_selection(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        page_id = str(uuid.uuid4())
        lease_token = str(uuid.uuid4())
        reference_path = f"{ctx.workspace_id}/refs/style.png"
        ctx.stored[reference_path] = (PNG, 'image/png')
        ctx.body = {
            'page_id': page_id,
            'lease_token': lease_token,
            'path': f"{ctx.workspace_id}/client-supplied-should-not-be-used.png",
        }
        ctx.rpc_results['ai_generation_page_reference_path'] = reference_path
        result = router.routes[('POST', '/api/ai/generation/worker/reference')](ctx)
        self.assertEqual(set(result), {'image_base64', 'content_type'})
        self.assertEqual(result['content_type'], 'image/png')
        self.assertEqual(base64.b64decode(result['image_base64']), PNG)
        call = [item for item in ctx.calls if item[0] == 'rpc' and item[1] == 'ai_generation_page_reference_path'][0]
        self.assertEqual(call[2]['p_page'], page_id)
        self.assertEqual(call[2]['p_lease_token'], lease_token)
        self.assertTrue(call[3])
    def test_retry_accepts_one_page(self):
        router = FakeRouter()
        generation.configure(router)
        ctx = FakeCtx()
        page_id = str(uuid.uuid4())
        ctx.body = {'job_id': str(uuid.uuid4()), 'page_ids': [page_id]}
        captured = {}

        def retry(params):
            captured.update(params)
            return job_view(status='queued')

        ctx.rpc_results['ai_generation_retry_pages'] = retry
        status, result = router.routes[('POST', '/api/ai/generation/retry')](ctx)
        self.assertEqual(status, 202)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(captured['p_page_ids'], [page_id])


class MockExeServer:
    def __init__(self, preflight_responses=None):
        self.lock = threading.RLock()
        self.calls = []
        self.request_headers = []
        self.projects = {}
        self.preflight_responses = dict(preflight_responses or {})
        self.active = 0
        self.max_active = 0
        self.delay = 0.18
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _read_json(self):
                size = int(self.headers.get('Content-Length') or '0')
                return json.loads(self.rfile.read(size).decode('utf-8') or '{}') if size else {}

            def _json(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _raw(self, status, raw):
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urlsplit(self.path).path
                with owner.lock:
                    owner.calls.append(('GET', path))
                    owner.request_headers.append({key.lower(): value for key, value in self.headers.items()})
                    if path in owner.preflight_responses:
                        status, payload = owner.preflight_responses[path]
                        return self._raw(status, payload) if isinstance(payload, bytes) else self._json(status, payload)
                    if path == '/health':
                        return self._json(200, {'status': 'ok'})
                    if path == '/api/image2ppt/config':
                        return self._json(200, {'data': {'worker_exists': True, 'max_files': 40}})
                    if path == '/api/image2ppt/settings':
                        return self._json(200, {'data': {'vision_model': 'gpt-6.1-sol', 'masked': True}})
                    if path.startswith('/api/projects/') and '/tasks/' in path:
                        project_id, task_id = path.split('/')[3], path.rsplit('/', 1)[-1]
                        return self._json(200, {'data': {'task_id': task_id, 'status': 'COMPLETED', 'progress': {'total': 1, 'completed': 1, 'failed': 0}}})
                    if path.startswith('/api/projects/'):
                        project_id = path.split('/')[3]
                        project = owner.projects.get(project_id)
                        if not project:
                            return self._json(404, {'error': {'message': 'not found'}})
                        pages = []
                        for page in project['pages'].values():
                            pages.append({
                                'page_id': page['page_id'],
                                'order_index': page['order_index'],
                                'generated_image_url': f"/files/{project_id}/pages/{page['page_id']}.png",
                                'status': 'COMPLETED',
                                'image_error': None,
                            })
                        return self._json(200, {'data': {'project_id': project_id, 'pages': pages}})
                    if path.startswith('/files/'):
                        raw = PNG
                        self.send_response(200)
                        self.send_header('Content-Type', 'image/png')
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                return self._json(404, {'error': {'message': 'not found'}})

            def do_POST(self):
                path = urlsplit(self.path).path
                body = self._read_json()
                with owner.lock:
                    owner.calls.append(('POST', path, body))
                    owner.request_headers.append({key.lower(): value for key, value in self.headers.items()})
                    if path == '/api/projects':
                        project_id = str(uuid.uuid4())
                        owner.projects[project_id] = {'pages': {}, 'style': body.get('template_style')}
                        return self._json(201, {'data': {'project_id': project_id}})
                    if path.startswith('/api/projects/') and path.endswith('/pages'):
                        project_id = path.split('/')[3]
                        page_id = str(uuid.uuid4())
                        owner.projects[project_id]['pages'][page_id] = {
                            'page_id': page_id,
                            'order_index': int(body.get('order_index') or 0),
                        }
                        return self._json(201, {'data': {'page_id': page_id, 'order_index': body.get('order_index')}})
                    if path.startswith('/api/projects/') and path.endswith('/generate/image'):
                        project_id = path.split('/')[3]
                        page_id = path.split('/')[5]
                        owner.active += 1
                        owner.max_active = max(owner.max_active, owner.active)
                try:
                    time.sleep(owner.delay)
                finally:
                    with owner.lock:
                        owner.active -= 1
                        if project_id not in owner.projects or page_id not in owner.projects[project_id]['pages']:
                            return self._json(404, {'error': {'message': 'page missing'}})
                return self._json(202, {'data': {'task_id': str(uuid.uuid4()), 'page_id': page_id, 'status': 'PENDING'}})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class MockGatewayServer:
    def __init__(self, workspace_id):
        self.lock = threading.RLock()
        self.workspace_id = workspace_id
        self.request_headers = []
        self.completed = []
        self.claimed = set()
        self.job = {
            'id': '22222222-2222-4222-8222-222222222222',
            'planId': 'plan-1',
            'deckId': 'generated-test',
            'status': 'queued',
            'title': '测试生成',
            'style': '白底深青',
            'outline': {'title': '测试生成'},
            'references': [],
            'pages': [],
        }
        for index in range(1, 5):
            page_id = str(uuid.uuid4())
            self.job['pages'].append({
                'id': page_id,
                'position': index,
                'planPageId': f'p{index}',
                'title': f'第 {index} 页',
                'outline': {
                    'title': f'第 {index} 页',
                    'narrativeRole': 'data',
                    'points': [f'第 {index} 页内容'],
                },
                'description': {
                    'text': f'页面说明 sentinel {index}',
                    'narrativeRole': 'data',
                },
                'prompt': f'legacy prompt {index}',
                'references': [],
                'status': 'queued',
                'storagePath': f'{workspace_id}/generated-test/{page_id}.png',
                'contentType': 'image/png',
            })
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _read_json(self):
                size = int(self.headers.get('Content-Length') or '0')
                return json.loads(self.rfile.read(size).decode('utf-8') or '{}') if size else {}

            def _json(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlsplit(self.path)
                with owner.lock:
                    owner.request_headers.append({key.lower(): value for key, value in self.headers.items()})
                    if parsed.path == '/api/ai/generation':
                        job_id = parse_qs(parsed.query).get('job_id', [''])[0]
                        if job_id != owner.job['id']:
                            return self._json(404, {'error': {'message': 'job not found'}})
                        return self._json(200, json.loads(json.dumps(owner.job)))
                return self._json(404, {'error': {'message': 'not found'}})

            def do_POST(self):
                path = urlsplit(self.path).path
                body = self._read_json()
                with owner.lock:
                    owner.request_headers.append({key.lower(): value for key, value in self.headers.items()})
                    if path == '/api/ai/generation/worker/claim':
                        available = [page for page in owner.job['pages'] if page['id'] not in owner.claimed and page['status'] == 'queued']
                        picked = available[:int(body.get('limit') or 4)]
                        for page in picked:
                            owner.claimed.add(page['id'])
                            page['status'] = 'running'
                            page['leaseToken'] = str(uuid.uuid4())
                        return self._json(200, {'pages': [dict(page, jobId=owner.job['id'], pageId=page['id'], pageTitle=page['title'], pageOutline=page['outline']) for page in picked]})
                    if path == '/api/ai/generation/worker/complete':
                        page_id = body['page_id']
                        page = next(page for page in owner.job['pages'] if page['id'] == page_id)
                        raw = base64.b64decode(body['image_base64'])
                        if raw != PNG:
                            return self._json(400, {'error': {'message': 'wrong image'}})
                        page['status'] = 'succeeded'
                        page['contentSha256'] = body['content_sha256']
                        owner.completed.append(page_id)
                        owner.job['status'] = 'completed' if all(row['status'] == 'succeeded' for row in owner.job['pages']) else 'running'
                        return self._json(200, json.loads(json.dumps(owner.job)))
                    if path == '/api/ai/generation/worker/heartbeat':
                        return self._json(200, {'ok': True})
                    if path == '/api/ai/generation/worker/fail':
                        page_id = body['page_id']
                        page = next(page for page in owner.job['pages'] if page['id'] == page_id)
                        page['status'] = 'failed'
                        page['error'] = body.get('error')
                        return self._json(200, json.loads(json.dumps(owner.job)))
                return self._json(404, {'error': {'message': 'not found'}})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class WorkerTestCase(unittest.TestCase):
    def _preflight_config(self, exe, directory):
        return WorkerConfig(
            gateway_base='http://127.0.0.1:1', gateway_token='x', worker_token='worker-secret',
            workspace_id=str(uuid.uuid4()), exe_base=exe.base, exe_token='local-token',
            worker_id='test-worker', workers=4, poll_seconds=0.01, state_dir=Path(directory),
        )

    def _run_preflight_cli(self, preflight_responses=None):
        exe = MockExeServer(preflight_responses=preflight_responses)
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = self._preflight_config(exe, directory)
                stdout, stderr = io.StringIO(), io.StringIO()
                with (patch.object(WorkerConfig, 'from_env', return_value=config),
                      contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
                    exit_code = main(['--preflight'])
                return exit_code, json.loads(stdout.getvalue())
        finally:
            exe.close()

    def _assert_preflight_rejected(self, preflight_responses, stage, message):
        exe = MockExeServer(preflight_responses=preflight_responses)
        try:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(PreflightError, message) as raised:
                    preflight(self._preflight_config(exe, directory))
                self.assertEqual(raised.exception.stage, stage)
                expected_gets = [
                    ('GET', '/health'),
                    ('GET', '/api/image2ppt/config'),
                    ('GET', '/api/image2ppt/settings'),
                ]
                self.assertEqual(exe.calls, expected_gets[:{'health': 1, 'config': 2, 'settings': 3}[stage]])
        finally:
            exe.close()

    def _run_once_cli(self, ready_count, processing_error=None):
        with tempfile.TemporaryDirectory() as directory:
            config = WorkerConfig(
                gateway_base='http://127.0.0.1:1', gateway_token='token', worker_token='worker-secret',
                workspace_id=str(uuid.uuid4()), exe_base='http://127.0.0.1:1', exe_token='local-token',
                worker_id='test-worker', workers=4, poll_seconds=0.01, state_dir=Path(directory),
            )
            gateway = Mock()
            pages = [{'pageId': str(index)} for index in range(ready_count)]
            gateway.claim.side_effect = lambda _id, limit, _lease: pages[:limit] if gateway.claim.call_count == 1 else self.fail('single-cycle worker polled again')
            worker = WindowsWorker(config, gateway=gateway, exe=Mock())
            stdout, stderr = io.StringIO(), io.StringIO()
            with (patch.object(WorkerConfig, 'from_env', return_value=config),
                  patch('cloud.windows_image_worker.WindowsWorker', return_value=worker),
                  patch.object(worker, 'process_page', return_value=True, side_effect=processing_error) as generate,
                  contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
                exit_code = main(['--once', '--max-pages', '1'])
            self.assertEqual(gateway.claim.call_count, 1)
            self.assertEqual(gateway.claim.call_args.args[1], 1)
            self.assertLessEqual(generate.call_count, 1)
            return exit_code, generate.call_count

    def test_run_once_claims_only_one_page_with_four_worker_slots(self):
        self.assertEqual(self._run_once_cli(4), (0, 1))

    def test_run_once_exits_on_empty_queue(self):
        self.assertEqual(self._run_once_cli(0), (3, 0))

    def test_run_once_surfaces_failed_generation(self):
        self.assertEqual(self._run_once_cli(1, RuntimeError('model generation failed')), (2, 1))

    def test_preflight_never_generates(self):
        exe = MockExeServer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                result = preflight(self._preflight_config(exe, directory))
                self.assertEqual(result['health']['status'], 'ok')
                self.assertTrue(result['httpProtocol']['verified'])
                self.assertEqual(result['httpProtocol']['checks'], [
                    '/health', '/api/image2ppt/config', '/api/image2ppt/settings',
                ])
                self.assertEqual(result['httpProtocol']['capabilities']['workerExists'], True)
                self.assertEqual(result['httpProtocol']['capabilities']['maxFiles'], 40)
                self.assertEqual(result['httpProtocol']['capabilities']['visionModel'], 'gpt-6.1-sol')
                self.assertFalse(result['runtimeImage']['verified'])
                self.assertIsNone(result['runtimeImage']['sha256'])
                self.assertEqual(result['paidRequests'], 0)
                self.assertFalse(any('/generate/' in call[1] for call in exe.calls))
                self.assertTrue(exe.request_headers)
                self.assertTrue(all('x-guanye-worker-token' not in headers for headers in exe.request_headers))
        finally:
            exe.close()

    def test_preflight_cli_separates_protocol_from_runtime_image(self):
        exit_code, result = self._run_preflight_cli()
        self.assertEqual(exit_code, 0)
        self.assertTrue(result['ok'])
        self.assertTrue(result['httpProtocol']['verified'])
        self.assertFalse(result['runtimeImage']['verified'])
        self.assertNotIn('expectedExeSha256', result)
        self.assertNotIn('protocolTarget', result)

    def test_preflight_accepts_null_max_files_as_unlimited(self):
        exit_code, result = self._run_preflight_cli({
            '/api/image2ppt/config': (200, {'data': {'worker_exists': True, 'max_files': None}}),
        })
        self.assertEqual(exit_code, 0, result)
        self.assertTrue(result['httpProtocol']['verified'])
        self.assertIsNone(result['httpProtocol']['capabilities']['maxFiles'])
        self.assertIsNone(result['config']['maxFiles'])

    def test_preflight_rejects_missing_or_invalid_max_files(self):
        configs = (
            {'worker_exists': True},
            *({'worker_exists': True, 'max_files': value} for value in (True, 0, -1, 1.5, '40', [])),
        )
        for config in configs:
            with self.subTest(config=config):
                self._assert_preflight_rejected(
                    {'/api/image2ppt/config': (200, {'data': config})},
                    'config', 'max_files',
                )

    def test_preflight_rejects_malformed_200_json(self):
        self._assert_preflight_rejected(
            {'/health': (200, b'{')}, 'health', '健康检查响应不是有效 JSON',
        )

    def test_preflight_rejects_non_healthy_response(self):
        self._assert_preflight_rejected(
            {
                '/health': (200, {'status': 'degraded'}),
                '/api/image2ppt/config': (200, b'{'),
            },
            'health', '健康检查状态不是 ok',
        )

    def test_preflight_rejects_missing_worker(self):
        self._assert_preflight_rejected(
            {'/api/image2ppt/config': (200, {'data': {'worker_exists': False, 'max_files': 40}})},
            'config', '生图 worker 缺失',
        )

    def test_preflight_rejects_incomplete_settings(self):
        self._assert_preflight_rejected(
            {'/api/image2ppt/settings': (200, {'data': {'masked': True}})},
            'settings', '设置检查响应缺少 vision_model',
        )

    def test_preflight_cli_reports_protocol_failure_stage(self):
        exit_code, result = self._run_preflight_cli({
            '/api/image2ppt/config': (200, {'data': {'worker_exists': False, 'max_files': 40}}),
        })
        self.assertEqual(exit_code, 2)
        self.assertFalse(result['ok'])
        self.assertEqual(result['httpProtocol']['stage'], 'config')
        self.assertEqual(result['httpProtocol']['error'], '生图 worker 缺失')
        self.assertFalse(result['runtimeImage']['verified'])

    def test_mock_full_chain_uses_shared_four_way_concurrency(self):
        exe = MockExeServer()
        workspace_id = str(uuid.uuid4())
        gateway = MockGatewayServer(workspace_id)
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = WorkerConfig(
                    gateway_base=gateway.base,
                    gateway_token='gateway-token',
                    worker_token='worker-secret',
                    workspace_id=workspace_id,
                    exe_base=exe.base,
                    exe_token='local-token',
                    worker_id='mock-worker',
                    workers=4,
                    lease_seconds=60,
                    poll_seconds=0.01,
                    task_timeout=10.0,
                    upload_retries=2,
                    state_dir=Path(directory),
                )
                worker = WindowsWorker(
                    config,
                    gateway=GatewayClient(gateway.base, 'gateway-token', workspace_id, worker_token='worker-secret'),
                )
                processed = worker.run(max_pages=4)
                self.assertEqual(processed, 4)
                self.assertEqual(len(gateway.completed), 4)
                self.assertEqual({row['status'] for row in gateway.job['pages']}, {'succeeded'})
                self.assertGreaterEqual(exe.max_active, 2)
                self.assertLessEqual(exe.max_active, 4)
                generate_calls = [call for call in exe.calls if call[0] == 'POST' and call[1].endswith('/generate/image')]
                self.assertEqual(len(generate_calls), 4)
                self.assertTrue(gateway.request_headers)
                self.assertTrue(any(
                    headers.get('x-guanye-worker-token') == 'worker-secret'
                    and headers.get('authorization') == 'Bearer gateway-token'
                    and headers.get('x-access-code') == 'gateway-token'
                    for headers in gateway.request_headers
                ))
                self.assertTrue(all(headers.get('authorization') != 'Bearer local-token' for headers in gateway.request_headers))
                self.assertTrue(exe.request_headers)
                self.assertTrue(any(headers.get('authorization') == 'Bearer local-token' for headers in exe.request_headers))
                self.assertTrue(all('x-guanye-worker-token' not in headers for headers in exe.request_headers))
                page_create_calls = [
                    call for call in exe.calls
                    if call[0] == 'POST' and call[1].endswith('/pages')
                ]
                self.assertEqual(len(page_create_calls), 4)
                created_bodies = [call[2] for call in page_create_calls]
                self.assertTrue(all(body['description_content']['narrativeRole'] == 'data' for body in created_bodies))
                self.assertTrue(all(body['outline_content']['narrativeRole'] == 'data' for body in created_bodies))
                self.assertEqual(
                    {body['description_content']['text'] for body in created_bodies},
                    {f'页面说明 sentinel {index}' for index in range(1, 5)},
                )
                self.assertTrue(all('legacy prompt' not in json.dumps(body, ensure_ascii=False) for body in created_bodies))
        finally:
            gateway.close()
            exe.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
