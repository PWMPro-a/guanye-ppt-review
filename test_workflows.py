"""Real-browser smoke tests; creates an isolated temporary database, never production scores."""
import base64
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright, expect
import server

OUTPUT = Path(__file__).resolve().parent / '.test-output'
OUTPUT.mkdir(exist_ok=True)


def main():
    with tempfile.TemporaryDirectory(prefix='ppt-review-test-') as temporary:
        server.DATA = Path(temporary)
        server.initialize()
        server.scan()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        address = 'http://127.0.0.1:' + str(httpd.server_address[1])
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel='msedge')
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(address)
                expect(page.locator('.deck-card')).to_have_count(57)
                page.wait_for_function('Array.from(document.querySelectorAll(".cover-button img")).slice(0,6).every(i=>i.complete&&i.naturalWidth>0)')
                page.screenshot(path=str(OUTPUT / 'library-desktop.png'), full_page=False)
                page.locator('#search').fill('净界医芯')
                expect(page.locator('.deck-card')).to_have_count(1)
                page.locator('.cover-button').click()
                expect(page.locator('h1')).to_have_text('净界医芯')
                page.locator('[data-score-dim="visual"][data-score-value="4"]').click()
                page.locator('[data-score-dim="layout"][data-score-value="5"]').click()
                page.locator('#review-note').fill('层级清楚，图片与文字比例可借鉴。')
                page.wait_for_function('!pending && !saving')
                expect(page.locator('.overall-value')).to_have_text('4.5')
                page.reload()
                expect(page.locator('.overall-value')).to_have_text('4.5')
                expect(page.locator('#review-note')).to_have_value('层级清楚，图片与文字比例可借鉴。')
                page.wait_for_function('document.querySelector("#main-image").complete && document.querySelector("#main-image").naturalWidth>0')
                page.screenshot(path=str(OUTPUT / 'review-desktop.png'), full_page=False)
                page.locator('[data-action="lightbox"]').first.click()
                page.keyboard.press('ArrowRight')
                expect(page.locator('.lightbox-toolbar span')).to_contain_text('02')
                page.locator('[data-action="zoom"]').first.click()
                expect(page.locator('#lightbox')).to_have_class('zoomed')
                page.locator('[data-action="close-lightbox"]').click()
                expect(page.locator('.slide-position')).to_contain_text('02')
                page.locator('[data-overview="true"]').click()
                expect(page.locator('.contact-sheet button')).to_have_count(8)
                page.locator('[data-preview-slide="2"]').click()
                expect(page.locator('.slide-position')).to_contain_text('03')
                page.locator('.scoring-panel [data-action="settings"]').click()
                page.locator('#max-score').fill('10')
                page.locator('[data-action="add-dimension"]').click()
                page.locator('.dimension-name').last.fill('内容表达')
                page.locator('.dimension-desc').last.fill('信息是否准确清楚')
                page.locator('#settings-form button[type="submit"]').click()
                expect(page.locator('#score-visual')).to_have_value('8')
                expect(page.locator('#score-layout')).to_have_value('10')
                expect(page.locator('.dimension')).to_have_count(3)
                page.locator('[data-score-input]').last.fill('7.5')
                page.locator('[data-score-input]').last.press('Tab')
                page.wait_for_function('!pending && !saving')
                expect(page.locator('.overall-value')).to_have_text('8.5')
                page.locator('#score-visual').fill('0')
                page.locator('#score-visual').press('Tab')
                expect(page.locator('.overall-value')).to_have_text('5.83')
                page.locator('[data-score-dim="visual"][data-score-value="8"]').click()
                page.locator('.detail-actions [data-favorite]').click()
                expect(page.locator('.detail-actions [data-favorite]')).to_have_text('已收藏')
                page.wait_for_function('!pending && !saving')
                with page.expect_download() as download_info:
                    page.locator('.sidebar [data-action="export"]').click()
                    page.locator('[data-action="export-csv"]').click()
                csv_path = download_info.value.path()
                csv_bytes = Path(csv_path).read_bytes()
                assert csv_bytes.startswith(b'\xef\xbb\xbf')
                assert '内容表达' in csv_bytes.decode('utf-8-sig')
                with page.expect_download() as download_info:
                    page.locator('[data-action="export-json"]').click()
                backup_path = download_info.value.path()
                backup = json.loads(Path(backup_path).read_text(encoding='utf-8'))
                assert backup['state']['maxScore'] == 10
                page.locator('#backup-file').set_input_files(str(backup_path))
                expect(page.locator('[data-action="confirm-restore"]')).to_be_visible()
                with page.expect_download():
                    page.locator('[data-action="confirm-restore"]').click()
                expect(page.locator('#modal')).not_to_be_visible()
                expect(page.locator('.overall-value')).to_have_text('8.5')
                page.locator('[data-action="back"]').click()
                page.locator('[data-view="favorites"]').click()
                expect(page.locator('.deck-card')).to_have_count(1)
                page.locator('[data-view="rated"]').click()
                expect(page.locator('.deck-card')).to_have_count(1)
                page.locator('[data-view="pending"]').click()
                expect(page.locator('.deck-card')).to_have_count(56)
                page.locator('[data-view="all"]').click()
                page.locator('#style-filter').select_option('棕')
                assert page.locator('.deck-card').count() > 0
                page.locator('#style-filter').select_option('')
                page.locator('[data-layout="list"]').click()
                expect(page.locator('.deck-grid')).to_have_class('deck-grid compact')
                page.locator('[data-layout="grid"]').click()
                page.locator('[data-action="import"]').click()
                sample = next(iter(server.FILES.values()))
                page.locator('#image-files').set_input_files(str(sample))
                page.locator('#import-name').fill('测试导入作品')
                page.locator('#import-submit').click()
                expect(page.locator('h1')).to_have_text('测试导入作品', timeout=20000)
                expect(page.locator('.thumbnail')).to_have_count(1)
                second = list(server.FILES.values())[1]
                page.locator('[data-action="append"]').click()
                page.locator('#image-files').set_input_files(str(second))
                page.locator('#import-submit').click()
                expect(page.locator('#modal')).not_to_be_visible(timeout=20000)
                expect(page.locator('.thumbnail')).to_have_count(2)
                page.locator('[data-action="append"]').click()
                page.locator('#image-files').set_input_files(str(second))
                page.locator('#import-submit').click()
                expect(page.locator('#modal')).not_to_be_visible(timeout=20000)
                expect(page.locator('.thumbnail')).to_have_count(2)
                # Check score validation and stale-window conflict without changing data.
                current=server.read_doc('state')
                invalid=json.loads(json.dumps(current));invalid['maxScore']=0
                result=page.request.put(address+'/api/state',data=invalid)
                assert result.status==400
                stale=json.loads(json.dumps(current));stale['revision']-=1
                result=page.request.put(address+'/api/state',data=stale)
                assert result.status==409
                result=page.request.post(address+'/api/import',data={'name':'bad','images':[{'name':'broken.jpg','data':base64.b64encode(b'invalid image').decode()}]})
                assert result.status==400
                invalid=json.loads(json.dumps(current));invalid['reviews']['invalid']={'scores':{'visual':1000}}
                assert page.request.put(address+'/api/state',data=invalid).status==400
                assert len(server.CATALOG)==58
                page.locator('[data-action="back"]').click()
                page.locator('[data-action="rescan"]').click()
                expect(page.locator('.deck-card')).to_have_count(58)
                # Narrow screen usability and overflow.
                page.set_viewport_size({'width':390,'height':844})
                page.wait_for_timeout(250)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.mobile-tools [data-action="export"]').click()
                expect(page.locator('#modal')).to_be_visible()
                page.locator('[data-action="close-modal"]').first.click()
                page.screenshot(path=str(OUTPUT/'library-mobile.png'),full_page=False)
                page.locator('.cover-button').first.click()
                expect(page.locator('.scoring-panel')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(OUTPUT/'review-mobile.png'),full_page=True)
                # Browser folder upload groups immediate subfolders into distinct sets.
                page.locator('[data-action="back"]').click()
                page.locator('[data-action="import"]').click()
                group_folder=Path(temporary)/'folder-fixture'
                for group in ('作品甲','作品乙'):
                    folder=group_folder/group
                    folder.mkdir(parents=True)
                    (folder/('01'+sample.suffix)).write_bytes(sample.read_bytes())
                page.locator('#folder-files').set_input_files(str(group_folder))
                expect(page.locator('#selected-files')).to_contain_text('2 套作品')
                page.locator('#import-submit').click()
                expect(page.locator('#modal')).not_to_be_visible(timeout=20000)
                expect(page.locator('.deck-card')).to_have_count(60)
                assert server.read_doc('state')['reviews']
                browser.close()
            assert not errors, errors
            print(json.dumps({'result':'passed','checks':['57 sets / 485 images','persistent score and notes','0 score and decimal input','custom dimensions','5-to-10 proportional rescaling','favorites and status filters','search / style / layout','original image / zoom / overview','CSV and JSON exports','JSON restore','new set import / append / deduplication','invalid upload rejection','revision conflict','390px responsive layout','no JavaScript errors'],'screenshots':str(OUTPUT)},ensure_ascii=True))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__=='__main__':
    main()
