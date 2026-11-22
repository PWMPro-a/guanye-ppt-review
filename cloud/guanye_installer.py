"""Per-user installer for the Guanye professional app; no admin rights required."""
from __future__ import annotations

from collections.abc import Callable
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

APP_NAME = "GuanyeProfessional"
SHORTCUT_NAME = "观页专业版.lnk"
UNINSTALL_SHORTCUT_NAME = "观页专业版卸载.lnk"
_UNINSTALL_CLEANUP_ARGUMENT = "--cleanup-self"


def _payload() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    path = base / "payload.zip"
    if not path.is_file():
        raise RuntimeError("安装包缺少 payload.zip")
    return path


def _local_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("缺失 LOCALAPPDATA")
    root = (Path(local) / APP_NAME).resolve()
    if root.parent != (Path(local) / APP_NAME).parent:
        raise RuntimeError("安装目录安全校验失败")
    return root


def _safe_remove(path: Path, allowed: Path) -> None:
    path = path.resolve()
    allowed = allowed.resolve()
    if path == allowed or allowed not in path.parents:
        raise RuntimeError("拒绝删除安装目录之外的路径")
    if path.exists():
        shutil.rmtree(path)


def _shortcut(target: Path, shortcut: Path, working: Path, arguments: str = "") -> None:
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        link = shell.CreateShortCut(str(shortcut))
        link.Targetpath = str(target)
        link.WorkingDirectory = str(working)
        link.Arguments = arguments
        link.IconLocation = str(target) + ",0"
        link.save()
    except Exception:
        # A missing shortcut must not hide a successful file installation.
        pass


def _installed_executables(root: Path) -> tuple[Path, Path]:
    return (
        root / "app" / "guanye-professional.exe",
        root / "app" / "hn-ppt2" / "HN-PPT2.exe",
    )


def _normalized_process_path(path: str | Path) -> str:
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.realpath(value))


def _windows_process_image_paths() -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    th32cs_snapprocess = 0x00000002
    process_query_limited_information = 0x1000

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        raise OSError(ctypes.get_last_error(), "无法枚举 Windows 进程")
    records: list[tuple[int, str]] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        present = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        if not present:
            raise OSError(ctypes.get_last_error(), "无法读取 Windows 进程列表")
        while present:
            pid = int(entry.th32ProcessID)
            if pid:
                handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
                if handle:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            image_path = buffer.value
                            if image_path:
                                records.append((pid, image_path))
                    finally:
                        kernel32.CloseHandle(handle)
            present = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return records


def _terminate_windows_process(pid: int, timeout: float = 5.0) -> None:
    if os.name != "nt":
        raise OSError("当前平台不支持停止 Windows 进程")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_terminate = 0x0001
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_terminate | synchronize, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"无法打开旧版进程 {pid}")
    try:
        if not kernel32.TerminateProcess(handle, 1):
            raise OSError(ctypes.get_last_error(), f"无法停止旧版进程 {pid}")
        result = kernel32.WaitForSingleObject(handle, max(1, int(timeout * 1000)))
        if result == wait_timeout:
            raise TimeoutError(f"等待旧版进程 {pid} 退出超时")
        if result != wait_object_0:
            raise OSError(f"等待旧版进程 {pid} 退出失败，状态 {result}")
    finally:
        kernel32.CloseHandle(handle)


def _stop_processes_for_root(
    root: Path,
    *,
    process_provider: Callable[[], list[tuple[int, str]]] | None = None,
    terminator: Callable[[int], None] | None = None,
    timeout: float = 8.0,
) -> tuple[int, ...]:
    provider = process_provider or _windows_process_image_paths
    terminate = terminator or _terminate_windows_process
    targets = {_normalized_process_path(path) for path in _installed_executables(root)}

    def current_matches() -> dict[int, str]:
        matches: dict[int, str] = {}
        for pid, image_path in provider():
            if _normalized_process_path(image_path) in targets:
                matches[int(pid)] = image_path
        return matches

    stopped: list[int] = []
    for pid in sorted(current_matches()):
        try:
            terminate(pid)
        except OSError:
            if pid in current_matches():
                raise
        else:
            stopped.append(pid)

    deadline = time.monotonic() + timeout
    while current_matches():
        if time.monotonic() >= deadline:
            raise RuntimeError("旧版进程未在限定时间内退出，安装已中止")
        time.sleep(0.1)
    return tuple(stopped)


