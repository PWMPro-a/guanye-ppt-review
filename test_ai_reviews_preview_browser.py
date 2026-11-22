"""Edge regression for the 360px whole-deck preview workflow."""

import base64
import json
import os
from pathlib import Path
import re
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
window.__previewCalls = [];
window.__previewBatches = [];
window.__previewWaiters = {};
window.__previewPinned = {};
let previewBatchStart = 0;
let previewBatchScheduled = false;
const previewKey = value => typeof value === 'string' ? value : value?.path || value?.name || String(value);
const previewRequestScope = () => document.querySelector('[data-preview-shell]') ? 'deck' : 'page';
window.__resetPreviewHistory = () => {
  window.__previewCalls = [];
  window.__previewBatches = [];
  previewBatchStart = 0;
};
window.Cloud = {
  imageUrl: value => new Promise((resolve, reject) => {
    const path = previewKey(value);
    window.__previewCalls.push(path);
    if (!previewBatchScheduled) {
      previewBatchScheduled = true;
      queueMicrotask(() => {
        const batch = window.__previewCalls.slice(previewBatchStart);
        previewBatchStart = window.__previewCalls.length;
        if (batch.length) window.__previewBatches.push(batch);
        previewBatchScheduled = false;
      });
    }
    const waiter = {resolve, reject, scope: previewRequestScope(), cached: Boolean(window.__previewPinned[path])};
    (window.__previewWaiters[path] ||= []).push(waiter);
  }),
};
window.__previewWaiterCount = (path, scope) => (window.__previewWaiters[path] || [])
  .filter(waiter => !scope || waiter.scope === scope).length;
