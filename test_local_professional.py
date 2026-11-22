from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from cloud.local_professional import ProfessionalServer, load_browser_config

PUBLIC = {"url": "https://fixture.supabase.co", "anonKey": "sb_publishable_fixturetesting",
          "workspaceId": "937c868a-8e42-4043-b3e4-c7cfa05076b5"}


class LocalProfessionalTests(unittest.TestCase):
    def test_rejects_secrets_malformed_and_unexpected_fields(self):
        with TemporaryDirectory(prefix='观页 fixture ') as temporary:
            path = Path(temporary) / '项目 公开.json'
            for payload in (
                {**PUBLIC, 'anonKey': 'sb_secret_should_not_expose'},
                {**PUBLIC, 'anonKey': 'a.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.z'},
                {**PUBLIC, 'url': 'https://user@example.net/'},
                {**PUBLIC, 'url': 'http://127.0.0.1'},
                {**PUBLIC, 'workerToken': 'never-serve-to-the-browser'},
            ):
                path.write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '只允许 HTTPS') as fault:
                    load_browser_config(path)
                self.assertNotIn('should_not_expose', str(fault.exception))
            path.write_text(json.dumps(PUBLIC), encoding='utf-8')
            self.assertEqual(load_browser_config(path), {**PUBLIC, 'singleJudge': True})

    def test_loopback_config_is_public_only_and_local_legacy_routes_are_closed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'index.html').write_text('<h1>观页</h1>', encoding='utf-8')
            (root / 'assets').mkdir()
            with ProfessionalServer(0, root, {**PUBLIC, 'singleJudge': True}) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertEqual(server.server_address[0], '127.0.0.1')
                    def call(method, path, host=None):
                        connection = HTTPConnection(*server.server_address, timeout=2)
                        try:
                            connection.request(method, path, headers={'Host': host} if host else {})
                            result = connection.getresponse()
                            return result.status, dict(result.getheaders()), result.read()
                        finally:
                            connection.close()
                    status, headers, payload = call('GET', '/cloud-config.js')
                    self.assertEqual(status, 200)
                    self.assertEqual(headers['Cache-Control'], 'no-store')
                    self.assertIn(b'"singleJudge": true', payload)
                    self.assertIn(b'"localReview": true', payload)
                    self.assertNotIn(b'"aiGatewayUrl"', payload)
                    self.assertEqual(call('GET', '/api/health', 'outside.invalid')[0], 421)
                    health = json.loads(call('GET', '/api/health')[2])
                    self.assertEqual(health['mode'], 'single-judge')
                    self.assertGreater(health['pid'], 0)
                    self.assertEqual(call('GET', '/')[0], 200)
                    self.assertEqual(call('GET', '/assets/')[0], 404)
                    self.assertEqual(call('GET', '/api/library')[0], 404)
                    self.assertEqual(call('PUT', '/api/state')[0], 501)
                    self.assertEqual(call('GET', '/image/private')[0], 404)
                finally:
                    server.shutdown()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
