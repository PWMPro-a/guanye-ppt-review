"""Upload the local catalog to private Supabase Storage, using server credentials only.

Requires SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, WORKSPACE_ID.
Run schema.sql and private bootstrap.sql before this script. Re-running is safe.
"""
import argparse
import base64
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=server.DEFAULT_SOURCE)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    # Read existing data without changing the local database or review state.
    server.SOURCE = args.source
    server.scan()
    print(f'{len(server.CATALOG)} sets / {sum(len(d["images"]) for d in server.CATALOG)} images')
    if args.dry_run:
        return
    url = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    workspace = str(uuid.UUID(os.environ['WORKSPACE_ID']))
    if not key.startswith('sb_secret_'):
        payload = key.split('.')[1]
        if json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('role') != 'service_role':
            raise SystemExit('A server-only service-role key is required for initial migration')

    def request(route, data, mime='application/json', method='POST', extra=None):
        headers = {'apikey':key, 'Authorization':'Bearer '+key, 'Content-Type':mime, **(extra or {})}
        req = urllib.request.Request(url+route, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            # Never include request headers / credentials in error output.
            raise RuntimeError(f'Cloud request failed ({exc.code}): {exc.read().decode()[:500]}') from None

    from PIL import Image, ImageOps
    for index, original in enumerate(server.CATALOG, 1):
        deck = {k:v for k,v in original.items() if k != 'images'}
        deck['images'] = []
        for image in original['images']:
            file = server.FILES[image['id']]
            content = file.read_bytes()
            digest = hashlib.sha256(content).hexdigest()[:24]
            path = f'{workspace}/{deck["id"]}/{digest}{file.suffix.lower()}'
            route = '/storage/v1/object/ppt-library/'+urllib.parse.quote(path, safe='/')
            request(route, content, mimetypes.guess_type(file.name)[0] or 'application/octet-stream', extra={'x-upsert':'true'})
            thumb_path = f'{workspace}/{deck["id"]}/{digest}-thumb.jpg'
            with Image.open(io.BytesIO(content)) as picture:
                picture = ImageOps.exif_transpose(picture).convert('RGB')
                picture.thumbnail((640,640))
                buffer = io.BytesIO()
                picture.save(buffer, 'JPEG', quality=82)
            request('/storage/v1/object/ppt-library/'+thumb_path, buffer.getvalue(), 'image/jpeg', extra={'x-upsert':'true'})
            deck['images'].append(dict(id=image['id'],name=image['name'],path=path,thumbPath=thumb_path))
        row = dict(id=deck['id'],workspace_id=workspace,data=deck)
        request('/rest/v1/decks?on_conflict=id', json.dumps(row,ensure_ascii=False).encode(), extra={'Prefer':'resolution=merge-duplicates'})
        print(f'Uploaded {index}/{len(server.CATALOG)}: {len(deck["images"])} images')
    print('Library uploaded. Private bucket access still requires team membership.')

if __name__ == '__main__':
    main()
