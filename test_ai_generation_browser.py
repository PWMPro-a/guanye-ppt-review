"""Edge acceptance test for generated image preview, download and single-page retry."""

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
SCRIPT = Path(__file__).resolve().parent / 'public' / 'ai-generation.js'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jcYQAAAAASUVORK5CYII=')
PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><section id="content"></section><section id="template"></section>
<script>
window.CLOUD_CONFIG = {url: 'http://generation.test', anonKey: 'fixture', workspaceId: 'workspace'};
window.supabase = {createClient: () => ({auth: {getSession: async () => ({data: {session: {access_token: 'fixture-user'}}})}})};
</script><script src="/ai-generation.js"></script>
<script>
window.panels = {};
for (const planId of ['content', 'template']) {
  window.panels[planId] = GuanyeGeneration.mount(document.getElementById(planId), {planId, pollMs: 1000});
}
</script></body></html>'''


WORKSPACE_PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>
<button data-ai-action="open">AI 工作台</button><dialog id="modal"></dialog>
<script>
window.CLOUD_CONFIG = {url: 'http://generation.test', anonKey: 'fixture', workspaceId: 'workspace', aiGatewayUrl: 'http://generation.test'};
window.supabase = {createClient: () => ({auth: {getSession: async () => ({data: {session: {access_token: 'fixture-user'}}})}})};
window.Cloud = {aiAvailable: true, member: {id: 'fixture-user'}, imageUrl: async () => ''};
window.__generationUpdates = new Map();
</script>
<script src="/ai-reviews.js"></script>
<script src="/ai-generation.js"></script>
<script>
const realGenerationMount = window.GuanyeGeneration.mount;
window.GuanyeGeneration.mount = function(root, options = {}) {
  const result = realGenerationMount(root, options);
  if (options.jobId && typeof options.onUpdate === 'function') {
    window.__generationUpdates.set(String(options.jobId), options.onUpdate);
  }
  return result;
};
</script>
<script defer src="/ai-workspace.js"></script>
</body></html>'''


