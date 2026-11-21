"""Build static Pages output. Only public browser configuration may enter this bundle."""
import base64
import json
import os
from pathlib import Path
import shutil
import uuid
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def _public_https_url(value, name):
    parsed = urlparse(value)
    local_http = parsed.scheme == 'http' and (parsed.hostname or '').lower() in {'localhost', '127.0.0.1', '::1'}
    if parsed.scheme != 'https' and not local_http:
        raise SystemExit(name + ' must use HTTPS')
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit(name + ' must be a credential-free site URL')
    return value.rstrip('/')


def build(environ=None, root=None):
    source = os.environ if environ is None else environ
    project_root = ROOT if root is None else Path(root)
    values = {
        key: source.get(key, '').strip()
        for key in ('SUPABASE_URL', 'SUPABASE_ANON_KEY', 'WORKSPACE_ID', 'AI_GATEWAY_URL')
    }
    required = ('SUPABASE_URL', 'SUPABASE_ANON_KEY', 'WORKSPACE_ID')
    if not all(values[key] for key in required):
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

    supabase_url = _public_https_url(values['SUPABASE_URL'], 'SUPABASE_URL')
    try:
        workspace_id = str(uuid.UUID(values['WORKSPACE_ID']))
    except ValueError as exc:
        raise SystemExit('WORKSPACE_ID must be a UUID') from exc

    config = dict(url=supabase_url, anonKey=key, workspaceId=workspace_id)
    if values['AI_GATEWAY_URL']:
        config['aiGatewayUrl'] = _public_https_url(values['AI_GATEWAY_URL'], 'AI_GATEWAY_URL')

    destination = project_root / 'dist'
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(project_root / 'public', destination)
    (destination / 'cloud-config.js').write_text(
        'window.CLOUD_CONFIG = ' + json.dumps(config, ensure_ascii=False) + ';\n',
        encoding='utf-8',
    )
    (destination / '.nojekyll').touch()
    print('Built dist: static frontend, no local images, scores or server secrets included')


if __name__ == '__main__':
    build()
