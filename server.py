"""Local PPT review library. Python 3.10+, Pillow optional for thumbnails."""
from __future__ import annotations
import argparse
import base64
import hashlib
import io
import json
import mimetypes
import math
import re
import sqlite3
import threading
import uuid
import subprocess
import sys
import tempfile
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DEFAULT_SOURCE = Path('C:/Users/LENOVO/Desktop/小红书PPT模板/手动收集/裁切汇总')
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
MAX_BODY = 160 * 1024 * 1024
LOCK = threading.RLock()
SOURCE = DEFAULT_SOURCE
CATALOG = []
FILES = {}
JOBS = {}
CONVERSION_LOCK = threading.Lock()
DOCUMENT_EXTS = {'.ppt', '.pptx', '.pdf'}


def natural(value):
    return [int(s) if s.isdigit() else s.casefold() for s in re.split(r'(\d+)', value)]


@contextmanager
def connect():
    db = sqlite3.connect(DATA / 'library.sqlite3', timeout=20)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        with db:
            yield db
    finally:
        db.close()


def initialize():
    DATA.mkdir(exist_ok=True)
    (DATA / 'imports').mkdir(exist_ok=True)
    (DATA / 'thumbs').mkdir(exist_ok=True)
    (DATA / 'documents').mkdir(exist_ok=True)
    (DATA / 'work').mkdir(exist_ok=True)
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS documents (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        defaults = {
            'state': {'revision': 0, 'maxScore': 5, 'dimensions': [
                {'id': 'visual', 'name': '视觉效果', 'description': '色彩、风格、图像与整体表现力'},
                {'id': 'layout', 'name': '排版', 'description': '层级、对齐、留白与阅读秩序'}
            ], 'reviews': {}},
            'imports': {'decks': [], 'extra': {}}
        }
        for key, value in defaults.items():
            db.execute('INSERT OR IGNORE INTO documents VALUES (?, ?)', (key, json.dumps(value, ensure_ascii=False)))


def read_doc(key):
    with connect() as db:
        return json.loads(db.execute('SELECT value FROM documents WHERE key=?', (key,)).fetchone()[0])


def write_doc(key, value):
    with connect() as db:
        db.execute('UPDATE documents SET value=? WHERE key=?', (json.dumps(value, ensure_ascii=False), key))


def scan():
    global CATALOG, FILES
    decks, files = [], {}
    imported = read_doc('imports')

    def register(path, name=None):
        file_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:28]
        files[file_id] = path
        return {'id': file_id, 'name': name or path.name, 'url': '/image/' + file_id, 'thumb': '/thumb/' + file_id}

    if SOURCE.is_dir():
        for directory in sorted((p for p in SOURCE.iterdir() if p.is_dir()), key=lambda p: natural(p.name)):
            images = sorted((p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_EXTS), key=lambda p: natural(str(p.relative_to(directory))))
            if not images:
                continue
            deck_id = 'source-' + hashlib.sha256(directory.name.encode()).hexdigest()[:18]
            style, sep, title = directory.name.partition('_')
            decks.append({'id': deck_id, 'name': directory.name, 'title': title if sep else style, 'style': style if sep else '本地收藏',
                          'origin': 'source', 'images': [register(p) for p in images]})
    for deck in imported['decks']:
        deck = dict(deck)
        deck['images'] = [register(DATA / 'imports' / item['file'], item['name']) for item in deck['files'] if (DATA / 'imports' / item['file']).is_file()]
        deck.pop('files', None)
        if deck['images']:
            decks.append(deck)
    for deck in decks:
        for item in imported['extra'].get(deck['id'], []):
            path = DATA / 'imports' / item['file']
            if path.is_file():
                deck['images'].append(register(path, item['name']))
    CATALOG, FILES = decks, files


