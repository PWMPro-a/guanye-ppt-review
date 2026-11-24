"""Edge journey for judge-only local planning and generation in the professional app."""

import json
import os
from pathlib import Path
import time
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None

from test_professional_review_ui_browser import EDGE, PREVIEW, fixture_uuid, generated_workspace, workspace_fixture


PUBLIC = Path(__file__).resolve().parent / 'public'
ORIGIN = 'http://127.0.0.1:8765'
PLAN_ID = fixture_uuid(201)
JOB_ID = fixture_uuid(202)
PAGE_ONE = fixture_uuid(203)
PAGE_TWO = fixture_uuid(204)


def plan_fixture():
    return {
        'planId': PLAN_ID,
        'revision': 1,
        'createdAt': '2026-11-22T05:00:00Z',
        'plan': {
            'title': 'S13 内容样板', 'summary': '专业人员可编辑的策划内容。', 'mode': 'content_draft',
            'pageCount': 2, 'generationBrief': '克制、清晰、深绿与米白。', 'warnings': [],
            'sourceManifest': [], 'references': [], 'generationStyleImages': [],
            'style': {
                'name': '克制深绿样板', 'summary': '深绿标题、米白背景与细线分隔。',
                'layoutLanguage': '左对齐网格，大留白，底部来源。', 'palette': ['#173d35', '#f7f3e8'],
            },
            'pages': [
                {
                    'index': 1, 'title': '封面样板', 'narrativeRole': 'cover',
                    'copy': [{'kind': 'title', 'text': 'S13 内容样板'}],
                    'layout': '左侧标题，右侧留白。', 'visualBrief': '深绿标题与米白背景。',
                    'contentSources': [], 'styleReferenceDeckIds': [],
                },
                {
                    'index': 2, 'title': '方法页面', 'narrativeRole': 'argument',
                    'copy': [{'kind': 'body', 'text': '按能力分轨，按角色隔离。'}],
                    'layout': '两栏对齐，底部标注来源。', 'visualBrief': '细线分隔，不使用渐变。',
                    'contentSources': [], 'styleReferenceDeckIds': [],
                },
            ],
        },
    }


def queued_job():
    return {'id': JOB_ID, 'planId': PLAN_ID, 'title': 'S13 生成样板', 'status': 'queued', 'pages': []}


def completed_job(retried=False):
    second = {
        'id': PAGE_TWO, 'position': 2, 'title': '方法页面', 'status': 'succeeded' if retried else 'failed',
        'error': '' if retried else '模型超时',
    }
    if retried:
        second.update(previewUrl='/signed/generated-p2.png', downloadUrl='/signed/generated-p2.png')
    return {
        'id': JOB_ID, 'planId': PLAN_ID, 'title': 'S13 生成样板', 'status': 'completed',
        'deckId': 'generated-deck',
        'pages': [
            {
                'id': PAGE_ONE, 'position': 1, 'title': '封面样板', 'status': 'succeeded',
                'previewUrl': '/signed/generated-p1.png', 'downloadUrl': '/signed/generated-p1.png',
            },
            second,
        ],
    }


def creation_workspace():
    result = generated_workspace()
    result['deck']['id'] = 'generated-deck'
    result['deck']['data']['id'] = 'generated-deck'
    result['deck']['data']['title'] = 'S13 生成作品'
    result['queues']['generated'] = [{'id': 'generated-deck', 'title': 'S13 生成作品'}]
    return result


