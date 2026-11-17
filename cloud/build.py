"""Build static Pages output. Only a public Supabase key may enter this bundle."""
import base64
import json
import os
from pathlib import Path
import shutil
import uuid
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

def build():
    values = {k: os.environ.get(k, '').strip() for k in ('SUPABASE_URL', 'SUPABASE_ANON_KEY', 'WORKSPACE_ID')}
    if not all(values.values()):
        raise SystemExit('Configure SUPABASE_URL, SUPABASE_ANON_KEY and WORKSPACE_ID first')
    key = values['SUPABASE_ANON_KEY']
    if not key.startswith('sb_publishable_'):
        try:
            payload = key.split('.')[1]
            role = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['role']
        except Exception:
            raise SystemExit('A Supabase publishable / anon key is required')
        if role != 'anon':
            raise SystemExit('Never deploy a service-role or secret key')
    url = urlparse(values['SUPABASE_URL'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise SystemExit('SUPABASE_URL must be an HTTPS project URL')
    uuid.UUID(values['WORKSPACE_ID'])
    destination = ROOT / 'dist'
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(ROOT / 'public', destination)
    config = dict(url=values['SUPABASE_URL'], anonKey=key, workspaceId=values['WORKSPACE_ID'])
    (destination / 'cloud-config.js').write_text('window.CLOUD_CONFIG = '+json.dumps(config)+';\n', encoding='utf-8')
    (destination / '.nojekyll').touch()
    print('Built dist: static frontend, no local images or scores included')

if __name__ == '__main__':
    build()