def validate_state(value):
    if not isinstance(value, dict):
        raise ValueError('评分记录格式无效')
    maximum = value.get('maxScore')
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not math.isfinite(maximum) or not 1 <= maximum <= 100:
        raise ValueError('满分必须为 1 至 100 之间的数字')
    dimensions = value.get('dimensions', [])
    if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 12:
        raise ValueError('请保留 1 至 12 个评分项')
    ids = set()
    for d in dimensions:
        if not isinstance(d, dict) or not isinstance(d.get('id'), str) or not 1 <= len(d['id']) <= 80 or d['id'] in ids:
            raise ValueError('评分项标识无效')
        ids.add(d['id'])
        if not isinstance(d.get('name'), str) or not d['name'].strip() or len(d['name']) > 30:
            raise ValueError('评分项名称应为 1 至 30 个字符')
        if not isinstance(d.get('description', ''), str) or len(d.get('description', '')) > 200:
            raise ValueError('评分项说明过长')
    names = [d['name'].strip() for d in dimensions]
    if len(set(names)) != len(names):
        raise ValueError('评分项名称不能重复')
    reviews = value.get('reviews', {})
    if not isinstance(reviews, dict) or len(reviews) > 20000:
        raise ValueError('评分记录无效')
    for deck_id, review in reviews.items():
        if not isinstance(review, dict) or not isinstance(review.get('scores', {}), dict):
            raise ValueError('评分记录格式无效')
        if not isinstance(review.get('note', ''), str) or len(review.get('note', '')) > 10000:
            raise ValueError('备注最多 10000 字')
        if not isinstance(review.get('favorite', False), bool):
            raise ValueError('收藏记录格式无效')
        if review.get('mode', 'deck') not in ('deck', 'page'):
            raise ValueError('评分模式无效')
        pages = review.get('pages', {})
        if not isinstance(pages, dict) or len(pages) > 600:
            raise ValueError('逐页评分记录无效')
        for entry in [review, *pages.values()]:
            if not isinstance(entry, dict) or not isinstance(entry.get('scores'), dict):
                raise ValueError('评分记录格式无效')
            if not isinstance(entry.get('note', ''), str) or len(entry.get('note', '')) > 10000:
                raise ValueError('备注最多 10000 字')
            for score in entry['scores'].values():
                if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= maximum):
                    raise ValueError('评分必须在 0 与满分之间')


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'public'), **kwargs)

    def log_message(self, fmt, *args):
        if args and ('/image/' in str(args[0]) or '/thumb/' in str(args[0])):
            return
        super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        super().end_headers()

    def json_response(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        size = int(self.headers.get('Content-Length', '0'))
        if size <= 0 or size > MAX_BODY:
            raise ValueError('本次导入过大，请分批导入（每批小于 100 MB）')
        return json.loads(self.rfile.read(size))

    def check_origin(self):
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            raise ValueError('请在本地评分页面内执行此操作')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise ValueError('不支持跨站请求')

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/health':
            return self.json_response({'app': 'ppt-review', 'version': 2})
        if path.startswith('/api/import-jobs/'):
            with LOCK:
                job = JOBS.get(path.rsplit('/', 1)[-1])
                if job is None:
                    return self.json_response({'error': '导入任务不存在，请刷新图片库查看结果'}, 404)
                result = {k: v for k, v in job.items() if k != 'progressPath'}
                progress_path = job.get('progressPath')
                if progress_path and Path(progress_path).is_file():
                    try:
                        result.update(json.loads(Path(progress_path).read_text(encoding='utf-8')))
                    except (OSError, ValueError):
                        pass
                if job['status'] == 'completed':
                    result['decks'] = CATALOG
                return self.json_response(result)
        if path == '/api/library':
            with LOCK:
                return self.json_response({'decks': CATALOG, 'state': read_doc('state'), 'source': str(SOURCE), 'sourceAvailable': SOURCE.is_dir()})
        if path == '/api/backup':
            with LOCK:
                return self.json_response({'format': 'ppt-review-backup', 'version': 1, 'exportedAt': datetime.now(timezone.utc).isoformat(),
                                           'state': read_doc('state'), 'decks': [{k: v for k, v in d.items() if k != 'images'} for d in CATALOG]})
        if path.startswith(('/image/', '/thumb/')):
            file_id = path.rsplit('/', 1)[-1]
            with LOCK:
                image_path = FILES.get(file_id)
            if image_path is None or not image_path.is_file():
                return self.json_response({'error': '图片不存在，请刷新图片库'}, 404)
            if path.startswith('/thumb/'):
                image_path = self.thumbnail(image_path, file_id)
            try:
                stat = image_path.stat()
                etag = '"%x-%x"' % (stat.st_mtime_ns, stat.st_size)
                if self.headers.get('If-None-Match') == etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', mimetypes.guess_type(image_path.name)[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(stat.st_size))
                self.send_header('ETag', etag)
                self.send_header('Cache-Control', 'private, max-age=3600')
                self.end_headers()
                with image_path.open('rb') as stream:
                    while chunk := stream.read(128 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path.startswith('/api/'):
            return self.json_response({'error': '接口不存在'}, 404)
        super().do_GET()

    def thumbnail(self, path, file_id):
        try:
            from PIL import Image, ImageOps
            signature = str(path.stat().st_mtime_ns) + str(path.stat().st_size)
            output = DATA / 'thumbs' / (file_id + '-' + hashlib.md5(signature.encode()).hexdigest()[:8] + '.jpg')
            if not output.exists():
                with Image.open(path) as image:
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((760, 600))
                    if image.mode != 'RGB':
                        rgba = image.convert('RGBA')
                        image = Image.new('RGB', rgba.size, '#ffffff')
                        image.paste(rgba, mask=rgba.getchannel('A'))
                    temp = output.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                    image.save(temp, 'JPEG', quality=85, optimize=True)
                    temp.replace(output)
            return output
        except Exception:
            return path

    def do_PUT(self):
        if urlsplit(self.path).path != '/api/state':
            return self.json_response({'error': '接口不存在'}, 404)
        try:
            self.check_origin()
            incoming = self.body_json()
            validate_state(incoming)
            with LOCK:
                current = read_doc('state')
                if incoming.get('revision') != current['revision']:
                    return self.json_response({'error': '其他窗口已更新评分。请先导出当前记录，再刷新页面继续。'}, 409)
                sanitized = {key: incoming[key] for key in ('maxScore', 'dimensions', 'reviews')}
                sanitized['revision'] = current['revision'] + 1
                write_doc('state', sanitized)
                return self.json_response({'revision': sanitized['revision']})
        except (ValueError, TypeError, KeyError) as exc:
            self.json_response({'error': str(exc)}, 400)
        except Exception:
            self.json_response({'error': '保存失败，请检查本地磁盘后重试'}, 500)

    def do_POST(self):
        try:
            self.check_origin()
            path = urlsplit(self.path).path
            if path == '/api/rescan':
                with LOCK:
                    scan()
                    return self.json_response({'decks': CATALOG, 'sourceAvailable': SOURCE.is_dir()})
            if path == '/api/import':
                value = self.body_json()
                return self.import_images(value)
            if path == '/api/import-documents':
                return self.import_documents(self.body_json())
            return self.json_response({'error': '接口不存在'}, 404)
        except (ValueError, TypeError, KeyError) as exc:
            return self.json_response({'error': str(exc)}, 400)
        except Exception:
            return self.json_response({'error': '导入失败，请检查文件格式与本地磁盘'}, 500)

    def import_documents(self, value):
        items = value.get('files', [])
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise ValueError('每批请选择 1 至 10 份 PPT 或 PDF')
        staged = []
        total = 0
        for item in items:
            name = str(item.get('name', '')).replace('\\', '/').rsplit('/', 1)[-1][:240]
            extension = Path(name).suffix.lower()
            if extension not in DOCUMENT_EXTS:
                raise ValueError('请选择 PPT、PPTX 或 PDF 文件')
            raw = base64.b64decode(item['data'], validate=True)
            total += len(raw)
            if not raw or len(raw) > 100 * 1024 * 1024 or total > 100 * 1024 * 1024:
                raise ValueError('每批文档总大小需小于 100 MB')
            staged.append((name, extension, raw))
        with LOCK:
            target = value.get('target') or None
            if target and not any(d['id'] == target for d in CATALOG):
                raise ValueError('目标作品不存在，请刷新后重试')
            if sum(j['status'] in ('queued', 'converting') for j in JOBS.values()) >= 3:
                raise ValueError('已有文档正在导入，请等待完成后重试')
            job_id = uuid.uuid4().hex
            JOBS[job_id] = {'id': job_id, 'status': 'queued', 'message': '等待转换文档…'}
            # Bound retained job metadata while keeping all active jobs.
            for key in list(JOBS)[:-30]:
                if JOBS[key]['status'] in ('completed', 'failed'):
                    JOBS.pop(key, None)
        threading.Thread(target=run_document_job, args=(job_id, staged, target, str(value.get('name') or '')[:100]), daemon=True).start()
        return self.json_response({'jobId': job_id}, 202)

    def import_images(self, value):
        items = value.get('images', [])
        if not isinstance(items, list) or not items or len(items) > 600:
            raise ValueError('每次请选择 1 至 600 张图片')
        with LOCK:
            existing_id = value.get('target')
            if existing_id and not any(d['id'] == existing_id for d in CATALOG):
                raise ValueError('目标作品不存在，请刷新后重试')
            staged = []
            for item in items:
                name = str(item.get('name', ''))[:240]
                ext = Path(name).suffix.lower()
                if ext not in IMAGE_EXTS:
                    raise ValueError('请选择 JPG、PNG、WEBP、GIF 或 BMP 图片')
                raw = base64.b64decode(item['data'], validate=True)
                if not raw or len(raw) > 25 * 1024 * 1024:
                    raise ValueError('单张图片需小于 25 MB')
                try:
                    from PIL import Image
                    with Image.open(io.BytesIO(raw)) as image:
                        image.verify()
                except ImportError:
                    pass
                except Exception:
                    raise ValueError('图片无法读取：' + name)
                filename = hashlib.sha256(raw).hexdigest() + ext
                staged.append((item, raw, filename, name))
            imported = read_doc('imports')
            added, skipped, created = 0, 0, []
            groups = {}
            for item, raw, filename, name in staged:
                if existing_id:
                    destination = imported['extra'].setdefault(existing_id, [])
                else:
                    group = str(item.get('group') or value.get('name') or '未命名作品').strip()[:100] or '未命名作品'
                    if group not in groups:
                        deck_id = 'import-' + uuid.uuid4().hex[:18]
                        style, sep, title = group.partition('_')
                        deck = {'id': deck_id, 'name': group, 'title': title if sep else group, 'style': style if sep else '新导入', 'origin': 'import', 'files': []}
                        imported['decks'].append(deck)
                        groups[group] = deck
                        created.append(deck_id)
                    destination = groups[group]['files']
                if any(x['file'] == filename for x in destination):
                    skipped += 1
                    continue
                output = DATA / 'imports' / filename
                if not output.exists():
                    output.write_bytes(raw)
                destination.append({'file': filename, 'name': name})
                added += 1
            write_doc('imports', imported)
            scan()
            self.json_response({'decks': CATALOG, 'added': added, 'skipped': skipped, 'created': created, 'target': existing_id})


def run_document_job(job_id, staged, target, title):
    job = JOBS[job_id]
    try:
        with CONVERSION_LOCK, tempfile.TemporaryDirectory(prefix='document-', dir=DATA / 'work') as directory:
            work = Path(directory)
            converted = []
            with LOCK:
                job.update(status='converting', message='正在准备文档…')
            for index, (name, extension, raw) in enumerate(staged):
                folder = work / str(index)
                folder.mkdir()
                source = folder / ('original' + extension)
                source.write_bytes(raw)
                with LOCK:
                    job.update(message='正在转换 ' + name, progressPath=str(folder / 'progress.json'), fileName=name, fileIndex=index + 1, fileCount=len(staged))
                try:
                    result = subprocess.run([sys.executable, str(ROOT / 'document_import.py'), str(source), str(folder)], capture_output=True, timeout=240, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except subprocess.TimeoutExpired:
                    raise ValueError('转换超时：' + name + '。请将文件另存为 PDF 或拆分后重试。')
                if result.returncode:
                    detail = folder / 'error.json'
                    message = json.loads(detail.read_text(encoding='utf-8'))['error'] if detail.is_file() else '转换程序未能完成，请检查 PyMuPDF 与 PowerPoint 是否可用'
                    raise ValueError(name + '：' + message)
                pages = json.loads((folder / 'result.json').read_text(encoding='utf-8'))['pages']
                converted.append((name, extension, raw, folder, pages))
            with LOCK:
                imported = read_doc('imports')
                if target and not any(d['id'] == target for d in CATALOG):
                    raise ValueError('目标作品不存在，请刷新后重试')
                created, added = [], 0
                for name, extension, raw, folder, pages in converted:
                    doc_hash = hashlib.sha256(raw).hexdigest()
                    original_name = doc_hash + extension
                    original_output = DATA / 'documents' / original_name
                    if not original_output.exists():
                        original_output.write_bytes(raw)
                    if target:
                        destination = imported['extra'].setdefault(target, [])
                    else:
                        deck_name = title.strip() if len(converted) == 1 and title.strip() else Path(name).stem
                        style, sep, deck_title = deck_name.partition('_')
                        deck = {'id': 'import-' + uuid.uuid4().hex[:18], 'name': deck_name, 'title': deck_title if sep else deck_name,
                                'style': style if sep else extension[1:].upper() + ' 导入', 'origin': 'import', 'document': {'name': name, 'file': original_name, 'format': extension[1:]}, 'files': []}
                        imported['decks'].append(deck)
                        destination = deck['files']
                        created.append(deck['id'])
                    for page in pages:
                        # Include document identity and page number to preserve repeated/blank pages.
                        filename = doc_hash + '-page-%04d.png' % page['page']
                        output = DATA / 'imports' / filename
                        if not output.exists():
                            shutil.copyfile(folder / page['file'], output)
                        destination.append({'file': filename, 'name': Path(name).stem + ' · 第 %d 页.png' % page['page']})
                        added += 1
                write_doc('imports', imported)
                scan()
                job.pop('progressPath', None)
                job.update(status='completed', message='文档导入完成', added=added, created=created, target=target)
    except Exception as exc:
        with LOCK:
            job.pop('progressPath', None)
            job.update(status='failed', error=str(exc), message='导入未完成，请检查文件后重试')


def main():
    global SOURCE
    parser = argparse.ArgumentParser(description='PPT review local web application')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--professional-config', type=Path, metavar='JSON')
    args = parser.parse_args()
    if args.professional_config is not None:
        from cloud.local_professional import ProfessionalServer, load_browser_config
        from cloud.professional_creation import ProfessionalCreation
        try:
            public_config = load_browser_config(args.professional_config)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        creation = ProfessionalCreation.from_local_secrets(public_config)
        with ProfessionalServer(args.port, ROOT / 'public', public_config, creation=creation) as server:
            print(f'Guanye Professional: http://127.0.0.1:{server.server_port}', flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return
    SOURCE = args.source.resolve()
    initialize()
    scan()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    print(f'PPT Review: http://127.0.0.1:{args.port} ({len(CATALOG)} sets)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
