"""Launcher configuration and smoke tests; no real cloud, EXE or browser is used."""
import json, os, socket, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from cloud.professional_launcher import launch, load_runtime

CONFIG = {"url": "https://guanye-test.invalid", "anonKey": "sb_publishable_fixture_api_key", "workspaceId": "c2c14344-0bd0-4dc3-96f5-617ad0fdf424"}


class LauncherTests(unittest.TestCase):
    def root(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="观页 启动器 "))
        root = Path(temp)
        (root / "config").mkdir()
        (root / "public").mkdir()
        (root / "public" / "index.html").write_text("<!doctype html><title>观页</title>", encoding="utf-8")
        (root / "config" / "professional.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        (root / "config" / "runtime.json").write_text(json.dumps({
            "port": port, "openBrowser": False,
            "hnPpt2": {"enabled": False}, "workers": {"review": False, "image": False, "intake": False},
        }), encoding="utf-8")
        return root

    def test_runtime_defaults_are_fixed_to_local_package(self):
        root = self.root()
        runtime = load_runtime(root)
        self.assertEqual(runtime["port"], json.loads((root / "config" / "runtime.json").read_text())["port"])
        self.assertTrue(runtime["hn"]["enabled"] is False)
        self.assertEqual(runtime["workers"]["concurrency"], 16)
        self.assertFalse(runtime["workers"]["intake"])
        self.assertEqual(runtime["root"], root.resolve())

    @patch("cloud.professional_launcher._single_instance", return_value=True)
    def test_smoke_launch_starts_and_closes_server(self, _single_instance):
        root = self.root()
        self.assertEqual(launch(root, smoke=True, no_browser=True), 0)
        self.assertTrue((root / "data" / "professional.log").is_file())

    @patch("cloud.professional_launcher._single_instance", return_value=True)
    def test_bad_hn_path_fails_closed(self, _single_instance):
        root = self.root()
        value = json.loads((root / "config" / "runtime.json").read_text())
        value["hnPpt2"] = {"enabled": True, "executable": "missing.exe", "readyFile": "missing.json"}
        (root / "config" / "runtime.json").write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            launch(root, smoke=True, no_browser=True)


if __name__ == "__main__":
    unittest.main()
