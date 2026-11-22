from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from cloud import guanye_installer


@unittest.skipUnless(os.name == "nt", "Windows batch uninstaller test")
class GuanyeInstallerUninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._work_dir = tempfile.TemporaryDirectory(
            prefix="guanye-uninstall-test-", ignore_cleanup_errors=True
        )
        self.base = Path(self._work_dir.name)
        self.local_appdata = self.base / "Local AppData 观页"
        self.roaming_appdata = self.base / "Roaming AppData 观页"
        self.user_profile = self.base / "User Profile 观页"
        self.temp_dir = self.base / "Temp 观页"
        self.test_bin = self.base / "Test Bin"
        for path in (self.local_appdata, self.roaming_appdata, self.user_profile, self.temp_dir, self.test_bin):
            path.mkdir(parents=True, exist_ok=True)

        self.env = os.environ.copy()
        # A renamed system executable keeps taskkill deterministic, avoids
        # terminating a real guanye-professional.exe, and reliably fails.
        system_root = Path(self.env.get("SystemRoot", r"C:\Windows"))
        taskkill_stub = self.test_bin / "taskkill.exe"
        shutil.copyfile(system_root / "System32" / "where.exe", taskkill_stub)
        self.env.update(
            {
                "LOCALAPPDATA": str(self.local_appdata),
                "APPDATA": str(self.roaming_appdata),
                "USERPROFILE": str(self.user_profile),
                "TEMP": str(self.temp_dir),
                "TMP": str(self.temp_dir),
                "HOME": str(self.user_profile),
                "PATH": str(self.test_bin) + os.pathsep + self.env.get("PATH", ""),
            }
        )
        drive, tail = os.path.splitdrive(str(self.user_profile))
        self.env["HOMEDRIVE"] = drive
        self.env["HOMEPATH"] = tail

    def tearDown(self) -> None:
        self._work_dir.cleanup()

    def _run_uninstaller(self, uninstaller: Path) -> subprocess.CompletedProcess[str]:
        comspec = self.env.get("ComSpec", r"C:\Windows\System32\cmd.exe")
        command = f'"{comspec}" /d /c ""{uninstaller}""'
        return subprocess.run(
            command,
            cwd=str(self.base),
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    def _wait_for(self, predicate, timeout: float = 12.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return predicate()

    def test_generated_uninstaller_removes_all_artifacts_and_exits_zero(self) -> None:
        install_root = self.local_appdata / guanye_installer.APP_NAME
        app_dir = install_root / "app"
        app_dir.mkdir(parents=True)
        (app_dir / "guanye-professional.exe").write_bytes(b"placeholder")
        desktop = self.user_profile / "Desktop" / guanye_installer.SHORTCUT_NAME
        startup = (
            self.roaming_appdata
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / guanye_installer.SHORTCUT_NAME
        )
        uninstall_shortcut = (
            self.roaming_appdata
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / guanye_installer.UNINSTALL_SHORTCUT_NAME
        )
        for shortcut in (desktop, startup, uninstall_shortcut):
            shortcut.parent.mkdir(parents=True, exist_ok=True)
            shortcut.write_bytes(b"placeholder")

        uninstaller = self.local_appdata / "GuanyeProfessional-Uninstall.cmd"
        guanye_installer.write_uninstall_script(uninstaller)
        self.assertTrue(uninstaller.read_bytes().startswith(codecs.BOM_UTF8))

        completed = self._run_uninstaller(uninstaller)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        for removed_path in (install_root, desktop, startup, uninstall_shortcut):
            self.assertFalse(removed_path.exists(), f"仍残留: {removed_path}")
        self.assertTrue(
            self._wait_for(lambda: not uninstaller.exists()),
            f"卸载脚本未删除: {uninstaller}",
        )
        self.assertTrue(
            self._wait_for(lambda: not list(self.temp_dir.glob("GuanyeProfessional-Uninstall-Cleanup-*.cmd"))),
            "延迟自删辅助脚本未清理",
        )

    def test_missing_cleanup_targets_still_return_zero(self) -> None:
        taskkill_stub = self.test_bin / "taskkill.exe"
        probe = subprocess.run(
            [str(taskkill_stub), "/IM", "guanye-professional.exe", "/F"],
            env=self.env,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(probe.returncode, 0, "taskkill 测试替身必须返回失败")
        uninstaller = self.local_appdata / "GuanyeProfessional-Uninstall.cmd"
        guanye_installer.write_uninstall_script(uninstaller)

        completed = self._run_uninstaller(uninstaller)
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        self.assertTrue(
            self._wait_for(lambda: not uninstaller.exists()),
            f"卸载脚本未删除: {uninstaller}",
        )

    def test_script_uses_environment_paths_and_chinese_shortcut_names(self) -> None:
        script = guanye_installer.build_uninstall_script()
        self.assertIn(r"%LOCALAPPDATA%\GuanyeProfessional", script)
        self.assertIn(r"%USERPROFILE%\Desktop\观页专业版.lnk", script)
        self.assertIn(
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\观页专业版.lnk",
            script,
        )
        self.assertIn(
            r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\观页专业版卸载.lnk",
            script,
        )
        self.assertIn("exit /b 0", script)
        self.assertIn('(goto) 2>nul & del /f /q "%~f0"', script)

        lowered = script.lower()
        for variable in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            real_value = os.environ.get(variable)
            if real_value:
                self.assertNotIn(real_value.lower(), lowered, f"脚本泄露真实 {variable} 路径")


@unittest.skipUnless(os.name == "nt", "Windows installer upgrade test")
class GuanyeInstallerUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve()
        self._work_dir = tempfile.TemporaryDirectory(
            prefix="guanye-upgrade-test-", dir=str(temp_root), ignore_cleanup_errors=True
        )
        self.base = Path(self._work_dir.name)
        self.local_appdata = self.base / "Local AppData 观页"
        self.roaming_appdata = self.base / "Roaming AppData 观页"
        self.user_profile = self.base / "User Profile 观页"
        self.temp_dir = self.base / "Temp 观页"
        for path in (self.local_appdata, self.roaming_appdata, self.user_profile, self.temp_dir):
            path.mkdir(parents=True, exist_ok=True)
        overrides = {
            "LOCALAPPDATA": str(self.local_appdata),
            "APPDATA": str(self.roaming_appdata),
            "USERPROFILE": str(self.user_profile),
            "TEMP": str(self.temp_dir),
            "TMP": str(self.temp_dir),
            "HOME": str(self.user_profile),
        }
        self._env_patch = mock.patch.dict(os.environ, overrides)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.addCleanup(self._work_dir.cleanup)
        self.root = self.local_appdata / guanye_installer.APP_NAME
        self.app_root = self.root / "app"
        self.old_root = self.root.with_name(guanye_installer.APP_NAME + ".old")
        self.uninstaller = self.local_appdata / "GuanyeProfessional-Uninstall.cmd"

    def _make_payload(
        self,
        marker: str,
        *,
        bootstrap: dict[str, object] | None = None,
    ) -> Path:
        payload = self.base / f"payload-{marker}.zip"
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("app/guanye-professional.exe", f"exe-{marker}".encode("ascii"))
            archive.writestr("app/hn-ppt2/HN-PPT2.exe", f"hn-{marker}".encode("ascii"))
            archive.writestr("app/marker.txt", marker)
            if bootstrap is not None:
                archive.writestr(
                    "app/config/bootstrap.json",
                    json.dumps(bootstrap, ensure_ascii=False),
                )
        return payload

    def _fake_shortcut(
        self,
        target: Path,
        shortcut: Path,
        working: Path,
        arguments: str = "",
    ) -> None:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_text(
            json.dumps(
                {
                    "target": os.fspath(target),
                    "working": os.fspath(working),
                    "arguments": arguments,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _assert_shortcuts(self) -> None:
        desktop = self.user_profile / "Desktop" / guanye_installer.SHORTCUT_NAME
        startup = (
            self.roaming_appdata
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / guanye_installer.SHORTCUT_NAME
        )
        uninstall_shortcut = (
            self.roaming_appdata
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / guanye_installer.UNINSTALL_SHORTCUT_NAME
        )
        expected_target = os.path.normcase(str(self.app_root / "guanye-professional.exe"))
        expected_working = os.path.normcase(str(self.app_root))
        for shortcut in (desktop, startup):
            self.assertTrue(shortcut.is_file(), f"缺少快捷方式: {shortcut}")
            record = json.loads(shortcut.read_text(encoding="utf-8"))
            self.assertEqual(os.path.normcase(record["target"]), expected_target)
            self.assertEqual(os.path.normcase(record["working"]), expected_working)
            self.assertEqual(record["arguments"], "")

        self.assertTrue(uninstall_shortcut.is_file(), "缺少开始菜单卸载快捷方式")
        record = json.loads(uninstall_shortcut.read_text(encoding="utf-8"))
        self.assertEqual(
            os.path.normcase(record["target"]),
            os.path.normcase(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")),
        )
        self.assertEqual(os.path.normcase(record["working"]), os.path.normcase(str(self.local_appdata)))
        self.assertEqual(record["arguments"], f'/c ""{self.uninstaller}""')
        self.assertTrue(self.uninstaller.is_file(), "缺少卸载脚本")
        self.assertTrue(self.uninstaller.read_bytes().startswith(codecs.BOM_UTF8))

    def test_first_install_and_upgrade_replace_root_without_old_residue(self) -> None:
        stop_calls: list[Path] = []
        bootstrap_markers: list[str] = []
        switch_observations: list[tuple[str, str]] = []

        def stop_processes(path: Path) -> None:
            stop_calls.append(path)
            self.assertTrue((path / "app" / "marker.txt").is_file())

        def bootstrap(app_root: Path) -> None:
            bootstrap_markers.append((app_root / "marker.txt").read_text(encoding="utf-8"))

        def switch_root(source: Path, destination: Path) -> None:
            self.assertFalse(destination.exists())
            switch_observations.append(
                (
                    (source / "app" / "marker.txt").read_text(encoding="utf-8"),
                    (self.old_root / "app" / "marker.txt").read_text(encoding="utf-8"),
                )
            )
            source.rename(destination)

        first = guanye_installer.install(
            payload=self._make_payload("v1"),
            process_stopper=stop_processes,
            shortcut_factory=self._fake_shortcut,
            bootstrap_runner=bootstrap,
        )
        self.assertEqual(first, self.app_root)
        self.assertEqual((self.app_root / "marker.txt").read_text(encoding="utf-8"), "v1")
        self.assertFalse(self.old_root.exists())
        self._assert_shortcuts()

        # Reproduce the reported residual .old from the previous installer.
        (self.old_root / "app").mkdir(parents=True)
        (self.old_root / "app" / "marker.txt").write_text("stale-old", encoding="utf-8")

        second = guanye_installer.install(
            payload=self._make_payload("v2"),
            process_stopper=stop_processes,
            shortcut_factory=self._fake_shortcut,
            switch_root=switch_root,
            bootstrap_runner=bootstrap,
        )
        self.assertEqual(second, self.app_root)
        self.assertEqual((self.app_root / "marker.txt").read_text(encoding="utf-8"), "v2")
        self.assertFalse(self.old_root.exists())
        self.assertEqual(stop_calls, [self.root])
        self.assertEqual(switch_observations, [("v2", "v1")])
        self.assertEqual(bootstrap_markers, ["v1", "v2"])
        self._assert_shortcuts()

    def test_process_stop_matches_only_exact_install_paths(self) -> None:
        target_guanye = self.app_root / "guanye-professional.exe"
        target_hn = self.app_root / "hn-ppt2" / "HN-PPT2.exe"
        other_guanye = self.base / "Other Install" / "guanye-professional.exe"
        for path in (target_guanye, target_hn, other_guanye):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"placeholder")
        active = {
            111: str(target_guanye),
            222: str(target_hn),
            333: str(other_guanye),
        }
        stopped: list[int] = []

        def provider() -> list[tuple[int, str]]:
            return list(active.items())

        def terminate(pid: int) -> None:
            stopped.append(pid)
            active.pop(pid, None)

        result = guanye_installer._stop_processes_for_root(
            self.root,
            process_provider=provider,
            terminator=terminate,
            timeout=1.0,
        )
        self.assertEqual(result, (111, 222))
        self.assertEqual(stopped, [111, 222])
        self.assertEqual(active, {333: str(other_guanye)})

    def test_switch_failure_rolls_back_existing_root(self) -> None:
        bootstrap_markers: list[str] = []

        def bootstrap(app_root: Path) -> None:
            bootstrap_markers.append((app_root / "marker.txt").read_text(encoding="utf-8"))

        guanye_installer.install(
            payload=self._make_payload("v1"),
            process_stopper=lambda path: None,
            shortcut_factory=self._fake_shortcut,
            bootstrap_runner=bootstrap,
        )

        def failing_switch(source: Path, destination: Path) -> None:
            raise RuntimeError("switch failed")

        with self.assertRaisesRegex(RuntimeError, "switch failed"):
            guanye_installer.install(
                payload=self._make_payload("v2"),
                process_stopper=lambda path: None,
                shortcut_factory=self._fake_shortcut,
                switch_root=failing_switch,
                bootstrap_runner=bootstrap,
            )

        self.assertTrue(self.root.exists())
        self.assertEqual((self.app_root / "marker.txt").read_text(encoding="utf-8"), "v1")
        self.assertFalse(self.old_root.exists())
        self.assertFalse(self.root.with_name(guanye_installer.APP_NAME + ".failed").exists())
        self.assertEqual(bootstrap_markers, ["v1"])
        self._assert_shortcuts()
        self.assertEqual(list(self.local_appdata.glob("guanye-install-*")), [])

    def test_run_bootstrap_rejects_nonzero_timeout_and_start_failure(self) -> None:
        expected_command = [str(self.app_root / "guanye-professional.exe"), "--bootstrap-only"]

        with self.subTest("nonzero exit"):
            completed = subprocess.CompletedProcess(expected_command, 7)
            with mock.patch.object(guanye_installer.subprocess, "run", return_value=completed) as run:
                with self.assertRaisesRegex(RuntimeError, "退出码 7"):
                    guanye_installer._run_bootstrap(self.app_root)
            args, kwargs = run.call_args
            self.assertEqual(args[0], expected_command)
            self.assertEqual(kwargs["timeout"], 60)

        with self.subTest("timeout"):
            timeout = subprocess.TimeoutExpired(expected_command, 60)
            with mock.patch.object(guanye_installer.subprocess, "run", side_effect=timeout):
                with self.assertRaisesRegex(RuntimeError, "超时"):
                    guanye_installer._run_bootstrap(self.app_root)

        with self.subTest("start failure"):
            with mock.patch.object(
                guanye_installer.subprocess,
                "run",
                side_effect=OSError("cannot start"),
            ):
                with self.assertRaisesRegex(RuntimeError, "启动失败"):
                    guanye_installer._run_bootstrap(self.app_root)

    def test_first_install_rejects_plaintext_bootstrap_and_cleans_root(self) -> None:
        payload = self._make_payload(
            "v1",
            bootstrap={
                "review": {"email": "plain-secret@example.invalid"},
                "image": {},
            },
        )
        with self.assertRaisesRegex(RuntimeError, "installed:true"):
            guanye_installer.install(
                payload=payload,
                process_stopper=lambda path: None,
                shortcut_factory=self._fake_shortcut,
                bootstrap_runner=lambda app_root: None,
            )

        self.assertFalse(self.root.exists())
        self.assertFalse(self.old_root.exists())
        self.assertFalse(self.root.with_name(guanye_installer.APP_NAME + ".failed").exists())
        self.assertFalse((self.user_profile / "Desktop" / guanye_installer.SHORTCUT_NAME).exists())
        self.assertFalse(
            (
                self.roaming_appdata
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / "Startup"
                / guanye_installer.SHORTCUT_NAME
            ).exists()
        )
        self.assertFalse(
            (
                self.roaming_appdata
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / guanye_installer.UNINSTALL_SHORTCUT_NAME
            ).exists()
        )
        self.assertFalse(self.uninstaller.exists())
        self.assertEqual(list(self.local_appdata.glob("guanye-install-*")), [])

    def test_first_install_nonzero_bootstrap_result_fails_closed(self) -> None:
        payload = self._make_payload(
            "v1",
            bootstrap={"review": {"email": "plain-secret@example.invalid"}, "image": {}},
        )

        def bootstrap(app_root: Path) -> subprocess.CompletedProcess[list[str]]:
            return subprocess.CompletedProcess(["bootstrap"], 9)

        with self.assertRaisesRegex(RuntimeError, "退出码 9"):
            guanye_installer.install(
                payload=payload,
                process_stopper=lambda path: None,
                shortcut_factory=self._fake_shortcut,
                bootstrap_runner=bootstrap,
            )
        self.assertFalse(self.root.exists())
        self.assertEqual(list(self.local_appdata.glob("guanye-install-*")), [])

    def test_install_verifies_converted_bootstrap_marker(self) -> None:
        observed: list[str] = []

        def bootstrap(app_root: Path) -> None:
            path = app_root / "config" / "bootstrap.json"
            observed.append(path.read_text(encoding="utf-8"))
            path.write_text('{"installed":true}\n', encoding="utf-8")

        result = guanye_installer.install(
            payload=self._make_payload(
                "v1",
                bootstrap={
                    "review": {"email": "plain-secret@example.invalid"},
                    "image": {},
                },
            ),
            process_stopper=lambda path: None,
            shortcut_factory=self._fake_shortcut,
            bootstrap_runner=bootstrap,
        )

        self.assertEqual(result, self.app_root)
        self.assertEqual(len(observed), 1)
        self.assertIn("plain-secret@example.invalid", observed[0])
        installed = (self.app_root / "config" / "bootstrap.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(installed), {"installed": True})
        self.assertNotIn("plain-secret", installed)
        self._assert_shortcuts()

    def test_install_accepts_already_installed_bootstrap(self) -> None:
        calls: list[Path] = []

        def bootstrap(app_root: Path) -> None:
            calls.append(app_root)

        result = guanye_installer.install(
            payload=self._make_payload("v1", bootstrap={"installed": True}),
            process_stopper=lambda path: None,
            shortcut_factory=self._fake_shortcut,
            bootstrap_runner=bootstrap,
        )

        self.assertEqual(result, self.app_root)
        self.assertEqual(calls, [self.app_root])
        self.assertEqual(
            json.loads((self.app_root / "config" / "bootstrap.json").read_text(encoding="utf-8")),
            {"installed": True},
        )

    def test_upgrade_bootstrap_failure_rolls_back_old_root(self) -> None:
        def bootstrap(app_root: Path) -> None:
            path = app_root / "config" / "bootstrap.json"
            self.assertIn("v2-secret", path.read_text(encoding="utf-8"))
            raise RuntimeError("bootstrap failed")

        guanye_installer.install(
            payload=self._make_payload("v1"),
            process_stopper=lambda path: None,
            shortcut_factory=self._fake_shortcut,
            bootstrap_runner=lambda app_root: None,
        )

        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            guanye_installer.install(
                payload=self._make_payload(
                    "v2",
                    bootstrap={"review": {"email": "v2-secret"}, "image": {}},
                ),
                process_stopper=lambda path: None,
                shortcut_factory=self._fake_shortcut,
                bootstrap_runner=bootstrap,
            )

        self.assertTrue(self.root.exists())
        self.assertEqual((self.app_root / "marker.txt").read_text(encoding="utf-8"), "v1")
        self.assertFalse((self.app_root / "config" / "bootstrap.json").exists())
        self.assertFalse(self.old_root.exists())
        self.assertFalse(self.root.with_name(guanye_installer.APP_NAME + ".failed").exists())
        self.assertEqual(list(self.local_appdata.glob("guanye-install-*")), [])
        self._assert_shortcuts()

class GuanyeInstallerMainTests(unittest.TestCase):
    def test_windowed_frozen_failure_shows_message_with_exception_text(self) -> None:
        with (
            mock.patch.object(guanye_installer, "_should_show_install_error_dialog", return_value=True),
            mock.patch.object(
                guanye_installer,
                "install",
                side_effect=RuntimeError("bootstrap 转换失败，退出码 7"),
            ),
            mock.patch.object(guanye_installer, "_show_install_error_dialog") as dialog,
            mock.patch.object(guanye_installer.sys, "stderr", None),
        ):
            result = guanye_installer.main([])
        self.assertEqual(result, 1)
        dialog.assert_called_once()
        message = dialog.call_args.args[0]
        self.assertIn("安装失败", message)
        self.assertIn("退出码 7", message)

    def test_dialog_failure_does_not_change_command_line_exit_code(self) -> None:
        with (
            mock.patch.object(guanye_installer, "_should_show_install_error_dialog", return_value=True),
            mock.patch.object(guanye_installer, "install", side_effect=RuntimeError("安装异常")),
            mock.patch.object(
                guanye_installer,
                "_show_install_error_dialog",
                side_effect=OSError("dialog failed"),
            ),
            mock.patch.object(guanye_installer.sys, "stderr", None),
        ):
            result = guanye_installer.main([])
        self.assertEqual(result, 1)

    def test_success_path_does_not_show_failure_dialog(self) -> None:
        with (
            mock.patch.object(guanye_installer, "_should_show_install_error_dialog", return_value=True),
            mock.patch.object(guanye_installer, "install", return_value=Path("installed")),
            mock.patch.object(guanye_installer, "_show_install_error_dialog") as dialog,
        ):
            result = guanye_installer.main([])
        self.assertEqual(result, 0)
        dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
