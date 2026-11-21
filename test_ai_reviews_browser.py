"""Edge browser regression for blind rating, readable unlocked AI summaries and per-track stats."""

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
PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><main id="review"></main>
<script>
window.Cloud = {imageUrl: async () => '/preview.png'};
window.uploadedComparisonReads = 0;
window.uploadedComparisonDelayed = false;
const originalFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await originalFetch(...args);
  if (args[0].includes('action=comparison&kind=uploaded')) {
    const readJson = response.json.bind(response);
    response.json = async () => {
      const data = await readJson();
      setTimeout(() => { window.uploadedComparisonReads++; }, 0);
      return data;
    };
  }
  return response;
};
</script><script src="/ai-reviews.js"></script>
<script>window.reviewInstance = window.GuanyeReviews.mount(document.querySelector('#review'), {deckId: 'uploaded-deck'});</script>
</body></html>'''
PREVIEW = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jcYQAAAAASUVORK5CYII=')
UPLOAD_RESULT = '上传模型对齐理由'
PAGE_RESULT = '逐页模型排版理由'
ASSISTED_CLASSIFICATION = '<b>清晰汇报</b>'
ASSISTED_REASON_OBJECT = '对齐清楚'
ASSISTED_REASON_STRING = '<script>window.aiReviewXss=1</script>层级稳定'
BASELINE_CLASSIFICATION = '旧分类 <img src=x onerror="window.aiReviewXss=2">'
BASELINE_SUMMARY = '<a href="javascript:window.aiReviewXss=3">对照摘要</a>'
UPLOAD_FIXTURE = b'PK\x03\x04fixture-pptx'


def workspace_fixture(kind, deck_id=None, pre_evaluation_status='completed'):
    deck_id = deck_id or kind + '-deck'
    title = '样本 ' + deck_id
    return {
        'deck': {
            'id': deck_id,
            'data': {
                'title': title,
                'images': [
                    {'id': 'p1', 'name': '封面', 'path': 'preview/p1.png', 'position': 1},
                    {'id': 'p2', 'name': '数据页', 'path': 'preview/p2.png', 'position': 2},
                ],
            },
        },
        'preEvaluationStatus': pre_evaluation_status,
        'deckVersion': deck_id + '-v1',
        'rubricVersion': 'r1',
        'isJudge': False,
        'workspaceSettings': {
            'dimensions': [{'id': 'visual', 'name': '视觉效果'}, {'id': 'layout', 'name': '排版'}],
            'maxScore': 5,
        },
        'queues': {
            'uploaded': [{'id': 'uploaded-deck', 'title': '上传样本'}],
            'generated': [{'id': 'generated-deck', 'title': '生成样本'}],
        },
        'queue': {
            'uploaded': {'queued': 0, 'running': 0, 'completed': 0, 'failed': 0},
            'generated': {'queued': 0, 'running': 0, 'completed': 0, 'failed': 0},
        },
        'settings': {'reasonCatalog': []},
        'review': {},
        'ai': [],
        'submissions': [],
        'decisions': [],
    }


def queue_item(deck_id, kind, title=None):
    return {
        'id': deck_id,
        'title': title or ('样本 ' + deck_id),
        'name': title or deck_id,
        'sourceKind': kind,
    }


def paginated_workspace(kind, deck_id=None, queues=None, cursors=None):
    fixture = workspace_fixture(kind, deck_id)
    if queues is not None:
        fixture['queues'] = queues
    if cursors is not None:
        fixture['queueCursors'] = cursors
    return fixture


@unittest.skipUnless(sync_playwright and EDGE.is_file(), 'requires Playwright and Microsoft Edge')
class ReviewBrowserTest(unittest.TestCase):
    def test_blind_review_ratings_and_stats_stay_in_scope(self):
        submissions = []
        delayed = []
        generated_loading = []
        calls = []

        def workspace(kind):
            images = [{'id': 'p1', 'name': '封面'}, {'id': 'p2', 'name': '精选数据页'}, {'id': 'p3', 'name': '总结'}]
            for image in images:
                image['path'] = f"preview/{image['id']}.png"
            ai = []
            submission_rows = []
            for item in submissions:
                if item['deckId'] != kind + '-deck':
                    continue
                for unit in item['units']:
                    summary = PAGE_RESULT if unit['unitId'] else UPLOAD_RESULT
                    submission_rows.append({
                        'id': f"submission-{kind}-{len(submission_rows)}",
                        'scope': item['scope'],
                        'unitId': unit['unitId'],
                        'entry': {'scores': unit['scores']},
                    })
                    ai.append({
                        'scope': item['scope'], 'unitId': unit['unitId'], 'variant': 'assisted',
                        'result': {
                            'scores': {'visual': 4, 'layout': 4},
                            'classification': {'label': ASSISTED_CLASSIFICATION},
                            'reasons': [{'text': ASSISTED_REASON_OBJECT}, ASSISTED_REASON_STRING],
                            'summary': summary,
                        },
                    })
                    ai.append({
                        'scope': item['scope'], 'unitId': unit['unitId'], 'variant': 'baseline',
                        'result': {
                            'scores': {'visual': 3, 'layout': 3},
                            'classification': BASELINE_CLASSIFICATION,
                            'summary': BASELINE_SUMMARY,
                        },
                    })
            return {
                'deck': {'id': kind + '-deck', 'data': {'title': '样本 ' + kind, 'images': images}},
                'preEvaluationStatus': 'completed',
                'deckVersion': kind + '-v1',
                'rubricVersion': 'r1',
                'isJudge': True,
                'workspaceSettings': {'dimensions': [{'id': 'visual', 'name': '视觉效果'}, {'id': 'layout', 'name': '排版'}], 'maxScore': 100 if kind == 'generated' else 5},
                'queues': {track: [{'id': track + '-deck', 'title': track}] for track in ('uploaded', 'generated')},
                'settings': {'reasonCatalog': [{'code': 'alignment', 'label': '对齐清楚'}, {'code': 'other', 'label': '其他'}]},
                'review': {'pages': {'p1': {'scores': {'visual': 3, 'layout': 3}}, 'p3': {'scores': {'visual': 2, 'layout': 2}}}},
                'ai': ai,
                'submissions': submission_rows,
            }

        def comparison(kind):
            uploaded = {
                'holdoutUnits': 1,
                'baseline': {'mae': .2345, 'dimensions': {'visual': 0.0, 'layout': .125}},
                'assisted': {'mae': .2, 'dimensions': {'visual': 0.0, 'layout': .0625}},
                'improvement': .0345,
                'sourceDedup': {'knownGroups': 2, 'unknownDecks': 3, 'excludedDuplicateDecks': 1},
            }
            generated = {
                'holdoutUnits': 1,
                'baseline': {'mae': .8765, 'dimensions': {'visual': .5, 'layout': .25, 'match': 0.0}},
                'assisted': {'mae': .2, 'dimensions': {'visual': .1, 'layout': .2, 'match': 0.0}},
                'improvement': .6765,
                'sourceDedup': {'knownGroups': 7, 'unknownDecks': 8, 'excludedDuplicateDecks': 9},
            }
            current = uploaded if kind == 'uploaded' else generated
            return {
                'holdoutUnits': current['holdoutUnits'],
                'baseline': current['baseline'],
                'assisted': current['assisted'],
                'improvement': current['improvement'],
                'byKind': {'uploaded': uploaded, 'generated': generated},
            }

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            calls.append((action, kind))
            if action == 'workspace':
                if kind == 'generated' and not generated_loading:
                    generated_loading.append(route)
                    return
                route.fulfill(json=workspace(kind))
            elif action == 'submit':
                submissions.append(json.loads(route.request.post_data))
                route.fulfill(json={'ok': True})
            elif action == 'comparison':
                if kind == 'uploaded' and calls.count(('comparison', 'uploaded')) == 2:
                    delayed.append(route)
                    return
                route.fulfill(json=comparison(kind))
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded', exact=True)).to_be_visible()
            expect(page.locator('[data-queue-more]')).to_have_count(0)
            expect(page.get_by_text(UPLOAD_RESULT)).to_have_count(0)
            expect(page.locator('.ga-ai')).to_have_count(0)
            page.wait_for_function("document.querySelector('.ga-preview img')?.naturalWidth === 1")
            submit = page.locator('[data-action="submit"]')
            visual_score = page.locator('[data-score-unit=""][data-dim="visual"]')
            layout_score = page.locator('[data-score-unit=""][data-dim="layout"]')
            visual_quick_85 = page.locator('[data-quick-unit=""][data-quick-dim="visual"][data-quick-value="4.25"]')
            expect(page.get_by_role('spinbutton', name='视觉效果', exact=True)).to_have_count(1)
            expect(page.get_by_role('spinbutton', name='排版', exact=True)).to_have_count(1)
            expect(page.get_by_role('group', name='整套作品评分 · 视觉效果速选，满分 5 分')).to_be_visible()
            self.assertTrue(visual_score.evaluate("(el) => el.labels.length === 1 && el.labels[0].textContent.trim() === '视觉效果'"))
            expect(visual_score).to_have_attribute('step', '0.01')
            visual_quick_85.focus()
            visual_quick_85.evaluate("(el) => { el.dataset.focusSentinel = 'kept'; }")
            page.keyboard.press('Enter')
            expect(visual_quick_85).to_be_focused()
            self.assertEqual(visual_quick_85.evaluate("(el) => el.dataset.focusSentinel"), 'kept')
            expect(visual_score).to_have_value('4.25')
            self.assertTrue(visual_score.evaluate('(el) => el.checkValidity()'))
            expect(page.locator('[data-score-echo-unit=""][data-score-echo-dim="visual"]')).to_contain_text('当前 4.25 分')
            visual_score.fill('0')
            expect(page.locator('[data-score-echo-unit=""][data-score-echo-dim="visual"]')).to_contain_text('当前 0 分')
            visual_score.fill('4.5')
            layout_score.fill('4')
            submit.click()
            expect(page.get_by_text(UPLOAD_RESULT, exact=False)).to_be_visible()
            expect(page.locator('.ga-ai')).to_have_count(1)
            expect(page.get_by_text('正式评分以专职评委提交为准。')).to_be_visible()
            self.assertEqual(page.locator('.ga-ai-group .ga-ai-group-label b').all_text_contents(), ['设计规则与样本辅助', '基线预评'])
            expect(page.locator('.ga-ai-group .ga-pill')).to_have_count(0)
            formals = page.locator('[data-unit-card=""] .ga-formal')
            expect(formals).to_have_count(1)
            self.assertTrue(formals.first.evaluate("(formal) => formal.compareDocumentPosition(document.querySelector('.ga-ai')) & Node.DOCUMENT_POSITION_FOLLOWING"))
            assisted = page.locator('.ga-ai-group[data-ai-variant="assisted"]')
            baseline = page.locator('.ga-ai-group[data-ai-variant="baseline"]')
            expect(assisted.locator('.ga-ai-summary-row').filter(has_text='视觉效果')).to_contain_text('4')
            expect(assisted.locator('.ga-ai-summary-row').filter(has_text='排版')).to_contain_text('4')
            expect(assisted.get_by_text('分类', exact=True)).to_be_visible()
            expect(assisted.get_by_text(ASSISTED_CLASSIFICATION, exact=True)).to_be_visible()
            expect(assisted.get_by_text(ASSISTED_REASON_OBJECT, exact=True)).to_be_visible()
            expect(assisted.get_by_text(ASSISTED_REASON_STRING, exact=False)).to_be_visible()
            expect(assisted.get_by_text(UPLOAD_RESULT, exact=False)).to_be_visible()
            expect(baseline.get_by_text(BASELINE_CLASSIFICATION, exact=False)).to_be_visible()
            expect(baseline.get_by_text(BASELINE_SUMMARY, exact=False)).to_be_visible()
            expect(baseline.get_by_text('理由', exact=True)).to_have_count(0)
            self.assertEqual(page.locator('.ga-ai script, .ga-ai img, .ga-ai a').count(), 0)
            self.assertIsNone(page.evaluate('window.aiReviewXss'))
            self.assertNotIn('"scores"', page.locator('.ga-ai').inner_text())
            page.locator('[data-scope="page"]').click()
            expect(page.get_by_text(UPLOAD_RESULT, exact=False)).to_have_count(0)
            expect(page.locator('.ga-ai')).to_have_count(0)
            expect(page.get_by_role('spinbutton', name='视觉效果', exact=True)).to_have_count(3)
            expect(page.get_by_role('spinbutton', name='排版', exact=True)).to_have_count(3)
            expect(page.locator('[data-select-unit]:checked')).to_have_count(0)
            expect(submit).to_be_disabled()
            page.locator('[data-select-unit="p2"]').check()
            p2_visual = page.locator('[data-score-unit="p2"][data-dim="visual"]')
            p2_quick_85 = page.locator('[data-quick-unit="p2"][data-quick-dim="visual"][data-quick-value="4.25"]')
            expect(page.get_by_role('group', name='第 2 页 · 精选数据页 · 视觉效果速选，满分 5 分')).to_be_visible()
            p2_quick_85.focus()
            page.keyboard.press('Enter')
            expect(p2_quick_85).to_be_focused()
            expect(p2_visual).to_have_value('4.25')
            expect(page.locator('[data-score-echo-unit="p2"][data-score-echo-dim="visual"]')).to_contain_text('当前 4.25 分')
            expect(page.locator('[data-score-unit="p1"][data-dim="visual"]')).to_have_value('3')
            expect(page.locator('[data-score-unit="p3"][data-dim="visual"]')).to_have_value('2')
            page.locator('[data-score-unit="p2"][data-dim="layout"]').fill('4.5')
            page.locator('[data-reason-unit="p2"][value="alignment"]').check()
            page.locator('[data-reason-unit="p2"][value="other"]').check()
            page.locator('[data-other-unit="p2"]').fill('数字标签很好读')
            submit.click()
            expect(page.get_by_text(PAGE_RESULT, exact=False)).to_be_visible()
            expect(page.locator('.ga-ai')).to_have_count(1)
            self.assertEqual(page.locator('.ga-ai-group').count(), 2)
            self.assertEqual([(unit['unitId'], unit['position']) for unit in submissions[1]['units']], [('p2', 2)])
            self.assertEqual(submissions[1]['units'][0]['scores'], {'visual': 4.25, 'layout': 4.5})
            self.assertEqual(submissions[1]['units'][0]['reasons'], ['alignment', 'other'])
            self.assertEqual(submissions[1]['units'][0]['reasonOther'], '数字标签很好读')
            expect(page.locator('[data-score-unit="p1"][data-dim="visual"]')).to_have_value('3')
            expect(page.locator('[data-score-unit="p3"][data-dim="visual"]')).to_have_value('2')
            page.locator('[data-action="stats"]').click()
            stats = page.locator('#ga-stats')
            expect(page.get_by_text('0.2345')).to_be_visible()
            expect(stats.get_by_text('基线预评', exact=True)).to_be_visible()
            expect(stats.get_by_text('设计规则与样本辅助', exact=True)).to_be_visible()
            expect(stats.get_by_text('无校准', exact=True)).to_have_count(0)
            expect(stats).to_contain_text('改善值（基线预评 − 设计规则与样本辅助）')
            expect(stats.locator('table.ga-stats-table')).to_have_count(1)
            expect(stats.locator('[data-stat-dimension="overall"]')).to_contain_text('0.2345')
            expect(stats.locator('[data-stat-dimension="visual"]')).to_contain_text('0.0000')
            expect(stats.locator('[data-stat-dimension="layout"]')).to_contain_text('0.1250')
            expect(stats.locator('[data-stats-pairs]')).to_have_text('1')
            expect(stats).to_contain_text('按评分单元计数，不是作品套数')
            expect(stats).to_contain_text('按满分归一化的 MAE')
            expect(stats).to_contain_text('数值越小越接近人工分')
            expect(stats.locator('[data-stats-improvement]')).to_contain_text('改善 0.0345')
            source_dedup = stats.locator('[data-stats-source-dedup]')
            expect(source_dedup).to_have_count(1)
            expect(source_dedup.locator('[data-stats-source-counts]')).to_have_text('已确认来源 2 组 · 来源未知 3 套 · 排除重复 1 套')
            expect(source_dedup.locator('[data-stats-source-note]')).to_have_text('同一已确认原文件只保留一套；来源未知的历史作品按原记录计入。')
            expect(source_dedup.locator('.ga-sr-only')).to_have_text('已确认来源 2 组，来源未知 3 套，排除重复 1 套。同一已确认原文件只保留一套；来源未知的历史作品按原记录计入。')
            self.assertNotIn('"dimensions"', stats.inner_text())
            page.locator('[data-action="stats"]').click()
            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('AI 盲评 · 生成作品')).to_be_visible()
            expect(page.get_by_text('样本 uploaded', exact=True)).to_have_count(0)
            expect(page.get_by_text(UPLOAD_RESULT, exact=False)).to_have_count(0)
            expect(page.locator('.ga-ai')).to_have_count(0)
            expect(submit).to_be_disabled()
            self.assertEqual(len(generated_loading), 1)
            generated_loading[0].fulfill(json=workspace('generated'))
            expect(page.get_by_text('样本 generated', exact=True)).to_be_visible()
            expect(page.get_by_text('0.2345')).to_have_count(0)
            expect(page.get_by_text(PAGE_RESULT, exact=False)).to_have_count(0)
            expect(page.locator('[data-score-unit=""][data-dim="match"]')).to_have_count(1)
            generated_visual = page.locator('[data-score-unit=""][data-dim="visual"]')
            generated_quick_85 = page.locator('[data-quick-unit=""][data-quick-dim="visual"][data-quick-value="85"]')
            expect(page.get_by_role('group', name='整套作品评分 · 视觉速选，满分 100 分')).to_be_visible()
            expect(generated_visual).to_have_attribute('step', '0.01')
            generated_quick_85.focus()
            page.keyboard.press('Enter')
            expect(generated_quick_85).to_be_focused()
            expect(generated_visual).to_have_value('85')
            expect(page.locator('[data-score-echo-unit=""][data-score-echo-dim="visual"]')).to_contain_text('当前 85 分')
            self.assertTrue(generated_visual.evaluate('(el) => el.checkValidity()'))
            generated_visual.fill('0.25')
            expect(page.locator('[data-score-echo-unit=""][data-score-echo-dim="visual"]')).to_contain_text('当前 0.25 分')
            page.locator('[data-action="stats"]').click()
            expect(page.get_by_text('0.8765')).to_be_visible()
            expect(page.locator('[data-stat-dimension="match"]')).to_contain_text('0.0000')
            expect(page.locator('[data-stats-source-dedup]')).to_have_count(0)
            self.assertEqual(len(delayed), 1)
            with page.expect_response(lambda response: 'action=comparison&kind=uploaded' in response.url):
                delayed[0].fulfill(json=comparison('uploaded'))
            page.wait_for_function('window.uploadedComparisonReads === 2')
            expect(page.get_by_text('0.2345')).to_have_count(0)
            expect(page.get_by_text('0.8765')).to_be_visible()
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()


    def test_stats_zero_state_failure_retry_and_stale_track_are_isolated(self):
        calls = []
        delayed_upload = []

        def empty_track():
            return {'holdoutUnits': 0, 'baseline': None, 'assisted': None, 'improvement': None}

        def track(holdout_units, baseline_mae, baseline_dimensions, assisted_mae, assisted_dimensions):
            return {
                'holdoutUnits': holdout_units,
                'baseline': {'mae': baseline_mae, 'dimensions': baseline_dimensions},
                'assisted': {'mae': assisted_mae, 'dimensions': assisted_dimensions},
                'improvement': baseline_mae - assisted_mae,
            }

        def payload(uploaded, generated):
            return {
                'holdoutUnits': 0,
                'baseline': None,
                'assisted': None,
                'improvement': None,
                'byKind': {'uploaded': uploaded, 'generated': generated},
            }

        uploaded = track(
            2, .2345, {'vision': 0.0, 'layout': .125, 'story': .03125},
            .2, {'vision': 0.0, 'layout': .0625, 'story': .015625},
        )
        stale_uploaded = track(
            7, .7777, {'visual': .7777, 'layout': .8888},
            .1111, {'visual': .1111, 'layout': .2222},
        )
        generated = track(
            3, .8765, {'visual': .5, 'layout': .25, 'match': 0.0},
            .9765, {'visual': .9, 'match': 0.0},
        )

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            calls.append((action, kind))
            if action == 'workspace':
                data = workspace_fixture(kind)
                if kind == 'uploaded':
                    data['workspaceSettings']['dimensions'] = [
                        {'id': 'visual', 'name': '画面表现'},
                        {'id': 'vision', 'name': '视觉别名'},
                        {'id': 'layout', 'name': '版式'},
                        {'id': 'story', 'name': '叙事'},
                    ]
                else:
                    data['workspaceSettings']['dimensions'].append({'id': 'story', 'name': '不应出现在生成轨'})
                route.fulfill(json=data)
            elif action == 'comparison':
                comparison_index = calls.count(('comparison', kind))
                if kind == 'uploaded':
                    if comparison_index == 2:
                        delayed_upload.append(route)
                        page.evaluate('() => { window.uploadedComparisonDelayed = true; }')
                        return
                    route.fulfill(json=payload(uploaded, empty_track()))
                elif comparison_index == 1:
                    route.fulfill(json=payload(uploaded, empty_track()))
                elif comparison_index == 2:
                    route.fulfill(status=500, json={'error': 'stats offline'})
                else:
                    route.fulfill(json=payload(uploaded, generated))
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()

            stats = page.locator('#ga-stats')
            stats_button = page.locator('[data-action="stats"]')
            stats_button.click()
            expect(stats.locator('[data-stat-dimension="overall"]')).to_contain_text('0.2345')
            expect(stats.locator('[data-stat-dimension="visual"]')).to_contain_text('0.0000')
            expect(stats.locator('[data-stat-dimension="layout"]')).to_contain_text('0.1250')
            expect(stats.locator('[data-stat-dimension="story"]')).to_contain_text('0.0313')
            expect(stats.locator('[data-stats-pairs]')).to_have_text('2')
            self.assertEqual(
                stats.locator('thead th').evaluate_all("(nodes) => nodes.map((node) => node.getAttribute('scope'))"),
                ['col', 'col', 'col'],
            )
            self.assertEqual(
                stats.locator('tbody th').evaluate_all("(nodes) => nodes.map((node) => node.getAttribute('scope'))"),
                ['row', 'row', 'row', 'row'],
            )
            self.assertEqual(
                stats.locator('tbody th').all_text_contents(),
                ['综合', '画面表现', '版式', '叙事'],
            )
            expect(stats.get_by_text('视觉别名', exact=True)).to_have_count(0)
            expect(stats).to_contain_text('综合（总体）为各配对评分单元 MAE 的平均')
            self.assertNotIn('"dimensions"', stats.inner_text())

            stats_button.click()
            expect(page.locator('[data-stats-loading]')).to_have_text('正在刷新统计…')
            expect(stats_button).to_be_disabled()
            expect(stats.locator('[data-stat-dimension="overall"]')).to_contain_text('0.2345')
            page.wait_for_function('window.uploadedComparisonDelayed === true')
            self.assertEqual(len(delayed_upload), 1)

            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('AI 盲评 · 生成作品')).to_be_visible()
            stats_button = page.locator('[data-action="stats"]')
            stats_button.click()
            expect(page.locator('[data-stats-zero]')).to_contain_text('尚无配对样本')
            expect(page.locator('[data-stats-pairs]')).to_have_text('0')
            expect(page.locator('[data-stats-improvement]')).to_have_count(0)
            expect(page.get_by_text('改善值（基线预评 − 设计规则与样本辅助）')).to_have_count(0)

            with page.expect_response(lambda response: 'action=comparison&kind=uploaded' in response.url):
                delayed_upload[0].fulfill(json=payload(stale_uploaded, empty_track()))
            page.wait_for_function('window.uploadedComparisonReads === 2')
            expect(page.get_by_text('0.7777')).to_have_count(0)
            expect(page.locator('[data-stats-kind="generated"]')).to_have_count(1)
            expect(page.locator('[data-stats-zero]')).to_contain_text('尚无配对样本')

            stats_button.click()
            expect(page.locator('[data-stats-error]')).to_contain_text('stats offline')
            expect(page.locator('[data-stats-zero]')).to_contain_text('尚无配对样本')
            expect(page.get_by_text('0.8765')).to_have_count(0)
            expect(page.locator('[data-stats-retry]')).to_be_visible()
            expect(page.locator('.ga-error')).to_have_count(1)

            page.locator('[data-stats-retry]').click()
            expect(page.locator('[data-stats-error]')).to_have_count(0)
            expect(page.locator('[data-stat-dimension="match"]')).to_contain_text('0.0000')
            expect(page.locator('[data-stat-dimension="layout"]')).to_contain_text('—')
            expect(page.locator('[data-stats-pairs]')).to_have_text('3')
            expect(page.locator('[data-stats-improvement]')).to_contain_text('变差 0.1000')
            self.assertEqual(
                page.locator('[data-stats-kind="generated"] tbody th').all_text_contents(),
                ['综合', '视觉', '排版', '匹配'],
            )
            self.assertEqual(page.evaluate("document.activeElement?.getAttribute('data-action')"), 'stats')
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()


    def test_source_dedup_is_uploaded_only_and_rejects_invalid_metadata(self):
        calls = []
        upload_reads = {'value': 0}
        upload_variants = [
            {
                'holdoutUnits': 1,
                'baseline': {'mae': .3456, 'dimensions': {'visual': .25, 'layout': .125}},
                'assisted': {'mae': .2345, 'dimensions': {'visual': .125, 'layout': .0625}},
                'improvement': .1111,
                'sourceDedup': {'knownGroups': 2, 'unknownDecks': 3, 'excludedDuplicateDecks': 1},
            },
            {
                'holdoutUnits': 0,
                'baseline': None,
                'assisted': None,
                'improvement': None,
                'sourceDedup': {'knownGroups': 0, 'unknownDecks': 0, 'excludedDuplicateDecks': 0},
            },
            {
                'holdoutUnits': 1,
                'baseline': {'mae': .2, 'dimensions': {'visual': .2, 'layout': .2}},
                'assisted': {'mae': .1, 'dimensions': {'visual': .1, 'layout': .1}},
                'improvement': .1,
                'sourceDedup': {'knownGroups': '2', 'unknownDecks': 3, 'excludedDuplicateDecks': 1},
            },
            {
                'holdoutUnits': 1,
                'baseline': {'mae': .2, 'dimensions': {'visual': .2, 'layout': .2}},
                'assisted': {'mae': .1, 'dimensions': {'visual': .1, 'layout': .1}},
                'improvement': .1,
                'sourceDedup': {'knownGroups': 2.5, 'unknownDecks': 3, 'excludedDuplicateDecks': 1},
            },
            {
                'holdoutUnits': 1,
                'baseline': {'mae': .2, 'dimensions': {'visual': .2, 'layout': .2}},
                'assisted': {'mae': .1, 'dimensions': {'visual': .1, 'layout': .1}},
                'improvement': .1,
                'sourceDedup': {'knownGroups': -1, 'unknownDecks': 3, 'excludedDuplicateDecks': 1},
            },
            {
                'holdoutUnits': 1,
                'baseline': {'mae': .2, 'dimensions': {'visual': .2, 'layout': .2}},
                'assisted': {'mae': .1, 'dimensions': {'visual': .1, 'layout': .1}},
                'improvement': .1,
                'sourceDedup': {'knownGroups': 2, 'unknownDecks': 3},
            },
            {
                'holdoutUnits': 1,
                'baseline': {'mae': .2, 'dimensions': {'visual': .2, 'layout': .2}},
                'assisted': {'mae': .1, 'dimensions': {'visual': .1, 'layout': .1}},
                'improvement': .1,
            },
        ]
        generated_track = {
            'holdoutUnits': 1,
            'baseline': {'mae': .4321, 'dimensions': {'visual': .4, 'layout': .3, 'match': .2}},
            'assisted': {'mae': .321, 'dimensions': {'visual': .3, 'layout': .2, 'match': .1}},
            'improvement': .1111,
            'sourceDedup': {'knownGroups': 7, 'unknownDecks': 8, 'excludedDuplicateDecks': 9},
        }

        def payload(kind):
            if kind == 'uploaded':
                current = upload_variants[upload_reads['value']]
                upload_reads['value'] += 1
            else:
                current = generated_track
            return {
                'holdoutUnits': current['holdoutUnits'],
                'baseline': current['baseline'],
                'assisted': current['assisted'],
                'improvement': current['improvement'],
                'sourceDedup': current.get('sourceDedup'),
                'byKind': {
                    'uploaded': current if kind == 'uploaded' else upload_variants[0],
                    'generated': generated_track,
                },
            }

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            calls.append((action, kind))
            if action == 'workspace':
                route.fulfill(json=workspace_fixture(kind))
            elif action == 'comparison':
                route.fulfill(json=payload(kind))
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()

            stats = page.locator('#ga-stats')
            stats_button = page.locator('[data-action="stats"]')
            with page.expect_response(lambda response: 'action=comparison&kind=uploaded' in response.url):
                stats_button.click()
            expect(stats.locator('[data-stat-dimension="overall"]')).to_contain_text('0.3456')
            expect(stats).to_have_attribute('aria-busy', 'false')
            expect(stats.locator('table.ga-stats-table')).to_have_count(1)
            expect(stats.locator('caption')).to_have_text('当前轨道的留出配对评分单元汇总')
            self.assertEqual(
                stats.locator('thead th').evaluate_all("(nodes) => nodes.map((node) => node.getAttribute('scope'))"),
                ['col', 'col', 'col'],
            )
            self.assertEqual(
                stats.locator('tbody th').evaluate_all("(nodes) => nodes.map((node) => node.getAttribute('scope'))"),
                ['row', 'row', 'row'],
            )
            source_dedup = stats.locator('[data-stats-source-dedup]')
            expect(source_dedup.locator('[data-stats-source-counts]')).to_have_text('已确认来源 2 组 · 来源未知 3 套 · 排除重复 1 套')
            expect(source_dedup.locator('[data-stats-source-note]')).to_have_text('同一已确认原文件只保留一套；来源未知的历史作品按原记录计入。')
            expect(source_dedup.locator('.ga-sr-only')).to_have_text('已确认来源 2 组，来源未知 3 套，排除重复 1 套。同一已确认原文件只保留一套；来源未知的历史作品按原记录计入。')

            upload_comparison_count = calls.count(('comparison', 'uploaded'))
            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('AI 盲评 · 生成作品')).to_be_visible()
            expect(page.locator('[data-stats-source-dedup]')).to_have_count(0)
            with page.expect_response(lambda response: 'action=comparison&kind=generated' in response.url):
                page.locator('[data-action="stats"]').click()
            expect(page.locator('[data-stat-dimension="overall"]')).to_contain_text('0.4321')
            expect(page.locator('[data-stats-source-dedup]')).to_have_count(0)

            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('AI 盲评 · 上传素材')).to_be_visible()
            expect(page.locator('[data-stats-source-dedup] [data-stats-source-counts]')).to_have_text('已确认来源 2 组 · 来源未知 3 套 · 排除重复 1 套')
            self.assertEqual(calls.count(('comparison', 'uploaded')), upload_comparison_count)

            with page.expect_response(lambda response: 'action=comparison&kind=uploaded' in response.url):
                page.locator('[data-action="stats"]').click()
            expect(page.locator('[data-stats-zero]')).to_contain_text('尚无配对样本')
            expect(page.locator('[data-stats-pairs]')).to_have_text('0')
            expect(page.locator('table.ga-stats-table')).to_have_count(0)
            expect(page.locator('[data-stats-source-dedup] [data-stats-source-counts]')).to_have_text('已确认来源 0 组 · 来源未知 0 套 · 排除重复 0 套')
            expect(page.locator('[data-stats-source-dedup] [data-stats-source-note]')).to_have_text('同一已确认原文件只保留一套；来源未知的历史作品按原记录计入。')

            for _ in upload_variants[2:]:
                with page.expect_response(lambda response: 'action=comparison&kind=uploaded' in response.url):
                    page.locator('[data-action="stats"]').click()
                expect(page.locator('[data-stats-source-dedup]')).to_have_count(0)
                expect(page.locator('table.ga-stats-table')).to_have_count(1)

            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()


    def test_formal_review_and_generated_admission_bind_latest_decision(self):
        formal_payloads = []
        admit_payloads = []
        admitted_state = {'pass': False}
        upload_decisions = {'rows': []}
        future = '2099-01-01T00:00:00Z'
        reason_catalog = [
            {'code': 'alignment', 'label': '对齐清楚'},
            {'code': 'other', 'label': '其他'},
        ]

        def submission(submission_id, kind, scores):
            return {
                'id': submission_id,
                'deckId': kind + '-deck',
                'scope': 'deck',
                'unitId': '',
                'position': 0,
                'deckVersion': kind + '-deck-v1',
                'rubricVersion': 'r1',
                'humanVersion': 'h-' + submission_id,
                'entry': {'scores': scores},
                'scoresComplete': True,
                'createdAt': '2026-11-21T00:00:00Z',
            }

        def decision(decision_id, submission_id, status, ratio, *, scores=None, reasons=None, reason_other='', library=False, expires=future):
            return {
                'decisionId': decision_id,
                'submissionId': submission_id,
                'status': status,
                'formalScores': scores or {'visual': 4.5, 'layout': 4.5, 'match': 4.5},
                'reasons': reasons or [],
                'reasonOther': reason_other,
                'formalTotal': ratio * 5,
                'scoreRatio': ratio,
                'expiresAt': expires,
                'libraryVisible': library,
                'entryId': 'entry-' + decision_id if library else None,
            }

        upload_submission = submission('upload-sub', 'uploaded', {'visual': 3, 'layout': 3})
        generated_submissions = [
            submission('gen-none', 'generated', {'visual': 3, 'layout': 3, 'match': 3}),
            submission('gen-pending', 'generated', {'visual': 3, 'layout': 3, 'match': 3}),
            submission('gen-low', 'generated', {'visual': 3, 'layout': 3, 'match': 3}),
            submission('gen-pass', 'generated', {'visual': 3, 'layout': 3, 'match': 3}),
            submission('gen-admitted', 'generated', {'visual': 3, 'layout': 3, 'match': 3}),
            submission('gen-reject', 'generated', {'visual': 3, 'layout': 3, 'match': 3}),
        ]

        def generated_decisions():
            pass_decision = decision(
                'dec-pass', 'gen-pass', 'corrected', .9,
                scores={'visual': 4.5, 'layout': 4.25, 'match': 4},
            )
            if admitted_state['pass']:
                pass_decision.update({'libraryVisible': True, 'entryId': 'entry-dec-pass'})
            return [
                decision('dec-low-new', 'gen-low', 'confirmed', .8),
                decision('dec-low-old', 'gen-low', 'confirmed', .95),
                pass_decision,
                decision('dec-pass-old', 'gen-pass', 'confirmed', .7, scores={'visual': 2, 'layout': 2, 'match': 2}),
                decision('dec-pending', 'gen-pending', 'pending', .9, scores={'visual': 1, 'layout': 1, 'match': 1}),
                decision('dec-admitted', 'gen-admitted', 'confirmed', .9, library=True),
                decision('dec-reject', 'gen-reject', 'confirmed', .91),
            ]

        def workspace(kind):
            data = workspace_fixture(kind)
            data['isJudge'] = True
            data['settings'] = {'humanThreshold': .85, 'reasonCatalog': reason_catalog}
            if kind == 'uploaded':
                data['submissions'] = [upload_submission]
                data['decisions'] = upload_decisions['rows']
                data['ai'] = [{
                    'scope': 'deck', 'unitId': '', 'variant': 'assisted',
                    'result': {'scores': {'visual': 5, 'layout': 5}},
                }]
            else:
                data['submissions'] = generated_submissions
                data['decisions'] = generated_decisions()
            return data

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            if action == 'workspace':
                route.fulfill(json=workspace(kind))
            elif action == 'formal':
                body = json.loads(route.request.post_data)
                formal_payloads.append(body)
                upload_decisions['rows'] = [decision(
                    'upload-decision', body['submissionId'], 'confirmed', .8,
                    scores=body['formalScores'], reasons=body['reasons'], reason_other=body['reasonOther'],
                )]
                route.fulfill(json={'ok': True, 'decisionId': 'upload-decision', 'formalTotal': 4})
            elif action == 'admit':
                body = json.loads(route.request.post_data)
                admit_payloads.append(body)
                if body.get('submissionId') == 'gen-reject':
                    route.fulfill(status=409, json={'error': '正式加权分未达到满分门槛'})
                elif body.get('submissionId') == 'gen-pass' and body.get('decisionId') == 'dec-pass':
                    admitted_state['pass'] = True
                    route.fulfill(json={'ok': True, 'idempotent': False, 'decisionId': 'dec-pass'})
                else:
                    route.fulfill(status=400, json={'error': 'unexpected admit payload'})
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()

            formal = page.locator('[data-formal-submission="upload-sub"]')
            expect(formal).to_have_count(1)
            expect(formal.locator('.ga-admission')).to_have_count(0)
            expect(page.get_by_text('收入素材库', exact=False)).to_have_count(0)
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="visual"]')).to_have_value('3')
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="layout"]')).to_have_value('3')
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="match"]')).to_have_count(0)

            upload_decisions['rows'] = [
                decision('dec-upload-pending', 'upload-sub', 'pending', .2, scores={'visual': 1, 'layout': 1}),
                decision(
                    'dec-upload-confirmed', 'upload-sub', 'corrected', .9,
                    scores={'visual': 4.5, 'layout': 4.25, 'match': 5},
                    reasons=['alignment'], reason_other='不应自动带入',
                ),
            ]
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="refresh"]').click()
            formal = page.locator('[data-formal-submission="upload-sub"]')
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="visual"]')).to_have_value('4.5')
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="layout"]')).to_have_value('4.25')
            expect(formal.locator('[data-formal-reason="upload-sub"][value="alignment"]')).not_to_be_checked()
            expect(formal.locator('[data-formal-other="upload-sub"]')).to_have_value('')

            formal.locator('[data-formal-score="upload-sub"][data-dim="visual"]').fill('4')
            formal.locator('[data-formal-score="upload-sub"][data-dim="layout"]').fill('4')
            formal.locator('[data-formal-reason="upload-sub"][value="alignment"]').check()
            formal.locator('[data-formal-reason="upload-sub"][value="other"]').check()
            formal.locator('[data-formal-other="upload-sub"]').fill('结构需调整')
            formal.locator('[data-formal-score="upload-sub"][data-dim="visual"]').clear()

            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="refresh"]').click()
            formal = page.locator('[data-formal-submission="upload-sub"]')
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="visual"]')).to_have_value('')
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="layout"]')).to_have_value('4')
            expect(formal.locator('[data-formal-reason="upload-sub"][value="alignment"]')).to_be_checked()
            expect(formal.locator('[data-formal-reason="upload-sub"][value="other"]')).to_be_checked()
            expect(formal.locator('[data-formal-other="upload-sub"]')).to_have_value('结构需调整')

            page.locator('[data-scope="page"]').click()
            page.locator('[data-scope="deck"]').click()
            formal = page.locator('[data-formal-submission="upload-sub"]')
            expect(formal.locator('[data-formal-score="upload-sub"][data-dim="visual"]')).to_have_value('')
            expect(formal.locator('[data-formal-reason="upload-sub"][value="alignment"]')).to_be_checked()
            expect(formal.locator('[data-formal-reason="upload-sub"][value="other"]')).to_be_checked()
            expect(formal.locator('[data-formal-other="upload-sub"]')).to_have_value('结构需调整')

            formal.locator('[data-action="formal"]').click()
            expect(page.get_by_text('请完整填写正式评分', exact=False)).to_be_visible()
            self.assertEqual(formal_payloads, [])
            formal = page.locator('[data-formal-submission="upload-sub"]')
            formal.locator('[data-formal-score="upload-sub"][data-dim="visual"]').fill('4')
            formal.locator('[data-formal-other="upload-sub"]').clear()
            formal.locator('[data-action="formal"]').click()
            expect(page.get_by_text('选择“其他”时请填写原因', exact=False)).to_be_visible()
            self.assertEqual(formal_payloads, [])
            formal = page.locator('[data-formal-submission="upload-sub"]')
            formal.locator('[data-formal-other="upload-sub"]').fill('结构需调整')

            with page.expect_request(lambda request: 'action=formal' in request.url):
                formal.locator('[data-action="formal"]').click()
            expect(page.get_by_text('正式评分已确认', exact=False)).to_be_visible()
            self.assertEqual(len(formal_payloads), 1)
            self.assertEqual(formal_payloads[0]['formalScores'], {'visual': 4, 'layout': 4})
            self.assertEqual(formal_payloads[0]['reasons'], ['alignment', 'other'])
            self.assertEqual(formal_payloads[0]['reasonOther'], '结构需调整')
            self.assertEqual(formal_payloads[0]['status'], 'confirmed')
            self.assertNotIn('decisionId', formal_payloads[0])
            expect(page.locator('[data-action="admit"]')).to_have_count(0)
            expect(page.get_by_text('收入素材库', exact=False)).to_have_count(0)

            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('AI 盲评 · 生成作品')).to_be_visible()
            pass_formal = page.locator('[data-formal-submission="gen-pass"]')
            expect(pass_formal.locator('[data-formal-score="gen-pass"][data-dim="visual"]')).to_have_value('4.5')
            expect(pass_formal.locator('[data-formal-score="gen-pass"][data-dim="layout"]')).to_have_value('4.25')
            expect(pass_formal.locator('[data-formal-score="gen-pass"][data-dim="match"]')).to_have_value('4')
            pending_formal = page.locator('[data-formal-submission="gen-pending"]')
            expect(pending_formal.locator('[data-formal-score="gen-pending"][data-dim="visual"]')).to_have_value('3')
            none_formal = page.locator('[data-formal-submission="gen-none"]')
            expect(none_formal.locator('[data-formal-score="gen-none"][data-dim="visual"]')).to_have_value('3')

            missing = page.locator('[data-admission-submission="gen-none"]')
            pending = page.locator('[data-admission-submission="gen-pending"]')
            low = page.locator('[data-admission-submission="gen-low"]')
            ready = page.locator('[data-admission-submission="gen-pass"]')
            admitted = page.locator('[data-admission-submission="gen-admitted"]')
            reject = page.locator('[data-admission-submission="gen-reject"]')
            expect(missing).to_have_attribute('data-admission-status', 'missing')
            expect(pending).to_have_attribute('data-admission-status', 'pending')
            expect(low).to_have_attribute('data-admission-status', 'low')
            expect(ready).to_have_attribute('data-admission-status', 'ready')
            expect(admitted).to_have_attribute('data-admission-status', 'admitted')
            expect(reject).to_have_attribute('data-admission-status', 'ready')
            expect(missing.locator('.ga-admission-action button')).to_be_disabled()
            expect(pending.locator('.ga-admission-action button')).to_be_disabled()
            expect(low.locator('.ga-admission-action button')).to_be_disabled()
            expect(low).to_contain_text('0.8000')
            expect(low).to_contain_text('0.8500')
            expect(low.locator('[data-decision-id="dec-low-old"]')).to_have_count(0)
            expect(ready.locator('[data-decision-id="dec-pass"]')).to_be_enabled()
            expect(ready).to_contain_text('已纠偏')
            expect(admitted.get_by_text('已收入素材库 · AI 自我学习生成', exact=True)).to_be_visible()
            expect(admitted.locator('.ga-admission-action button')).to_have_count(0)

            with page.expect_request(lambda request: 'action=admit' in request.url):
                reject.locator('[data-decision-id="dec-reject"]').click()
            expect(page.get_by_text('正式加权分未达到满分门槛', exact=False)).to_be_visible()
            self.assertEqual(admit_payloads[-1], {'submissionId': 'gen-reject', 'decisionId': 'dec-reject'})

            with page.expect_request(lambda request: 'action=admit' in request.url):
                page.locator('[data-admission-submission="gen-pass"] [data-decision-id="dec-pass"]').click()
            expect(page.locator('[data-admission-submission="gen-pass"]')).to_have_attribute('data-admission-status', 'admitted')
            expect(page.locator('[data-admission-submission="gen-pass"]')).to_contain_text('已收入素材库 · AI 自我学习生成')
            self.assertEqual(admit_payloads[-1], {'submissionId': 'gen-pass', 'decisionId': 'dec-pass'})
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()


    def test_formal_prefill_handles_inflight_scope_switch_and_concurrent_edit(self):
        workspace_reads = []
        formal_routes = []
        decisions = []
        future = '2099-01-01T00:00:00Z'

        def submission():
            return {
                'id': 'sub-shared', 'deckId': 'uploaded-deck', 'scope': 'deck', 'unitId': '',
                'position': 0, 'deckVersion': 'uploaded-deck-v1', 'rubricVersion': 'r1',
                'humanVersion': 'h-sub-shared', 'entry': {'scores': {'visual': 3, 'layout': 3}},
                'scoresComplete': True, 'createdAt': '2026-11-21T00:00:00Z',
            }

        def decision(decision_id, scores):
            return {
                'decisionId': decision_id, 'submissionId': 'sub-shared', 'status': 'confirmed',
                'formalScores': scores, 'reasons': [], 'reasonOther': '', 'formalTotal': 4,
                'scoreRatio': .8, 'expiresAt': future, 'libraryVisible': False, 'entryId': None,
            }

        def workspace():
            data = workspace_fixture('uploaded')
            data['isJudge'] = True
            data['settings'] = {'reasonCatalog': []}
            data['submissions'] = [submission()]
            data['decisions'] = list(decisions)
            return data

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            if action == 'workspace':
                workspace_reads.append(route)
                route.fulfill(json=workspace())
            elif action == 'formal':
                formal_routes.append(route)
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')

            formal = page.locator('[data-formal-submission="sub-shared"]')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="visual"]')).to_have_value('3')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="layout"]')).to_have_value('3')

            with page.expect_request(lambda request: 'action=formal' in request.url):
                formal.locator('[data-action="formal"]').click()
            page.wait_for_timeout(50)
            self.assertEqual(len(formal_routes), 1)
            formal.locator('[data-formal-score="sub-shared"][data-dim="visual"]').fill('4.75')
            decisions[:] = [decision('dec-edit', {'visual': 4.1, 'layout': 3.9})]
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                formal_routes[0].fulfill(json={
                    'ok': True, 'decisionId': 'dec-edit', 'formalTotal': 4,
                    'formalScores': {'visual': 4.1, 'layout': 3.9}, 'status': 'confirmed',
                })
            expect(page.get_by_text('正式评分已确认', exact=False)).to_be_visible()
            formal = page.locator('[data-formal-submission="sub-shared"]')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="visual"]')).to_have_value('4.75')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="layout"]')).to_have_value('3')
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="refresh"]').click()
            expect(page.get_by_text('正式评分已确认', exact=False)).to_have_count(0)
            formal = page.locator('[data-formal-submission="sub-shared"]')

            with page.expect_request(lambda request: 'action=formal' in request.url):
                formal.locator('[data-action="formal"]').click()
            page.wait_for_timeout(50)
            self.assertEqual(len(formal_routes), 2)
            page.locator('[data-scope="page"]').click()
            expect(page.locator('[data-select-unit="p1"]')).to_be_visible()
            reads_after_switch = len(workspace_reads)
            decisions[:] = [decision('dec-switch', {'visual': 4.2, 'layout': 3.8})]
            formal_routes[1].fulfill(json={
                'ok': True, 'decisionId': 'dec-switch', 'formalTotal': 4,
                'formalScores': {'visual': 4.2, 'layout': 3.8}, 'status': 'confirmed',
            })
            page.wait_for_timeout(300)
            self.assertEqual(len(workspace_reads), reads_after_switch)
            expect(page.locator('[data-formal-submission="sub-shared"]')).to_have_count(0)
            expect(page.get_by_text('正式评分已确认', exact=False)).to_have_count(0)

            page.locator('[data-scope="deck"]').click()
            formal = page.locator('[data-formal-submission="sub-shared"]')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="visual"]')).to_have_value('4.75')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="layout"]')).to_have_value('3')
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="refresh"]').click()
            formal = page.locator('[data-formal-submission="sub-shared"]')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="visual"]')).to_have_value('4.2')
            expect(formal.locator('[data-formal-score="sub-shared"][data-dim="layout"]')).to_have_value('3.8')
            self.assertEqual(errors, [])
            browser.close()


    def test_human_submit_single_flight_clears_only_clean_selected_drafts(self):
        workspace_routes = []
        submit_routes = []
        submitted = []
        preview_requests = []
        saved = {'value': False}

        def uploaded_workspace():
            data = workspace_fixture('uploaded')
            data['deck']['data']['images'].append({
                'id': 'p3', 'name': '总结', 'path': 'preview/p3.png', 'position': 3,
            })
            data['isJudge'] = True
            data['settings'] = {
                'reasonCatalog': [
                    {'code': 'alignment', 'label': '对齐清楚'},
                    {'code': 'other', 'label': '其他'},
                ],
            }
            if saved['value']:
                data['review'] = {
                    'scores': {'visual': 0.5},
                    'note': '服务器整套',
                    'pages': {
                        'p1': {'scores': {'visual': 3}, 'note': '服务器p1'},
                        'p2': {'scores': {'visual': 5}, 'note': '服务器p2'},
                        'p3': {'scores': {'visual': 4}, 'note': '服务器p3'},
                    },
                }
            return data

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
            if url.path == '/preview.png':
                preview_requests.append(route.request.url)
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            if action == 'workspace':
                workspace_routes.append(route)
                if len(workspace_routes) == 1:
                    route.fulfill(json=uploaded_workspace())
            elif action == 'submit':
                submitted.append(json.loads(route.request.post_data))
                submit_routes.append(route)
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 800, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()

            page.locator('[data-unit-card=""] [data-score-unit=""][data-dim="visual"]').fill('1')
            page.locator('[data-unit-card=""] [data-note-unit=""]').fill('整套草稿')
            page.locator('[data-scope="page"]').click()
            page.locator('[data-select-unit="p1"]').check()
            page.locator('[data-select-unit="p2"]').check()
            page.locator('[data-score-unit="p1"][data-dim="visual"]').fill('4')
            page.locator('[data-score-unit="p2"][data-dim="visual"]').fill('3')
            page.locator('[data-score-unit="p3"][data-dim="visual"]').fill('2')
            page.locator('[data-note-unit="p3"]').fill('未选中草稿')
            page.locator('[data-reason-unit="p1"][value="alignment"]').check()
            page.locator('[data-reason-unit="p2"][value="other"]').check()
            page.locator('[data-other-unit="p2"]').fill('结构问题')
            page.wait_for_timeout(250)
            preview_count = len(preview_requests)
            page.evaluate("window.__submitCard = document.querySelector('[data-unit-card=\"p1\"]')")

            with page.expect_request(lambda request: 'action=submit' in request.url):
                page.locator('[data-action="submit"]').click()
            submit_button = page.locator('[data-action="submit"]')
            expect(submit_button).to_have_text('提交中…')
            expect(submit_button).to_be_disabled()
            expect(submit_button).to_have_attribute('aria-busy', 'true')
            self.assertEqual(len(submit_routes), 1)
            self.assertEqual(len(preview_requests), preview_count)
            self.assertTrue(page.evaluate("window.__submitCard === document.querySelector('[data-unit-card=\"p1\"]')"))
            body = submitted[0]
            self.assertEqual(body['scope'], 'page')
            self.assertEqual([unit['unitId'] for unit in body['units']], ['p1', 'p2'])
            self.assertEqual(body['units'][0]['reasons'], ['alignment'])
            self.assertEqual(body['units'][1]['reasons'], ['other'])

            page.locator('[data-note-unit="p1"]').fill('等待期间编辑')
            page.evaluate("document.querySelector('[data-action=\"submit\"]').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
            page.wait_for_timeout(50)
            self.assertEqual(len(submit_routes), 1)
            self.assertEqual(page.evaluate("document.activeElement?.getAttribute('data-note-unit')"), 'p1')

            page.locator('[data-scope="deck"]').click()
            page.locator('[data-scope="page"]').click()
            expect(page.locator('[data-note-unit="p1"]')).to_have_value('等待期间编辑')
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="refresh"]').click()
            self.assertEqual(len(workspace_routes), 2)
            expect(submit_button).to_have_text('提交中…')
            workspace_routes[1].fulfill(json=uploaded_workspace())
            expect(page.locator('[data-note-unit="p1"]')).to_have_value('等待期间编辑')
            page.evaluate("document.querySelector('[data-action=\"submit\"]').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
            page.wait_for_timeout(50)
            self.assertEqual(len(submit_routes), 1)

            saved['value'] = True
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                submit_routes[0].fulfill(json={'ok': True, 'message': '人工评分已提交'})
            page.wait_for_timeout(100)
            self.assertEqual(len(workspace_routes), 3)
            expect(submit_button).to_have_text('提交完成，刷新中…')
            expect(submit_button).to_be_disabled()
            page.locator('[data-scope="deck"]').click()
            page.evaluate("document.querySelector('[data-action=\"submit\"]').dispatchEvent(new MouseEvent('click', {bubbles: true}))")
            page.wait_for_timeout(50)
            self.assertEqual(len(submit_routes), 1)
            page.locator('[data-scope="page"]').click()
            workspace_routes[2].fulfill(json=uploaded_workspace())
            expect(page.get_by_text('人工评分已提交', exact=True)).to_be_visible()
            expect(submit_button).to_be_enabled()
            expect(submit_button).to_have_text('提交所选人工评分')
            expect(page.locator('[data-action="prepare"]')).to_be_enabled()
            expect(page.locator('[data-score-unit="p1"][data-dim="visual"]')).to_have_value('4')
            expect(page.locator('[data-note-unit="p1"]')).to_have_value('等待期间编辑')
            expect(page.locator('[data-score-unit="p2"][data-dim="visual"]')).to_have_value('5')
            expect(page.locator('[data-note-unit="p2"]')).to_have_value('服务器p2')
            expect(page.locator('[data-score-unit="p3"][data-dim="visual"]')).to_have_value('2')
            expect(page.locator('[data-note-unit="p3"]')).to_have_value('未选中草稿')

            page.locator('[data-scope="deck"]').click()
            expect(page.locator('[data-score-unit=""][data-dim="visual"]')).to_have_value('1')
            expect(page.locator('[data-note-unit=""]')).to_have_value('整套草稿')
            self.assertEqual(errors, [])
            browser.close()

    def test_human_submit_failure_retry_and_stale_deck_are_isolated(self):
        submit_routes = []
        submitted = []
        workspace_reads = []

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            if action == 'workspace':
                kind = query.get('kind', ['uploaded'])[0]
                workspace_reads.append(kind)
                route.fulfill(json=workspace_fixture(kind, query.get('deckId', [''])[0] or None))
            elif action == 'submit':
                submitted.append(json.loads(route.request.post_data))
                submit_routes.append(route)
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 800, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()

            page.locator('[data-score-unit=""][data-dim="visual"]').fill('2')
            page.locator('[data-note-unit=""]').fill('失败保留')
            with page.expect_request(lambda request: 'action=submit' in request.url):
                page.locator('[data-action="submit"]').click()
            page.wait_for_timeout(100)
            self.assertEqual(len(submit_routes), 1)
            expect(page.locator('[data-action="submit"]')).to_have_text('提交中…')
            submit_routes[0].fulfill(status=500, json={'error': '评分服务暂不可用'})
            expect(page.locator('.ga-error')).to_contain_text('评分服务暂不可用')
            submit_button = page.locator('[data-action="submit"]')
            expect(submit_button).to_be_enabled()
            expect(submit_button).to_have_text('提交所选人工评分')
            expect(page.locator('[data-score-unit=""][data-dim="visual"]')).to_have_value('2')
            expect(page.locator('[data-note-unit=""]')).to_have_value('失败保留')

            with page.expect_request(lambda request: 'action=submit' in request.url):
                submit_button.click()
            page.wait_for_timeout(100)
            self.assertEqual(len(submit_routes), 2)
            self.assertEqual(submitted[0], submitted[1])

            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('样本 generated-deck', exact=True)).to_be_visible()
            expect(page.locator('[data-action="submit"]')).to_be_enabled()

            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()
            expect(page.locator('[data-action="submit"]')).to_have_text('提交中…')
            expect(page.locator('[data-action="submit"]')).to_be_disabled()

            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('样本 generated-deck', exact=True)).to_be_visible()
            expect(page.locator('[data-action="submit"]')).to_be_enabled()
            page.locator('[data-score-unit=""][data-dim="visual"]').fill('4')
            page.locator('[data-note-unit=""]').fill('新作品草稿')
            page.evaluate("window.__generatedCard = document.querySelector('[data-unit-card=\"\"]')")
            generated_reads = workspace_reads.count('generated')
            submit_routes[1].fulfill(json={'ok': True, 'message': '旧作品提交完成'})
            page.wait_for_timeout(250)
            self.assertEqual(workspace_reads.count('generated'), generated_reads)
            self.assertTrue(page.evaluate("window.__generatedCard === document.querySelector('[data-unit-card=\"\"]')"))
            expect(page.locator('[data-score-unit=""][data-dim="visual"]')).to_have_value('4')
            expect(page.locator('[data-note-unit=""]')).to_have_value('新作品草稿')
            expect(page.get_by_text('旧作品提交完成', exact=True)).to_have_count(0)
            expect(page.locator('.ga-error')).to_have_count(0)
            expect(page.locator('[data-action="submit"]')).to_be_enabled()
            self.assertEqual(errors, [])
            browser.close()

    def test_upload_preflight_duplicate_guard_and_retry(self):
        calls = []
        import_requests = []

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews/import':
                import_requests.append(route)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            calls.append((action, kind))
            if action == 'workspace':
                route.fulfill(json=workspace_fixture(kind, query.get('deckId', [''])[0] or None))
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()

            too_many = [
                {
                    'name': f'deck-{index}.pptx',
                    'mimeType': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                    'buffer': UPLOAD_FIXTURE,
                }
                for index in range(11)
            ]
            page.locator('[data-import-files]').set_input_files(too_many)
            page.locator('[data-action="import"]').click()
            expect(page.locator('[data-import-status]')).to_contain_text('每批最多 10 份')
            self.assertEqual(import_requests, [])
            expect(page.locator('[data-action="import"]')).to_be_enabled()

            page.evaluate(
                """() => {
                    const input = document.querySelector('[data-import-files]');
                    const oversized = {name: '过大.pptx', size: 25 * 1024 * 1024 + 1};
                    Object.defineProperty(input, 'files', {configurable: true, value: [oversized]});
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                }"""
            )
            page.locator('[data-action="import"]').click()
            expect(page.locator('[data-import-status]')).to_contain_text('单个文件不得超过 25 MiB')
            self.assertEqual(import_requests, [])
            page.evaluate("delete document.querySelector('[data-import-files]').files")

            page.locator('[data-import-files]').set_input_files({
                'name': 'deck.pptx',
                'mimeType': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                'buffer': UPLOAD_FIXTURE,
            })
            expect(page.locator('[data-import-summary]')).to_contain_text('deck.pptx')

            with page.expect_request(lambda request: request.url.endswith('/api/ai/reviews/import')) as first_request:
                page.evaluate(
                    """() => {
                        const button = document.querySelector('[data-action="import"]');
                        button.click();
                        button.click();
                    }"""
                )
            self.assertEqual(first_request.value.method, 'POST')
            expect(page.locator('[data-import-status]')).to_contain_text('正在上传')
            expect(page.locator('[data-action="import"]')).to_be_disabled()
            page.wait_for_timeout(150)
            self.assertEqual(len(import_requests), 1)

            page.evaluate("document.querySelector('[data-action=\"import\"]').click()")
            page.wait_for_timeout(150)
            self.assertEqual(len(import_requests), 1)

            import_requests[0].fulfill(
                status=500,
                content_type='application/json',
                body=json.dumps({'error': 'TypeError: internal upstream exploded'}),
            )
            expect(page.locator('[data-import-status]')).to_contain_text('上传服务暂时不可用')
            self.assertNotIn('TypeError', page.locator('[data-import-status]').text_content())
            expect(page.locator('[data-action="import"]')).to_be_enabled()
            expect(page.locator('[data-import-summary]')).to_contain_text('deck.pptx')

            with page.expect_request(lambda request: request.url.endswith('/api/ai/reviews/import')) as request_info:
                page.locator('[data-action="import"]').click()
            self.assertEqual(request_info.value.method, 'POST')
            expect(page.locator('[data-import-status]')).to_contain_text('正在上传')
            page.wait_for_timeout(150)
            self.assertEqual(len(import_requests), 2)
            payload = json.loads(import_requests[1].request.post_data)
            self.assertEqual([item['name'] for item in payload['files']], ['deck.pptx'])
            self.assertTrue(payload['files'][0]['data'])
            import_requests[1].fulfill(json={
                'ok': True,
                'deckId': 'imported-deck',
                'deck': workspace_fixture('uploaded', 'imported-deck')['deck'],
            })
            expect(page.locator('.ga-band .ga-row b').filter(has_text='样本 imported-deck')).to_be_visible()
            self.assertEqual(len(import_requests), 2)
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()

    def test_queue_pagination_appends_in_order_dedupes_and_keeps_cross_page_selection(self):
        queue_requests = []
        first_pages = {
            'uploaded': [
                queue_item('uploaded-deck', 'uploaded', '上传首页'),
                queue_item('  padded-id  ', 'uploaded', '保留空格 ID'),
            ],
            'generated': [queue_item('generated-deck', 'generated', '生成首页')],
        }
        cursors = {'uploaded': ' cursor up 1 ', 'generated': 'cursor-gen-1'}

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            deck_id = query.get('deckId', [''])[0]
            if action == 'workspace':
                fixture = paginated_workspace(kind, deck_id or None, queues=first_pages, cursors=cursors)
                fixture['queue'] = None
                route.fulfill(json=fixture)
            elif action == 'queue-decks':
                queue_requests.append((kind, query.get('cursor', [''])[0], route))
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')

            def wait_for_queue_route_count(expected):
                for _ in range(200):
                    if len(queue_requests) >= expected:
                        return
                    page.wait_for_timeout(10)
                self.fail(f'route handler registration timed out: expected {expected}, got {len(queue_requests)}')

            uploaded_select = page.locator('select[data-queue-kind="uploaded"]')
            expect(uploaded_select).to_be_visible()
            expect(uploaded_select.locator('option[value="uploaded-deck"]')).to_have_count(1)
            expect(uploaded_select.locator('option[value="  padded-id  "]')).to_have_count(1)
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-deck', '  padded-id  '],
            )
            expect(page.locator('[data-queue-track="uploaded"]')).to_contain_text('排队 —')
            expect(page.locator('[data-queue-more]')).to_have_count(2)

            with page.expect_request(lambda request: 'action=queue-decks&kind=uploaded' in request.url):
                page.evaluate(
                    """() => {
                        const button = document.querySelector('[data-queue-more="uploaded"]');
                        button.click();
                        button.click();
                    }"""
                )
            wait_for_queue_route_count(1)
            self.assertEqual(len(queue_requests), 1, 'double click must not duplicate one track request')
            self.assertEqual(queue_requests[0][1], ' cursor up 1 ')
            expect(page.locator('[data-queue-more="uploaded"]')).to_be_disabled()
            queue_requests[0][2].fulfill(json={
                'kind': 'uploaded',
                'items': [queue_item('  padded-id  ', 'uploaded'), queue_item('uploaded-page-2', 'uploaded')],
                'nextCursor': ' cursor up 2 ',
            })
            expect(uploaded_select.locator('option[value="uploaded-page-2"]')).to_have_count(1)
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-deck', '  padded-id  ', 'uploaded-page-2'],
            )

            with page.expect_request(lambda request: 'action=queue-decks&kind=generated' in request.url):
                page.locator('[data-queue-more="generated"]').click()
            with page.expect_request(lambda request: 'action=queue-decks&kind=uploaded' in request.url):
                page.locator('[data-queue-more="uploaded"]').click()
            wait_for_queue_route_count(3)
            self.assertEqual([item[0] for item in queue_requests], ['uploaded', 'generated', 'uploaded'])
            self.assertEqual(queue_requests[2][1], ' cursor up 2 ')
            queue_requests[1][2].fulfill(json={
                'kind': 'generated',
                'items': [queue_item('generated-page-2', 'generated')],
                'nextCursor': None,
            })
            expect(page.locator('select[data-queue-kind="generated"] option[value="generated-page-2"]')).to_have_count(1)
            expect(page.locator('[data-queue-more="generated"]')).to_have_count(0)
            expect(page.locator('[data-queue-more="uploaded"]')).to_be_disabled()
            queue_requests[2][2].fulfill(json={
                'kind': 'uploaded',
                'items': [queue_item('uploaded-page-2', 'uploaded'), queue_item('uploaded-page-3', 'uploaded')],
                'nextCursor': None,
            })
            expect(page.locator('[data-queue-more="uploaded"]')).to_have_count(0)
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-deck', '  padded-id  ', 'uploaded-page-2', 'uploaded-page-3'],
            )

            with page.expect_request(lambda request: 'action=workspace&kind=uploaded&deckId=uploaded-page-2' in request.url):
                uploaded_select.select_option('uploaded-page-2')
            expect(page.locator('.ga-band .ga-row b').filter(has_text='样本 uploaded-page-2')).to_be_visible()
            self.assertEqual(uploaded_select.input_value(), 'uploaded-page-2')
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-deck', '  padded-id  ', 'uploaded-page-2', 'uploaded-page-3'],
            )

            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 生成作品')
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded&deckId=uploaded-page-2' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 上传素材')
            self.assertEqual(uploaded_select.input_value(), 'uploaded-page-2')
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-deck', '  padded-id  ', 'uploaded-page-2', 'uploaded-page-3'],
            )
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()


    def test_late_upload_response_does_not_steal_queue_navigation(self):
        import_requests = []
        workspace_requests = []

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews/import':
                import_requests.append(route)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            deck_id = query.get('deckId', [''])[0]
            if action == 'workspace':
                workspace_requests.append((kind, deck_id))
                route.fulfill(json=workspace_fixture(kind, deck_id or None, 'running'))
            elif action == 'pre-evaluation-status':
                route.fulfill(json={'preEvaluationStatus': 'running'})
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()

            page.locator('[data-import-files]').set_input_files({
                'name': 'late-upload.pptx',
                'mimeType': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                'buffer': UPLOAD_FIXTURE,
            })
            with page.expect_request(lambda request: request.url.endswith('/api/ai/reviews/import')):
                page.locator('[data-action="import"]').click()
            expect(page.locator('[data-import-status]')).to_contain_text('正在上传')
            page.wait_for_timeout(150)
            self.assertEqual(len(import_requests), 1)

            with page.expect_request(lambda request: 'action=pre-evaluation-status' in request.url):
                pass
            expect(page.locator('[data-import-summary]')).to_contain_text('late-upload.pptx')
            expect(page.locator('[data-action="import"]')).to_be_disabled()
            self.assertEqual(page.locator('[data-import-files]').evaluate('(input) => input.files.length'), 1)

            page.locator('[data-action="refresh"]').click()
            expect(page.get_by_text('样本 uploaded-deck', exact=True)).to_be_visible()
            expect(page.locator('[data-import-summary]')).to_contain_text('late-upload.pptx')
            expect(page.locator('[data-action="import"]')).to_be_disabled()
            self.assertEqual(page.locator('[data-import-files]').evaluate('(input) => input.files.length'), 1)
            self.assertEqual(page.locator('[data-import-panel]').get_attribute('aria-busy'), 'true')

            page.locator('[data-action="switch-kind"]').click()
            expect(page.get_by_text('AI 盲评 · 生成作品')).to_be_visible()
            expect(page.get_by_text('样本 generated-deck', exact=True)).to_be_visible()
            expect(page.locator('[data-import-summary]')).to_contain_text('late-upload.pptx')
            expect(page.locator('[data-action="import"]')).to_be_disabled()
            self.assertEqual(page.locator('[data-import-files]').evaluate('(input) => input.files.length'), 1)

            import_requests[0].fulfill(json={
                'ok': True,
                'deckId': 'late-imported-deck',
                'deck': workspace_fixture('uploaded', 'late-imported-deck')['deck'],
            })
            expect(page.locator('[data-import-status]')).to_contain_text('未自动打开新作品')
            expect(page.locator('[data-action="import"]')).to_be_enabled()
            expect(page.get_by_text('样本 generated-deck', exact=True)).to_be_visible()
            expect(page.get_by_text('样本 late-imported-deck', exact=True)).to_have_count(0)
            self.assertNotIn(('uploaded', 'late-imported-deck'), workspace_requests)
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()


    def test_backfill_scans_tracks_independently_with_guard_completion_and_retry(self):
        backfill_requests = []
        workspace_requests = []

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            if action == 'workspace':
                workspace_requests.append(kind)
                fixture = workspace_fixture(kind, query.get('deckId', [''])[0] or None)
                fixture['isJudge'] = True
                route.fulfill(json=fixture)
            elif action == 'backfill':
                backfill_requests.append(route)
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 上传素材')
            status = page.locator('[data-backfill-status]')
            expect(status).to_have_attribute('data-kind', 'uploaded')
            expect(status).to_have_attribute('data-status', 'idle')
            expect(status).to_contain_text('仅创建预评任务')
            expect(status).to_contain_text('不改写已有评分')

            with page.expect_request(lambda request: 'action=backfill' in request.url):
                page.evaluate(
                    """() => {
                        const button = document.querySelector('[data-action="backfill"]');
                        button.click();
                        button.click();
                    }"""
                )
            page.wait_for_timeout(100)
            self.assertEqual(len(backfill_requests), 1, 'same-track double click must create one batch request')
            first_upload_payload = json.loads(backfill_requests[0].request.post_data)
            self.assertEqual(set(first_upload_payload), {'kind', 'offset', 'limit'})
            self.assertEqual(first_upload_payload, {'kind': 'uploaded', 'offset': 0, 'limit': 8})
            self.assertEqual(backfill_requests[0].request.method, 'POST')
            expect(page.locator('[data-action="backfill"]')).to_be_disabled()
            expect(page.locator('[data-action="backfill"]')).to_have_text('补档中…')

            page.locator('[data-action="switch-kind"]').click()
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 生成作品')
            expect(status).to_have_attribute('data-kind', 'generated')
            expect(status).to_have_attribute('data-status', 'idle')

            with page.expect_request(lambda request: 'action=backfill' in request.url):
                page.evaluate(
                    """() => {
                        const button = document.querySelector('[data-action="backfill"]');
                        button.click();
                        button.click();
                    }"""
                )
            page.wait_for_timeout(100)
            self.assertEqual(len(backfill_requests), 2, 'generated track must use its own in-flight guard')
            generated_payload = json.loads(backfill_requests[1].request.post_data)
            self.assertEqual(generated_payload, {'kind': 'generated', 'offset': 0, 'limit': 8})
            backfill_requests[1].fulfill(json={
                'kind': 'generated', 'offset': 0, 'decks': 2, 'preparedUnits': 3,
                'nextOffset': None, 'totalDecks': 2,
            })
            expect(status).to_have_attribute('data-status', 'complete')
            expect(status).to_contain_text('补档扫描完成')
            expect(status).to_contain_text('累计准备 3 个预评任务')
            expect(page.locator('[data-action="backfill"]')).to_have_text('重新扫描')
            expect(page.locator('[data-action="backfill"]')).to_be_enabled()

            backfill_requests[0].fulfill(
                status=500,
                content_type='application/json',
                body=json.dumps({'error': 'late upload failure'}),
            )
            page.wait_for_timeout(100)
            self.assertEqual(workspace_requests, ['uploaded', 'generated'])
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 生成作品')
            expect(status).to_have_attribute('data-kind', 'generated')
            expect(status).to_have_attribute('data-status', 'complete')
            expect(status).not_to_contain_text('late upload failure')

            page.locator('[data-action="switch-kind"]').click()
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 上传素材')
            expect(status).to_have_attribute('data-kind', 'uploaded')
            expect(status).to_have_attribute('data-status', 'failed')
            expect(status).to_contain_text('late upload failure')
            expect(status).to_contain_text('本批未推进')
            expect(page.locator('[data-action="backfill"]')).to_be_enabled()

            with page.expect_request(lambda request: 'action=backfill' in request.url):
                page.locator('[data-action="backfill"]').click()
            page.wait_for_timeout(100)
            self.assertEqual(len(backfill_requests), 3)
            retry_payload = json.loads(backfill_requests[2].request.post_data)
            self.assertEqual(retry_payload['offset'], 0, 'failed batch must remain retryable at the same offset')
            backfill_requests[2].fulfill(json={
                'kind': 'uploaded', 'offset': 0, 'decks': 8, 'preparedUnits': 9,
                'nextOffset': 8, 'totalDecks': 16,
            })
            expect(status).to_have_attribute('data-status', 'partial')
            expect(status).to_contain_text('已扫描至第 8 套 / 16')
            expect(status).to_contain_text('累计准备 9 个预评任务')
            expect(page.locator('[data-action="backfill"]')).to_have_text('继续补档')

            with page.expect_request(lambda request: 'action=backfill' in request.url):
                page.locator('[data-action="backfill"]').click()
            page.wait_for_timeout(100)
            self.assertEqual(len(backfill_requests), 4)
            next_payload = json.loads(backfill_requests[3].request.post_data)
            self.assertEqual(next_payload['offset'], 8, 'track progress must advance only after a successful batch')
            backfill_requests[3].fulfill(json={
                'kind': 'uploaded', 'offset': 8, 'decks': 8, 'preparedUnits': 4,
                'nextOffset': None, 'totalDecks': 16,
            })
            expect(status).to_have_attribute('data-status', 'complete')
            expect(status).to_contain_text('共检查 16 套')
            expect(status).to_contain_text('累计准备 13 个预评任务')
            expect(page.locator('[data-action="backfill"]')).to_have_text('重新扫描')

            with page.expect_request(lambda request: 'action=backfill' in request.url):
                page.locator('[data-action="backfill"]').click()
            page.wait_for_timeout(100)
            self.assertEqual(len(backfill_requests), 5)
            rescan_payload = json.loads(backfill_requests[4].request.post_data)
            self.assertEqual(rescan_payload['offset'], 0, 'completed track must rescan only on explicit click')
            expect(status).to_have_attribute('data-status', 'fetching')
            backfill_requests[4].fulfill(json={
                'kind': 'uploaded', 'offset': 0, 'decks': 0, 'preparedUnits': 0,
                'nextOffset': None, 'totalDecks': 0,
            })
            expect(status).to_have_attribute('data-status', 'complete')
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            browser.close()

    def test_queue_pagination_retries_stale_responses_and_refresh_resets_to_new_head(self):
        queue_requests = []
        first_pages = {
            'uploaded': [queue_item('uploaded-deck', 'uploaded', '上传首页')],
            'generated': [queue_item('generated-deck', 'generated', '生成首页')],
        }
        cursors = {'uploaded': 'fresh-c1', 'generated': 'gen-c1'}

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
            if url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            query = parse_qs(url.query)
            action = query.get('action', [''])[0]
            kind = query.get('kind', ['uploaded'])[0]
            deck_id = query.get('deckId', [''])[0]
            if action == 'workspace':
                route.fulfill(json=paginated_workspace(kind, deck_id or None, queues=first_pages, cursors=cursors))
            elif action == 'queue-decks':
                queue_requests.append((kind, query.get('cursor', [''])[0], route))
            else:
                route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://review.test/')
            uploaded_select = page.locator('select[data-queue-kind="uploaded"]')
            expect(uploaded_select).to_be_visible()

            def click_more(kind):
                before = len(queue_requests)
                with page.expect_request(lambda request, kind=kind: f'action=queue-decks&kind={kind}' in request.url):
                    page.locator(f'[data-queue-more="{kind}"]').click()
                for _ in range(100):
                    if len(queue_requests) > before:
                        return queue_requests[before]
                    page.wait_for_timeout(10)
                self.fail(f'{kind} queue request was not retained')

            stale_refresh = click_more('uploaded')
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="refresh"]').click()
            stale_refresh[2].fulfill(json={
                'kind': 'uploaded',
                'items': [queue_item('stale-after-refresh', 'uploaded')],
                'nextCursor': 'stale-c2',
            })
            page.wait_for_timeout(100)
            expect(page.locator('option[value="stale-after-refresh"]')).to_have_count(0)
            expect(page.locator('[data-queue-more="uploaded"]')).to_be_enabled()

            stale_switch = click_more('uploaded')
            with page.expect_request(lambda request: 'action=workspace&kind=generated' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 生成作品')
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="switch-kind"]').click()
            expect(page.locator('.ga-shell > .ga-head h2')).to_have_text('AI 盲评 · 上传素材')
            stale_switch[2].fulfill(json={
                'kind': 'uploaded',
                'items': [queue_item('stale-after-switch', 'uploaded')],
                'nextCursor': 'stale-c3',
            })
            page.wait_for_timeout(100)
            expect(page.locator('option[value="stale-after-switch"]')).to_have_count(0)
            expect(page.locator('[data-queue-more="uploaded"]')).to_be_enabled()

            failure = click_more('uploaded')
            self.assertEqual(failure[1], 'fresh-c1')
            failure[2].fulfill(status=500, json={'error': 'queue offline'})
            expect(page.locator('[data-queue-error="uploaded"]')).to_contain_text('queue offline')
            expect(page.locator('[data-queue-more="uploaded"]')).to_have_text('加载更多')
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-deck'],
            )

            retry = click_more('uploaded')
            self.assertEqual(retry[1], 'fresh-c1')
            retry[2].fulfill(json={
                'kind': 'uploaded',
                'items': [queue_item('uploaded-page-2', 'uploaded')],
                'nextCursor': 'cursor-c2',
            })
            expect(page.locator('[data-queue-error="uploaded"]')).to_have_count(0)
            expect(page.locator('option[value="uploaded-page-2"]')).to_have_count(1)

            final_page = click_more('uploaded')
            self.assertEqual(final_page[1], 'cursor-c2')
            final_page[2].fulfill(json={
                'kind': 'uploaded',
                'items': [queue_item('uploaded-page-3', 'uploaded')],
                'nextCursor': None,
            })
            expect(page.locator('[data-queue-more="uploaded"]')).to_have_count(0)

            with page.expect_request(lambda request: 'action=workspace&kind=uploaded&deckId=uploaded-page-2' in request.url):
                uploaded_select.select_option('uploaded-page-2')
            expect(page.locator('.ga-band .ga-row b').filter(has_text='样本 uploaded-page-2')).to_be_visible()

            first_pages['uploaded'] = [
                queue_item('uploaded-deck', 'uploaded', '上传首页'),
                queue_item('uploaded-new-head', 'uploaded', '后来插入首页之后'),
            ]
            cursors['uploaded'] = 'fresh-c1-reset'
            request_count = len(queue_requests)
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded' in request.url):
                page.locator('[data-action="refresh"]').click()
            expect(page.locator('option[value="uploaded-new-head"]')).to_have_count(1)
            self.assertEqual(len(queue_requests), request_count, 'refresh must reset to the new head without crawling the old chain')
            self.assertEqual(uploaded_select.input_value(), 'uploaded-page-2')
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-page-2', 'uploaded-deck', 'uploaded-new-head'],
            )
            expect(page.locator('[data-queue-more="uploaded"]')).to_have_text('加载更多')

            resumed = click_more('uploaded')
            self.assertEqual(resumed[1], 'fresh-c1-reset')
            resumed[2].fulfill(json={
                'kind': 'uploaded',
                'items': [
                    queue_item('uploaded-page-2', 'uploaded'),
                    queue_item('uploaded-late-page', 'uploaded'),
                ],
                'nextCursor': None,
            })
            expect(page.locator('option[value="uploaded-late-page"]')).to_have_count(1)
            expect(page.locator('[data-queue-more="uploaded"]')).to_have_count(0)
            self.assertEqual(uploaded_select.input_value(), 'uploaded-page-2')
            self.assertEqual(
                uploaded_select.locator('option').evaluate_all('(options) => options.map((option) => option.value)'),
                ['uploaded-deck', 'uploaded-new-head', 'uploaded-page-2', 'uploaded-late-page'],
            )

            destroyed_request = click_more('generated')
            page.evaluate('window.reviewInstance.destroy()')
            destroyed_request[2].fulfill(json={
                'kind': 'generated',
                'items': [queue_item('stale-after-destroy', 'generated')],
                'nextCursor': None,
            })
            page.wait_for_timeout(100)
            self.assertEqual(page.locator('#review').inner_html(), '')
            self.assertEqual(errors, [])
            browser.close()


if __name__ == '__main__':
    unittest.main()
