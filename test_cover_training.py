from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from PIL import Image

from cloud.ai_gateway import HTTPResponse
from cloud.cover_image_channel import CoverImageChannel, ENDPOINT, MODELS
from cloud.cover_training import compare, run_pair, split, original_visual, style_facts, visual_style_prompt
from cloud.ai_planning import _cover_style_facts
from cloud.windows_image_worker import WindowsWorker, WorkerConfig, WorkerError


def picture():
    data = BytesIO()
    Image.new('RGB', (32, 18), '#345678').save(data, 'PNG')
    return data.getvalue()


class CoverVisualLearningTest(unittest.TestCase):
    def test_style_facts_are_required_and_become_generation_constraints(self):
        facts = style_facts({
            "palette": [{"hex": "#123abc", "role": "背景", "share": "约 70%"}],
            "typography": "标题粗体，左对齐，副标题为标题的 40%",
            "composition": "主体位于左侧三分之一，右侧保留大面积留白",
            "whitespace": "右侧约 45% 留白",
            "decoration": "仅有一条细分隔线",
            "avoid": "不加渐变、发光和横线堆叠",
        })
        prompt = visual_style_prompt(facts)
        self.assertIn('#123ABC', prompt)
        self.assertIn('不复制参考图的文字', prompt)
        self.assertIn('仅有一条细分隔线', prompt)

    def test_incomplete_plan_evidence_is_repaired_from_the_pinned_image(self):
        calls = []
        evidence = {
            "palette": [{"hex": "#123abc", "role": "背景", "share": "约 70%"}],
            "typography": "标题粗体，左对齐，副标题为标题的 40%",
            "composition": "主体位于左侧三分之一，右侧保留大面积留白",
            "whitespace": "右侧约 45% 留白",
            "decoration": "不存在额外装饰",
            "avoid": "不照抄原文、标识或水印",
        }

        class Ctx:
            def chat_json(self, messages):
                calls.append(messages)
                return {"coverStyleEvidence": evidence}

        facts = _cover_style_facts(
            Ctx(), {"deckId": "deck-one", "title": "授权封面"},
            {"mime_type": "image/png", "bytes": picture()}, {},
        )
        self.assertEqual(facts["palette"][0]["hex"], "#123ABC")
        self.assertEqual(len(calls), 1)
        parts = calls[0][1]["content"]
        self.assertEqual(parts[-1]["type"], "image_url")
        self.assertTrue(parts[-1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_original_visual_rejects_invalid_and_keeps_digest_of_exact_bytes(self):
        mime, digest = original_visual(picture())
        self.assertEqual(mime, 'image/png')
        self.assertEqual(len(digest), 64)
        with self.assertRaises(ValueError):
            original_visual(b'not an image')


class CoverLearningTest(unittest.TestCase):
    def test_only_assisted_sees_corrected_examples_and_style(self):
        context = {'id': 'holdout', 'scoreId': 'private-id', 'human': {'visual': 1.25, 'layout': 2.75},
                   'imagePath': 'target.png', 'cases': [{'id': 'calibration', 'imagePath': 'case.png',
                       'human': {'visual': 4, 'layout': 3}, 'correctionId': 'correction-1',
                       'previousAI': {'visual': 5, 'layout': 3}, 'reason': 'specific'},
                   ], 'rules': {'version': 3, 'markdown': 'Use a grid'}}
        prompts = []

        def model(messages):
            prompts.append(messages)
            return {'scores': {'visual': 2, 'layout': 3}, 'reasons': {'visual': 'Art', 'layout': 'Alignment'}}

        result = run_pair(context, lambda p, size: picture(), model, recheck=lambda: context)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0][0], prompts[1][0])
        self.assertEqual(prompts[0][1]['content'], prompts[1][1]['content'][-2:])
        self.assertNotIn('specific', repr(prompts[0]))
        self.assertIn('specific', repr(prompts[1]))
        self.assertNotIn('private-id', repr(prompts))
        self.assertNotIn('1.25', repr(prompts))
        self.assertEqual(result['correctionIds'], ['correction-1'])
        self.assertEqual(result['styleVersion'], 3)
        with self.assertRaises(PermissionError):
            run_pair(context, lambda p, size: picture(), model, recheck=lambda: {})
        self.assertEqual(len(prompts), 2)

    def test_holdout_threshold_leakage_and_false_pass(self):
        def row(key, group, human):
            return {'id': key, 'sourceGroupSha256': f'{group:02x}' + '0' * 62, 'pageType': 'cover',
                    'aiAllowed': True, 'scope': 'page', 'rubricVersion': 'v1', 'maxScore': 5, 'human': human}
        good = {'visual': 3, 'layout': 4}
        rows = [row('train', 3, good)] + [row(f'held-{i}', i * 5, good) for i in range(5)]
        rows.append(row('same-source', 0, good))
        rows.append({**row('unknown', 1, good), 'sourceGroupSha256': None})
        partition = split(rows, 'v1')
        self.assertEqual(len(partition['training']), 1)
        self.assertEqual(partition['excluded'], {'unknownSource': 1})
        trials = {'rubricVersion': 'v1', 'modelConfigId': 'mock', 'rows': [
            {'id': item['id'], 'baselineCaseIds': [], 'assistedCaseIds': ['train'],
             'baseline': {'visual': 4, 'layout': 4}, 'assisted': good} for item in partition['holdout']]}
        self.assertEqual(compare(partition, trials)['status'], 'pass')
        self.assertEqual(compare(partition, trials)['pairedSourceGroups'], 5)
        trials['rows'][0]['assisted'] = {'visual': 4, 'layout': 4}
        self.assertTrue(compare(partition, trials)['reviewQueue'][0]['falsePass'])
        trials['rows'][0]['assistedCaseIds'] = [trials['rows'][1]['id']]
        with self.assertRaises(ValueError):
            compare(partition, trials)
        trials['rows'].pop(0)
        self.assertEqual(compare(partition, trials)['status'], 'insufficient')