window.__resolvePreview = (path, url, scope) => {
  const waiters = window.__previewWaiters[path] || [];
  const index = waiters.map((waiter, position) => ({waiter, position}))
    .filter(row => !scope || row.waiter.scope === scope)
    .map(row => row.position)
    .pop();
  if (index === undefined) throw new Error('missing preview waiter: ' + path + (scope ? ' [' + scope + ']' : ''));
  const [waiter] = waiters.splice(index, 1);
  waiter.resolve(url);
  return waiter.scope;
};
window.__rejectPreview = (path, message, scope) => {
  const waiters = window.__previewWaiters[path] || [];
  const index = waiters.map((waiter, position) => ({waiter, position}))
    .filter(row => !scope || row.waiter.scope === scope)
    .map(row => row.position)
    .pop();
  if (index === undefined) throw new Error('missing preview waiter: ' + path + (scope ? ' [' + scope + ']' : ''));
  const [waiter] = waiters.splice(index, 1);
  waiter.reject(new Error(message));
  return waiter.scope;
};
</script><script src="/ai-reviews.js"></script>
<script>window.reviewInstance = window.GuanyeReviews.mount(document.querySelector('#review'), window.__reviewMountOptions || {deckId: 'mobile-preview-deck'});</script>
</body></html>'''


def preview_workspace(page_count=3):
    image = lambda unit_id, name, position: {
        'id': unit_id,
        'name': name,
        'path': f'preview/{unit_id}.png',
        'thumbPath': f'preview/{unit_id}.png',
        'position': position,
    }
    labels = {1: '封面页', 2: '数据页', 3: '总结页'}
    images = [
        image(f'p{position}', labels.get(position, f'第 {position} 页'), position)
        for position in range(page_count, 0, -1)
    ]
    return {
        'deck': {
            'id': 'mobile-preview-deck',
            'data': {
                'title': '移动预览样本',
                'images': images,
            },
        },
        'preEvaluationStatus': 'completed',
        'deckVersion': 'mobile-preview-deck-v1',
        'rubricVersion': 'r1',
        'isJudge': False,
        'workspaceSettings': {
            'dimensions': [{'id': 'visual', 'name': '视觉效果'}, {'id': 'layout', 'name': '排版'}],
            'maxScore': 5,
        },
        'queues': {
            'uploaded': [{'id': 'mobile-preview-deck', 'title': '移动预览样本'}],
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


@unittest.skipUnless(sync_playwright and EDGE.is_file(), 'requires Playwright and Microsoft Edge')
class ReviewPreviewBrowserTest(unittest.TestCase):
    def assert_page_announcement(self, page, current, total):
        pattern = re.compile(
            rf'(?:^|\D){current}\s*/\s*{total}(?:\D|$)|第\s*{current}\s*/\s*{total}\s*页'
        )
        page.wait_for_function(
            """([current, total]) => {
                const pattern = new RegExp(`(?:^|\\D)${current}\\s*/\\s*${total}(?:\\D|$)|第\\s*${current}\\s*/\\s*${total}\\s*页`);
                return Array.from(document.querySelectorAll('[aria-live="polite"], [role="status"]')).some(node => {
                    return pattern.test(node.textContent || node.getAttribute('aria-label') || '');
                });
            }""",
            arg=[current, total],
        )
        self.assertRegex(page.locator('body').inner_text(), pattern)

    def wait_for_waiter(self, page, path, scope=None):
        page.wait_for_function(
            '({path, scope}) => window.__previewWaiterCount(path, scope) >= 1',
            arg={'path': path, 'scope': scope},
        )

    def resolve_preview(self, page, path, scope=None):
        page.evaluate(
            "({path, scope}) => window.__resolvePreview(path, '/signed/' + path, scope)",
            {'path': path, 'scope': scope},
        )

    def resolve_preview_url(self, page, path, url, scope=None):
        page.evaluate(
            "({path, url, scope}) => window.__resolvePreview(path, url, scope)",
            {'path': path, 'url': url, 'scope': scope},
        )

    def settle_preview(self, page, path, scope=None):
        page.wait_for_function(
            """({path, scope}) => Array.from(document.querySelectorAll('.ga-preview img')).some(image => image.src.includes(path))
                || window.__previewWaiterCount(path, scope) >= 1""",
            arg={'path': path, 'scope': scope},
        )
        if page.evaluate(
            '({path, scope}) => window.__previewWaiterCount(path, scope) > 0',
            {'path': path, 'scope': scope},
        ):
            page.evaluate(
                """({path, scope}) => window.__resolvePreview(path, '/signed/' + path, scope)""",
                {'path': path, 'scope': scope},
            )
        expect(page.locator(f'.ga-preview img[src*="/{path}"]')).to_be_visible()

    def assert_quick_targets(self, page):
        targets = page.locator('.ga-quick .ga-pill')
        self.assertGreater(targets.count(), 0)
        sizes = targets.evaluate_all(
            """elements => elements.map(element => {
                const box = element.getBoundingClientRect();
                return {width: box.width, height: box.height};
            })"""
        )
        for size in sizes:
            self.assertGreaterEqual(size['width'] + 0.01, 24, size)
            self.assertGreaterEqual(size['height'] + 0.01, 24, size)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 360)

    def test_360px_deck_preview_navigation_signature_failure_lightbox_and_submit_retry(self):
        submissions = []
        workspace = preview_workspace()

        def respond(route):
            url = urlsplit(route.request.url)
            if url.hostname != 'review.test':
                route.abort()
                return
            if url.path == '/':
                route.fulfill(content_type='text/html; charset=utf-8', body=PAGE)
                return
            if url.path == '/ai-reviews.js':
                route.fulfill(
                    content_type='application/javascript; charset=utf-8',
                    body=SCRIPT.read_text(encoding='utf-8'),
                )
                return
            if url.path.startswith('/signed/') or url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews':
                query = parse_qs(url.query)
                action = query.get('action', [''])[0]
                if action == 'workspace':
                    route.fulfill(json=workspace)
                    return
                if action == 'submit':
                    submissions.append(json.loads(route.request.post_data))
                    if len(submissions) == 1:
                        route.fulfill(status=503, json={'error': '评分提交暂时失败'})
                    else:
                        route.fulfill(json={'ok': True, 'message': '人工评分已保存'})
                    return
            route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page = browser.new_page(
                    viewport={'width': 360, 'height': 800},
                    is_mobile=True,
                    has_touch=True,
                )
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', respond)
                page.goto('http://review.test/')
                expect(page.get_by_text('移动预览样本', exact=True).first).to_be_visible()
                expect(page.locator('.ga-preview-shell')).to_have_attribute('role', 'group')
                expect(page.locator('.ga-preview-controls')).to_have_attribute('role', 'group')
                self.assert_quick_targets(page)

                page.wait_for_function('() => window.__previewCalls.length >= 1')
                self.assertEqual(
                    page.evaluate('window.__previewCalls'),
                    ['preview/p1.png'],
                    'whole-deck preview must sign only the position-1 image',
                )
                self.wait_for_waiter(page, 'preview/p1.png', 'deck')
                self.resolve_preview(page, 'preview/p1.png', 'deck')
                expect(page.locator('.ga-preview img[src*="/preview/p1.png"]')).to_be_visible()
                self.assert_page_announcement(page, 1, 3)

                previous = page.get_by_role('button', name='上一页', exact=True)
                next_page = page.get_by_role('button', name='下一页', exact=True)
                expect(previous).to_be_visible()
                expect(previous).to_be_disabled()
                expect(next_page).to_be_enabled()

                score = page.locator('[data-score-unit=""][data-dim="visual"]')
                note = page.locator('[data-note-unit=""]')
                score.fill('4.5')
                note.fill('整套评分草稿')
                next_page.click()
                self.wait_for_waiter(page, 'preview/p2.png', 'deck')
                self.assertNotIn('preview/p3.png', page.evaluate('window.__previewCalls'))
                self.assert_page_announcement(page, 2, 3)
                expect(score).to_have_value('4.5')
                expect(note).to_have_value('整套评分草稿')

                page.evaluate(
                    "({path, message}) => window.__rejectPreview(path, message)",
                    {'path': 'preview/p2.png', 'message': '预览签名暂时失败'},
                )
                retry_preview = page.get_by_role('button', name='重试预览', exact=True)
                expect(retry_preview).to_be_visible()
                expect(score).to_have_value('4.5')
                expect(note).to_have_value('整套评分草稿')

                retry_preview.click()
                page.wait_for_function(
                    'path => window.__previewCalls.filter(value => value === path).length === 2',
                    arg='preview/p2.png',
                )
                self.assertNotIn('preview/p3.png', page.evaluate('window.__previewCalls'))
                self.settle_preview(page, 'preview/p2.png', 'deck')
                self.assert_page_announcement(page, 2, 3)

                next_page.click()
                self.wait_for_waiter(page, 'preview/p3.png', 'deck')
                self.assert_page_announcement(page, 3, 3)
                expect(previous).to_be_enabled()
                previous.click()
                self.settle_preview(page, 'preview/p2.png', 'deck')

                self.resolve_preview(page, 'preview/p3.png', 'deck')
                page.wait_for_timeout(150)
                expect(page.locator('.ga-preview img[src*="/preview/p3.png"]')).to_have_count(0)
                expect(page.locator('.ga-preview img[src*="/preview/p2.png"]')).to_be_visible()
                self.assert_page_announcement(page, 2, 3)

                shortcut = page.get_by_role('button', name='给这一页打分', exact=True)
                shortcut.focus()
                page.keyboard.press('Enter')
                page_scope = page.get_by_role('button', name='选页', exact=True)
                deck_scope = page.get_by_role('button', name='整套', exact=True)
                p2_card = page.locator('[data-unit-card="p2"]')
                p2_score = p2_card.locator('[data-dim="visual"]')
                expect(page_scope).to_have_attribute('aria-pressed', 'true')
                expect(p2_card.get_by_role('checkbox')).to_be_checked()
                expect(p2_score).to_be_focused()
                for other_id in ('p1', 'p3'):
                    expect(page.locator(f'[data-unit-card="{other_id}"]').get_by_role('checkbox')).not_to_be_checked()
                p2_score.fill('4')
                expect(p2_score).to_have_value('4')
                self.assertEqual(submissions, [])

                deck_scope.click()
                self.assert_page_announcement(page, 2, 3)
                expect(score).to_have_value('4.5')
                expect(note).to_have_value('整套评分草稿')
                shortcut.click()
                expect(p2_score).to_be_focused()
                expect(p2_score).to_have_value('4')
                deck_scope.click()
                self.assert_page_announcement(page, 2, 3)
                page.evaluate('''() => {
                    const path = 'preview/p2.png';
                    while ((window.__previewWaiters[path] || []).length) {
                        window.__resolvePreview(path, '/signed/' + path);
                    }
                }''')
                self.settle_preview(page, 'preview/p2.png', 'deck')
                expect(score).to_have_value('4.5')
                expect(note).to_have_value('整套评分草稿')

                next_page.click()
                self.settle_preview(page, 'preview/p3.png', 'deck')
                self.assert_page_announcement(page, 3, 3)
                expect(score).to_have_value('4.5')
                expect(note).to_have_value('整套评分草稿')

                open_preview = page.get_by_role('button', name='放大预览', exact=True)
                expect(open_preview).to_be_visible()
                open_preview.click()
                dialog = page.locator('[role="dialog"], dialog[open]').first
                expect(dialog).to_be_visible()
                close_preview = page.get_by_role('button', name='关闭大图', exact=True)
                expect(close_preview).to_be_visible()
                page.keyboard.press('Escape')
                expect(dialog).to_be_hidden()
                expect(open_preview).to_be_focused()

                submit = page.get_by_role('button', name='提交所选人工评分', exact=True)
                submit.click()
                expect(page.locator('[data-review-status]')).to_contain_text('评分提交暂时失败')
                expect(score).to_have_value('4.5')
                expect(note).to_have_value('整套评分草稿')
                submit.click()
                page.wait_for_function('() => true')
                expect(page.locator('[data-review-status]')).to_contain_text('人工评分已保存')

                self.assertEqual(len(submissions), 2)
                for payload in submissions:
                    self.assertEqual(payload['scope'], 'deck')
                    self.assertEqual(payload['units'][0]['scores'], {'visual': 4.5})
                    self.assertEqual(payload['units'][0]['note'], '整套评分草稿')

                previous.click()
                self.assert_page_announcement(page, 2, 3)
                shortcut.click()
                expect(p2_score).to_be_focused()
                expect(p2_score).to_have_value('4')
                submit.click()
                page.wait_for_function('() => document.querySelector("[data-review-status]").textContent.includes("人工评分已保存")')
                self.assertEqual(len(submissions), 3)
                self.assertEqual(submissions[2]['scope'], 'page')
                self.assertEqual([unit['unitId'] for unit in submissions[2]['units']], ['p2'])
                self.assertEqual(submissions[2]['units'][0]['scores'], {'visual': 4})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 360)
                self.assertEqual(errors, [])
            finally:
                browser.close()




    def test_single_judge_bundle_submits_deck_and_selected_page_once(self):
        submissions = []
        submit_routes = []
        workspace_calls = []
        workspace = preview_workspace()

        def respond(route):
            url = urlsplit(route.request.url)
            if url.hostname != 'review.test':
                route.abort()
                return
            if url.path == '/':
                route.fulfill(content_type='text/html; charset=utf-8', body=PAGE)
                return
            if url.path == '/ai-reviews.js':
                route.fulfill(
                    content_type='application/javascript; charset=utf-8',
                    body=SCRIPT.read_text(encoding='utf-8'),
                )
                return
            if url.path.startswith('/signed/') or url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews':
                action = parse_qs(url.query).get('action', [''])[0]
                if action == 'workspace':
                    workspace_calls.append(route.request.url)
                    route.fulfill(json=workspace)
                    return
                if action == 'submit-bundle':
                    submissions.append(json.loads(route.request.post_data))
                    submit_routes.append(route)
                    return
            route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page = browser.new_page(
                    viewport={'width': 360, 'height': 800},
                    is_mobile=True,
                    has_touch=True,
                )
                page.add_init_script(
                    """window.__reviewMountOptions = {
                        deckId: 'mobile-preview-deck',
                        singleJudge: true,
                    };"""
                )
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', respond)
                page.goto('http://review.test/')
                expect(page.get_by_text('移动预览样本', exact=True).first).to_be_visible()

                submit = page.locator('[data-action="submit"]')
                expect(submit).to_be_disabled()

                page.locator('[data-score-unit=""][data-dim="visual"]').fill('4')
                page.locator('[data-note-unit=""]').fill('整套草稿')
                expect(submit).to_be_disabled()
                page.locator('[data-score-unit=""][data-dim="layout"]').fill('0')
                expect(submit).to_be_enabled()  # Page scores are optional; zero is a valid score.

                page.get_by_role('button', name='选页', exact=True).click()
                page.locator('[data-select-unit="p2"]').check()
                expect(submit).to_be_disabled()
                page.locator('[data-score-unit="p2"][data-dim="visual"]').fill('4.5')
                page.locator('[data-note-unit="p2"]').fill('第二页草稿')
                expect(submit).to_be_disabled()
                page.locator('[data-score-unit="p2"][data-dim="layout"]').fill('4')
                expect(submit).to_be_enabled()
                self.assertEqual(submissions, [])

                with page.expect_request(
                    lambda request: 'action=submit-bundle' in request.url
                    and request.method == 'POST'
                ):
                    submit.click()
                expect(submit).to_be_disabled()
                expect(submit).to_have_attribute('aria-busy', 'true')
                self.assertEqual(len(submit_routes), 1)

                page.locator('[data-note-unit="p2"]').fill('提交中修改')
                submit_routes[0].fulfill(json={'ok': True, 'message': '整套与选页评分已保存'})
                expect(page.locator('[data-review-status]')).to_contain_text('整套与选页评分已保存')

                self.assertEqual(len(submissions), 1)
                body = submissions[0]
                self.assertNotIn('scope', body)
                self.assertEqual(body['deckId'], 'mobile-preview-deck')
                self.assertEqual(body['deckVersion'], 'mobile-preview-deck-v1')
                self.assertEqual(body['rubricVersion'], 'r1')
                self.assertEqual(
                    [(unit['scope'], unit['unitId']) for unit in body['units']],
                    [('deck', ''), ('page', 'p2')],
                )
                self.assertEqual(body['units'][0]['scores'], {'visual': 4, 'layout': 0})
                self.assertEqual(body['units'][0]['note'], '整套草稿')
                self.assertEqual(body['units'][1]['scores'], {'visual': 4.5, 'layout': 4})
                self.assertEqual(body['units'][1]['note'], '第二页草稿')
                self.assertNotIn('p1', [unit['unitId'] for unit in body['units']])
                self.assertNotIn('p3', [unit['unitId'] for unit in body['units']])

                expect(page.locator('[data-note-unit="p2"]')).to_have_value('提交中修改')
                page.get_by_role('button', name='整套', exact=True).click()
                expect(page.locator('[data-note-unit=""]')).to_have_value('')
                expect(page.locator('[data-score-unit=""][data-dim="visual"]')).to_have_value('')
                page.wait_for_timeout(100)
                self.assertEqual(len(submissions), 1)
                self.assertEqual(len(workspace_calls), 2)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_keyboard_scope_selection_submit_live_region_and_focus(self):
        workspace = preview_workspace()
        odd_unit_id = "页:01'任意"
        workspace['deck']['data']['images'][-1]['id'] = odd_unit_id

        def respond(route):
            url = urlsplit(route.request.url)
            if url.hostname != 'review.test':
                route.abort()
                return
            if url.path == '/':
                route.fulfill(content_type='text/html; charset=utf-8', body=PAGE)
                return
            if url.path == '/ai-reviews.js':
                route.fulfill(
                    content_type='application/javascript; charset=utf-8',
                    body=SCRIPT.read_text(encoding='utf-8'),
                )
                return
            if url.path.startswith('/signed/') or url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews':
                if parse_qs(url.query).get('action', [''])[0] == 'workspace':
                    route.fulfill(json=workspace)
                    return
            route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page = browser.new_page(
                    viewport={'width': 360, 'height': 800},
                    is_mobile=True,
                    has_touch=True,
                )
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', respond)
                page.goto('http://review.test/')
                expect(page.get_by_text('移动预览样本', exact=True).first).to_be_visible()
                self.wait_for_waiter(page, 'preview/p1.png')
                self.resolve_preview(page, 'preview/p1.png')

                scope_group = page.get_by_role('group', name='评分范围', exact=True)
                deck_scope = scope_group.get_by_role('button', name='整套', exact=True)
                page_scope = scope_group.get_by_role('button', name='选页', exact=True)
                expect(deck_scope).to_have_attribute('aria-pressed', 'true')
                expect(page_scope).to_have_attribute('aria-pressed', 'false')

                deck_scope.focus()
                page.keyboard.press('Tab')
                expect(page_scope).to_be_focused()
                page.keyboard.press('Space')
                expect(page_scope).to_have_attribute('aria-pressed', 'true')
                expect(deck_scope).to_have_attribute('aria-pressed', 'false')
                expect(page_scope).to_be_focused()

                p1_card = page.locator('[data-unit-card]').first
                p1_checkbox = p1_card.get_by_role('checkbox')
                p1_checkbox.focus()
                page.evaluate(
                    """() => {
                        window.__selectedCard = document.querySelectorAll('[data-unit-card]')[0];
                    }"""
                )
                page.keyboard.press('Space')
                expect(p1_checkbox).to_be_checked()
                expect(p1_checkbox).to_be_focused()
                self.assertTrue(
                    page.evaluate(
                        """() => window.__selectedCard === document.querySelectorAll('[data-unit-card]')[0]"""
                    ),
                    '勾选单页必须保留现有单元卡片节点，避免整页 render 替换导致焦点丢失',
                )
                p1_card.locator('[data-score-unit]').first.fill('4.5')

                page.evaluate(
                    """() => {
                        window.__submitCalls = [];
                        window.__submitBodies = [];
                        const originalFetch = window.fetch;
                        window.fetch = (input, init = {}) => {
                            const url = String(input);
                            if (url.includes('/api/ai/reviews?action=submit') && init.method === 'POST') {
                                window.__submitBodies.push(JSON.parse(init.body));
                                return new Promise((resolve, reject) => {
                                    window.__submitCalls.push({resolve, reject});
                                });
                            }
                            return originalFetch(input, init);
                        };
                    }"""
                )
                submit = page.locator('[data-action="submit"]')
                expect(submit).to_be_enabled()
                submit.focus()
                page.keyboard.press('Enter')
                page.wait_for_function('() => window.__submitCalls.length === 1')
                expect(submit).to_be_disabled()
                expect(submit).to_have_attribute('aria-busy', 'true')
                live = page.locator('[data-review-live]')
                expect(live).to_have_attribute('role', 'status')
                expect(live).to_have_attribute('aria-live', 'polite')
                expect(live).to_have_attribute('aria-atomic', 'true')
                expect(live).to_contain_text('正在提交人工评分…')

                page.evaluate(
                    """() => {
                        window.__submitCalls.shift().resolve(new Response(
                            JSON.stringify({error: '评分提交暂时失败'}),
                            {status: 503, headers: {'content-type': 'application/json'}},
                        ));
                    }"""
                )
                expect(page.locator('[data-review-status]')).to_contain_text('评分提交暂时失败')
                expect(live).to_contain_text('评分提交暂时失败')
                expect(submit).to_be_enabled()
                expect(submit).to_be_focused()

                page.keyboard.press('Enter')
                page.wait_for_function('() => window.__submitCalls.length === 1')
                expect(live).to_contain_text('正在提交人工评分…')
                page.keyboard.press('Tab')
                moved_focus = page.evaluate('document.activeElement.outerHTML')
                self.assertNotIn('data-action="submit"', moved_focus)
                page.evaluate(
                    """() => {
                        window.__submitCalls.shift().resolve(new Response(
                            JSON.stringify({error: '评分提交仍然失败'}),
                            {status: 503, headers: {'content-type': 'application/json'}},
                        ));
                    }"""
                )
                expect(live).to_contain_text('评分提交仍然失败')
                self.assertEqual(page.evaluate('document.activeElement.outerHTML'), moved_focus)
                expect(submit).to_be_enabled()

                submit.focus()
                page.evaluate(
                    """() => {
                        window.__liveRegionBefore = document.querySelector('[data-review-live]');
                    }"""
                )
                page.keyboard.press('Enter')
                page.wait_for_function('() => window.__submitCalls.length === 1')
                page.evaluate(
                    """() => {
                        window.__submitCalls.shift().resolve(new Response(
                            JSON.stringify({ok: true, message: '人工评分已保存'}),
                            {status: 200, headers: {'content-type': 'application/json'}},
                        ));
                    }"""
                )
                expect(live).to_contain_text('人工评分已保存')
                expect(page.locator('[data-review-status]')).to_be_focused()
                self.assertTrue(
                    page.evaluate(
                        """() => window.__liveRegionBefore === document.querySelector('[data-review-live]')
                            && window.__liveRegionBefore.isConnected"""
                    ),
                    '提交状态 live region 必须在成功重渲染后保持同一节点',
                )
                moved_during_success = page.locator('[data-unit-card]').first.locator('[data-score-unit]').first
                submit.focus()
                page.keyboard.press('Enter')
                page.wait_for_function('() => window.__submitCalls.length === 1')
                moved_during_success.focus()
                expect(moved_during_success).to_be_focused()
                page.evaluate(
                    """() => {
                        window.__submitCalls.shift().resolve(new Response(
                            JSON.stringify({ok: true, message: '人工评分已保存'}),
                            {status: 200, headers: {'content-type': 'application/json'}},
                        ));
                    }"""
                )
                expect(live).to_contain_text('人工评分已保存')
                expect(moved_during_success).to_be_focused()

                for body in page.evaluate('window.__submitBodies'):
                    self.assertEqual(body['scope'], 'page')
                    self.assertEqual(body['units'][0]['unitId'], odd_unit_id)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_360px_page_preview_observes_visible_batch_and_destroy_race(self):
        submissions = []
        signed_p2_requests = []
        workspace = preview_workspace(page_count=5)

        def respond(route):
            url = urlsplit(route.request.url)
            if url.hostname != 'review.test':
                route.abort()
                return
            if url.path == '/':
                route.fulfill(content_type='text/html; charset=utf-8', body=PAGE)
                return
            if url.path == '/ai-reviews.js':
                route.fulfill(
                    content_type='application/javascript; charset=utf-8',
                    body=SCRIPT.read_text(encoding='utf-8'),
                )
                return
            if url.path == '/signed/preview/p2.png':
                signed_p2_requests.append(route.request.url)
                route.fulfill(
                    content_type='image/png',
                    body=PREVIEW if len(signed_p2_requests) > 1 else b'not a valid png',
                )
                return
            if url.path.startswith('/signed/') or url.path == '/preview.png':
                route.fulfill(content_type='image/png', body=PREVIEW)
                return
            if url.path == '/api/ai/reviews':
                action = parse_qs(url.query).get('action', [''])[0]
                if action == 'workspace':
                    route.fulfill(json=workspace)
                    return
                if action == 'submit':
                    submissions.append(json.loads(route.request.post_data))
                    route.fulfill(json={'ok': True, 'message': '单页评分已保存'})
                    return
            route.fulfill(status=404, json={'error': 'unhandled fixture request'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            try:
                page = browser.new_page(
                    viewport={'width': 360, 'height': 800},
                    is_mobile=True,
                    has_touch=True,
                )
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', respond)
                page.goto('http://review.test/')
                expect(page.get_by_text('移动预览样本', exact=True).first).to_be_visible()

                self.wait_for_waiter(page, 'preview/p1.png')
                self.resolve_preview(page, 'preview/p1.png')
                expect(page.locator('.ga-preview img[src*="/preview/p1.png"]')).to_be_visible()
                page.wait_for_timeout(80)
                page.evaluate('window.__resetPreviewHistory()')
                page.add_style_tag(content='.ga-grid > .ga-card{min-height:760px;}')

                page.get_by_role('button', name='选页', exact=True).click()
                p1_card = page.locator('[data-unit-card="p1"]')
                expect(p1_card).to_be_visible()
                p1_card.scroll_into_view_if_needed()
                self.wait_for_waiter(page, 'preview/p1.png')
                self.wait_for_waiter(page, 'preview/p2.png')
                page.wait_for_timeout(100)

                batches = page.evaluate('window.__previewBatches')
                initial_paths = [path for batch in batches for path in batch]
                self.assertIn('preview/p1.png', initial_paths)
                self.assertIn('preview/p2.png', initial_paths)
                self.assertNotIn('preview/p3.png', initial_paths)
                self.assertTrue(
                    any(
                        'preview/p1.png' in batch and 'preview/p2.png' in batch
                        for batch in batches
                    ),
                    f'visible page previews must share one synchronous signing batch: {batches}',
                )

                self.resolve_preview(page, 'preview/p1.png')
                p1_preview = p1_card.locator('.ga-preview')
                expect(p1_preview).to_have_attribute('data-loaded', '1')
                page.evaluate(
                    """() => {
                        window.__p1PreviewImage = document.querySelector(
                            '[data-unit-card="p1"] .ga-preview img'
                        );
                    }"""
                )
                p1_score = page.locator('[data-score-unit="p1"][data-dim="visual"]')
                p1_note = page.locator('[data-note-unit="p1"]')
                p1_score.fill('4.5')
                p1_note.fill('相邻页评分草稿')

                page.evaluate(
                    "({path, message}) => window.__rejectPreview(path, message)",
                    {'path': 'preview/p2.png', 'message': '单页预览签名失败'},
                )
                expect(page.locator('.ga-preview img[src*="/preview/p1.png"]')).to_be_visible()
                p2_card = page.locator('[data-unit-card="p2"]')
                p2_preview = p2_card.locator('.ga-preview')
                expect(p2_card.get_by_text('预览暂不可用', exact=True)).to_be_visible()
                expect(p2_preview).to_have_attribute('data-loaded', '0')

                p2_retry = p2_card.get_by_role('button', name='重试预览', exact=True)
                expect(p2_retry).to_be_visible()
                calls_before_retry = page.evaluate('window.__previewCalls')
                p2_retry.click()
                self.wait_for_waiter(page, 'preview/p2.png')
                expect(p2_preview).to_be_focused()
                self.assertEqual(
                    page.evaluate('window.__previewCalls'),
                    calls_before_retry + ['preview/p2.png'],
                    '签名失败重试只能重新签名失败的单页',
                )
                signed_p2_url = '/signed/preview/p2.png'
                self.resolve_preview(page, 'preview/p2.png')
                page.evaluate(
                    "({path, url}) => { window.__previewPinned[path] = url; }",
                    {'path': 'preview/p2.png', 'url': signed_p2_url},
                )
                expect(
                    p2_card.get_by_text('图片加载失败，请重试', exact=True)
                ).to_be_visible()
                p2_retry = p2_card.get_by_role('button', name='重试预览', exact=True)
                expect(p2_retry).to_be_focused()
                expect(p2_preview).to_have_attribute('data-loaded', '0')
                self.assertEqual(len(signed_p2_requests), 1)
                self.assertIn(signed_p2_url, signed_p2_requests[0])

                calls_before_image_retry = page.evaluate('window.__previewCalls')
                p2_retry.click()
                self.wait_for_waiter(page, 'preview/p2.png')
                expect(p2_preview).to_be_focused()
                p1_score.focus()
                expect(p1_score).to_be_focused()
                expect(p2_preview).to_have_attribute('data-preview-state', 'loading')
                self.assertEqual(
                    page.evaluate('window.__previewCalls'),
                    calls_before_image_retry + ['preview/p2.png'],
                    '坏图重试只能重新签名失败的单页',
                )
                self.resolve_preview_url(page, 'preview/p2.png', signed_p2_url)
                expect(p2_card.locator('.ga-preview img[src*="/preview/p2.png"]')).to_be_visible()
                expect(p2_preview).to_have_attribute('data-loaded', '1')
                expect(p1_score).to_be_focused()
                self.assertEqual(len(signed_p2_requests), 2)
                self.assertEqual(
                    signed_p2_requests[0],
                    signed_p2_requests[1],
                    '坏图恢复必须复用同一签名 URL',
                )
                self.assertEqual(
                    page.evaluate(
                        "[...window.__previewCalls].filter(path => path === 'preview/p2.png').length"
                    ),
                    3,
                    '初次签名、签名重试和坏图重试各请求一次失败页',
                )
                page.evaluate(
                    "path => { delete window.__previewPinned[path]; }",
                    'preview/p2.png',
                )

                self.assertTrue(
                    page.evaluate(
                        """() => window.__p1PreviewImage === document.querySelector(
                            '[data-unit-card="p1"] .ga-preview img'
                        )"""
                    ),
                    '相邻页图片节点不得因单页重试被替换',
                )
                expect(p1_score).to_have_value('4.5')
                expect(p1_note).to_have_value('相邻页评分草稿')
                self.assertNotIn('preview/p3.png', page.evaluate('window.__previewCalls'))

                p3_card = page.locator('[data-unit-card="p3"]')
                p3_card.scroll_into_view_if_needed()
                self.wait_for_waiter(page, 'preview/p3.png')
                self.wait_for_waiter(page, 'preview/p4.png')
                page.wait_for_timeout(100)
                self.assertNotIn('preview/p5.png', page.evaluate('window.__previewCalls'))

                page.get_by_role('button', name='整套', exact=True).click()
                self.wait_for_waiter(page, 'preview/p1.png')
                self.resolve_preview(page, 'preview/p3.png')
                self.resolve_preview(page, 'preview/p4.png')
                self.resolve_preview(page, 'preview/p1.png')
                expect(page.locator('.ga-preview img[src*="/preview/p1.png"]')).to_be_visible()
                page.wait_for_timeout(100)
                self.assertEqual(page.locator('.ga-preview img[src*="/preview/p3.png"]').count(), 0)

                page.get_by_role('button', name='选页', exact=True).click()
                self.assert_quick_targets(page)
                p1_card = page.locator('[data-unit-card="p1"]')
                p1_card.scroll_into_view_if_needed()
                self.wait_for_waiter(page, 'preview/p1.png')
                self.resolve_preview(page, 'preview/p1.png')
                expect(page.locator('.ga-preview img[src*="/preview/p1.png"]')).to_be_visible()

                page.locator('[data-select-unit="p1"]').check()
                page.locator('[data-score-unit="p1"][data-dim="visual"]').fill('4.5')
                page.locator('[data-note-unit="p1"]').fill('单页评分草稿')
                submit = page.get_by_role('button', name='提交所选人工评分', exact=True)
                expect(submit).to_be_enabled()
                submit.click()
                expect(page.locator('[data-review-status]')).to_contain_text('单页评分已保存')

                self.assertEqual(len(submissions), 1)
                self.assertEqual(submissions[0]['scope'], 'page')
                self.assertEqual(submissions[0]['units'][0]['unitId'], 'p1')
                self.assertEqual(submissions[0]['units'][0]['position'], 1)
                self.assertEqual(submissions[0]['units'][0]['scores'], {'visual': 4.5})
                self.assertEqual(submissions[0]['units'][0]['note'], '单页评分草稿')

                page.locator('[data-unit-card="p5"]').scroll_into_view_if_needed()
                self.wait_for_waiter(page, 'preview/p5.png')
                page.evaluate('window.reviewInstance.destroy()')
                page.evaluate(
                    "({path, message}) => window.__rejectPreview(path, message)",
                    {'path': 'preview/p5.png', 'message': '销毁后的过时预览失败'},
                )
                page.wait_for_timeout(150)
                self.assertEqual(page.locator('#review').inner_html(), '')
                self.assertEqual(errors, [])
            finally:
                browser.close()

if __name__ == '__main__':
    unittest.main()
