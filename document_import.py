"""Isolated document renderer used by the local import service."""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

MAX_PAGES = 600
_POWERPOINT_LOCK = threading.Lock()


def _set_powerpoint_setting(app, name, value, changed):
    original = getattr(app, name)
    if original == value:
        return
    setattr(app, name, value)
    changed.append((name, original))


def progress(folder, message, current=0, total=0):
    target = folder / 'progress.json'
    temporary = folder / 'progress.tmp'
    temporary.write_text(json.dumps({'message': message, 'current': current, 'total': total}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(target)


def powerpoint_pdf(source, output):
    if source.suffix.lower() == '.pptx':
        try:
            with zipfile.ZipFile(source) as archive:
                if 'ppt/presentation.xml' not in archive.namelist():
                    raise ValueError('文件不是有效的 PPTX 演示文稿')
        except zipfile.BadZipFile:
            raise ValueError('PPTX 文件已损坏，请重新保存后导入')
    elif source.read_bytes()[:8] != bytes.fromhex('D0CF11E0A1B11AE1'):
        raise ValueError('PPT 文件已损坏或格式不正确')
    try:
        import pythoncom
        import win32com.client
        # PowerPoint is a MultiUse COM server, so DispatchEx does not guarantee an
        # isolated process. Never Quit the shared app; only close the presentation
        # opened here. Serialize the COM apartment and temporary setting changes.
        with _POWERPOINT_LOCK:
            pythoncom.CoInitialize()
            app = presentation = None
            changed_settings = []
            try:
                app = win32com.client.DispatchEx('PowerPoint.Application')
                _set_powerpoint_setting(app, 'AutomationSecurity', 3, changed_settings)
                _set_powerpoint_setting(app, 'DisplayAlerts', 1, changed_settings)
                presentation = app.Presentations.Open(str(source.resolve()), -1, 0, 0)
                if presentation.Slides.Count > MAX_PAGES:
                    raise ValueError('每份文档最多支持 600 页，请拆分后导入')
                # SaveAs PDF avoids pywin32's optional COM PrintRange marshaling issue.
                presentation.SaveAs(str(output.resolve()), 32)
                return
            finally:
                if presentation is not None:
                    try: presentation.Close()
                    except Exception: pass
                if app is not None:
                    for name, original in reversed(changed_settings):
                        try: setattr(app, name, original)
                        except Exception: pass
                app = presentation = None
                try: pythoncom.CoUninitialize()
                except Exception: pass
    except ValueError:
        raise
    except Exception as office_error:
        executable = shutil.which('soffice')
        if not executable:
            for candidate in ('C:/Program Files/LibreOffice/program/soffice.exe', 'C:/Program Files (x86)/LibreOffice/program/soffice.exe'):
                if Path(candidate).is_file(): executable = candidate; break
        if not executable:
            raise ValueError('PPT 转换失败。请确认本机 PowerPoint 可正常打开该文件，或将文件另存为 PDF 后导入。') from office_error
        profile = output.parent / 'libreoffice-profile'
        result = subprocess.run([executable, '-env:UserInstallation=' + profile.as_uri(), '--headless', '--convert-to', 'pdf', '--outdir', str(output.parent), str(source)], capture_output=True, timeout=150, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        generated = output.parent / (source.stem + '.pdf')
        if result.returncode or not generated.is_file():
            raise ValueError('PPT 转换失败，请在 PowerPoint 中另存为 PDF 后导入')
        if generated != output: generated.replace(output)


def convert(source, folder):
    import fitz
    folder.mkdir(parents=True, exist_ok=True)
    pdf = source
    if source.suffix.lower() in ('.ppt', '.pptx'):
        progress(folder, '正在转换 PPT，请稍候…')
        pdf = folder / 'converted.pdf'
        powerpoint_pdf(source, pdf)
    try:
        document = fitz.open(pdf)
    except Exception as exc:
        raise ValueError('文件无法读取，请确认文档完整且可正常打开') from exc
    pages = []
    with document:
        if document.needs_pass:
            raise ValueError('文档有打开密码，请先移除密码再导入')
        if not 1 <= len(document) <= MAX_PAGES:
            raise ValueError('每份文档需包含 1 至 600 页，请拆分后导入')
        for index, page in enumerate(document):
            progress(folder, '正在生成第 %d / %d 页预览' % (index + 1, len(document)), index, len(document))
            longest = max(page.rect.width, page.rect.height)
            if longest <= 0:
                raise ValueError('文档页面尺寸无效')
            scale = min(2.5, 2000 / longest)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
            name = 'page-%04d.png' % (index + 1)
            pixmap.save(str(folder / name))
            pages.append({'file': name, 'page': index + 1})
    (folder / 'result.json').write_text(json.dumps({'pages': pages}), encoding='utf-8')
    progress(folder, '文档预览已生成', len(pages), len(pages))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    try:
        convert(args.source, args.folder)
    except Exception as exc:
        (args.folder / 'error.json').write_text(json.dumps({'error': str(exc)}, ensure_ascii=False), encoding='utf-8')
        raise SystemExit(1)