class CoverCreateContractTest(unittest.TestCase):
    def test_web_cover_payload_uses_text_only_description_without_losing_role(self):
        from cloud.ai_generation import _normalize_pages
        from cloud.professional_creation import CreationContext, RPC

        pages = _normalize_pages([{'title': 'Wetlands', 'narrativeRole': 'cover',
                                   'copy': [{'kind': 'title', 'text': 'Wetlands'}]}],
                                 'Editorial', '', structured_plan=True)
        service = Mock(workspace='workspace')
        ctx = CreationContext(service, 'judge', 'token', {}, {})
        ctx.cover_reference = ('synthetic', 'workspace/synthetic/preview/1.png')
        params = dict.fromkeys(RPC['ai_generation_create'][1])
        params['p_pages'] = pages
        ctx.rpc('ai_generation_create', params)
        call = service._json.call_args
        self.assertEqual(call.args[2], '/rest/v1/rpc/single_judge_cover_start')
        sent, = call.kwargs['value']['p_pages']
        self.assertEqual(sent['description'], {'text': pages[0]['description']['text']})
        self.assertEqual(sent['outline']['narrativeRole'], 'cover')
        self.assertEqual(sent['prompt'], pages[0]['prompt'])
        self.assertEqual(pages[0]['description']['narrativeRole'], 'cover')


class CoverImageChannelTest(unittest.TestCase):
    def test_reference_multipart_and_actual_png_decode(self):
        calls = []
        raw = picture()
        page_id = '2aa85153-16d3-47c8-9afa-d89ad4987ded'

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, headers, body))
            return HTTPResponse(200, {}, json.dumps({'data': [{'b64_json': base64.b64encode(raw).decode()}]}).encode())

        channel = CoverImageChannel('example-only-fixture', transport=transport)
        self.assertEqual(channel.generate({'pageId': page_id, 'prompt': 'Keep approved copy'}, (raw, 'image/png')), (raw, 'image/png', 32, 18))
        self.assertEqual(len(calls), 1)
        method, url, headers, payload = calls[0]
        self.assertIn('Mozilla/5.0', headers['User-Agent'])
        self.assertEqual(headers['Accept'], 'application/json')
        self.assertEqual(headers['Idempotency-Key'], page_id)
        self.assertEqual((method, url), ('POST', ENDPOINT))
        self.assertIn(MODELS[0].encode(), payload)
        self.assertIn(raw, payload)
        self.assertIn(b'Keep approved copy', payload)

    def test_known_denial_ambiguous_timeout_and_bad_image_never_resend(self):
        for response, text in [(HTTPResponse(403, {}, b'sensitive detail'), '403'),
                               (TimeoutError(), '结果未知'),
                               (HTTPResponse(200, {}, b'{"data":[{"url":"https://example.test/never.png"}]}'), 'PNG')]:
            calls = []

            def transport(*args):
                calls.append(args[:2])
                if isinstance(response, Exception):
                    raise response
                return response

            with self.subTest(expected=text):
                with self.assertRaisesRegex(WorkerError, text) as raised:
                    CoverImageChannel('example-only-fixture', transport=transport).generate({'prompt': 'x'}, (picture(), 'image/png'))
                self.assertEqual(calls, [('POST', ENDPOINT)])
                self.assertNotIn('sensitive detail', str(raised.exception))


class CoverWorkerLoopTest(unittest.TestCase):
    def test_dispatch_once_and_resume_private_upload_without_another_model_request(self):
        with TemporaryDirectory() as temp:
            config = WorkerConfig('https://example.test', '', '', 'test', '', '', 'test',
                                  upload_retries=1, state_dir=Path(temp))
            raw = picture()
            transport = Mock(return_value=HTTPResponse(200, {}, json.dumps({
                'data': [{'b64_json': base64.b64encode(raw).decode('ascii')}],
            }).encode()))
            gateway = Mock()
            gateway.reference_image.return_value = (raw, 'image/png')
            completed = gateway.complete
            completed.side_effect = [WorkerError('上传失效', status=403), {}]
            worker = WindowsWorker(config, gateway=gateway, exe=Mock(),
                                   cover_channel=CoverImageChannel('example-only-fixture', transport=transport))
            page_id = 'd3ed960e-40f0-4646-a445-9df33cb065a4'
            page = {'pageId': page_id, 'leaseToken': 'initial', 'coverTraining': True, 'attempts': 1,
                    'hasReferenceImage': True, 'prompt': '保持确认的文案，只更改构图'}
            with self.assertRaisesRegex(WorkerError, '上传失效'):
                worker.process_page(page)
            self.assertIsNotNone(worker.state.load_spool(page_id))
            page2 = {**page, 'leaseToken': 'renewed', 'attempts': 2}
            self.assertTrue(worker.process_page(page2))
            completed.assert_called_with(page2, raw, 'image/png', 32, 18)
            self.assertIsNone(worker.state.load_spool(page_id))
            for denied, reason in [({**page2, 'pageId': 'no-spool'}, '未重复发送'),
                                   ({**page, 'pageId': 'revoked', 'hasReferenceImage': False}, '原参考已失效')]:
                with self.assertRaisesRegex(WorkerError, reason):
                    worker.process_page(denied)
            gateway.reference_image.side_effect = WorkerError('原参考已撤权', status=403)
            with self.assertRaisesRegex(WorkerError, '已撤权'):
                worker.process_page({**page, 'pageId': 'authorization-recheck'})
            transport.assert_called_once()
            self.assertEqual(completed.call_count, 2)
            self.assertEqual(worker.exe.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
