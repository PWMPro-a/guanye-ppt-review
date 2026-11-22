"""Credential isolation and literal local EXE readiness tests (no live endpoint)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloud.professional_image_settings import (
    ImageSettings, build_config, image_fingerprint, image_singleton, load_settings, save_settings, settings_path,
)
from cloud.professional_worker_secrets import _path

CONFIG = {"url": "https://guanye-test.invalid", "workspaceId": "c2c14344-0bd0-4dc3-96f5-617ad0fdf424"}
TOKEN = "a" * 39


class ImageSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="观页 生图"))
        self.root = Path(temp)
        self.enterContext(patch.dict(os.environ, {"LOCALAPPDATA": temp}))
        self.ready = self.root / "desktop-ready.json"
        self.settings = ImageSettings(TOKEN, str(self.ready))

    def test_literal_readiness_and_shared_capacity(self):
        self.ready.write_text(json.dumps({"url": "http://127.0.0.1:18228/", "executable": "C:\\HN PPT2.exe"}), encoding="utf-8-sig")
        worker = build_config(CONFIG, self.settings)
        self.assertEqual(worker.exe_base, "http://127.0.0.1:18228")
        self.assertTrue(worker.exe_desktop_proxy)
        self.assertEqual(worker.exe_release_shell, "HN PPT2.exe")
        self.assertEqual(worker.workers, 16)
        self.assertNotIn(TOKEN, repr(worker))
        self.assertNotIn(TOKEN, repr(self.settings))
        self.assertNotEqual(image_fingerprint(CONFIG), image_fingerprint({**CONFIG, "url": "https://other.invalid"}))
        backend = self.root / "backend-ready.json"
        backend.write_text('{"url":"http://[::1]:18888"}', encoding="utf-8")
        plus = build_config(CONFIG, ImageSettings(TOKEN, str(backend), "fixture-exe-code"))
        self.assertFalse(plus.exe_desktop_proxy)
        self.assertEqual(plus.exe_token, "fixture-exe-code")

    def test_bad_readiness_never_becomes_client_origin(self):
        for bad in ["http://localhost:100", "http://127.0.0.1:0", "http://127.0.0.1:99999", "http://127.0.0.1:80/private",
                    "http://127.0.0.1:80/?x=y", "https://foreign.example:443", "http://user@127.0.0.1:80", "http://127.0.0.1:80\n"]:
            with self.subTest(bad=bad):
                self.ready.write_text(json.dumps({"url": bad}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    build_config(CONFIG, self.settings)
        self.ready.write_bytes(b" " * (16 * 1024 + 1))
        with self.assertRaises(ValueError):
            build_config(CONFIG, self.settings)
        with self.assertRaises(ValueError):
            ImageSettings(TOKEN, str(self.ready), "should-not-use-proxy-credential")
        with self.assertRaises(ValueError):
            ImageSettings(TOKEN, str(self.root / "backend-ready.json"))

    @unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI plus native mutex")
    def test_dpapi_and_separate_singleton(self):
        save_settings(CONFIG, self.settings)
        persisted = settings_path(CONFIG).read_bytes()
        self.assertNotIn(TOKEN.encode(), persisted)
        self.assertNotIn(str(self.ready).encode(), persisted)
        self.assertFalse(_path(CONFIG).exists())
        self.assertEqual(load_settings(CONFIG), self.settings)
        other = {**CONFIG, "url": "https://other.invalid"}
        settings_path(other).write_bytes(persisted)
        with self.assertRaises(OSError):
            load_settings(other)
        with image_singleton(CONFIG):
            with self.assertRaises(RuntimeError), image_singleton(CONFIG):
                pass
            with image_singleton(other):
                pass
        with image_singleton(CONFIG):
            pass


if __name__ == "__main__":
    unittest.main()