def open_creation_app(browser, role, *, cover=False, hosted=False):
    origin = "https://training.test:8443" if hosted else ORIGIN
    prefix = "/training" if hosted else ""
    page = browser.new_page(viewport={'width': 1360, 'height': 1100})
    errors, ai_requests, rpc_calls, signed = [], [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    retried = False
    upload = workspace_fixture()
    generated = creation_workspace()
    state = {**upload['workspaceSettings'], 'revision': 0, 'configRevision': 0, 'reviews': {}}
    config = {
        'url': 'https://review.test', 'anonKey': 'fixture-key', 'workspaceId': fixture_uuid(1000),
        'singleJudge': True, 'localImport': True, 'localReview': True, 'localCreation': True,
    }
    if hosted:
        config.update(aiGatewayUrl=origin, coverTrainingSite=True, localCreation=False, localImport=False)
    elif cover:
        config['localCoverTraining'] = True
    supabase = """window.supabase={createClient:()=>({
      auth:{
        getSession:async()=>({data:{session:{access_token:'fixture-token',user:{id:'fixture',email:'fixture@example.test'}}}}),
        refreshSession:async()=>({data:{session:{access_token:'refreshed-token',user:{id:'fixture'}}}}),
        signOut:async()=>({data:null}),updateUser:async()=>({data:{user:{id:'fixture'}}})
      },
      rpc:async(name,args)=>await (await fetch('/rpc/'+name,{method:'POST',body:JSON.stringify(args)})).json(),
      storage:{from:()=>({createSignedUrls:async paths=>({data:paths.map(path=>({path,signedUrl:'/signed/'+path}))})})}
    })};"""

    def static_asset(path):
        relative = unquote(path).lstrip('/')
        asset = (PUBLIC / relative).resolve()
        if asset != PUBLIC and PUBLIC not in asset.parents:
            return None
        return asset if asset.is_file() else None

    def respond(route):
        nonlocal retried
        url = urlsplit(route.request.url)
        if f'{url.scheme}://{url.netloc}' != origin:
            route.abort()
            return
        path = url.path.removeprefix(prefix) if hosted else url.path
        if path == '/':
            route.fulfill(path=str(PUBLIC / 'index.html'))
            return
        if path == '/cloud-config.js':
            route.fulfill(content_type='text/javascript; charset=utf-8', body='window.CLOUD_CONFIG=' + json.dumps(config))
            return
        if path == '/vendor/supabase.js':
            route.fulfill(content_type='text/javascript; charset=utf-8', body=supabase)
            return
        if url.path.startswith('/signed/'):
            signed.append(url.path)
            route.fulfill(content_type='image/png', body=PREVIEW)
            return
        if url.path.startswith('/rpc/'):
            name = url.path.rsplit('/', 1)[-1]
            args = route.request.post_data_json
            rpc_calls.append((name, args))
            if name == 'single_judge_access':
                result = {'mode': 'single-judge', 'role': role}
            elif name == 'get_review_library' and role in ('judge', 'admin'):
                result = {'decks': [{'data': {**upload['deck']['data'], 'id': upload['deck']['id']}}]}
            elif name == 'get_review_state' and role in ('judge', 'admin'):
                result = state
            elif name == 'review_score_summary' and role in ('judge', 'admin'):
                result = {}
            elif name == 'single_judge_review_workspace' and role == 'judge':
                result = generated if args.get('p_kind') == 'generated' else upload
            elif name == 'single_judge_comparison' and role in ('judge', 'admin'):
                result = {'byKind': {'uploaded': {'holdoutUnits': 1}, 'generated': {'holdoutUnits': 1}}}
            else:
                route.fulfill(status=403, json={'error': 'unexpected rpc ' + name})
                return
            route.fulfill(json={'data': result})
            return
        if path.startswith('/api/ai/'):
            method = route.request.method
            body = route.request.post_data_json if route.request.post_data else None
            ai_requests.append({'method': method, 'path': path, 'query': parse_qs(url.query), 'body': body,
                                'authorization': route.request.headers.get('authorization')})
            if path == '/api/ai/planning/history':
                route.fulfill(json={'items': [], 'nextCursor': None})
            elif path == '/api/ai/planning/analyze':
                route.fulfill(json=plan_fixture())
            elif path == '/api/ai/planning':
                route.fulfill(json=plan_fixture())
            elif path == '/api/ai/planning/save':
                payload = plan_fixture()
                payload.update(planId=PLAN_ID, revision=2, draft=body['draft'])
                route.fulfill(json=payload)
            elif path == '/api/ai/planning/confirm':
                route.fulfill(json={
                    'planId': PLAN_ID, 'status': 'confirmed', 'revision': 2, 'version': 'v1',
                    'versionId': fixture_uuid(205), 'contentHash': 's13-content-hash',
                })
            elif path == '/api/ai/generation' and method == 'POST':
                route.fulfill(json=queued_job())
            elif path == '/api/ai/generation' and parse_qs(url.query).get('action') == ['recent']:
                route.fulfill(json={'jobs': [{'id': JOB_ID, 'planId': PLAN_ID, 'title': 'S13 生成样板', 'createdAt': '2026-11-22T05:01:00Z'}]})
            elif path == '/api/ai/generation' and (parse_qs(url.query).get('job_id') == [JOB_ID] or parse_qs(url.query).get('plan_id') == [PLAN_ID]):
                route.fulfill(json=completed_job(retried))
            elif path == '/api/ai/generation/retry':
                retried = True
                route.fulfill(json=completed_job(True))
            elif path == '/api/ai/generation/artifact':
                route.fulfill(json={'data': {'dataBase64': 'iVBORw0KGgo=', 'contentType': 'image/png'}})
            else:
                route.fulfill(status=404, json={'error': 'unhandled local route ' + path})
            return
        asset = static_asset(path)
        if asset is not None:
            route.fulfill(path=str(asset))
        else:
            route.fulfill(status=404, body='')

    page.route('**/*', respond)
    page.goto(origin + prefix + '/')
    return page, ai_requests, rpc_calls, signed, errors


def wait_until(page, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError('condition was not met before timeout')


@unittest.skipUnless(sync_playwright and EDGE.is_file(), 'requires Playwright and Microsoft Edge')
class ProfessionalCreationBrowserTest(unittest.TestCase):
    def test_judge_can_open_plan_confirm_start_retry_and_jump_to_existing_review(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, ai_requests, rpc_calls, signed, errors = open_creation_app(browser, 'judge')
                expect(page.locator('#app')).to_have_attribute('data-single-judge-role', 'judge')
                expect(page.locator('[data-professional-creation="true"]').first).to_be_visible()
                expect(page.get_by_role('button', name='正式评分工作台', exact=True)).to_be_visible()

                page.locator('[data-professional-creation="true"]').first.click()
                expect(page.locator('#modal')).to_be_visible()
                expect(page.get_by_role('heading', name='专业创作工作台')).to_be_visible()
                expect(page.locator('[data-ai-tab="reviews"]')).to_be_visible()
                expect(page.locator('[data-ai-tab="planning"]')).to_be_visible()
                expect(page.locator('[data-ai-tab="generation"]')).to_be_visible()
                expect(page.get_by_role('heading', name='已上传作品队列')).to_be_visible()
                expect(page.get_by_role('heading', name='生成作品队列')).to_be_visible()

                page.locator('[data-ai-tab="planning"]').click()
                planning = page.locator('[data-ai-root="planning"]')
                mode = planning.locator('select[name="mode"]')
                expect(mode.locator('option[value="content_draft"]')).to_have_count(1)
                expect(mode.locator('option[value="template_sample"]')).to_have_count(1)
                planning.locator('input[name="pageCount"]').fill('2')
                planning.locator('textarea[name="outline"]').fill('S13：封面与方法两页样板')
                planning.locator('button#gp-analyze').click()
                expect(planning.locator('[data-gp-plan-field="title"]')).to_have_value('S13 内容样板')
                expect(planning.locator('#gp-status')).to_contain_text('策划草稿已生成')

                page.once('dialog', lambda dialog: dialog.accept())
                planning.locator('[data-gp-action="confirm"]').click()
                expect(planning.locator('#gp-status')).to_contain_text('策划已确认并锁定')
                planning.locator('[data-gp-action="generate"]').click()
                expect(page.locator('[data-ai-tab="generation"]')).to_have_attribute('aria-selected', 'true')

                wait_until(page, lambda: any(row['method'] == 'GET' and row['path'] == '/api/ai/generation' and row['query'].get('job_id') == [JOB_ID] for row in ai_requests), 8)
                generation = page.locator('[data-ai-root="generation"]')
                expect(generation.locator('.gg-card')).to_have_count(2)
                expect(generation.get_by_text('已完成', exact=True).first).to_be_visible()
                expect(generation.get_by_text('生成失败', exact=True).first).to_be_visible()
                expect(generation.get_by_role('button', name='仅重试本页', exact=True)).to_be_visible()
                wait_until(page, lambda: bool(signed), 5)

                refresh = page.locator('[data-ai-action="refresh-generation"]')
                refresh.click()
                refresh.click()
                wait_until(page, lambda: sum(row['path'] == '/api/ai/generation' and row['query'].get('action') == ['recent'] for row in ai_requests) >= 3, 5)

                generation.get_by_role('button', name='仅重试本页', exact=True).click()
                wait_until(page, lambda: any(row['path'] == '/api/ai/generation/retry' for row in ai_requests), 5)
                retry = next(row for row in ai_requests if row['path'] == '/api/ai/generation/retry')
                self.assertEqual(retry['method'], 'POST')
                self.assertEqual(retry['body'], {'job_id': JOB_ID, 'page_ids': [PAGE_TWO]})
                expect(generation.get_by_text('生成失败', exact=True)).to_have_count(0)
                expect(generation.get_by_role('button', name='下载图片', exact=True)).to_have_count(2)

                generation.get_by_role('button', name='查看生成评审', exact=True).click()
                expect(page.locator('[data-professional-review-only="true"]')).to_have_count(2)
                expect(page.get_by_role('heading', name='专业人工评分 · 生成作品')).to_be_visible()
                generated_calls = [args for name, args in rpc_calls if name == 'single_judge_review_workspace' and args.get('p_kind') == 'generated']
                self.assertTrue(generated_calls)
                self.assertEqual(generated_calls[-1]['p_deck_id'], 'generated-deck')
                generated_review = page.locator('[data-ai-root="reviews-generated"]')
                self.assertEqual(generated_review.locator('[data-action="formal"], [data-action="import"], [data-action="settings"]').count(), 0)

                self.assertTrue(any(row['method'] == 'POST' and row['path'] == '/api/ai/planning/analyze' for row in ai_requests))
                self.assertTrue(any(row['method'] == 'POST' and row['path'] == '/api/ai/planning/confirm' for row in ai_requests))
                self.assertTrue(any(row['method'] == 'POST' and row['path'] == '/api/ai/generation' for row in ai_requests))
                self.assertTrue(all(row['authorization'] == 'Bearer fixture-token' for row in ai_requests))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_hosted_cover_reopens_job_from_isolated_generation_history(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, requests, _, _, errors = open_creation_app(browser, 'judge', hosted=True)
                page.route('**/training/api/ai/generation?job_id=*', lambda route: route.fulfill(json=completed_job(False)))
                page.route('**/training/api/ai/generation/cover?job_id=*', lambda route: route.fulfill(json={'enabled': False}))
                page.route('**/training/api/ai/generation?action=recent', lambda route: route.fulfill(json={
                    'jobs': [{'id': JOB_ID, 'title': '封面训练首稿'}],
                }))
                expect(page.locator('#app')).to_have_attribute('data-single-judge-role', 'judge')
                page.locator('[data-professional-creation="true"]').first.click()
                expect(page.get_by_role('tab', name='训练任务')).to_have_attribute('aria-selected', 'true')
                expect(page.get_by_label('选择训练任务')).to_have_value(JOB_ID, timeout=4000)
                expect(page.locator('[data-ai-root="generation"] .gg-card')).to_have_count(2)
                self.assertFalse(any(row['path'] == '/api/ai/generation' for row in requests))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_hosted_cover_save_returns_to_active_task_without_second_generation(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, _, _, _, errors = open_creation_app(browser, 'judge', hosted=True)
                workspace = creation_workspace()
                workspace['deck']['data']['images'] = [{**workspace['deck']['data']['images'][0], 'id': PAGE_ONE}]
                workspace.update(submissions=[], decisions=[], ai=[])
                candidate = {'pageId': PAGE_ONE, 'jobId': JOB_ID, 'deckId': 'generated-deck', 'ordinal': 0,
                             'status': 'succeeded', 'feedback': None, 'image': {'path': 'x/root.png'}}
                state = {'enabled': True, 'rootPageId': PAGE_ONE, 'selectedPageId': None, 'candidates': [candidate]}
                writes = []

                def save(route):
                    writes.append(route.request.post_data_json['payload'])
                    candidate['feedback'] = {'human': writes[-1]['units'][0]['scores'], 'reviewStatus': 'queued'}
                    route.fulfill(json={'data': {'ok': True}})

                page.route('**/rpc/single_judge_submit_bundle', save)
                page.route('**/rpc/single_judge_review_workspace', lambda route: route.fulfill(json={'data': workspace}))
                page.route('**/training/api/ai/generation?action=recent', lambda route: route.fulfill(json={
                    'jobs': [{'id': JOB_ID, 'title': '待评封面'}],
                }))
                page.route('**/training/api/ai/generation?job_id=*', lambda route: route.fulfill(json=completed_job()))
                page.route('**/training/api/ai/generation/cover?job_id=*', lambda route: route.fulfill(json=state))
                page.locator('[data-professional-creation="true"]').first.click()
                cover = page.get_by_role('region', name='封面训练')
                expect(cover.get_by_role('status')).to_contain_text('提交人工评分')
                cover.get_by_role('button', name='独立打分').click()
                review = page.locator('[data-ai-root="reviews-generated"]')
                expect(page.get_by_role('button', name='返回训练任务')).to_be_visible()
                fields = review.locator(f'[data-score-unit="{PAGE_ONE}"]')
                expect(fields).to_have_count(3)
                expect(review.locator('[data-score-unit=""]')).to_have_count(0)
                for i, value in enumerate(('4.25', '4.5', '5')):
                    fields.nth(i).fill(value)
                review.locator('[data-action="submit"]').click()
                expect(cover.get_by_role('status')).to_contain_text('人工评分已保存')
                expect(cover.get_by_role('status')).to_contain_text('Sol 评审已排队')
                expect(page.get_by_role('tab', name='训练任务')).to_have_attribute('aria-selected', 'true')
                self.assertEqual(len(writes), 1)
                self.assertEqual([(unit['scope'], unit['unitId']) for unit in writes[0]['units']],
                                 [('deck', ''), ('page', PAGE_ONE)])
                self.assertEqual(writes[0]['units'][0]['scores'], writes[0]['units'][1]['scores'])
                self.assertEqual(errors, [])
            finally:
                browser.close()


    def test_hosted_cover_reports_pending_database_and_uses_isolated_prefix(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, requests, _, _, errors = open_creation_app(browser, 'judge', hosted=True)
                seen = []
                def pending(route):
                    seen.append(urlsplit(route.request.url).path)
                    route.fulfill(status=404, json={'error': '训练服务尚未完成数据库配置，请联系管理员'})
                page.route('**/training/api/ai/training/cover-inventory**', pending)
                page.route('**/training/api/ai/generation?action=recent', lambda route: route.fulfill(json={'jobs': []}))
                expect(page.locator('#app')).to_have_attribute('data-single-judge-role', 'judge')
                page.locator('[data-professional-creation="true"]').first.click()
                page.get_by_role('tab', name='新建封面').click()
                planning = page.locator('[data-ai-root="planning"]')
                expect(planning.locator('#gp-form')).to_be_hidden()
                planning.get_by_role('button', name='读取素材盘点').click()
                expect(planning.get_by_role('status').filter(has_text='尚未完成数据库配置')).to_be_visible()
                expect(planning.locator('#gp-form')).to_be_hidden()
                self.assertEqual(seen, ['/training/api/ai/training/cover-inventory'])
                self.assertEqual([row['path'] for row in requests], ['/api/ai/training/cover-references'])
                self.assertFalse(any(row['path'] == '/api/ai/planning/analyze' for row in requests))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_hosted_cover_loads_library_references_with_get(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, ai_requests, _, _, errors = open_creation_app(browser, 'judge', hosted=True)
                seen = []

                def references(route):
                    seen.append((route.request.method, urlsplit(route.request.url).path))
                    route.fulfill(json={'references': [{
                        'deckId': 'deck-one', 'title': '授权封面', 'score': 4.6,
                        'imagePaths': ['workspace/deck-one/preview/1.png'], 'completeReviewCount': 1,
                    }]})

                response = plan_fixture()
                response['plan']['pages'] = response['plan']['pages'][:1]
                response['plan']['pageCount'] = 1
                response['plan']['references'] = [{
                    'deckId': 'deck-one', 'title': '授权封面', 'score': 4.6,
                    'imagePaths': ['workspace/deck-one/preview/1.png'], 'completeReviewCount': 1,
                }]
                response['coverStyleEvidence'] = {
                    'deckId': 'deck-one', 'title': '授权封面',
                    'path': 'workspace/deck-one/preview/1.png', 'sha256': 'a' * 64,
                    'facts': {'typography': '标题左对齐', 'composition': '左文右图',
                              'whitespace': '右侧留白', 'decoration': '细线', 'avoid': '不照抄原文',
                              'palette': [{'hex': '#123ABC', 'role': '背景', 'share': '约 70%'}]},
                }

                def analyze(route):
                    ai_requests.append({
                        'method': route.request.method, 'path': '/api/ai/planning/analyze',
                        'query': {}, 'body': route.request.post_data_json,
                        'authorization': route.request.headers.get('authorization'),
                    })
                    route.fulfill(json=response)

                def load_plan(route):
                    route.fulfill(json={
                        'planId': PLAN_ID, 'status': 'draft', 'revision': 1,
                        'draft': response['plan'], 'coverStyleEvidence': response['coverStyleEvidence'],
                    })

                page.route('**/training/api/ai/training/cover-references', references)
                page.route('**/training/api/ai/planning/analyze', analyze)
                page.route('**/training/api/ai/planning?plan_id=*', load_plan)
                expect(page.locator('#app')).to_have_attribute('data-single-judge-role', 'judge')
                page.locator('[data-professional-creation="true"]').first.click()
                page.get_by_role('tab', name='新建封面').click()
                planning = page.locator('[data-ai-root="planning"]')
                picker = planning.get_by_label('选择素材库视觉参考')
                expect(picker).to_be_visible()
                expect(picker.locator('option')).to_have_count(2)
                picker.select_option('deck-one')
                page.get_by_role('button', name='生成策划').click()
                wait_until(page, lambda: any(row['path'] == '/api/ai/planning/analyze' for row in ai_requests), 5)
                analyze = next(row for row in ai_requests if row['path'] == '/api/ai/planning/analyze')
                self.assertEqual(analyze['body']['reference_deck_id'], 'deck-one')
                expect(planning.get_by_text('本次实际学习的素材库原图')).to_be_visible()
                article = planning.locator('article').filter(has_text='S13 内容样板').first
                article.get_by_role('button', name='打开').click()
                expect(planning.get_by_role('status').filter(has_text='已打开草稿')).to_be_visible()
                expect(planning.get_by_text('本次实际学习的素材库原图')).to_be_visible()
                self.assertEqual(seen, [('GET', '/training/api/ai/training/cover-references')])
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_cover_pilot_local_transport_and_manual_revision(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, _, _, _, errors = open_creation_app(browser, 'judge', cover=True)
                extra_job = fixture_uuid(301)
                record = fixture_uuid(302)
                decision = fixture_uuid(303)
                proof = {'human': {'visual': 4, 'layout': 3}, 'recordId': record,
                         'aiScores': {'visual': 4, 'layout': 4}, 'decisionId': decision}
                root = {'pageId': PAGE_ONE, 'jobId': JOB_ID, 'deckId': 'generated-deck', 'ordinal': 0,
                        'status': 'succeeded', 'feedback': None, 'image': {'path': 'x/root.png'}}
                variant = {'pageId': PAGE_TWO, 'jobId': extra_job, 'deckId': 'generated-revision', 'ordinal': 1,
                           'status': 'succeeded', 'feedback': proof, 'image': {'path': 'x/revised.png'}}
                flow = {'rootPageId': PAGE_ONE, 'enabled': True, 'candidates': [root], 'selectedPageId': None}
                requests = []
                ref = {'deckId': 'original', 'title': '授权封面', 'imagePaths': ['x/cover.png']}

                def respond(route):
                    path = urlsplit(route.request.url).path
                    if path == '/api/ai/training/cover-inventory':
                        route.fulfill(json={'rows': [], 'nextAfter': None, 'rubricVersion': 'fixture'})
                    elif path == '/api/ai/planning/analyze':
                        plan = plan_fixture()
                        plan['plan']['pages'] = plan['plan']['pages'][:1]
                        plan['plan']['references'] = [ref]
                        plan['plan']['pageCount'] = 1
                        route.fulfill(json=plan)
                    elif path == '/api/ai/generation/cover/start':
                        requests.append((path, route.request.post_data_json))
                        route.fulfill(json=queued_job())
                    elif path == '/api/ai/generation/cover':
                        requests.append((path, None))
                        route.fulfill(json=flow)
                    elif path == '/api/ai/generation/cover/suggest':
                        requests.append((path, route.request.post_data_json))
                        route.fulfill(json={'recordId': record, 'instructions':
                                           {'keep': '保留风格', 'change': '增加留白', 'doNotChange': '文案不变'}})
                    elif path == '/api/ai/generation/cover/revise':
                        requests.append((path, route.request.post_data_json))
                        flow['candidates'] = [root, variant]
                        route.fulfill(json=flow)
                    elif path == '/api/ai/generation/cover/select':
                        requests.append((path, route.request.post_data_json))
                        flow['selectedPageId'] = route.request.post_data_json['page_id']
                        route.fulfill(json=flow)
                    else:
                        route.fallback()
                page.route('**/api/ai/**', respond)
                expect(page.locator('#app')).to_have_attribute('data-single-judge-role', 'judge')
                page.locator('[data-professional-creation="true"]').first.click()
                page.locator('[data-ai-tab="planning"]').click()
                planning = page.locator('[data-ai-root="planning"]')
                planning.get_by_text('封面训练 · 素材盘点与 Sol 对照').click()
                planning.get_by_role('button', name='读取素材盘点').click()
                expect(planning.get_by_text('已盘点 0 套', exact=False)).to_be_visible()
                planning.locator('input[name="pageCount"]').fill('1')
                planning.locator('textarea[name="outline"]').fill('培训封面')
                planning.locator('#gp-analyze').click()
                page.once('dialog', lambda dialog: dialog.accept())
                planning.locator('[data-gp-action="confirm"]').click()
                expect(planning.get_by_role('button', name='提交封面训练首稿')).to_be_visible()
                planning.locator('label').filter(has_text='选择一张已确认的高分原作封面参考').locator('select').select_option('0')
                planning.get_by_role('button', name='提交封面训练首稿').click()
                pilot = page.get_by_role('region', name='封面训练')
                expect(pilot.get_by_role('button', name='独立打分')).to_be_visible()
                expect(pilot.get_by_role('button', name='获取改稿建议')).to_have_count(0)
                self.assertEqual(requests[0][1]['reference_path'], 'x/cover.png')
                # The real SQL fixture submits independent human scores; this browser
                # fixture changes the server snapshot after that action.
                root['feedback'] = {'human': proof['human'], 'reviewStatus': 'queued'}
                pilot.get_by_role('button', name='刷新状态').click()
                expect(pilot.get_by_role('status')).to_contain_text('人工评分已保存')
                expect(pilot.get_by_text('Sol 原判', exact=False)).to_have_count(0)
                root['feedback'] = {'human': proof['human'], 'reviewStatus': 'retrying'}
                expect(pilot.get_by_role('status')).to_contain_text('等待系统重试', timeout=6000)
                root['feedback'] = proof
                expect(pilot.get_by_role('status')).to_contain_text('Sol 已完成独立评分', timeout=6000)
                pilot.get_by_role('button', name='获取改稿建议').click()
                expect(pilot.get_by_role('button', name='按建议改一版')).to_be_visible()
                self.assertFalse(any(path.endswith('/revise') for path, _ in requests))
                pilot.get_by_role('button', name='按建议改一版').click()
                expect(pilot.locator('.gg-card')).to_have_count(2)
                revision = next(data for path, data in requests if path.endswith('/revise'))
                self.assertEqual(revision['record_id'], record)
                self.assertEqual(revision['instructions']['change'], '增加留白')
                pilot.get_by_role('button', name='开始盲比').click()
                expect(pilot.locator('.gg-card').get_by_text('首稿', exact=False)).to_have_count(0)
                expect(pilot.locator('.gg-card').get_by_text('改稿 1', exact=False)).to_have_count(0)
                first = pilot.locator('.gg-card').first
                first.get_by_label('选用理由（必填）').fill('构图更克制，没有额外装饰。')
                first.get_by_role('button', name='选用候选 A').click()
                self.assertFalse(any(path.endswith('/select') for path, _ in requests))
                first.get_by_role('checkbox', name='已核对文案，无错漏和裁切').check()
                first.get_by_role('button', name='选用候选 A').click()
                expect(pilot.get_by_role('status')).to_contain_text('已选版，等待入库')
                selected = next(row for row in flow['candidates'] if row['pageId'] == flow['selectedPageId'])
                selected['feedback'] = {**selected['feedback'], 'libraryVisible': True}
                pilot.get_by_role('button', name='刷新状态').click()
                expect(pilot.get_by_role('status')).to_contain_text('已收入素材库')
                expect(pilot.get_by_role('button', name='开始下一张')).to_be_visible()
                expect(pilot.get_by_role('button', name='查看素材库')).to_be_visible()
                expect(pilot.get_by_role('button', name='核对评分并入库')).to_have_count(0)
                self.assertEqual(sum(path.endswith('/revise') for path, _ in requests), 1)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_admin_and_submitter_have_no_creation_entry_or_transport(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                admin_page, admin_requests, _, _, admin_errors = open_creation_app(browser, 'admin')
                expect(admin_page.locator('#app')).to_have_attribute('data-single-judge-role', 'admin')
                admin_page.wait_for_timeout(300)
                expect(admin_page.locator('[data-ai-action="open"]').first).to_be_visible()
                self.assertEqual(admin_errors, [])
                admin_page.close()

                page, ai_requests, _, _, errors = open_creation_app(browser, 'submitter')
                expect(page.locator('#app')).to_have_attribute('data-single-judge-role', 'submitter')
                page.wait_for_timeout(300)
                expect(page.locator('[data-ai-action="open"], [data-professional-creation="true"]')).to_have_count(0)
                message = page.evaluate("""async () => {
                  try { await window.GuanyeAI.request('/api/ai/planning/history'); return 'allowed'; }
                  catch (error) { return String(error && error.message || error); }
                }""")
                self.assertIn('无权使用策划与生图工作台', message)
                self.assertEqual(ai_requests, [])
                self.assertEqual(errors, [])
                page.close()
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
