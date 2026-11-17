"""Browser coverage of per-page scoring with an isolated database."""
import json
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright, expect
import server


def main():
    with tempfile.TemporaryDirectory(prefix='ppt-page-test-') as temporary:
        server.DATA = Path(temporary)
        server.initialize()
        server.scan()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel='msedge')
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:' + str(httpd.server_address[1]))
                page.locator('.cover-button').first.click()
                def score(dim, value):
                    page.locator('#score-' + dim).fill(str(value))
                    page.locator('#score-' + dim).press('Tab')
                score('visual', 4)
                score('layout', 5)
                page.locator('[data-scoring-mode="page"]').click()
                expect(page.locator('#score-visual')).to_have_value('')
                score('visual', 0)
                score('layout', 2.5)
                page.locator('#review-note').fill('第一页独立笔记')
                expect(page.locator('.overall-value')).to_have_text('1.25')
                page.locator('[data-action="next-page"]').click()
                expect(page.locator('.panel-heading h2')).to_have_text('第 2 页评分')
                expect(page.locator('#score-layout')).to_have_value('')
                expect(page.locator('#review-note')).to_have_value('')
                score('visual', 3)
                score('layout', 4)
                page.locator('#review-note').fill('第二页独立笔记')
                page.locator('[data-slide-index="0"]').click()
                expect(page.locator('#score-visual')).to_have_value('0')
                expect(page.locator('#review-note')).to_have_value('第一页独立笔记')
                page.wait_for_function('!pending && !saving')
                page.reload()
                expect(page.locator('[data-scoring-mode="page"]')).to_have_class('selected')
                expect(page.locator('#score-layout')).to_have_value('2.5')
                page.locator('[data-scoring-mode="deck"]').click()
                expect(page.locator('#score-visual')).to_have_value('4')
                expect(page.locator('.overall-value')).to_have_text('4.5')
                page.locator('.scoring-panel [data-action="settings"]').click()
                page.locator('#max-score').fill('10')
                page.locator('#settings-form button[type="submit"]').click()
                expect(page.locator('#score-visual')).to_have_value('8')
                page.locator('[data-scoring-mode="page"]').click()
                expect(page.locator('#score-layout')).to_have_value('5')
                page.locator('[data-slide-index="1"]').click()
                expect(page.locator('#score-visual')).to_have_value('6')
                page.wait_for_function('!pending && !saving')
                page.locator('.sidebar [data-action="export"]').click()
                with page.expect_download() as download:
                    page.locator('[data-action="export-csv"]').click()
                csv = Path(download.value.path()).read_text(encoding='utf-8-sig')
                assert '逐页评分' in csv and '第一页独立笔记' in csv and '第二页独立笔记' in csv
                with page.expect_download() as download:
                    page.locator('[data-action="export-json"]').click()
                backup = json.loads(Path(download.value.path()).read_text(encoding='utf-8'))
                page.locator('#backup-file').set_input_files({'name': 'backup.json', 'mimeType': 'application/json', 'buffer': json.dumps(backup).encode()})
                with page.expect_download():
                    page.locator('[data-action="confirm-restore"]').click()
                expect(page.locator('#modal')).not_to_be_visible()
                expect(page.locator('#score-visual')).to_have_value('6')
                page_count = page.evaluate('activeDeck().images.length')
                for index in range(2, page_count):
                    page.locator('[data-slide-index="' + str(index) + '"]').click()
                    score('visual', 8)
                    score('layout', 10)
                assert page.evaluate('completed(activeId)')
                page.wait_for_function('!pending && !saving')
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(Path(__file__).parent / '.test-output/page-scoring-mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors, errors
                browser.close()
        finally:
            httpd.shutdown()
    print('PASS: per-page scores/notes, zero/decimals, mode switching, reload, rescale, CSV/JSON restore, completion and mobile')


if __name__ == '__main__':
    main()
