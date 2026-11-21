"""End-to-end document imports against an isolated local library."""
import base64
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest import mock
from playwright.sync_api import sync_playwright, expect
import fitz
import document_import
import server

ROOT = Path(__file__).resolve().parent
PDF = ROOT.parent / 'slideai-studio-integration/export-verification/presentation.pdf'
PPTX = ROOT.parent / 'douyan_templates/output/个人成长_去水印试验_单页.pptx'


class FakeSlides:
    Count = 1


class FakePresentation:
    def __init__(self, save_error=None):
        self.Slides = FakeSlides()
        self.save_error = save_error
        self.saved = []
        self.close_calls = 0

    def SaveAs(self, path, format):
        self.saved.append((path, format))
        if self.save_error:
            raise self.save_error

    def Close(self):
        self.close_calls += 1


class FakePresentations:
    def __init__(self, app, presentation):
        self.app = app
        self.presentation = presentation
        self.open_calls = []
        self.settings_at_open = None

    def Open(self, *args):
        self.open_calls.append(args)
        self.settings_at_open = (self.app.AutomationSecurity, self.app.DisplayAlerts)
        return self.presentation


class FakePowerPoint:
    def __init__(self, presentation, automation_security=2, display_alerts=-1):
        self.AutomationSecurity = automation_security
        self.DisplayAlerts = display_alerts
        self.Presentations = FakePresentations(self, presentation)
        self.quit_calls = 0

    def Quit(self):
        self.quit_calls += 1


def fake_com_modules(app):
    pythoncom = types.ModuleType('pythoncom')
    pythoncom.CoInitialize = mock.Mock()
    pythoncom.CoUninitialize = mock.Mock()
    client = types.ModuleType('win32com.client')
    client.DispatchEx = mock.Mock(return_value=app)
    package = types.ModuleType('win32com')
    package.client = client
    return {"pythoncom": pythoncom, "win32com": package, "win32com.client": client}, pythoncom, client


class PowerPointComLifetimeTests(unittest.TestCase):
    def paths(self, folder):
        source = Path(folder) / 'source.ppt'
        source.write_bytes(bytes.fromhex('D0CF11E0A1B11AE1'))
        return source, Path(folder) / 'converted.pdf'

    def test_success_closes_only_this_presentation_and_restores_settings(self):
        with tempfile.TemporaryDirectory(prefix='powerpoint-com-test-') as folder:
            source, output = self.paths(folder)
            presentation = FakePresentation()
            app = FakePowerPoint(presentation)
            modules, pythoncom, client = fake_com_modules(app)
            with mock.patch.dict(sys.modules, modules):
                document_import.powerpoint_pdf(source, output)

            self.assertEqual(client.DispatchEx.call_args, mock.call('PowerPoint.Application'))
            self.assertEqual(app.Presentations.settings_at_open, (3, 1))
            self.assertEqual(app.Presentations.open_calls, [(str(source.resolve()), -1, 0, 0)])
            self.assertEqual(presentation.saved, [(str(output.resolve()), 32)])
            self.assertEqual(presentation.close_calls, 1)
            self.assertEqual(app.quit_calls, 0)
            self.assertEqual((app.AutomationSecurity, app.DisplayAlerts), (2, -1))
            pythoncom.CoInitialize.assert_called_once_with()
            pythoncom.CoUninitialize.assert_called_once_with()

    def test_error_closes_presentation_restores_settings_and_uses_fallback(self):
        with tempfile.TemporaryDirectory(prefix='powerpoint-com-test-') as folder:
            source, output = self.paths(folder)
            presentation = FakePresentation(save_error=RuntimeError('mock save failed'))
            app = FakePowerPoint(presentation, automation_security=1, display_alerts=0)
            modules, pythoncom, _ = fake_com_modules(app)
            with mock.patch.dict(sys.modules, modules), \
                    mock.patch.object(document_import.shutil, 'which', return_value=None), \
                    mock.patch.object(Path, 'is_file', return_value=False):
                with self.assertRaisesRegex(ValueError, 'PPT 转换失败'):
                    document_import.powerpoint_pdf(source, output)

            self.assertEqual(presentation.close_calls, 1)
            self.assertEqual(app.quit_calls, 0)
            self.assertEqual((app.AutomationSecurity, app.DisplayAlerts), (1, 0))
            pythoncom.CoUninitialize.assert_called_once_with()


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