def _run_bootstrap(app_root: Path) -> None:
    try:
        completed = subprocess.run(
            [str(app_root / "guanye-professional.exe"), "--bootstrap-only"],
            cwd=str(app_root),
            check=False,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("bootstrap 转换超时") from exc
    except OSError as exc:
        raise RuntimeError("bootstrap 转换启动失败") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"bootstrap 转换失败，退出码 {completed.returncode}")


def _bootstrap_path(app_root: Path) -> Path:
    return app_root / "config" / "bootstrap.json"


def _load_bootstrap(app_root: Path) -> object:
    try:
        return json.loads(_bootstrap_path(app_root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise RuntimeError("安装包凭据 bootstrap 无效") from None


def _bootstrap_requires_installed_marker(app_root: Path) -> bool:
    path = _bootstrap_path(app_root)
    if not path.is_file():
        return False
    value = _load_bootstrap(app_root)
    if not isinstance(value, dict):
        raise RuntimeError("安装包凭据 bootstrap 无效")
    return value.get("installed") is not True


def _verify_bootstrap_installed(app_root: Path) -> None:
    try:
        value = _load_bootstrap(app_root)
    except RuntimeError as exc:
        raise RuntimeError("bootstrap 转换后未标记 installed:true") from exc
    if not isinstance(value, dict) or value.get("installed") is not True:
        raise RuntimeError("bootstrap 转换后未标记 installed:true")


def _raise_for_bootstrap_result(result: object) -> None:
    returncode = getattr(result, "returncode", None)
    if isinstance(returncode, int) and returncode != 0:
        raise RuntimeError(f"bootstrap 转换失败，退出码 {returncode}")


def _scrub_plaintext_bootstrap(root: Path) -> None:
    path = _bootstrap_path(root / "app")
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _rename_for_install(source: Path, destination: Path) -> None:
    source.rename(destination)


def _rollback_install_root(root: Path, previous: Path) -> None:
    failed = root.with_name(APP_NAME + ".failed")
    if failed.exists():
        _safe_remove(failed, root.parent)
    if root.exists():
        root.rename(failed)
    if not previous.exists():
        raise RuntimeError("旧安装备份不存在，无法回滚")
    previous.rename(root)
    if failed.exists():
        try:
            _safe_remove(failed, root.parent)
        except OSError:
            pass


def build_uninstall_script() -> str:
    """Build the UTF-8 batch script used for per-user uninstallation."""
    return "\r\n".join(
        (
            "@echo off",
            "setlocal DisableDelayedExpansion",
            "chcp 65001 >nul 2>&1",
            f'if /I "%~1"=="{_UNINSTALL_CLEANUP_ARGUMENT}" goto cleanup_self',
            "",
            "taskkill /IM guanye-professional.exe /F >nul 2>&1",
            "taskkill /IM HN-PPT2.exe /F >nul 2>&1",
            "ping.exe -n 3 127.0.0.1 >nul 2>&1",
            f'if exist "%LOCALAPPDATA%\\{APP_NAME}" rmdir /s /q "%LOCALAPPDATA%\\{APP_NAME}" >nul 2>&1',
            f'del /f /q "%USERPROFILE%\\Desktop\\{SHORTCUT_NAME}" >nul 2>&1',
            f'del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\{SHORTCUT_NAME}" >nul 2>&1',
            f'del /f /q "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\{UNINSTALL_SHORTCUT_NAME}" >nul 2>&1',
            "",
            'set "cleanup=%TEMP%\\GuanyeProfessional-Uninstall-Cleanup-%RANDOM%-%RANDOM%.cmd"',
            'copy /y "%~f0" "%cleanup%" >nul 2>&1',
            'if exist "%cleanup%" start "" /b "%ComSpec%" /d /c call "%cleanup%" --cleanup-self "%~f0"',
            "exit /b 0",
            "",
            ":cleanup_self",
            "ping.exe -n 3 127.0.0.1 >nul 2>&1",
            'if not "%~2"=="" del /f /q "%~2" >nul 2>&1',
            '(goto) 2>nul & del /f /q "%~f0" >nul 2>&1',
        )
    ) + "\r\n"


def write_uninstall_script(path: Path) -> None:
    """Write the uninstaller with a BOM so cmd.exe decodes Chinese paths."""
    path.write_text(build_uninstall_script(), encoding="utf-8-sig")


def install(
    target: Path | None = None,
    *,
    payload: Path | None = None,
    process_stopper: Callable[[Path], object] | None = None,
    shortcut_factory: Callable[..., object] | None = None,
    switch_root: Callable[[Path, Path], None] | None = None,
    bootstrap_runner: Callable[[Path], object] | None = None,
) -> Path:
    root = _local_root() if target is None else target.resolve()
    allowed = _local_root()
    if root != allowed and allowed not in root.parents:
        raise RuntimeError("安装目录不在当前用户目录下")
    payload_path = Path(payload).resolve() if payload is not None else _payload()
    stop_processes = process_stopper or _stop_processes_for_root
    make_shortcut = shortcut_factory or _shortcut
    replace_root = switch_root or _rename_for_install
    run_bootstrap = bootstrap_runner or _run_bootstrap
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="guanye-install-", dir=str(root.parent)))
    previous = root.with_name(APP_NAME + ".old")
    old_moved = False
    new_root_started = False
    created_paths: list[Path] = []

    def create_shortcut(
        target: Path,
        shortcut: Path,
        working: Path,
        arguments: str = "",
    ) -> None:
        existed = shortcut.exists()
        make_shortcut(target, shortcut, working, arguments)
        if not existed and shortcut.exists():
            created_paths.append(shortcut)

    try:
        with zipfile.ZipFile(payload_path) as archive:
            archive.extractall(staging)
        app = staging / "app"
        exe = app / "guanye-professional.exe"
        if not exe.is_file():
            raise RuntimeError("payload 缺少 guanye-professional.exe")

        recovered_previous = False
        if previous.exists() and not root.exists():
            stop_processes(previous)
            previous.rename(root)
            recovered_previous = True
        if root.exists():
            if not recovered_previous:
                stop_processes(root)
            if previous.exists():
                _safe_remove(previous, root.parent)
            root.rename(previous)
            old_moved = True

        new_root_started = True
        replace_root(staging, root)
        app_root = root / "app"
        if not (app_root / "guanye-professional.exe").is_file():
            raise RuntimeError("切换后的安装缺少 guanye-professional.exe")

        # A real plaintext bootstrap must end as installed:true. A missing
        # bootstrap or an already-installed marker is intentionally idempotent.
        requires_installed_marker = _bootstrap_requires_installed_marker(app_root)

        desktop = Path(os.environ["USERPROFILE"]) / "Desktop" / SHORTCUT_NAME
        desktop.parent.mkdir(parents=True, exist_ok=True)
        create_shortcut(app_root / "guanye-professional.exe", desktop, app_root)
        # Keep the uninstall script outside the app directory so it can remove
        # the complete install tree without a running self-lock.
        uninstaller = root.parent / "GuanyeProfessional-Uninstall.cmd"
        uninstaller_existed = uninstaller.exists()
        write_uninstall_script(uninstaller)
        if not uninstaller_existed:
            created_paths.append(uninstaller)
        uninstall_shortcut = (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / UNINSTALL_SHORTCUT_NAME
        )
        uninstall_shortcut.parent.mkdir(parents=True, exist_ok=True)
        create_shortcut(
            Path(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")),
            uninstall_shortcut,
            root.parent,
            f'/c ""{uninstaller}""',
        )
        startup = (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
            / SHORTCUT_NAME
        )
        startup.parent.mkdir(parents=True, exist_ok=True)
        create_shortcut(app_root / "guanye-professional.exe", startup, app_root)
        # Convert the packaged bootstrap into current-user DPAPI immediately.
        bootstrap_result = run_bootstrap(app_root)
        _raise_for_bootstrap_result(bootstrap_result)
        if requires_installed_marker:
            _verify_bootstrap_installed(app_root)
        if previous.exists():
            _safe_remove(previous, root.parent)
        old_moved = False
        return app_root
    except Exception as exc:
        if old_moved:
            _scrub_plaintext_bootstrap(root)
            try:
                _rollback_install_root(root, previous)
            except Exception as rollback_exc:
                raise RuntimeError(f"安装切换失败且回滚失败：{rollback_exc}") from exc
        elif new_root_started:
            cleanup_errors: list[str] = []
            _scrub_plaintext_bootstrap(root)
            if root.exists():
                try:
                    _safe_remove(root, root.parent)
                except Exception as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
            for created_path in reversed(created_paths):
                try:
                    created_path.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    cleanup_errors.append(str(cleanup_exc))
            if cleanup_errors:
                raise RuntimeError("首次安装失败且清理不完整：" + "；".join(cleanup_errors)) from exc
        raise
    finally:
        if staging.exists():
            _safe_remove(staging, root.parent)


def _schedule_self_delete(path: Path) -> None:
    script = Path(tempfile.gettempdir()) / ("GuanyeUninstallCleanup-" + str(os.getpid()) + ".cmd")
    try:
        script.write_text(
            '@echo off\r\ntimeout /t 2 /nobreak >nul\r\n'
            + 'del /f /q "' + str(path) + '"\r\n'
            + 'del /f /q "%~f0"\r\n', encoding='ascii')
        subprocess.Popen(["cmd.exe", "/c", str(script)], close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))
    except OSError:
        pass


