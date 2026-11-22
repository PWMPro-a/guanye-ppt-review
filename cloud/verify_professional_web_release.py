"""Read-only release preflight for an existing Guanye professional site."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

REQUIRED_CHECKS = {
    'Build and verify gateway image', 'Edge Function TypeScript tests',
    'Node 24 SQL tests', 'Playwright browser smoke tests', 'Python 3.12 tests',
}
CONFIG_PREFIX = 'window.CLOUD_CONFIG = '
CHECK_LIMIT = 1024 * 1024
CHECKOUT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def site_endpoint(value, label):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError(f'{label} must be a credential-free HTTPS URL')
    return value.rstrip('/')


def http_get(url):
    with build_opener(ProxyHandler(), NoRedirect()).open(url, timeout=15) as response:
        data = response.read(CHECK_LIMIT + 1)
        if response.status != 200 or len(data) > CHECK_LIMIT:
            raise ValueError('unexpected response')
        return data


def config(raw):
    text = raw.decode('utf-8').strip()
    if not text.startswith(CONFIG_PREFIX) or not text.endswith(';'):
        raise ValueError('Pages configuration has an unexpected format')
    obj = json.loads(text[len(CONFIG_PREFIX):-1])
    if not isinstance(obj, dict) or not obj.get('workspaceId') or not obj.get('url'):
        raise ValueError('Pages workspace configuration is incomplete')
    return obj


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def check_backup(root):
    copy = read_json(root / 'last-verified.json')
    restore = read_json(root / 'last-restore-verification.json')
    remote = PurePosixPath(copy['copy'])
    if remote.parent != PurePosixPath('/var/backups/guanye-encrypted') or not re.fullmatch(r'daily-\d{8}-\d{6}\.tar\.age', remote.name):
        raise ValueError('unexpected offsite archive path')
    if remote.name != restore['archive'] or copy['sha256'] != restore['sha256']:
        raise ValueError('restore evidence does not cover the latest verified offsite copy')
    if restore['objects'] != copy['objects'] or restore['objects'] <= 0:
        raise ValueError('restore and offsite private file counts differ')
    vault_empty = restore.get('vaultEmpty') is True
    if restore['tables'] + int(vault_empty) != copy['tables'] or restore['tables'] <= 0:
        raise ValueError('restore does not account for all database tables in the manifest')
    age = datetime.now(timezone.utc) - datetime.fromisoformat(copy['verifiedAt'])
    if not timedelta(0) <= age <= timedelta(hours=26):
        raise ValueError('last verified offsite copy is too old or timestamp is invalid')
    if sha256(root / remote.name) != copy['sha256']:
        raise ValueError('local archive SHA-256 differs from verified offsite copy')
    return f"{remote.name}: {restore['objects']} files; {restore['tables']} restored tables"


def check_pages(site, artifact, expected_digest):
    if not re.fullmatch(r'[a-f0-9]{64}', expected_digest) or sha256(artifact) != expected_digest:
        raise ValueError('rollback artifact SHA-256 differs from the recorded value')
    live_config = http_get(site + '/cloud-config.js')
    live_index = http_get(site + '/')
    with tarfile.open(artifact, 'r:*') as package:
        def read(name):
            item = package.getmember('./'+name)
            if not item.isfile() or item.size > CHECK_LIMIT:
                raise ValueError('unexpected rollback artifact file')
            with package.extractfile(item) as stream:
                return stream.read()
        if read('cloud-config.js') != live_config or read('index.html') != live_index:
            raise ValueError('rollback artifact differs from the current public Pages build')
    if config(live_config).get('webProfessional') is True:
        raise ValueError('public Pages is already in professional mode')
    return 'public index and configuration match the saved team artifact'


def command(args):
    executable = shutil.which(args[0])
    if executable is None:
        raise ValueError(f'{args[0]} executable not found')
    result = subprocess.run([executable, *args[1:]], cwd=CHECKOUT, capture_output=True, encoding='utf-8', timeout=100)
    if result.returncode:
        raise ValueError(f'{args[0]} command failed with exit {result.returncode}')
    return result.stdout.strip()


def check_checks(repo, pr):
    fields = 'state,mergeable,headRefOid,baseRefName,statusCheckRollup'
    report = json.loads(command(['gh','pr','view',str(pr),'--repo',repo,'--json',fields]))
    if (report['state'], report['baseRefName'], report['mergeable']) != ('OPEN','main','MERGEABLE'):
        raise ValueError('release PR is not mergeable into main')
    if command(['git','rev-parse','HEAD']) != report['headRefOid'] or command(['git','status','--porcelain']):
        raise ValueError('checked-out tree does not match the clean PR revision')
    checks = {run['name']: run['conclusion'] for run in report['statusCheckRollup'] if run.get('status') == 'COMPLETED'}
    missing = sorted(name for name in REQUIRED_CHECKS if checks.get(name) != 'SUCCESS')
    if missing:
        raise ValueError('required checks not successful: ' + ', '.join(missing))
    return report['headRefOid'][:12] + ': all five required checks passed'


def check_database():
    plan = json.loads(command([sys.executable, '-X','utf8','-B','-m','cloud.manage','migration-plan']))
    if (plan['current_type'], plan['history_status'], plan['pending_versions'], plan.get('read_only')) != ('managed_complete','complete',[],True):
        raise ValueError('database migration plan is not complete')
    return plan['project_ref']


def check_https(gateway):
    # urllib checks the certificate chain and the hostname or IP SAN.
    if http_get(gateway + '/healthz').strip() != b'ok':
        raise ValueError('unexpected public TLS health response')
    return 'public TLS certificate accepted; health endpoint returned 200'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--pr', required=True, type=int)
    parser.add_argument('--site', required=True)
    parser.add_argument('--gateway', required=True)
    parser.add_argument('--rollback-artifact', required=True, type=Path)
    parser.add_argument('--rollback-sha256', required=True)
    parser.add_argument('--backup-directory', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', args.repository) or args.pr < 1:
        parser.error('invalid repository or PR number')
    site = site_endpoint(args.site, 'Pages site')
    gateway = site_endpoint(args.gateway, 'Gateway')
    if urlsplit(gateway).path:
        parser.error('Gateway URL must contain only the origin')
    def database_for_site():
        ref = check_database()
        deployed = config(http_get(site + '/cloud-config.js'))
        if urlsplit(deployed['url']).hostname != ref + '.supabase.co':
            raise ValueError('Pages and the migration plan target different Supabase projects')
        return 'database migration history matches this revision and the Pages project'

    checks = (
        ('release_revision', lambda: check_checks(args.repository,args.pr)),
        ('rollback_pages', lambda: check_pages(site,args.rollback_artifact,args.rollback_sha256)),
        ('database_plan', database_for_site),
        ('backup_and_restore', lambda: check_backup(args.backup_directory)),
        ('public_https', lambda: check_https(gateway)),
    )
    results = {}
    for key, check in checks:
        try:
            results[key] = {'status':'pass','detail':check()}
        except ValueError as exc:
            results[key] = {'status':'fail','detail':str(exc)}
        except (OSError, KeyError, subprocess.SubprocessError, tarfile.TarError) as exc:
            results[key] = {'status':'fail','detail':type(exc).__name__}
    print(json.dumps({'checks':results,'cutover_authorized':False,'separate_gates':['host_firewall','live_reference_image_generation','production_smoke']}, ensure_ascii=False,indent=2))
    return int(any(x['status'] != 'pass' for x in results.values()))


if __name__ == '__main__':
    raise SystemExit(main())
