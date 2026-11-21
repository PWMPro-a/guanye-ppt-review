from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, call

from cloud.windows_image_worker import ExeClient, GatewayClient, JsonHttpClient, LocalState, WorkerError


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII='
)


class WorkerReferenceTests(unittest.TestCase):
    def test_gateway_reference_uses_page_lease_and_checks_image_format(self):
        gateway = GatewayClient('https://gateway.example', 'account', 'workspace', worker_token='worker')
        gateway.http.request = Mock(return_value={
            'content_type': 'image/png', 'image_base64': base64.b64encode(PNG).decode('ascii'),
        })
        page = {'pageId': 'page-1', 'leaseToken': 'lease-1'}
        self.assertEqual(gateway.reference_image(page), (PNG, 'image/png'))
        gateway.http.request.assert_called_once_with(
            'POST', '/api/ai/generation/worker/reference', {'page_id': 'page-1', 'lease_token': 'lease-1'}, timeout=60.0,
        )
        gateway.http.request.return_value['content_type'] = 'image/jpeg'
        with self.assertRaisesRegex(WorkerError, '格式不符'):
            gateway.reference_image(page)

    def test_desktop_template_upload_uses_hn_multipart_contract(self):
        http = JsonHttpClient('http://127.0.0.1:52724', 'desktop')
        http.opener.open = Mock(return_value=BytesIO(b'{"data":{"template_image_url":"/files/reference.png"}}'))
        http.request('POST', '/api/projects/project-1/template', template=(PNG, 'image/png'))
        request = http.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertTrue(request.get_header('Content-type').startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="template_image"; filename="reference.png"', request.data)
        self.assertIn(PNG, request.data)
        self.assertEqual(request.get_header('Authorization'), 'Bearer desktop')
        self.assertIsNone(request.get_header('X-guanye-worker-token'))

    def test_worker_reuses_project_for_same_reference_and_keeps_different_templates_apart(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = ExeClient('http://127.0.0.1:52724', 'desktop', LocalState(Path(directory)))
            projects = iter(['project-a', 'project-b'])

            def request(method, path, body=None, timeout=None, *, template=None):
                if path == '/api/projects':
                    return {'data': {'project_id': next(projects)}}
                if path.endswith('/template'):
                    return {'data': {'template_image_url': '/files/reference.png'}}
                if path.endswith('/pages'):
                    return {'data': {'page_id': 'page-a'}}
                if path.endswith('/generate/image'):
                    return {'data': {'task_id': 'task-a'}}
                if '/tasks/' in path:
                    return {'data': {'status': 'COMPLETED'}}
                if path.endswith('/image-versions'):
                    project_id = path.split('/')[3]
                    return {'data': {'versions': [{
                        'version_number': 1,
                        'image_url': f'/files/{project_id}/pages/original.png',
                        'is_current': True,
                    }]}}
                return {'data': {'pages': [{
                    'page_id': 'page-a',
                    'generated_image_url': '/files/project-a/pages/thumb.jpg',
                }]}}

            exe.http.request = Mock(side_effect=request)
            exe.http.download = Mock(return_value=(PNG, 'image/png'))
            base_job = {'jobId': 'job-a', 'workspaceId': 'workspace-a'}
            page = {'pageId': 'plan-page-a', 'position': 1, 'pageTitle': '测试封面'}
            reference_a = (PNG, 'image/png')
            reference_b = (PNG + b'after-iend', 'image/png')
            job_a = {**base_job, 'referenceSha256': hashlib.sha256(reference_a[0]).hexdigest()}
            job_b = {**base_job, 'referenceSha256': hashlib.sha256(reference_b[0]).hexdigest()}
            self.assertEqual(exe.ensure_project(job_a, [page], reference_a)['projectId'], 'project-a')
            self.assertEqual(exe.ensure_project(job_a, [page], reference_a)['projectId'], 'project-a')
            self.assertEqual(exe.ensure_project(job_b, [page], reference_b)['projectId'], 'project-b')
            exe.generate_page(job_a, page)
            exe.generate_page(job_b, page)
            template_calls = [item for item in exe.http.request.call_args_list if item.args[1].endswith('/template')]
            self.assertEqual(template_calls, [
                call('POST', '/api/projects/project-a/template', timeout=60.0, template=reference_a),
                call('POST', '/api/projects/project-b/template', timeout=60.0, template=reference_b),
            ])
            generate_calls = [item for item in exe.http.request.call_args_list if item.args[1].endswith('/generate/image')]
            self.assertEqual(len(generate_calls), 2)
            self.assertTrue(all(item.args[2]['use_template'] is True for item in generate_calls))
            self.assertEqual(exe.http.download.call_args_list, [
                call('/files/project-a/pages/original.png', timeout=120.0),
                call('/files/project-b/pages/original.png', timeout=120.0),
            ])

    def test_generate_page_downloads_current_original_version_not_newer_or_thumb(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = ExeClient(
                'http://127.0.0.1:52724',
                'desktop',
                LocalState(Path(directory)),
                desktop_proxy=True,
            )

            def request(method, path, body=None, timeout=None, *, template=None):
                if path == '/api/projects':
                    return {'data': {'project_id': 'project-a'}}
                if path.endswith('/pages'):
                    return {'data': {'page_id': 'page-a'}}
                if path.endswith('/generate/image'):
                    return {'data': {'task_id': 'task-a'}}
                if '/tasks/' in path:
                    return {'data': {'status': 'COMPLETED'}}
                if path == '/api/projects/project-a/pages/page-a/image-versions':
                    return {'data': {'versions': [
                        {
                            'version_number': 2,
                            'image_url': '/files/project-a/pages/newer-not-current.png',
                            'is_current': False,
                        },
                        {
                            'version_number': 1,
                            'image_url': '/files/project-a/pages/current-original.png',
                            'is_current': True,
                        },
                    ]}}
                if path == '/api/projects/project-a':
                    return {'data': {'pages': [{
                        'page_id': 'page-a',
                        'generated_image_url': '/files/project-a/pages/thumb.jpg',
                    }]}}
                raise AssertionError(f'unexpected request: {method} {path}')

            exe.http.request = Mock(side_effect=request)
            exe.http.download = Mock(return_value=(PNG, 'image/jpeg'))
            job = {'jobId': 'job-a', 'workspaceId': 'workspace-a'}
            page = {'pageId': 'plan-page-a', 'position': 1, 'pageTitle': '测试封面'}
            exe.ensure_project(job, [page])

            raw, content_type, width, height = exe.generate_page(job, page)

            self.assertEqual(raw, PNG)
            self.assertEqual(content_type, 'image/png')
            self.assertEqual((width, height), (1, 1))
            exe.http.download.assert_called_once_with(
                '/files/project-a/pages/current-original.png', timeout=120.0,
            )
            requested_paths = [item.args[1] for item in exe.http.request.call_args_list]
            self.assertIn('/api/projects/project-a/pages/page-a/image-versions', requested_paths)
            self.assertNotIn('/api/projects/project-a', requested_paths)

    def test_generate_page_rejects_missing_current_version_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = ExeClient(
                'http://127.0.0.1:52724',
                'desktop',
                LocalState(Path(directory)),
                desktop_proxy=True,
            )

            def request(method, path, body=None, timeout=None, *, template=None):
                if path == '/api/projects':
                    return {'data': {'project_id': 'project-a'}}
                if path.endswith('/pages'):
                    return {'data': {'page_id': 'page-a'}}
                if path.endswith('/generate/image'):
                    return {'data': {'task_id': 'task-a'}}
                if '/tasks/' in path:
                    return {'data': {'status': 'COMPLETED'}}
                if path.endswith('/image-versions'):
                    return {'data': {'versions': [{
                        'version_number': 2,
                        'image_url': '/files/project-a/pages/newer.png',
                        'is_current': False,
                    }]}}
                return {'data': {'pages': [{
                    'page_id': 'page-a',
                    'generated_image_url': '/files/project-a/pages/thumb.jpg',
                }]}}

            exe.http.request = Mock(side_effect=request)
            exe.http.download = Mock(return_value=(PNG, 'image/png'))
            job = {'jobId': 'job-a', 'workspaceId': 'workspace-a'}
            page = {'pageId': 'plan-page-a', 'position': 1, 'pageTitle': '测试封面'}
            exe.ensure_project(job, [page])

            with self.assertRaisesRegex(WorkerError, '当前图像版本'):
                exe.generate_page(job, page)

            exe.http.download.assert_not_called()
            requested_paths = [item.args[1] for item in exe.http.request.call_args_list]
            self.assertNotIn('/api/projects/project-a', requested_paths)

    def test_generate_page_rejects_cross_origin_version_url(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = ExeClient(
                'http://127.0.0.1:52724',
                'desktop',
                LocalState(Path(directory)),
                desktop_proxy=True,
            )

            def request(method, path, body=None, timeout=None, *, template=None):
                if path == '/api/projects':
                    return {'data': {'project_id': 'project-a'}}
                if path.endswith('/pages'):
                    return {'data': {'page_id': 'page-a'}}
                if path.endswith('/generate/image'):
                    return {'data': {'task_id': 'task-a'}}
                if '/tasks/' in path:
                    return {'data': {'status': 'COMPLETED'}}
                if path.endswith('/image-versions'):
                    return {'data': {'versions': [{
                        'version_number': 2,
                        'image_url': 'https://evil.example/files/project-a/pages/current.png',
                        'is_current': True,
                    }]}}
                raise AssertionError(f'unexpected request: {method} {path}')

            exe.http.request = Mock(side_effect=request)
            exe.http.download = Mock(return_value=(PNG, 'image/png'))
            job = {'jobId': 'job-a', 'workspaceId': 'workspace-a'}
            page = {'pageId': 'plan-page-a', 'position': 1, 'pageTitle': '测试封面'}
            exe.ensure_project(job, [page])

            with self.assertRaisesRegex(WorkerError, '同源'):
                exe.generate_page(job, page)

            exe.http.download.assert_not_called()

    def test_direct_exe_falls_back_only_when_image_versions_route_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = ExeClient(
                'http://127.0.0.1:52724',
                'desktop',
                LocalState(Path(directory)),
            )

            def request(method, path, body=None, timeout=None, *, template=None):
                if path == '/api/projects':
                    return {'data': {'project_id': 'project-a'}}
                if path.endswith('/pages'):
                    return {'data': {'page_id': 'page-a'}}
                if path.endswith('/generate/image'):
                    return {'data': {'task_id': 'task-a'}}
                if '/tasks/' in path:
                    return {'data': {'status': 'COMPLETED'}}
                if path.endswith('/image-versions'):
                    raise WorkerError('HTTP 404', 404, {})
                if path == '/api/projects/project-a':
                    return {'data': {'pages': [{
                        'page_id': 'page-a',
                        'generated_image_url': '/files/project-a/pages/legacy-original.png',
                    }]}}
                raise AssertionError(f'unexpected request: {method} {path}')

            exe.http.request = Mock(side_effect=request)
            exe.http.download = Mock(return_value=(PNG, 'image/png'))
            job = {'jobId': 'job-a', 'workspaceId': 'workspace-a'}
            page = {'pageId': 'plan-page-a', 'position': 1, 'pageTitle': '测试封面'}
            exe.ensure_project(job, [page])

            exe.generate_page(job, page)

            exe.http.download.assert_called_once_with(
                '/files/project-a/pages/legacy-original.png', timeout=120.0,
            )
            requested_paths = [item.args[1] for item in exe.http.request.call_args_list]
            self.assertIn('/api/projects/project-a/pages/page-a/image-versions', requested_paths)
            self.assertIn('/api/projects/project-a', requested_paths)


if __name__ == '__main__':
    unittest.main()