def uninstall(*, detached: bool = False) -> None:
    root = _local_root()
    running = Path(sys.executable).resolve()
    if not detached and root in running.parents:
        # Windows locks a running EXE, so remove the install tree from a
        # temporary copy rather than trying to delete the running Uninstall.exe.
        temp = Path(tempfile.gettempdir()) / ("GuanyeUninstall-" + str(os.getpid()) + ".exe")
        shutil.copy2(running, temp)
        subprocess.Popen([str(temp), "--uninstall", "--detached"], close_fds=True,
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))
        return
    exe = root / "app" / "guanye-professional.exe"
    try:
        subprocess.run(["taskkill", "/IM", exe.name, "/F"], check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass
    desktop = Path(os.environ["USERPROFILE"]) / "Desktop" / SHORTCUT_NAME
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / SHORTCUT_NAME
    uninstall_shortcut = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / UNINSTALL_SHORTCUT_NAME
    for path in (desktop, startup, uninstall_shortcut):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if root.exists():
        _safe_remove(root, root.parent)
    if detached:
        _schedule_self_delete(running)


def _should_show_install_error_dialog() -> bool:
    return (
        os.name == "nt"
        and bool(getattr(sys, "frozen", False))
        and getattr(sys, "stderr", None) is None
    )


def _show_install_error_dialog(message: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, message, "观页专业版安装失败", 0x10)


def _install_error_message(exc: Exception) -> str:
    # Bootstrap subprocess output is never included in exceptions, so this
    # text cannot contain credentials read from bootstrap.json.
    detail = " ".join(str(exc).split())
    return f"安装失败：{detail}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="观页专业版安装器")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--target", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    # A copied Uninstall.exe behaves like --uninstall when double-clicked.
    if not args.uninstall and Path(sys.executable).name.lower() == "uninstall.exe":
        args.uninstall = True
    try:
        if args.uninstall:
            uninstall(detached=args.detached)
            print("观页专业版已卸载")
        else:
            print(f"已安装到 {install(args.target)}")
        return 0
    except Exception as exc:
        message = _install_error_message(exc)
        if getattr(sys, "stderr", None) is not None:
            print(message, file=sys.stderr)
        if _should_show_install_error_dialog():
            try:
                _show_install_error_dialog(message)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
