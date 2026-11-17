"""End-to-end document imports against an isolated local library."""
import base64
import json
import tempfile
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright, expect
import fitz
import server

ROOT = Path(__file__).resolve().parent
PDF = ROOT.parent / 'slideai-studio-integration/export-verification/presentation.pdf'
PPTX = ROOT.parent / 'douyan_templates/output/个人成长_去水印试验_单页.pptx'


def legacy_copy(source, output):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    app = presentation = None
    try:
        app = win32com.client.DispatchEx('PowerPoint.Application')
        app.AutomationSecurity = 3
        app.DisplayAlerts = 1
        presentation = app.Presentations.Open(str(source.resolve()), -1, 0, 0)
        presentation.SaveAs(str(output.resolve()), 1)
    finally:
        if presentation is not None: presentation.Close()
        if app is not None: app.Quit()
        pythoncom.CoUninitialize()


def main():
    with tempfile.TemporaryDirectory(prefix='ppt-documents-test-') as temporary:
        server.DATA = Path(temporary) / 'data'
        server.initialize()
        server.scan()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        address = 'http://127.0.0.1:' + str(httpd.server_address[1])
        errors = []
        try:
            legacy = Path(temporary) / '旧版演示.ppt'
            legacy_copy(PPTX, legacy)
            invalid = Path(temporary) / '损坏文档.pdf'
            invalid.write_bytes(b'not a PDF')
            encrypted = Path(temporary) / '有密码.pdf'
            with fitz.open(PDF) as pdf:
                pdf.save(str(encrypted), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw='secret', owner_pw='owner')
            repeated = Path(temporary) / '重复页面.pdf'
            with fitz.open(PDF) as original, fitz.open() as copied:
                for _ in range(2): copied.insert_pdf(original, from_page=0, to_page=0)
                copied.save(str(repeated))
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel='msedge')
                page = browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(address)
                expect(page.locator('.deck-card')).to_have_count(57)
                page.locator('[data-action="import"]').click()
                page.locator('#document-files').set_input_files(str(PDF))
                page.locator('#import-name').fill('PDF 导入验证')
                page.screenshot(path=str(ROOT / '.test-output/import-documents.png'))
                page.locator('#import-submit').click()
                expect(page.locator('h1')).to_have_text('PDF 导入验证',timeout=30000)
                expect(page.locator('.thumbnail')).to_have_count(2)
                page.wait_for_function('document.querySelector("#main-image").naturalWidth>0')
                page.locator('[data-score-dim="visual"][data-score-value="4"]').click()
                page.locator('[data-score-dim="layout"][data-score-value="5"]').click()
                page.wait_for_function('!pending&&!saving')
                deck_id=page.evaluate('activeId')
                page.reload()
                expect(page.locator('.overall-value')).to_have_text('4.5')
                page.locator('[data-action="back"]').click()
                page.locator('[data-action="import"]').click()
                page.locator('#document-files').set_input_files(str(PPTX))
                page.locator('#import-name').fill('PPTX 导入验证')
                page.locator('#import-submit').click()
                expect(page.locator('h1')).to_have_text('PPTX 导入验证',timeout=60000)
                expect(page.locator('.thumbnail')).to_have_count(1)
                page.wait_for_function('document.querySelector("#main-image").naturalWidth>0')
                page.screenshot(path=str(ROOT / '.test-output/imported-pptx.png'))
                page.locator('[data-action="append"]').click()
                page.locator('#document-files').set_input_files(str(legacy))
                page.locator('#import-submit').click()
                expect(page.locator('#modal')).not_to_be_visible(timeout=60000)
                expect(page.locator('.thumbnail')).to_have_count(2)
                page.locator('[data-action="back"]').click()
                page.locator('[data-action="import"]').click()
                page.locator('#document-files').set_input_files([str(PDF),str(repeated)])
                expect(page.locator('#import-name')).to_be_disabled()
                page.locator('#import-submit').click()
                expect(page.locator('#modal')).not_to_be_visible(timeout=60000)
                expect(page.locator('.deck-card')).to_have_count(61)
                # Identical pages must remain two distinct entries, in page order.
                repeated_deck=next(d for d in server.CATALOG if d['title']=='重复页面')
                assert len(repeated_deck['images'])==2
                assert repeated_deck['images'][0]['id']!=repeated_deck['images'][1]['id']
                page.locator('[data-action="import"]').click()
                page.locator('#document-files').set_input_files([str(PDF),str(invalid)])
                page.locator('#import-submit').click()
                expect(page.locator('#import-error')).to_contain_text('无法读取',timeout=30000)
                assert len(server.CATALOG)==61
                page.locator('#document-files').set_input_files(str(encrypted))
                page.locator('#import-submit').click()
                expect(page.locator('#import-error')).to_contain_text('密码',timeout=30000)
                assert len(server.CATALOG)==61
                page.locator('[data-action="close-modal"]').first.click()
                page.set_viewport_size({'width':390,'height':844})
                page.locator('[data-action="import"]').click()
                expect(page.locator('[data-action="choose-documents"]')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(ROOT / '.test-output/import-documents-mobile.png'))
                assert server.read_doc('state')['reviews'][deck_id]['scores']=={'visual':4,'layout':5}
                assert len(list((server.DATA/'documents').iterdir()))>=3
                browser.close()
            assert not errors, errors
            assert not any((server.DATA/'work').iterdir()), 'Temporary conversion files should be cleaned'
            print(json.dumps({'result':'passed','checks':['PDF page previews and score persistence','PPTX via PowerPoint','legacy PPT appended to existing set','multi-document separate sets','duplicate-page preservation','corrupt PDF rejection and batch atomicity','password PDF rejection','source document archive','mobile import controls','conversion temp cleanup','no JavaScript errors']},ensure_ascii=True))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    main()
