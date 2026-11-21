"""Browser regression for searchable effective style/classification metadata."""

import base64
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None


ROOT = Path(__file__).resolve().parent
APP = ROOT / 'public' / 'app.js'
SCORING = ROOT / 'public' / 'scoring.js'
STYLES = ROOT / 'public' / 'styles.css'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jcYQAAAAASUVORK5CYII=')
BROWSER_CANDIDATES = [
    Path(os.environ.get('GUANYE_TEST_EDGE', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')),
    Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
    Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
    Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
]
AI_CLASSIFICATION = '数据叙事 <img src=x onerror="window.__classificationXss=1">'

PAGE_TEMPLATE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/styles.css"></head><body>
<div id="app"><div class="boot">正在打开作品库…</div></div>
<div id="toast" role="status" aria-live="polite"></div>
<dialog id="modal" aria-labelledby="modal-title"></dialog>
<dialog id="lightbox" aria-label="图片大图预览"></dialog>
<script>
'use strict';
window.__libraryResponse = __LIBRARY_FIXTURE__;
window.Cloud = {
  member: {role: 'member'},
  summary: __SUMMARY_FIXTURE__,
  ready: async () => true,
  request: async url => {
    if (url === '/api/library') return window.__libraryResponse;
    throw new Error('unexpected cloud request: ' + url);
  },
  imageUrl: async () => '/preview.png'
};
function decorateCloud() {}
</script>
<script src="/scoring.js"></script><script src="/app.js"></script>
</body></html>'''


def browser_path():
    for candidate in BROWSER_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def image(deck_id, index=1):
    suffix = str(index)
    return {'id': deck_id + '-' + suffix, 'name': '封面' if index == 1 else f'第 {index} 页', 'path': f'fixture/{deck_id}-{suffix}.png', 'thumbPath': f'fixture/{deck_id}-{suffix}.png'}


def deck(deck_id, title, old_style='', effective_style='', classification=None, source='none', page_count=1, generated=False):
    row = {
        'id': deck_id,
        'title': title,
        'name': title,
        'style': old_style,
        'images': [image(deck_id, index) for index in range(1, page_count + 1)],
    }
    if effective_style:
        row['effectiveStyle'] = effective_style
    if classification is not None:
        row['effectiveClassification'] = classification
    if generated:
        row['generatedEntryId'] = 'generated-' + deck_id
    row['classificationSource'] = source
    return row


def library(decks):
    return {
        'decks': decks,
        'source': 'fixture library',
        'sourceAvailable': True,
        'state': {'dimensions': [{'id': 'visual', 'name': '视觉'}, {'id': 'layout', 'name': '排版'}], 'maxScore': 5, 'reviews': {}, 'revision': 1},
    }


FULL_LIBRARY = library([
    deck('existing-class', '品牌发布素材', old_style='蓝色旧风格', effective_style='深绿', classification='品牌提案', source='existing', page_count=3),
    deck('ai-class', 'AI 数据复盘', old_style='绿色旧风格', effective_style='深蓝', classification=AI_CLASSIFICATION, source='ai'),
    deck('empty-class', '无分类本地素材', old_style='米色'),
    deck('generated-single', '生成单页正式分', old_style='青色旧风格', effective_style='青绿', generated=True),
    deck('unscored', '未评分作品', old_style='灰色旧风格', effective_style='浅灰'),
])
EMPTY_LIBRARY = library([
    deck('empty-only', '纯本地素材', old_style='米色'),
])
FULL_SUMMARY = {
    'existing-class': {
        'count': 4,
        'mean': 4.9,
        'formalScore': 1.5,
        'formalPageScores': {'existing-class-1': 2.5, 'existing-class-2': None, 'existing-class-3': 0},
    },
    'ai-class': {'count': 3, 'mean': 2.0, 'formalScore': 3.2},
    'empty-class': {'count': 2, 'mean': 4.8, 'formalScore': 0},
    'generated-single': {'count': 5, 'mean': 2.4, 'formalPageScores': {'generated-single-1': 4.25}},
    'unscored': {'count': 0, 'mean': None},
}


def page_html(fixture, summary=None):
    encoded = json.dumps(fixture, ensure_ascii=False).replace('</', '<\\/')
    encoded_summary = json.dumps(summary or {}, ensure_ascii=False).replace('</', '<\\/')
    return PAGE_TEMPLATE.replace('__LIBRARY_FIXTURE__', encoded).replace('__SUMMARY_FIXTURE__', encoded_summary)


FULL_PAGE = page_html(FULL_LIBRARY, FULL_SUMMARY)
EMPTY_PAGE = page_html(EMPTY_LIBRARY, {})


@unittest.skipUnless(sync_playwright and browser_path(), 'requires Playwright and Microsoft Edge/Chrome')
class LibraryClassificationBrowserTest(unittest.TestCase):
    @staticmethod
    def _respond(route):
        url = urlsplit(route.request.url)
        path = url.path
        if path in {'/', '/full'}:
            route.fulfill(content_type='text/html; charset=utf-8', body=FULL_PAGE)
        elif path == '/empty':
            route.fulfill(content_type='text/html; charset=utf-8', body=EMPTY_PAGE)
        elif path == '/app.js':
            route.fulfill(content_type='application/javascript; charset=utf-8', body=APP.read_text(encoding='utf-8'))
        elif path == '/scoring.js':
            route.fulfill(content_type='application/javascript; charset=utf-8', body=SCORING.read_text(encoding='utf-8'))
        elif path == '/styles.css':
            route.fulfill(content_type='text/css; charset=utf-8', body=STYLES.read_text(encoding='utf-8'))
        elif path == '/preview.png':
            route.fulfill(content_type='image/png', body=PNG)
        else:
            route.fulfill(status=404, body='not found')

    def _page(self, browser, viewport):
        context = browser.new_context(viewport=viewport)
        page = context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', self._respond)
        return context, page, errors

    @staticmethod
    def _assert_no_overflow(page, width):
        metrics = page.evaluate('''() => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
          controls: [...document.querySelectorAll('.filter-controls select, .filter-controls .layout-toggle')].map(el => {
            const rect = el.getBoundingClientRect();
            return {left: rect.left, right: rect.right};
          })
        })''')
        assert metrics['scrollWidth'] <= width + 1, metrics
        assert metrics['clientWidth'] <= width + 1, metrics
        for control in metrics['controls']:
            assert control['left'] >= -1, control
            assert control['right'] <= width + 1, control

    def test_effective_metadata_facets_filters_and_escaped_values(self):
        browser_file = browser_path()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(browser_file), headless=True)

            context, page, errors = self._page(browser, {'width': 1440, 'height': 1000})
            page.goto('http://library.test/')
            cards = page.locator('#deck-grid .deck-card')
            expect(cards).to_have_count(5)
            classification = page.get_by_label('分类筛选')
            expect(classification).to_be_visible()
            expect(classification.locator('option')).to_have_count(3)

            existing_card = cards.filter(has_text='品牌发布素材')
            ai_card = cards.filter(has_text='AI 数据复盘')
            empty_card = cards.filter(has_text='无分类本地素材')
            expect(existing_card.locator('.card-taxonomy')).to_contain_text('深绿')
            expect(existing_card.locator('.classification-tag')).to_have_text('分类 · 品牌提案')
            expect(ai_card.locator('.classification-tag.ai-suggestion')).to_contain_text('AI 建议')
            expect(ai_card.locator('.classification-tag')).to_contain_text('数据叙事')
            expect(empty_card.locator('.classification-tag')).to_have_count(0)
            expect(empty_card.locator('.card-taxonomy')).to_contain_text('米色')
            self.assertIsNone(page.evaluate('window.__classificationXss ?? null'))
            expect(page.locator('img[src="x"]')).to_have_count(0)

            option_texts = [text.strip() for text in classification.locator('option').all_text_contents()]
            self.assertEqual(option_texts[0], '全部分类')
            self.assertTrue(any(text.startswith('品牌提案') for text in option_texts), option_texts)
            self.assertTrue(any(text.startswith('数据叙事') for text in option_texts), option_texts)

            page.fill('#search', 'AI 建议')
            expect(cards).to_have_count(1)
            expect(cards.locator('.classification-tag.ai-suggestion')).to_have_count(1)
            page.fill('#search', '')

            page.select_option('#style-filter', '绿')
            page.select_option('#classification-filter', '品牌提案')
            page.fill('#search', '素材')
            expect(cards).to_have_count(1)
            expect(cards.locator('.card-taxonomy')).to_contain_text('深绿')
            expect(cards.locator('.classification-tag')).to_have_text('分类 · 品牌提案')

            page.fill('#search', '完全不存在')
            expect(cards).to_have_count(0)
            expect(page.locator('.empty-state')).to_be_visible()
            page.get_by_role('button', name='查看全部作品').click()
            expect(cards).to_have_count(5)
            expect(page.get_by_label('分类筛选')).to_have_value('')
            expect(page.get_by_label('风格筛选')).to_have_value('')
            expect(page.get_by_label('搜索作品')).to_have_value('')

            page.select_option('#classification-filter', AI_CLASSIFICATION)
            page.fill('#search', 'AI')
            expect(cards).to_have_count(1)
            page.locator('[data-view="pending"]').click()
            expect(cards).to_have_count(5)
            expect(page.get_by_label('分类筛选')).to_have_value('')
            expect(page.get_by_label('风格筛选')).to_have_value('')
            expect(page.get_by_label('搜索作品')).to_have_value('')

            cards.filter(has_text='品牌发布素材').locator('.card-title').click()
            detail_label = page.locator('.detail-heading .style-label')
            expect(detail_label).to_contain_text('深绿')
            expect(detail_label.locator('.classification-tag')).to_have_text('分类 · 品牌提案')
            page.locator('[data-action="back"]').click()

            cards.filter(has_text='AI 数据复盘').locator('.card-title').click()
            ai_detail = page.locator('.detail-heading .classification-tag.ai-suggestion')
            expect(ai_detail).to_contain_text('AI 建议')
            self.assertNotIn('人工', ai_detail.inner_text())
            self.assertIsNone(page.evaluate('window.__classificationXss ?? null'))

            self._assert_no_overflow(page, 1440)
            self.assertEqual(errors, [])
            context.close()

            context, page, errors = self._page(browser, {'width': 320, 'height': 800})
            page.goto('http://library.test/')
            expect(page.locator('#deck-grid .deck-card')).to_have_count(5)
            expect(page.get_by_label('分类筛选')).to_be_visible()
            page.select_option('#classification-filter', AI_CLASSIFICATION)
            expect(page.locator('#deck-grid .deck-card')).to_have_count(1)
            self._assert_no_overflow(page, 320)
            self.assertEqual(errors, [])

            page.goto('http://library.test/empty')
            expect(page.locator('#deck-grid .deck-card')).to_have_count(1)
            expect(page.get_by_label('分类筛选')).to_have_count(0)
            expect(page.locator('.classification-tag')).to_have_count(0)
            expect(page.locator('#deck-grid .deck-card')).to_contain_text('纯本地素材')
            self._assert_no_overflow(page, 320)
            self.assertEqual(errors, [])
            context.close()
            browser.close()


    def test_formal_scores_drive_priority_cards_and_page_context(self):
        browser_file = browser_path()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(browser_file), headless=True)

            context, page, errors = self._page(browser, {'width': 1440, 'height': 1000})
            page.goto('http://library.test/')
            cards = page.locator('#deck-grid .deck-card')
            expect(cards).to_have_count(5)

            page.select_option('#sort', 'high')
            titles = [text.strip() for text in page.locator('#deck-grid .card-title').all_text_contents()]
            self.assertEqual(
                titles,
                ['AI 数据复盘', '生成单页正式分', '品牌发布素材', '无分类本地素材', '未评分作品'],
                'formal scores must outrank conflicting team means, keep zero valid, and put unrated last',
            )

            ai_card = cards.filter(has_text='AI 数据复盘')
            generated_card = cards.filter(has_text='生成单页正式分')
            existing_card = cards.filter(has_text='品牌发布素材')
            zero_card = cards.filter(has_text='无分类本地素材')
            unscored_card = cards.filter(has_text='未评分作品')
            expect(ai_card.locator('.score-source-label')).to_have_text('正式分')
            expect(ai_card.locator('.score-badge')).to_contain_text('3.2')
            expect(existing_card.locator('.score-source-label')).to_have_text('正式分')
            expect(existing_card.locator('.score-badge')).to_contain_text('1.5')
            expect(zero_card.locator('.score-source-label')).to_have_text('正式分')
            expect(zero_card.locator('.score-badge')).to_contain_text('0')
            expect(generated_card.locator('.score-source-label')).to_have_text('团队均分')
            expect(generated_card.locator('.score-badge')).to_contain_text('2.4')
            expect(generated_card.locator('.score-badge')).not_to_contain_text('4.25')
            expect(unscored_card.locator('.score-source-label')).to_have_text('团队均分')
            expect(unscored_card.locator('.score-badge')).to_contain_text('—')

            page.select_option('#sort', 'low')
            low_titles = [text.strip() for text in page.locator('#deck-grid .card-title').all_text_contents()]
            self.assertEqual(
                low_titles,
                ['无分类本地素材', '品牌发布素材', '生成单页正式分', 'AI 数据复盘', '未评分作品'],
            )

            cards.filter(has_text='品牌发布素材').locator('.card-title').click()
            whole_formal = page.locator('.whole-formal-score')
            page_formal = page.locator('.page-formal-score')
            expect(whole_formal).to_contain_text('整套正式分')
            expect(whole_formal.locator('b')).to_have_text('1.5')
            expect(page_formal).to_contain_text('本页正式分')
            expect(page_formal.locator('b')).to_have_text('2.5')

            page.locator('.thumbnail').nth(1).click()
            expect(whole_formal.locator('b')).to_have_text('1.5')
            expect(page_formal).to_have_count(0)

            page.locator('.thumbnail').nth(2).click()
            expect(page_formal).to_contain_text('本页正式分')
            expect(page_formal.locator('b')).to_have_text('0')
            expect(whole_formal.locator('b')).to_have_text('1.5')
            page.locator('[data-action="back"]').click()

            cards.filter(has_text='生成单页正式分').locator('.card-title').click()
            expect(page.locator('.thumbnail')).to_have_count(1)
            expect(page.locator('.whole-formal-score')).to_have_count(0)
            expect(page.locator('.page-formal-score').locator('b')).to_have_text('4.25')
            page.locator('[data-action="back"]').click()

            cards.filter(has_text='未评分作品').locator('.card-title').click()
            expect(page.locator('.whole-formal-score')).to_have_count(0)
            expect(page.locator('.page-formal-score')).to_have_count(0)
            self.assertEqual(errors, [])
            context.close()

            for width in (330, 320):
                context, page, errors = self._page(browser, {'width': width, 'height': 900})
                page.goto('http://library.test/')
                page.select_option('#sort', 'high')
                page.locator('#deck-grid .deck-card').filter(has_text='品牌发布素材').locator('.card-title').click()
                expect(page.locator('.whole-formal-score')).to_be_visible()
                expect(page.locator('.page-formal-score')).to_be_visible()
                self._assert_no_overflow(page, width)
                self.assertEqual(errors, [])
                context.close()

            browser.close()


if __name__ == '__main__':
    unittest.main()