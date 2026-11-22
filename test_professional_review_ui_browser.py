'''Edge regression for the professional review-only workbench.'''

import base64
import json
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None


EDGE = Path(os.environ.get('GUANYE_TEST_EDGE', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'))
SCRIPT = Path(__file__).resolve().parent / 'public' / 'ai-reviews.js'
PREVIEW = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jcYQAAAAASUVORK5CYII='
)
PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><main id="review"></main>
<script>
window.Cloud={
  imageUrl: async value => '/signed/'+(typeof value==='string'?value:value?.path||value?.name||''),
};
</script><script src="/ai-reviews.js"></script>
<script>window.reviewInstance=window.GuanyeReviews.mount(document.querySelector('#review'),{kind:'uploaded',deckId:'professional-deck',singleJudge:true,professionalReviewOnly:true});</script>
</body></html>'''


def deck_fixture(ai_allowed=True):
    return {
        'id': 'professional-deck',
        'data': {
            'title': '专业评分样本',
            'aiAllowed': ai_allowed,
            'images': [
                {'id': 'p1', 'name': 'p1.png', 'path': 'professional/p1.png', 'thumbPath': 'professional/p1.png', 'position': 1},
                {'id': 'p2', 'name': 'p2.png', 'path': 'professional/p2.png', 'thumbPath': 'professional/p2.png', 'position': 2},
            ],
        },
    }


def workspace_fixture(deck_scores=None, page_scores=None, ai_allowed=True, include_ai=True):
    deck_scores = deck_scores or {'visual': 4.25, 'layout': 4.5}
    page_scores = page_scores or {'visual': 3.75, 'layout': 4.0}
    submissions = [
        {
            'id': 'deck-latest', 'deckId': 'professional-deck', 'scope': 'deck', 'unitId': '', 'position': 0,
            'deckVersion': 'v1', 'rubricVersion': 'r1', 'humanVersion': 'h4', 'createdAt': '2026-11-22T03:00:00Z',
            'entry': {'scores': {'visual': 1, 'layout': 1}, 'note': '最新提交基础值'},
        },
        {
            'id': 'page-latest', 'deckId': 'professional-deck', 'scope': 'page', 'unitId': 'p1', 'position': 1,
            'deckVersion': 'v1', 'rubricVersion': 'r1', 'humanVersion': 'h4', 'createdAt': '2026-11-22T02:59:00Z',
            'entry': {'scores': {'visual': 1, 'layout': 1}, 'note': '页面提交基础值'},
        },
        {
            'id': 'deck-older-v1', 'deckId': 'professional-deck', 'scope': 'deck', 'unitId': '', 'position': 0,
            'deckVersion': 'v1', 'rubricVersion': 'r1', 'createdAt': '2026-11-20T03:00:00Z',
            'entry': {'scores': {'visual': 2, 'layout': 2}},
        },
        {
            'id': 'deck-old-version', 'deckId': 'professional-deck', 'scope': 'deck', 'unitId': '', 'position': 0,
            'deckVersion': 'v0', 'rubricVersion': 'r0', 'createdAt': '2026-11-21T03:00:00Z',
            'entry': {'scores': {'visual': 5, 'layout': 5}},
        },
    ]
    decisions = [
        {
            'decisionId': 'decision-latest-deck', 'submissionId': 'deck-latest', 'status': 'confirmed',
            'formalScores': deck_scores, 'classification': '人工纠偏整套', 'reasons': ['alignment'],
            'reasonOther': '整套其他说明', 'formalTotal': sum(deck_scores.values()),
        },
        {
            'decisionId': 'decision-latest-page', 'submissionId': 'page-latest', 'status': 'corrected',
            'formalScores': page_scores, 'classification': '人工纠偏A', 'reasons': ['alignment', 'other'],
            'reasonOther': '其他说明', 'formalTotal': sum(page_scores.values()),
        },
        {
            'decisionId': 'decision-older-deck', 'submissionId': 'deck-older-v1', 'status': 'confirmed',
            'formalScores': {'visual': 2, 'layout': 2}, 'classification': '旧纠偏', 'reasons': [], 'formalTotal': 4,
        },
    ]
    ai = [
        {
            'recordId': 'ai-deck-assisted', 'deckId': 'professional-deck', 'scope': 'deck', 'unitId': '',
            'variant': 'assisted', 'deckVersion': 'v1', 'rubricVersion': 'r1',
            'result': {'scores': {'visual': 0.5, 'layout': 0.5}, 'reasons': ['AI 对比理由'], 'summary': '首次对比摘要'},
        },
        {
            'recordId': 'ai-page-assisted', 'deckId': 'professional-deck', 'scope': 'page', 'unitId': 'p1',
            'variant': 'assisted', 'deckVersion': 'v1', 'rubricVersion': 'r1',
            'result': {'scores': {'visual': 0.5, 'layout': 0.5}, 'summary': '页面首次对比'},
        },
    ]
    return {
        'kind': 'uploaded',
        'deck': deck_fixture(ai_allowed),
        'review': {},
        'submissions': submissions,
        'ai': ai if include_ai else [],
        'decisions': decisions,
        'settings': {'reasonCatalog': [{'code': 'alignment', 'label': '对齐清楚'}, {'code': 'other', 'label': '其他'}]},
        'workspaceSettings': {'dimensions': [{'id': 'visual', 'name': '视觉效果'}, {'id': 'layout', 'name': '排版'}], 'maxScore': 5},
        'role': 'judge', 'isJudge': True, 'singleJudge': True, 'professionalReviewOnly': True,
        'queues': {
            'uploaded': [{'id': 'professional-deck', 'title': '专业评分样本', 'sourceKind': 'uploaded'}],
            'generated': [],
        },
        'queueCursors': {'uploaded': 'next-page-must-not-load', 'generated': None},
        'queue': {'uploaded': {'queued': 0, 'running': 0, 'completed': 1, 'failed': 0}, 'generated': {}},
        'deckVersion': 'v1', 'rubricVersion': 'r1', 'preEvaluationStatus': 'completed',
        'blind': {'aiVisible': True, 'rule': '仅与已提交人工记录同版本、同评分标准、同粒度的预评解锁'},
    }


@unittest.skipUnless(sync_playwright and EDGE.is_file(), 'requires Playwright and Microsoft Edge')
class ProfessionalReviewOnlyBrowserTest(unittest.TestCase):
    def test_professional_review_only_hides_unwired_actions_and_saves_revision(self):
        requests = []
        submissions = []
        current_workspace = workspace_fixture()

        def respond(route):
            nonlocal current_workspace
            url = urlsplit(route.request.url)
            if url.hostname != 'review.test':
                route.abort()
                return
            if url.path == '/':
                route.fulfill(content_type='text/html; charset=utf-8', body=PAGE)
                return
            if url.path == '/ai-reviews.js':
                route.fulfill(content_type='application/javascript; charset=utf-8', body=SCRIPT.read_text(encoding='utf-8'))
                return
            if url.path.startswith('/signed/'):
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews':
                action = parse_qs(url.query).get('action', [''])[0]
                requests.append((route.request.method, action))
                if action == 'workspace':
                    route.fulfill(json=current_workspace)
                    return
                if action == 'pre-evaluation-status':
                    route.fulfill(json={'preEvaluationStatus': 'completed'})
                    return
                if action == 'queue-decks':
                    query = parse_qs(url.query)
                    assert query == {'action': ['queue-decks'], 'kind': ['uploaded'], 'cursor': ['next-page-must-not-load']}
                    route.fulfill(json={'kind': 'uploaded', 'items': [{'id': 'next-deck', 'title': '下一页作品', 'sourceKind': 'uploaded'}], 'nextCursor': None})
                    return
                if action == 'backfill':
                    body = json.loads(route.request.post_data)
                    assert body == {'kind': 'uploaded', 'offset': 0, 'limit': 8}
                    route.fulfill(json={'ok': True, 'decks': 1, 'preparedUnits': 1, 'totalDecks': 1, 'nextOffset': None})
                    return
                if action == 'submit-bundle':
                    body = json.loads(route.request.post_data)
                    submissions.append(body)
                    units = {str(unit.get('scope')): unit for unit in body.get('units', [])}
                    current_workspace = workspace_fixture(
                        deck_scores=units['deck']['scores'],
                        page_scores=units['page']['scores'],
                    )
                    route.fulfill(json={'ok': True, 'message': '正式改分已保存'})
                    return
            route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page = browser.new_page(viewport={'width': 1200, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', respond)
                page.goto('http://review.test/')
                shell = page.locator('[data-professional-review-only="true"]')
                expect(shell).to_be_visible()
                expect(page.get_by_role('heading', name='专业人工评分 · 上传素材')).to_be_visible()
                expect(page.locator('[data-queue-kind="uploaded"]')).to_be_visible()
                for selector in (
                    '[data-action="prepare"]', '[data-action="run"]',
                    '[data-action="import"]', '[data-action="settings"]',
                    '[data-action="formal"]', '[data-action="admit"]',
                ):
                    self.assertEqual(page.locator(selector).count(), 0, selector)
                expect(page.locator('[data-action="queue-more"]')).to_be_visible()
                expect(page.locator('[data-action="backfill"]')).to_be_visible()
                expect(page.locator('[data-backfill-status]')).to_be_visible()
                self.assertEqual(page.locator('[data-formal-submission]').count(), 0)
                self.assertEqual(page.locator('[data-admission-submission]').count(), 0)

                self.assertEqual(page.locator('[data-manual-only-note]').count(), 0)
                comparison = page.locator('[data-ai-comparison="first-pair"]').first
                expect(comparison).to_be_visible()
                expect(comparison).to_contain_text('首次有效配对已解锁')
                with page.expect_request(lambda request: 'action=queue-decks' in request.url):
                    page.locator('[data-action="queue-more"]').first.click()
                expect(page.locator('option[value="next-deck"]')).to_have_count(1)
                with page.expect_request(lambda request: 'action=backfill' in request.url):
                    page.locator('[data-action="backfill"]').click()
                expect(page.locator('[data-backfill-status]')).to_contain_text('补档扫描完成')
                expect(page.locator('[data-action="backfill"]')).to_have_text('重新扫描')

                deck_visual = page.locator('[data-score-unit=""][data-dim="visual"]')
                deck_layout = page.locator('[data-score-unit=""][data-dim="layout"]')
                expect(deck_visual).to_have_value('4.25')
                expect(deck_layout).to_have_value('4.5')
                self.assertNotEqual(deck_visual.input_value(), '0.5')

                current_workspace = workspace_fixture(
                    deck_scores={'visual': 4.75, 'layout': 4.8},
                    page_scores={'visual': 3.75, 'layout': 4.0},
                )
                page.locator('[data-action="refresh"]').click()
                expect(deck_visual).to_have_value('4.75')
                expect(deck_layout).to_have_value('4.8')
                expect(page.locator('[data-ai-comparison="first-pair"]').first).to_be_visible()

                page_scope = page.get_by_role('button', name='选页', exact=True)
                deck_scope = page.get_by_role('button', name='整套', exact=True)
                page_scope.click()
                page.locator('[data-select-unit="p1"]').check()
                p1_visual = page.locator('[data-score-unit="p1"][data-dim="visual"]')
                p1_layout = page.locator('[data-score-unit="p1"][data-dim="layout"]')
                expect(p1_visual).to_have_value('3.75')
                expect(p1_layout).to_have_value('4')
                expect(page.locator('[data-reason-unit="p1"][value="alignment"]')).to_be_checked()
                expect(page.locator('[data-reason-unit="p1"][value="other"]')).to_be_checked()
                expect(page.locator('[data-other-unit="p1"]')).to_have_value('其他说明')
                expect(page.locator('[data-correct-unit="p1"]')).to_have_value('人工纠偏A')
                expect(page.locator('[data-ai-comparison="first-pair"]')).to_have_count(1)

                submit = page.locator('[data-action="submit"]')
                expect(submit).to_have_text('保存改分')
                expect(submit).to_have_attribute('data-submit-intent', 'revision')
                expect(submit).to_be_enabled()
                p1_visual.fill('3.25')
                expect(page.locator('[data-ai-comparison="first-pair"]')).to_have_count(1)

                with page.expect_request(lambda request: 'action=submit-bundle' in request.url):
                    submit.click()
                expect(page.locator('[data-review-status]')).to_contain_text('正式改分已保存')
                expect(submit).to_have_text('保存改分')
                expect(page.locator('[data-ai-comparison="first-pair"]')).to_have_count(1)
                expect(page.locator('[data-score-unit="p1"][data-dim="visual"]')).to_have_value('3.25')
                expect(page.locator('[data-score-unit="p1"][data-dim="layout"]')).to_have_value('4')

                self.assertEqual(len(submissions), 1)
                body = submissions[0]
                self.assertEqual(body['deckId'], 'professional-deck')
                self.assertEqual(body['deckVersion'], 'v1')
                self.assertEqual(body['rubricVersion'], 'r1')
                self.assertEqual([(unit['scope'], unit['unitId']) for unit in body['units']], [('deck', ''), ('page', 'p1')])
                self.assertEqual(body['units'][0]['scores'], {'visual': 4.75, 'layout': 4.8})
                self.assertEqual(body['units'][1]['scores'], {'visual': 3.25, 'layout': 4})
                self.assertEqual(body['units'][1]['classificationCorrection'], '人工纠偏A')
                self.assertEqual(body['units'][1]['reasons'], ['alignment', 'other'])
                self.assertEqual(body['units'][1]['reasonOther'], '其他说明')

                actions = [action for _, action in requests]
                self.assertTrue(all(action in {'workspace', 'pre-evaluation-status', 'backfill', 'queue-decks', 'submit-bundle'} for action in actions), actions)
                self.assertEqual(actions.count('backfill'), 1)
                self.assertEqual(actions.count('queue-decks'), 1)
                self.assertNotIn('comparison', actions)
                self.assertNotIn('prepare', actions)
                self.assertNotIn('run', actions)
                self.assertNotIn('settings', actions)
                self.assertNotIn('formal', actions)
                self.assertNotIn('admit', actions)
                self.assertEqual(errors, [])
            finally:
                browser.close()


    def test_manual_only_explains_without_claiming_first_pair(self):
        requests = []
        manual_workspace = workspace_fixture(ai_allowed=False, include_ai=False)

        def respond(route):
            url = urlsplit(route.request.url)
            if url.hostname != 'review.test':
                route.abort()
                return
            if url.path == '/':
                route.fulfill(content_type='text/html; charset=utf-8', body=PAGE)
                return
            if url.path == '/ai-reviews.js':
                route.fulfill(content_type='application/javascript; charset=utf-8', body=SCRIPT.read_text(encoding='utf-8'))
                return
            if url.path.startswith('/signed/'):
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews':
                action = parse_qs(url.query).get('action', [''])[0]
                requests.append(action)
                if action == 'workspace':
                    route.fulfill(json=manual_workspace)
                    return
                if action == 'pre-evaluation-status':
                    route.fulfill(json={'preEvaluationStatus': 'completed'})
                    return
            route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page = browser.new_page(viewport={'width': 1200, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', respond)
                page.goto('http://review.test/')
                expect(page.locator('[data-professional-review-only="true"]')).to_be_visible()
                manual = page.locator('[data-manual-only-note]')
                expect(manual).to_be_visible()
                expect(manual).to_contain_text('仅人工素材')
                expect(manual).to_contain_text('不会产生模型对比')
                expect(page.locator('[data-ai-comparison="first-pair"]')).to_have_count(0)
                self.assertNotIn('首次有效配对已解锁', page.locator('#review').inner_text())
                expect(page.locator('[data-action="submit"]')).to_have_text('保存改分')
                self.assertEqual(errors, [])
                self.assertTrue(all(action in {'workspace', 'pre-evaluation-status'} for action in requests), requests)
            finally:
                browser.close()


    def test_professional_app_admission_uses_current_saved_generated_unit(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, calls, workspace, admitted, errors = open_professional_app(browser, 'judge')
                page.get_by_role('button', name='正式评分工作台', exact=True).click()
                expect(page.locator('[data-professional-review-only="true"]')).to_have_count(1)
                expect(page.locator('#ga-review-live')).to_have_count(1)
                expect(page.get_by_role('heading', name='专业人工评分 · 上传素材')).to_be_visible()
                expect(page.locator('[data-action="admit"]')).to_have_count(0)
                page.get_by_role('button', name='刷新统计').click()
                expect(page.locator('[data-stats-pairs]')).to_have_text('3')
                page.get_by_role('button', name='切换到生成作品').click()
                expect(page.get_by_role('heading', name='专业人工评分 · 生成作品')).to_be_visible()
                expect(page.locator('[data-stats-pairs]')).to_have_text('7')
                expect(page.locator('#ga-review-units')).to_have_count(1)
                expect(page.locator('[data-action="formal"]')).to_have_count(0)
                expect(page.locator('[data-ai-comparison="first-pair"]')).to_be_visible()
                deck_button = page.get_by_role('button', name='收入素材库', exact=True)
                expect(deck_button).to_have_attribute('data-decision-id', fixture_uuid(102))

                score = page.locator('[data-score-unit=""][data-dim="visual"]')
                score.fill('2')
                expect(deck_button).to_have_count(0)
                page.get_by_role('button', name='保存改分', exact=True).click()
                expect(page.locator('[data-admission-status="low"]')).to_be_visible()
                expect(deck_button).to_have_count(0)
                expect(page.locator('[data-ai-comparison="first-pair"]')).to_be_visible()
                self.assertFalse(any(name == 'single_judge_admit' for name, _ in calls))

                score.fill('5')
                page.get_by_role('button', name='保存改分', exact=True).click()
                expect(deck_button).to_have_attribute('data-decision-id', fixture_uuid(123))
                deck_button.click()
                expect(page.locator('[data-admission-status="admitted"]')).to_be_visible()
                expect(page.locator('[data-review-status]')).to_contain_text('AI 自我学习生成')
                expect(deck_button).to_have_count(0)
                expect(page.locator('[data-ai-comparison="first-pair"]')).to_be_visible()

                page.get_by_role('button', name='选页', exact=True).click()
                first = page.locator('[data-unit-card="p1"]')
                second = page.locator('[data-unit-card="p2"]')
                expect(first.locator('[data-admission-status="unselected"]')).to_be_visible()
                expect(first.locator('[data-action="admit"]')).to_have_count(0)
                second.locator('[data-select-unit="p2"]').check()
                expect(second.locator('[data-action="admit"]')).to_have_count(0)
                first.locator('[data-select-unit="p1"]').check()
                first.get_by_role('button', name='收入素材库', exact=True).click()
                expect(first.locator('[data-admission-status="admitted"]')).to_be_visible()
                expect(page.locator('[data-review-status]')).to_contain_text('此评分单元已收入素材库')
                select_args = [(args['p_submission'], args['p_decision']) for name, args in calls if name == 'single_judge_admit']
                self.assertEqual(select_args, [(fixture_uuid(23), fixture_uuid(123)), (fixture_uuid(10), fixture_uuid(110))])
                self.assertEqual(sum(name == 'get_review_library' for name, _ in calls), 3)
                page.get_by_role('button', name='关闭', exact=True).click()
                expect(page.locator('[data-open="generated-sample"]').first).to_be_visible()
                expect(page.locator('[data-professional-panel]')).not_to_be_visible()
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_admin_permissions_compare_only_and_submitter_no_review(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page, calls, _, _, errors = open_professional_app(browser, 'admin')
                expect(page.get_by_role('button', name='正式评分工作台', exact=True)).to_be_visible()
                page.get_by_role('button', name='素材与数据', exact=True).click()
                page.get_by_role('button', name='打开双轨评审比较', exact=True).click()
                expect(page.locator('[data-professional-comparison-only]')).to_be_visible()
                expect(page.locator('[data-stats-pairs]')).to_have_text('3')
                page.get_by_role('button', name='生成作品', exact=True).click()
                expect(page.locator('[data-stats-pairs]')).to_have_text('7')
                expect(page.locator('[data-ai-comparison], [data-score-unit], [data-action="submit"], [data-action="admit"]')).to_have_count(0)
                self.assertEqual([name for name, _ in calls if name.startswith('single_judge_')], ['single_judge_access', 'single_judge_comparison'])
                self.assertEqual(errors, [])

                page, calls, _, _, errors = open_professional_app(browser, 'submitter')
                expect(page.locator('#app')).to_have_attribute('data-single-judge-role', 'submitter')
                expect(page.locator('[data-action="professional-review"], [data-action="professional-permissions"], [data-ai-action="open"]')).to_have_count(0)
                self.assertEqual([name for name, _ in calls], ['single_judge_access'])
                self.assertEqual(errors, [])
            finally:
                browser.close()


def fixture_uuid(i):
    return f'10000000-0000-4000-8000-{i:012d}'


def generated_workspace():
    result = workspace_fixture(include_ai=False)
    result['kind'] = 'generated'
    result['deck'] = {'id': 'generated-sample', 'data': {
        **result['deck']['data'], 'id': 'generated-sample', 'sourceKind': 'generated', 'title': '生成评分样本',
    }}
    result['queues']['generated'] = [{'id': 'generated-sample', 'title': '生成评分样本'}]
    result['settings']['humanThreshold'] = 0.85
    result['submissions'], result['decisions'] = [], []
    for i, page, values in [(1, '', (5, 5, 5)), (2, '', (5, 5, 5)), (10, 'p1', (4.5, 5, 5))]:
        append_generated_decision(result, i, page, dict(zip(('visual', 'layout', 'match'), values)))
    result['ai'] = [{
        'recordId': fixture_uuid(51), 'submissionId': fixture_uuid(1), 'deckId': 'generated-sample',
        'scope': 'deck', 'unitId': '', 'variant': 'assisted', 'deckVersion': 'v1', 'rubricVersion': 'r1',
        'result': {'scores': {'visual': 1, 'layout': 1, 'match': 1}},
    }]
    return result


def append_generated_decision(data, i, page, scores):
    total = round(scores['visual'] * .4 + scores['layout'] * .3 + scores['match'] * .3, 2)
    data['submissions'].insert(0, {
        'id': fixture_uuid(i), 'scope': 'page' if page else 'deck', 'unitId': page,
        'deckId': 'generated-sample', 'deckVersion': 'v1', 'rubricVersion': 'r1',
        'createdAt': f'2026-11-22T12:00:{i:02d}+00:00', 'entry': {'scores': scores},
    })
    data['decisions'].insert(0, {
        'submissionId': fixture_uuid(i), 'decisionId': fixture_uuid(100 + i), 'formalScores': scores,
        'formalTotal': total, 'scoreRatio': total / 5, 'status': 'confirmed',
        'expiresAt': '2099-01-01T00:00:00Z', 'libraryVisible': False,
    })


def open_professional_app(browser, role):
    page = browser.new_page(viewport={'width': 1280, 'height': 1024})
    errors, calls, admitted = [], [], set()
    page.on('pageerror', lambda error: errors.append(str(error)))
    ws = fixture_uuid(1000)
    upload, generated = workspace_fixture(), generated_workspace()
    state = {**upload['workspaceSettings'], 'revision': 0, 'configRevision': 0, 'reviews': {}}
    config = {'url': 'https://review.test', 'anonKey': 'fixture-key', 'workspaceId': ws, 'singleJudge': True, 'localReview': True}
    sb = """window.supabase={createClient:()=>({
      auth:{getSession:async()=>({data:{session:{access_token:'fixture',user:{id:'fixture',email:'fixture@example.test'}}}})},
      rpc:async(name,args)=>await (await fetch('/rpc/'+name,{method:'POST',body:JSON.stringify(args)})).json(),
      storage:{from:()=>({createSignedUrls:async paths=>({data:paths.map(path=>({path,signedUrl:'/signed/'+path}))})})},
    })};"""

    def respond(route):
        url = urlsplit(route.request.url)
        if url.hostname != 'review.test':
            route.abort()
            return
        if url.path == '/cloud-config.js':
            route.fulfill(content_type='text/javascript', body='window.CLOUD_CONFIG='+json.dumps(config))
        elif url.path == '/vendor/supabase.js':
            route.fulfill(content_type='text/javascript', body=sb)
        elif url.path.startswith('/rpc/'):
            name = url.path.split('/')[-1]
            args = route.request.post_data_json
            calls.append((name, args))
            if name == 'single_judge_access':
                result = {'mode': 'single-judge', 'role': role}
            elif name == 'get_review_library':
                result = {'decks': [{'data': {**upload['deck']['data'], 'id': upload['deck']['id']}}]}
                if admitted:
                    result['decks'].append({'data': {**generated['deck']['data'], 'aiLabel': 'AI 自我学习生成'}})
            elif name == 'get_review_state':
                result = state
            elif name == 'single_judge_review_workspace' and role == 'judge':
                result = generated if args['p_kind'] == 'generated' else upload
            elif name == 'single_judge_submit_bundle' and role == 'judge':
                i = 22 + sum(n == name for n, _ in calls) - 1
                append_generated_decision(generated, i, '', args['payload']['units'][0]['scores'])
                result = {'ok': True}
            elif name == 'single_judge_admit' and role == 'judge':
                sub = args['p_submission']
                decision = next(d for d in generated['decisions'] if d['submissionId'] == sub)
                decision['libraryVisible'] = True
                admitted.add(sub)
                result = {'ok': True, 'admitted': True, 'idempotent': sub == fixture_uuid(10), 'entryId': fixture_uuid(200)}
            elif name == 'single_judge_comparison' and role in ('admin', 'judge'):
                result = {'byKind': {kind: {
                    'holdoutUnits': count, 'baseline': {'count': count, 'mae': .3, 'dimensions': {'visual': .3}},
                    'assisted': {'count': count, 'mae': .2, 'dimensions': {'visual': .2}},
                } for kind, count in [('uploaded', 3), ('generated', 7)]}}
            else:
                route.fulfill(json={'error': {'message': 'Unexpected RPC: '+name}})
                return
            route.fulfill(json={'data': result})
        elif url.path.startswith('/signed/'):
            route.fulfill(content_type='image/png', body=PREVIEW)
        else:
            name = url.path.strip('/') or 'index.html'
            asset = SCRIPT.parent / name
            if asset.parent != SCRIPT.parent or not asset.is_file():
                route.fulfill(status=404, body='')
            else:
                route.fulfill(path=asset)

    page.route('**/*', respond)
    page.goto('https://review.test/')
    return page, calls, generated, admitted, errors

if __name__ == '__main__':
    unittest.main()
