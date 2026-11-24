"""Professional image worker composition without a real EXE or cloud request."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloud.professional_image_client import ProfessionalImageClient
from cloud.professional_image_settings import ImageSettings, save_settings, settings_path
from cloud.professional_image_worker import build_worker, status_path
from cloud.professional_review_worker import Credentials
from cloud.windows_image_worker import ExeClient

CONFIG = {"url": "https://guanye-test.invalid", "anonKey": "sb_publishable_fixture_api_key", "workspaceId": "c2c14344-0bd0-4dc3-96f5-617ad0fdf424"}
SECRETS = Credentials("judge@example.invalid", "fixture password", "r" * 40, "model-fixture")


class CompositionTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="观页生图组合 "))
        self.root = Path(temp)
        self.enterContext(patch.dict(os.environ, {"LOCALAPPDATA": temp}))
        self.ready = self.root / "desktop-ready.json"
        self.ready.write_text('{"url":"http://127.0.0.1:18888","executable":"C:\\\\PPT工具.exe"}', encoding="utf-8")

    def test_composition_is_sixteen_way_and_uses_separate_gateway(self):
        settings = ImageSettings("w" * 40, str(self.ready))
        worker = build_worker(CONFIG, SECRETS, settings)
        self.assertIsInstance(worker.gateway, ProfessionalImageClient)
        self.assertIsInstance(worker.exe, ExeClient)
        self.assertEqual(worker.config.workers, 16)
        self.assertEqual(worker.config.workspace_id, CONFIG["workspaceId"])
        self.assertNotIn(SECRETS.password, repr(worker))
        self.assertNotIn(settings.worker_token, repr(worker))
        self.assertEqual(worker.config.state_dir, settings_path(CONFIG).with_suffix(""))
        self.assertNotEqual(worker.config.state_dir, Path(tempfile.gettempdir()))

    def test_cover_worker_does_not_need_an_exe_or_review_provider_key(self):
        from cloud.cover_image_channel import CoverImageChannel
        from cloud.professional_image_settings import CoverWorkerSettings
        from cloud.professional_image_worker import build_cover_worker

        self.ready.unlink()
        settings = CoverWorkerSettings(SECRETS.email, SECRETS.password, "w" * 40)
        channel = CoverImageChannel("fixture-image-key-0000")
        worker = build_cover_worker(CONFIG, settings, channel)
        self.assertIsNone(worker.exe)
        self.assertTrue(worker.cover_only)
        self.assertEqual(worker.config.workers, 1)
        self.assertIs(worker.cover_channel, channel)
        self.assertEqual(worker.gateway._provider.config.email, SECRETS.email)
        self.assertNotIn(SECRETS.password, repr(settings))
        self.assertNotIn(settings.worker_token, repr(settings))

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI")
    def test_dpapi_worker_settings_do_not_replace_review_credentials(self):
        settings = ImageSettings("w" * 40, str(self.ready))
        save_settings(CONFIG, settings)
        self.assertNotIn(settings.worker_token.encode(), status_path(CONFIG).parent.glob("image-*.dpapi").__next__().read_bytes())
        worker = build_worker(CONFIG, SECRETS, settings)
        self.assertEqual(worker.gateway._worker_token, settings.worker_token)
        self.assertEqual(worker.gateway._provider.config.email, SECRETS.email)


if __name__ == "__main__":
    unittest.main()