def review_workspace(kind, deck_id):
    default_id = 'uploaded-one' if kind == 'uploaded' else 'generated-deck-a'
    deck_id = deck_id or default_id
    title = '评审 ' + deck_id
    return {
        'deck': {
            'id': deck_id,
            'data': {'title': title, 'images': [{'id': 'p1', 'name': '封面', 'path': '', 'position': 1}]},
        },
        'preEvaluationStatus': 'not_queued',
        'deckVersion': deck_id + '-v1',
        'rubricVersion': 'r1',
        'isJudge': False,
        'role': 'reviewer',
        'workspaceSettings': {
            'dimensions': [{'id': 'visual', 'name': '视觉效果'}, {'id': 'layout', 'name': '排版'}],
            'maxScore': 5,
        },
        'queues': {
            'uploaded': [
                {'id': 'uploaded-one', 'title': '上传一'},
                {'id': 'uploaded-two', 'title': '上传二'},
            ],
            'generated': [
                {'id': 'generated-deck-a', 'title': '生成 A'},
                {'id': 'generated-deck-b', 'title': '生成 B'},
                {'id': 'generated-deck-running', 'title': '生成中'},
            ],
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


def generation_job(job_id, status, deck_id):
    page_status = 'succeeded' if status == 'completed' else 'running'
    return {
        'job': {
            'id': job_id,
            'title': job_id + ' 进度',
            'status': status,
            'deckId': deck_id,
            'pages': [{
                'id': job_id + '-1',
                'position': 1,
                'title': job_id + ' 第 1 页',
                'status': page_status,
            }],
        },
    }


def fixture_job(job_id, status, states):
    pages = []
    for position, page_status in reversed(list(enumerate(states, start=1))):
        page_id = f'{job_id}-{position}'
        row = {'id': page_id, 'position': position, 'title': f'{job_id} 第 {position} 页', 'status': page_status}
        if page_status == 'succeeded':
            row['image'] = {'path': f'private/{page_id}.png'}
            row['previewUrl'] = f'/preview/{page_id}.png'
            row['downloadPath'] = f'/api/ai/generation/artifact?job_id={job_id}&page_id={page_id}&download=1'
        if page_status == 'failed':
            row['error'] = '创作服务暂时不可用'
        pages.append(row)
    return {'job': {'id': job_id, 'title': f'{job_id} 进度', 'status': status, 'pages': pages}}


@unittest.skipUnless(sync_playwright and EDGE.is_file(), 'requires Playwright and Microsoft Edge')
class GenerationBrowserTest(unittest.TestCase):
    def test_artifact_previews_follow_viewport_and_current_render(self):
        initial = fixture_job('viewport', 'completed', ['succeeded'] * 30)
        for row in initial['job']['pages']:
            row['previewUrl'] = row['downloadPath'].replace('&download=1', '')
        current_job = json.loads(json.dumps(initial))
        requests = []
        held_previews = {}
        artifact = {'dataBase64': base64.b64encode(PNG).decode(), 'contentType': 'image/png'}
        html = PAGE.replace(
            "for (const planId of ['content', 'template']) {",
            "for (const planId of ['content']) {",
        ).replace('{planId, pollMs: 1000}', '{job: ' + json.dumps(initial['job']) + '}')

        def respond(route):
            parsed = urlsplit(route.request.url)
            if parsed.path == '/':
                return route.fulfill(content_type='text/html; charset=utf-8', body=html)
            if parsed.path == '/ai-generation.js':
                return route.fulfill(content_type='application/javascript', body=SCRIPT.read_bytes())
            if parsed.path == '/api/ai/generation':
                return route.fulfill(json=current_job)
            if parsed.path == '/api/ai/generation/artifact':
                query = parse_qs(parsed.query)
                key = (query['page_id'][0], query.get('revision', ['1'])[0], 'download' in query)
                requests.append(key)
                if key in (('viewport-1', '1', False), ('viewport-1', '2', False)) and key not in held_previews:
                    held_previews[key] = route
                    return
                return route.fulfill(json=artifact)
            return route.fulfill(status=404)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            context = browser.new_context(viewport={'width': 390, 'height': 650}, accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(5000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('http://generation.test/**', respond)
            page.goto('http://generation.test/')
            cards = page.locator('#content .gg-card')
            expect(cards).to_have_count(30)
            expect(cards.nth(1).locator('img')).to_be_visible()
            self.assertTrue(page.evaluate("document.querySelector('.gg-card:last-child').getBoundingClientRect().top > innerHeight * 5"))
            second_button = cards.nth(1).get_by_role('button', name='下载图片')
            second_button.focus()
            page.evaluate("""() => {
                window.beforeRefresh = {
                    cards: [...document.querySelectorAll('#content .gg-card')],
                    secondImage: document.querySelectorAll('#content .gg-card')[1].querySelector('img'),
                    focused: document.activeElement,
                };
            }""")
            page.wait_for_timeout(150)
            initial_previews = [key[0] for key in requests]
            self.assertIn('viewport-1', initial_previews)
            self.assertNotIn('viewport-30', initial_previews, f'{len(initial_previews)} initial preview requests')
            self.assertLessEqual(len(initial_previews), 5)

            # A no-change poll must preserve cards, loaded images, focus and in-flight work.
            with page.expect_request(lambda request: request.url == 'http://generation.test/api/ai/generation?job_id=viewport'):
                page.evaluate('window.panels.content.refresh()')
            self.assertTrue(page.evaluate('window.beforeRefresh.cards.every(card => card.isConnected)'))
            self.assertTrue(page.evaluate("window.beforeRefresh.secondImage === document.querySelectorAll('#content .gg-card')[1].querySelector('img')"))
            self.assertTrue(page.evaluate('window.beforeRefresh.focused === document.activeElement'))
            self.assertIn(('viewport-1', '1', False), held_previews)
            self.assertEqual(requests.count(('viewport-1', '1', False)), 1)
            self.assertEqual(requests.count(('viewport-2', '1', False)), 1)

            # Download the far card independently, without scrolling it into view.
            with page.expect_download(timeout=10000) as downloaded:
                cards.last.get_by_role('button', name='下载图片').dispatch_event('click')
            self.assertEqual(Path(downloaded.value.path()).read_bytes(), PNG)
            self.assertIn(('viewport-30', '1', True), requests)
            self.assertNotIn(('viewport-30', '1', False), requests)

            # Only the page whose source changed is replaced; its old request cannot write back.
            current_job = json.loads(json.dumps(initial))
            next(row for row in current_job['job']['pages'] if row['position'] == 1)['previewUrl'] += '&revision=2'
            page.evaluate('window.panels.content.refresh()')
            page.wait_for_timeout(100)
            self.assertIn(('viewport-1', '2', False), held_previews)
            self.assertFalse(page.evaluate('window.beforeRefresh.cards[0].isConnected'))
            self.assertTrue(page.evaluate('window.beforeRefresh.cards.slice(1).every(card => card.isConnected)'))
            self.assertTrue(page.evaluate("window.beforeRefresh.secondImage === document.querySelectorAll('#content .gg-card')[1].querySelector('img')"))
            self.assertTrue(page.evaluate('window.beforeRefresh.focused === document.activeElement'))
            self.assertEqual(cards.first.get_attribute('data-page-key'), '["id","viewport-1",0]')
            expect(cards.first.locator('img')).to_have_count(0)

            held_previews[('viewport-1', '1', False)].fulfill(json=artifact)
            page.wait_for_timeout(100)
            expect(cards.first.locator('img')).to_have_count(0)
            self.assertFalse(page.evaluate("!!window.beforeRefresh.cards[0].querySelector('img')"))

            held_previews[('viewport-1', '2', False)].fulfill(json=artifact)
            expect(cards.first.locator('img')).to_be_visible()

            cards.last.scroll_into_view_if_needed()
            expect(cards.last.locator('img')).to_be_visible()
            self.assertIn(('viewport-30', '1', False), requests)
            self.assertNotIn(('viewport-30', '2', False), requests)
            self.assertNotIn(('viewport-15', '1', False), requests)

            # Queue a scroll toward a pending card and tear down in the same turn.
            page.evaluate("""() => {
                document.querySelectorAll('#content .gg-card')[14].scrollIntoView();
                window.panels.content.destroy();
            }""")
            expect(cards).to_have_count(0)
            page.wait_for_timeout(150)
            self.assertNotIn(('viewport-15', '1', False), requests)
            self.assertEqual(errors, [])

            fallback_page = context.new_page()
            fallback_page.add_init_script('window.IntersectionObserver = undefined')
            fallback_page.route('http://generation.test/**', respond)
            fallback_page.goto('http://generation.test/')
            expect(fallback_page.locator('#content .gg-card').last.locator('img')).to_have_count(1)
            self.assertIn(('viewport-30', '1', False), requests)
            context.close()
            browser.close()

    def test_waiting_preview_and_download_failures_recover(self):
        row = {
            'id': 'retry-image',
            'position': 1,
            'title': '失败重试',
            'status': 'succeeded',
            'previewUrl': '/api/ai/generation/artifact?job_id=retry-image&page_id=retry-image&revision=1',
        }
        current_job = {'job': {'id': 'retry-image', 'title': 'retry-image 进度', 'status': 'completed', 'pages': [row]}}
        html = PAGE.replace(
            "for (const planId of ['content', 'template']) {",
            "for (const planId of ['content']) {",
        ).replace('{planId, pollMs: 1000}', '{job: ' + json.dumps(current_job['job']) + '}')
        attempts = []
        downloads = []
        held_preview = []
        artifact = {'dataBase64': base64.b64encode(PNG).decode(), 'contentType': 'image/png'}

        def respond(route):
            parsed = urlsplit(route.request.url)
            if parsed.path == '/':
                return route.fulfill(content_type='text/html; charset=utf-8', body=html)
            if parsed.path == '/ai-generation.js':
                return route.fulfill(content_type='application/javascript', body=SCRIPT.read_bytes())
            if parsed.path == '/api/ai/generation':
                return route.fulfill(json=current_job)
            if parsed.path == '/api/ai/generation/artifact':
                query = parse_qs(parsed.query)
                if 'download' in query:
                    downloads.append(query)
                    return route.fulfill(status=500, json={'error': 'fixture download failure'})
                attempts.append(query)
                if len(attempts) == 1:
                    held_preview.append(route)
                    return
                return route.fulfill(json=artifact)
            return route.fulfill(status=404)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            context = browser.new_context(viewport={'width': 390, 'height': 650})
            page = context.new_page()
            page.set_default_timeout(5000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('http://generation.test/**', respond)
            page.goto('http://generation.test/')
            preview = page.locator('#content .gg-preview')
            expect(preview).to_have_text('加载图片…')
            for _ in range(20):
                if held_preview:
                    break
                page.wait_for_timeout(50)
            self.assertEqual(len(held_preview), 1)
            page.evaluate('window.failedCard = document.querySelector("#content .gg-card")')
            page.evaluate('window.panels.content.refresh()')
            self.assertTrue(page.evaluate('window.failedCard.isConnected'))
            held_preview[0].fulfill(status=500, json={'error': 'fixture failure'})
            expect(preview).to_have_text('图片暂不可用')

            page.evaluate('window.panels.content.refresh()')
            expect(preview.locator('img')).to_be_visible()
            self.assertEqual(len(attempts), 2)
            self.assertFalse(page.evaluate('window.failedCard.isConnected'))

            current_job = json.loads(json.dumps(current_job))
            current_job['job']['pages'][0]['downloadPath'] = (
                '/api/ai/generation/artifact?job_id=retry-image&page_id=retry-image&revision=1&download=1'
            )
            page.evaluate('window.panels.content.refresh()')
            expect(preview.locator('img')).to_be_visible()
            page.evaluate('window.retriedCard = document.querySelector("#content .gg-card")')
            download_button = page.get_by_role('button', name='下载图片')
            download_button.evaluate('button => button.click()')
            expect(page.locator('#content .gg-note')).to_have_text('fixture download failure')
            expect(download_button).to_be_enabled()
            expect(download_button).to_have_text('下载图片')
            self.assertEqual(len(downloads), 1)
            self.assertTrue(page.evaluate('window.retriedCard.isConnected'))
            page.evaluate('window.panels.content.refresh()')
            self.assertTrue(page.evaluate('window.retriedCard.isConnected'))
            expect(preview.locator('img')).to_be_visible()
            self.assertEqual(errors, [])
            browser.close()

    def test_keyed_refresh_handles_duplicate_missing_ids_and_new_pages(self):
        rows = [
            {'id': 'slide', 'position': 2, 'title': 'B', 'status': 'succeeded', 'previewUrl': '/preview/b.png'},
            {'id': 'slide', 'position': 1, 'title': 'A', 'status': 'succeeded', 'previewUrl': '/preview/a.png'},
            {'id': 'slide:duplicate:0', 'position': 3, 'title': 'C', 'status': 'succeeded', 'previewUrl': '/preview/c.png'},
            {'position': 4, 'title': 'D', 'status': 'succeeded', 'previewUrl': '/preview/d.png'},
            {'id': 'four', 'position': 5, 'title': 'E', 'status': 'succeeded', 'previewUrl': '/preview/e.png'},
        ]
        current_job = {'job': {'id': 'keyed', 'title': 'keyed 进度', 'status': 'completed', 'pages': rows}}
        html = PAGE.replace(
            "for (const planId of ['content', 'template']) {",
            "for (const planId of ['content']) {",
        ).replace('{planId, pollMs: 1000}', '{job: ' + json.dumps(current_job['job']) + '}')

        def respond(route):
            parsed = urlsplit(route.request.url)
            if parsed.path == '/':
                return route.fulfill(content_type='text/html; charset=utf-8', body=html)
            if parsed.path == '/ai-generation.js':
                return route.fulfill(content_type='application/javascript', body=SCRIPT.read_bytes())
            if parsed.path == '/api/ai/generation':
                return route.fulfill(json=current_job)
            if parsed.path.startswith('/preview/'):
                return route.fulfill(content_type='image/png', body=PNG)
            return route.fulfill(status=404)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            context = browser.new_context(viewport={'width': 390, 'height': 650})
            page = context.new_page()
            page.set_default_timeout(5000)
            page.add_init_script('window.IntersectionObserver = undefined')
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('http://generation.test/**', respond)
            page.goto('http://generation.test/')
            cards = page.locator('#content .gg-card')
            expect(cards).to_have_count(5)
            expect(page.locator('#content .gg-name')).to_have_text(['A', 'B', 'C', 'D', 'E'])
            keys = cards.evaluate_all('cards => cards.map(card => card.dataset.pageKey)')
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(set(keys), {
                json.dumps(['duplicate-id', 'slide', 0], separators=(',', ':')),
                json.dumps(['duplicate-id', 'slide', 1], separators=(',', ':')),
                json.dumps(['id', 'slide:duplicate:0', 0], separators=(',', ':')),
                json.dumps(['missing-id', 4, 0], separators=(',', ':')),
                json.dumps(['id', 'four', 0], separators=(',', ':')),
            })

            page.evaluate('window.keyedBefore = [...document.querySelectorAll("#content .gg-card")]')
            page.evaluate('window.panels.content.refresh()')
            self.assertTrue(page.evaluate('window.keyedBefore.every(card => card.isConnected)'))

            current_job = json.loads(json.dumps(current_job))
            current_job['job']['pages'][0]['title'] = 'B 已变更'
            page.evaluate('window.panels.content.refresh()')
            expect(page.locator('#content .gg-name')).to_have_text(['A', 'B 已变更', 'C', 'D', 'E'])
            self.assertFalse(page.evaluate('window.keyedBefore[1].isConnected'))
            self.assertTrue(page.evaluate('window.keyedBefore.filter((card, index) => index !== 1).every(card => card.isConnected)'))

            page.evaluate('window.keyedChanged = [...document.querySelectorAll("#content .gg-card")]')
            current_job = json.loads(json.dumps(current_job))
            current_job['job']['pages'].append({
                'id': 'five', 'position': 6, 'title': 'F', 'status': 'succeeded', 'previewUrl': '/preview/f.png',
            })
            page.evaluate('window.panels.content.refresh()')
            expect(page.locator('#content .gg-name')).to_have_text(['A', 'B 已变更', 'C', 'D', 'E', 'F'])
            self.assertTrue(page.evaluate('window.keyedChanged.every(card => card.isConnected)'))
            expect(cards.last.locator('img')).to_be_visible()
            self.assertEqual(errors, [])
            browser.close()

    def test_two_jobs_keep_page_order_download_and_retry_only_failed_page(self):
        submitted = []
        retries = []
        downloads = []
        unauthorized = []
        get_calls = []

        def respond(route):
            request = route.request
            parsed = urlsplit(request.url)
            query = parse_qs(parsed.query)
            if parsed.path == '/':
                return route.fulfill(content_type='text/html; charset=utf-8', body=PAGE)
            if parsed.path == '/ai-generation.js':
                return route.fulfill(content_type='application/javascript; charset=utf-8', body=SCRIPT.read_text(encoding='utf-8'))
            if parsed.path.startswith('/preview/'):
                return route.fulfill(content_type='image/png', body=PNG)
            if not parsed.path.startswith('/api/ai/generation'):
                return route.fulfill(status=404)
            if request.headers.get('authorization') != 'Bearer fixture-user':
                unauthorized.append(parsed.path)
                return route.fulfill(status=401, json={'error': 'missing fixture authorization'})
            if parsed.path == '/api/ai/generation/artifact':
                downloads.append(query)
                return route.fulfill(json={'dataBase64': base64.b64encode(PNG).decode(), 'contentType': 'image/png'})
            if parsed.path == '/api/ai/generation/retry':
                retries.append(json.loads(request.post_data))
                return route.fulfill(json=fixture_job('content', 'running', ['succeeded', 'queued', 'succeeded']))
            if request.method == 'POST':
                plan_id = json.loads(request.post_data)['plan_id']
                submitted.append(plan_id)
                states = ['running', 'running', 'succeeded'] if plan_id == 'content' else ['running', 'succeeded']
                return route.fulfill(json=fixture_job(plan_id, 'running', states))
            job_id = query['job_id'][0]
            get_calls.append(job_id)
            if job_id == 'template':
                return route.fulfill(json=fixture_job('template', 'completed', ['succeeded', 'succeeded']))
            if not retries:
                return route.fulfill(json=fixture_job('content', 'partial', ['succeeded', 'failed', 'succeeded']))
            return route.fulfill(json=fixture_job('content', 'completed', ['succeeded', 'succeeded', 'succeeded']))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            context = browser.new_context(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True, accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(5000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://generation.test/')
            content = page.locator('#content')
            template = page.locator('#template')
            expect(content.locator('.gg-name')).to_have_text(['content 第 1 页', 'content 第 2 页', 'content 第 3 页'])
            expect(template.locator('.gg-name')).to_have_text(['template 第 1 页', 'template 第 2 页'])
            expect(content.locator('.gg-pill')).to_have_text('部分失败 · 2/3')
            expect(template.locator('.gg-pill')).to_have_text('已完成 · 2/2')
            cover = content.locator('.gg-card').first
            expect(cover.get_by_role('img')).to_be_visible()
            page.wait_for_function("document.querySelector('#content .gg-card img')?.naturalWidth === 1")
            with page.expect_download() as downloaded:
                cover.get_by_role('button', name='下载图片').click()
            download = downloaded.value
            self.assertEqual(download.suggested_filename, 'page-0001.png')
            self.assertEqual(Path(download.path()).read_bytes(), PNG)
            self.assertEqual(downloads, [{'job_id': ['content'], 'page_id': ['content-1'], 'download': ['1']}])
            retry = content.get_by_role('button', name='仅重试本页')
            expect(retry).to_have_count(1)
            expect(template.get_by_role('button', name='仅重试本页')).to_have_count(0)
            retry.click()
            expect(content.locator('.gg-pill')).to_have_text('已完成 · 3/3')
            expect(content.get_by_role('button', name='仅重试本页')).to_have_count(0)
            expect(content.locator('.gg-name')).to_have_text(['content 第 1 页', 'content 第 2 页', 'content 第 3 页'])
            self.assertEqual(retries, [{'job_id': 'content', 'page_ids': ['content-2']}])
            self.assertCountEqual(submitted, ['content', 'template'])
            self.assertEqual(get_calls.count('template'), 1)
            self.assertEqual(unauthorized, [])
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 320)
            self.assertEqual(errors, [])
            context.close()
            browser.close()


    def test_workspace_reopens_recent_jobs_and_switches_between_them(self):
        workspace_script = SCRIPT.with_name('ai-workspace.js')
        shell = PAGE[:PAGE.index('<script>\nwindow.panels')] + """
<script>
window.CLOUD_CONFIG.aiGatewayUrl = 'http://generation.test';
window.Cloud = {aiAvailable: true, member: {id: 'user'}};
</script>
<button data-ai-action="open">AI 工作台</button><dialog id="modal"></dialog>
<script defer src="/ai-workspace.js"></script></body></html>"""
        jobs = [
            {'id': 'template', 'title': '运营月报模板', 'createdAt': '2026-11-21T08:30:00Z'},
            {'id': 'content', 'title': '运营月报内容稿', 'createdAt': '2026-11-21T08:00:00Z'},
        ]
        requested = []

        def respond(route):
            parsed = urlsplit(route.request.url)
            query = parse_qs(parsed.query)
            if parsed.path == '/':
                return route.fulfill(content_type='text/html; charset=utf-8', body=shell)
            if parsed.path in ('/ai-generation.js', '/ai-workspace.js'):
                script = SCRIPT if parsed.path == '/ai-generation.js' else workspace_script
                return route.fulfill(content_type='application/javascript; charset=utf-8', body=script.read_text(encoding='utf-8'))
            if parsed.path.startswith('/preview/'):
                return route.fulfill(content_type='image/png', body=PNG)
            if parsed.path != '/api/ai/generation':
                return route.fulfill(status=404)
            if query.get('action') == ['recent']:
                return route.fulfill(json={'jobs': jobs})
            job_id = query['job_id'][0]
            requested.append(job_id)
            return route.fulfill(json=fixture_job(job_id, 'completed', ['succeeded']))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            page.route('**/*', respond)
            page.goto('http://generation.test/')
            opener = page.get_by_role('button', name='AI 工作台')
            generation_tab = page.get_by_role('tab', name='生成进度')
            selector = page.get_by_role('combobox', name='生成任务', exact=True)
            opener.click()
            generation_tab.click()
            expect(selector).to_have_value('template')
            expect(page.get_by_role('heading', name='template 进度')).to_be_visible()
            selector.select_option('content')
            expect(page.get_by_role('heading', name='content 进度')).to_be_visible()
            page.locator('#modal').evaluate('(dialog) => dialog.close()')
            expect(page.locator('#modal')).not_to_be_visible()
            page.reload()
            opener.click()
            generation_tab.click()
            expect(selector.locator('option')).to_have_count(2)
            selector.select_option('content')
            expect(page.get_by_role('heading', name='content 进度')).to_be_visible()
            self.assertIn('template', requested)
            self.assertEqual(requested.count('content'), 2)
            browser.close()


    def test_generation_review_entry_opens_exact_generated_deck_without_resetting_upload_track(self):
        workspace_script = SCRIPT.with_name('ai-workspace.js')
        reviews_script = SCRIPT.with_name('ai-reviews.js')
        jobs = [
            {'id': 'job-a', 'title': '任务 A', 'createdAt': '2026-11-21T08:30:00Z'},
            {'id': 'job-b', 'title': '任务 B', 'createdAt': '2026-11-21T08:20:00Z'},
            {'id': 'job-running', 'title': '生成中任务', 'createdAt': '2026-11-21T08:10:00Z'},
            {'id': 'job-partial', 'title': '部分失败任务', 'createdAt': '2026-11-21T08:00:00Z'},
            {'id': 'job-invalid', 'title': '非法 deck 任务', 'createdAt': '2026-11-21T07:50:00Z'},
        ]
        review_calls = []
        review_posts = []
        generation_calls = []

        def respond(route):
            request = route.request
            parsed = urlsplit(request.url)
            query = parse_qs(parsed.query)
            if parsed.path == '/':
                return route.fulfill(content_type='text/html; charset=utf-8', body=WORKSPACE_PAGE)
            if parsed.path == '/ai-workspace.js':
                return route.fulfill(content_type='application/javascript; charset=utf-8', body=workspace_script.read_text(encoding='utf-8'))
            if parsed.path == '/ai-generation.js':
                return route.fulfill(content_type='application/javascript; charset=utf-8', body=SCRIPT.read_text(encoding='utf-8'))
            if parsed.path == '/ai-reviews.js':
                return route.fulfill(content_type='application/javascript; charset=utf-8', body=reviews_script.read_text(encoding='utf-8'))
            if parsed.path == '/favicon.ico':
                return route.fulfill(status=204)
            if parsed.path == '/api/ai/reviews':
                kind = query.get('kind', [''])[0]
                deck_id = query.get('deckId', [''])[0]
                review_calls.append({'method': request.method, 'kind': kind, 'deckId': deck_id})
                if request.method != 'GET':
                    review_posts.append({'method': request.method, 'path': parsed.path, 'body': request.post_data})
                return route.fulfill(json=review_workspace(kind, deck_id))
            if parsed.path != '/api/ai/generation':
                return route.fulfill(status=404)
            if query.get('action') == ['recent']:
                return route.fulfill(json={'jobs': jobs})
            job_id = query.get('job_id', [''])[0]
            generation_calls.append(job_id)
            decks = {
                'job-a': ('completed', 'generated-deck-a'),
                'job-b': ('completed', 'generated-deck-b'),
                'job-running': ('running', 'generated-deck-running'),
                'job-partial': ('partial', 'generated-deck-running'),
                'job-invalid': ('completed', 'bad deck!'),
            }
            status, deck_id = decks[job_id]
            return route.fulfill(json=generation_job(job_id, status, deck_id))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(EDGE))
            page = browser.new_page(viewport={'width': 320, 'height': 800}, is_mobile=True, has_touch=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', respond)
            page.goto('http://generation.test/')

            page.get_by_role('button', name='AI 工作台').click()
            generation_tab = page.get_by_role('tab', name='生成进度')
            reviews_tab = page.get_by_role('tab', name='AI 评审')
            job_select = page.get_by_role('combobox', name='生成任务', exact=True)
            review_entry = page.get_by_role('button', name='查看生成评审')
            generation_tab.click()
            expect(job_select).to_have_value('job-a')
            expect(page.get_by_role('heading', name='job-a 进度')).to_be_visible()
            expect(review_entry).to_be_visible()

            for job_id, status in (
                ('job-running', 'running'),
                ('job-partial', 'partial'),
                ('job-invalid', 'completed'),
            ):
                job_select.select_option(job_id)
                expect(page.get_by_role('heading', name=job_id + ' 进度')).to_be_visible()
                expect(review_entry).to_have_count(0)

            job_select.select_option('job-a')
            expect(page.get_by_role('heading', name='job-a 进度')).to_be_visible()
            expect(review_entry).to_be_visible()

            reviews_tab.click()
            uploaded_root = page.locator('[data-ai-root="reviews-uploaded"]')
            generated_root = page.locator('[data-ai-root="reviews-generated"]')
            uploaded_select = uploaded_root.get_by_role('combobox', name='选择上传作品', exact=True)
            generated_select = generated_root.get_by_role('combobox', name='选择生成作品', exact=True)
            expect(uploaded_select).to_have_value('uploaded-one')
            with page.expect_request(lambda request: 'action=workspace&kind=uploaded&deckId=uploaded-two' in request.url):
                uploaded_select.select_option('uploaded-two')
            expect(uploaded_root.locator('.ga-band .ga-row b').filter(has_text='评审 uploaded-two')).to_be_visible()
            uploaded_calls_before = len([call for call in review_calls if call['kind'] == 'uploaded'])

            generation_tab.click()
            job_select.select_option('job-b')
            expect(page.get_by_role('heading', name='job-b 进度')).to_be_visible()
            page.evaluate("""() => {
                const stale = window.__generationUpdates.get('job-a');
                stale({id: 'job-a', title: 'job-a 进度', status: 'completed', deckId: 'generated-deck-a', pages: []});
            }""")
            with page.expect_request(lambda request: 'action=workspace&kind=generated&deckId=generated-deck-b' in request.url):
                review_entry.click()

            expect(reviews_tab).to_have_attribute('aria-selected', 'true')
            expect(generated_select).to_have_value('generated-deck-b')
            expect(generated_root.get_by_role('heading', name='AI 盲评 · 生成作品')).to_be_visible()
            expect(uploaded_select).to_have_value('uploaded-two')
            self.assertEqual(uploaded_calls_before, len([call for call in review_calls if call['kind'] == 'uploaded']))
            expect(generated_root.get_by_role('button', name='预评当前粒度')).to_have_count(0)
            page.wait_for_function("document.activeElement?.getAttribute('data-review-focus-target') === 'generated'")
            title_box = page.locator('[data-review-focus-target="generated"] h3').bounding_box()
            self.assertIsNotNone(title_box)
            self.assertGreaterEqual(title_box['y'], 0)
            self.assertLess(title_box['y'], 800)
            self.assertIn('job-b', generation_calls)
            self.assertIn({'method': 'GET', 'kind': 'generated', 'deckId': 'generated-deck-b'}, review_calls)
            self.assertEqual([], review_posts)
            self.assertTrue(all(call['method'] == 'GET' for call in review_calls))

            page.locator('#modal').evaluate('(dialog) => dialog.close()')
            expect(page.locator('#modal')).not_to_be_visible()
            page.get_by_role('button', name='AI 工作台').click()
            generation_tab.click()
            expect(job_select).to_have_value('job-a')
            expect(page.get_by_role('heading', name='job-a 进度')).to_be_visible()
            review_entry.click()
            expect(reviews_tab).to_have_attribute('aria-selected', 'true')
            expect(generated_select).to_have_value('generated-deck-a')
            self.assertEqual([], errors)
            browser.close()


if __name__ == '__main__':
    unittest.main()
