
import contextlib
from email import policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock

from PIL import Image

from cloud.ai_gateway import Gateway, GatewayHTTPServer, HTTPResponse, Router
from cloud.ecs_image_provider import ImageProvider, ImageRequestUncertain
from cloud.ecs_worker import DailyLimits, ImageQueue, LimitReached
from cloud.professional_creation import ProfessionalCreation
from cloud.windows_image_worker import LocalState
from test_ai_gateway import http_request, make_config


class ECSWorkerTest(unittest.TestCase):
    def test_daily_billing_limit_and_ambiguous_request_survive_restart(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / 'ledger.sqlite3'
            today = datetime(2026, 11, 23, 23, 59)
            limits = DailyLimits(db, 'image', 1, now=lambda: today)
            before = Mock(side_effect=OSError('connection lost after request'))
            channel = ImageProvider('https://images.example/v1', 'test-key', 'gpt-image-2.6-sunburst', transport=before)
            client = Mock()
            page = {'pageId': 'page-a', 'prompt': 'landscape', 'contentType': 'image/png', 'hasReferenceImage': False}
            spool = LocalState(Path(root) / 'image')
            ImageQueue(client, channel, limits, spool).handle(page)
            client.fail.assert_called_once()
            restarted = DailyLimits(db, 'image', 1, now=lambda: today)
            with self.assertRaises(LimitReached):
                restarted.reserve(page_id='page-b')
            today = datetime(2026, 11, 24, 0, 1)
            ImageQueue(client, channel, restarted, spool).handle(page)
            self.assertEqual(before.call_count, 1)
            self.assertEqual(restarted.pending_requests(), ['page-a'])
            self.assertEqual(restarted.remaining(), 1)

    def test_download_receipt_recovers_without_second_provider_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            limits = DailyLimits(root / 'ledger.sqlite3', 'image', 1)
            client, provider = Mock(), Mock()
            page = {'pageId': 'recovery', 'hasReferenceImage': False}
            item = (b'test-image', 'image/png', 100, 100)
            def generate(_, __, *, before_send):
                before_send()
                return item
            provider.generate.side_effect = generate
            client.complete.side_effect = [OSError('connection lost after upload'), None]
            with self.assertRaises(OSError):
                ImageQueue(client, provider, limits, LocalState(root / 'image')).handle(page)
            state = LocalState(root / 'image')
            self.assertEqual(state.load_spool(page['pageId']), item)
            ImageQueue(client, provider, limits, state).handle(page)
            self.assertEqual(provider.generate.call_count, 1)
            self.assertIsNone(state.load_spool(page['pageId']))
            self.assertEqual(limits.pending_requests(), [])

    def test_image_channel_never_sends_key_to_result_origin(self):
        public = 'https://media.example/result.png?sig=fixture'
        result = HTTPResponse(200, {}, json.dumps({'data': [{'url': public}]}).encode())
        transport = Mock(side_effect=[result, HTTPResponse(200, {}, b'not an image')])
        page = {'prompt': 'lake', 'contentType': 'image/png'}
        channel = ImageProvider('https://images.example/v1', 'test-key', 'gpt-image-2.6-sunburst',
                                result_origins=['https://media.example'], transport=transport)
        with self.assertRaises(ImageRequestUncertain):
            channel.generate(page)
        self.assertEqual(transport.call_args_list[0].args[1], 'https://images.example/v1/images/generations')
        self.assertEqual(transport.call_args_list[1].args[:3], ('GET', public, {'Accept': 'image/*'}))

    def test_image_worker_submits_one_wide_edit_with_reference_and_inline_response(self):
        import base64

        reference = io.BytesIO()
        Image.new('RGB', (32, 18), 'navy').save(reference, 'PNG')
        raw = reference.getvalue()
        transport = Mock(return_value=HTTPResponse(200, {}, json.dumps({
            'data': [{'b64_json': base64.b64encode(raw).decode('ascii')}],
        }).encode()))
        channel = ImageProvider('https://images.example/v1', 'test-key', 'gpt-image-2.6-sunburst',
                                size='1536x864', response_format='b64_json', transport=transport)
        page = {'prompt': '参考图测试', 'contentType': 'image/png'}
        self.assertEqual(channel.generate(page, (raw, 'image/png')), (raw, 'image/png', 32, 18))
        transport.assert_called_once()
        method, url, headers, body, _ = transport.call_args.args
        self.assertEqual((method, url), ('POST', 'https://images.example/v1/images/edits'))
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: '+headers['Content-Type']+'\r\n\r\n').encode('ascii') + body)
        parts = {p.get_param('name', header='Content-Disposition'): p for p in message.iter_parts()}
        for key, value in {'model': 'gpt-image-2.6-sunburst', 'prompt': '参考图测试', 'n': '1',
                           'size': '1536x864', 'response_format': 'b64_json'}.items():
            self.assertEqual(parts[key].get_payload(decode=True).decode('utf-8'), value)
        self.assertEqual(parts['image'].get_content_type(), 'image/png')
        self.assertEqual(parts['image'].get_payload(decode=True), raw)


@contextlib.contextmanager
def professional_gateway(transport):
    allowed = 'https://pwmpro-a.github.io'
    config = make_config(allowed_origins=(allowed,), supabase_service_role_key='')
    service = ProfessionalCreation({'url': config.supabase_url, 'anonKey': config.supabase_anon_key,
                                    'workspaceId': config.workspace_id}, '', transport=transport,
                                   chat=Mock(), require_cutover=True)
    with GatewayHTTPServer(('127.0.0.1', 0), Gateway(config), Router(), professional=service) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, allowed
        finally:
            server.shutdown()
            thread.join(timeout=2)


class ECSGatewayTest(unittest.TestCase):
    def test_browser_origin_and_cutover_checked_before_actions(self):
        def reply(_, url, headers, body, timeout):
            self.assertEqual(headers['Authorization'], 'Bearer user-token')
            self.assertNotIn('service', headers['apikey'])
            result = ({'id': '11111111-1111-4111-a111-111111111111'} if url.endswith('/user') else
                      {'mode': 'single-judge', 'role': 'judge'})
            return HTTPResponse(200, {}, json.dumps(result).encode())
        transport = Mock(side_effect=reply)
        with professional_gateway(transport) as (server, allowed):
            path = '/api/ai/generation?action=recent'
            status, headers, _ = http_request(server, 'OPTIONS', path, headers={'Origin': allowed})
            self.assertEqual(status, 204)
            self.assertEqual(headers['access-control-allow-origin'], allowed)
            self.assertEqual(headers['access-control-allow-methods'], 'GET, POST, OPTIONS')
            status, headers, _ = http_request(server, 'POST', '/api/ai/planning/analyze', {}, {'Origin': 'https://other.example'})
            self.assertEqual(status, 403)
            self.assertEqual(headers['connection'], 'close')
            self.assertNotIn('access-control-allow-origin', headers)
            transport.assert_not_called()
            status, _, _ = http_request(server, 'GET', '/api/admin', headers={'Origin': allowed})
            self.assertEqual(status, 404)
            transport.assert_not_called()
            status, headers, _ = http_request(server, 'GET', path, headers={'Origin': allowed, 'Authorization': 'Bearer user-token'})
            self.assertEqual(status, 403)
            self.assertEqual(headers['access-control-allow-origin'], allowed)
            self.assertEqual(transport.call_count, 2)


if __name__ == '__main__':
    unittest.main()
