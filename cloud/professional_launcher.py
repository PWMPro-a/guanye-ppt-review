"""Zero-config Windows launcher for the professional Guanye app.

The packaged app keeps one local launcher process alive.  It serves the loopback
UI, starts the existing review/image workers in-process, optionally starts the
bundled HN PPT2 executable, and opens the browser.  No system Python or manual
configuration is needed on the target machine.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any

from cloud.local_professional import ProfessionalServer, load_browser_config
from cloud.professional_creation import ProfessionalCreation
from cloud.professional_image_settings import ImageSettings, image_singleton, load_settings, save_settings, settings_path
from cloud.professional_image_worker import build_worker as build_image_worker
from cloud.professional_intake import build_intake_worker
from cloud.professional_review_worker import build_worker as build_review_worker, serve as serve_review_worker
from cloud.professional_worker_secrets import load_secrets, save_secrets

DEFAULT_PORT = 8765
MAX_WAIT_SECONDS = 240


def _single_instance():
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateMutexW
    create.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    handle = create(None, False, r"Local\GuanyeProfessionalLauncher")
    error = ctypes.get_last_error()
    if not handle:
        raise OSError("无法建立单实例锁")
    if error == 183:
        kernel32.CloseHandle(handle)
        return False
    return handle


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _path(value: str, root: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    return path if path.is_absolute() else (root / path)


def load_runtime(root: Path | None = None) -> dict[str, Any]:
    root = app_root() if root is None else root.resolve()
    config_path = root / "config" / "runtime.json"
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("安装包缺少有效的 config/runtime.json") from None
    if not isinstance(value, dict):
        raise ValueError("安装包运行时配置必须是 JSON 对象")
    port = value.get("port", DEFAULT_PORT)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("安装包端口无效")
    hn = value.get("hnPpt2", {})
    if hn is None:
        hn = {}
    if not isinstance(hn, dict):
        raise ValueError("HN PPT2 配置无效")
    executable = str(hn.get("executable", "")).strip()
    ready_file = str(hn.get("readyFile", "")).strip()
    workers = value.get("workers", {})
    if workers is None:
        workers = {}
    if not isinstance(workers, dict):
        raise ValueError("Worker 配置无效")
    return {
        "root": root,
        "port": port,
        "openBrowser": value.get("openBrowser", True) is not False,
        "hn": {
            "enabled": bool(hn.get("enabled", True)),
            "executable": str(_path(executable, root)) if executable else "",
            "readyFile": str(_path(ready_file, root)) if ready_file else "",
            "dataDir": str(_path(str(hn.get("dataDir", "")), root)) if hn.get("dataDir") else "",
            "providersFile": str(_path(str(hn.get("providersFile", "")), root)) if hn.get("providersFile") else "",
            "initialSettingsFile": str(_path(str(hn.get("initialSettingsFile", "")), root)) if hn.get("initialSettingsFile") else "",
            "waitSeconds": max(1, min(int(hn.get("waitSeconds", MAX_WAIT_SECONDS)), 900)),
        },
        "workers": {
            "review": bool(workers.get("review", True)),
            "image": bool(workers.get("image", True)),
            "intake": bool(workers.get("intake", True)),
            "concurrency": 16,
        },
    }


def install_bootstrap(root: Path | None = None) -> bool:
    """Install build-time DPAPI credentials once, then remove plaintext bootstrap."""
    root = app_root() if root is None else root.resolve()
    public_path = root / "config" / "professional.json"
    bootstrap_path = root / "config" / "bootstrap.json"
    public_config = load_browser_config(public_path)
    if not bootstrap_path.is_file():
        return False
    try:
        value = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("安装包凭据 bootstrap 无效") from None
    if not isinstance(value, dict) or value.get("installed") is True:
        return False
    review = value.get("review")
    image = value.get("image")
    if not isinstance(review, dict) or not isinstance(image, dict):
        raise ValueError("安装包凭据 bootstrap 字段不完整")
    from cloud.professional_review_worker import Credentials
    credentials = Credentials(
        str(review.get("email", "")), str(review.get("password", "")),
        str(review.get("workerToken", "")), str(review.get("modelKey", "")),
    )
    settings = ImageSettings(
        str(image.get("workerToken", "")), str(_path(str(image.get("readyFile", "")), root)),
        str(image.get("exeToken", "")),
    )
    save_secrets(public_config, credentials)
    save_settings(public_config, settings)
    bootstrap_path.write_text('{"installed":true}\n', encoding="utf-8")
    return True


def _log(root: Path, message: str) -> None:
    path = root / "data" / "professional.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")


def _wait_ready(path: Path, process: subprocess.Popen | None, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("url"):
                    return True
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        time.sleep(0.5)
    return False


def start_hn(runtime: dict[str, Any], root: Path, stack: ExitStack):
    hn = runtime["hn"]
    if not hn["enabled"]:
        return None
    ready_path = Path(hn["readyFile"]) if hn["readyFile"] else None
    if ready_path is not None and ready_path.is_file():
        return None
    executable = Path(hn["executable"]) if hn["executable"] else None
    if executable is None or not executable.is_file():
        raise ValueError("安装包的 HN PPT2 程序不存在")
    env = dict(os.environ, HN_LOCAL_HEADLESS="1")
    if hn["dataDir"]:
        data_dir = Path(hn["dataDir"]); data_dir.mkdir(parents=True, exist_ok=True)
        env["HN_LOCAL_DATA"] = str(data_dir)
        for configured, name in ((hn["providersFile"], "providers.json"), (hn["initialSettingsFile"], "initial-settings.json")):
            if configured:
                source = Path(configured)
                target = data_dir / name
                if source.is_file() and not target.exists():
                    target.write_bytes(source.read_bytes())
    process = subprocess.Popen([str(executable)], cwd=str(executable.parent), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stack.callback(lambda: process.terminate() if process.poll() is None else None)
    if hn["readyFile"] and not _wait_ready(Path(hn["readyFile"]), process, hn["waitSeconds"]):
        raise ValueError("HN PPT2 未在限定时间内就绪")
    _log(root, "HN PPT2 started")
    return process


def start_workers(runtime: dict[str, Any], public_config: dict[str, Any], root: Path, stack: ExitStack) -> None:
    workers = runtime["workers"]
    if not workers.get("review", True) and not workers.get("image", True) and not workers.get("intake", True):
        return
    secrets = load_secrets(public_config)
    if workers.get("review", True):
        broker, model = build_review_worker(public_config, secrets)
        thread = threading.Thread(target=serve_review_worker, args=(broker, model), name="guanye-review", daemon=True)
        thread.start()
        _log(root, "review worker started")
    if workers.get("image", True):
        image_settings = load_settings(public_config)
        worker = build_image_worker(public_config, secrets, image_settings)
        guard = stack.enter_context(image_singleton(public_config))
        thread = threading.Thread(target=worker.run, name="guanye-image", daemon=True)
        thread.start()
        _log(root, "image worker started (16 lanes)")
    if workers.get("intake", True):
        intake = build_intake_worker(public_config, secrets, log=lambda message: _log(root, message))
        stack.callback(intake.stop)
        intake.start()
        _log(root, "intake worker started")


def launch(root: Path | None = None, *, smoke: bool = False, no_browser: bool = False) -> int:
    root = app_root() if root is None else root.resolve()
    runtime = load_runtime(root)
    instance = _single_instance()
    if instance is False:
        if runtime["openBrowser"] and not no_browser:
            webbrowser.open(f"http://127.0.0.1:{runtime['port']}/", new=1)
        return 0
    install_bootstrap(root)
    public_config = load_browser_config(root / "config" / "professional.json")
    creation = ProfessionalCreation.from_local_secrets(public_config)
    server = ProfessionalServer(runtime["port"], root / "public", public_config, creation=creation)
    thread = threading.Thread(target=server.serve_forever, name="guanye-server", daemon=True)
    thread.start()
    _log(root, f"server started on 127.0.0.1:{runtime['port']}")
    stack = ExitStack()
    try:
        start_hn(runtime, root, stack)
        try:
            start_workers(runtime, public_config, root, stack)
        except Exception as exc:
            _log(root, "worker startup deferred: " + type(exc).__name__)
        if runtime["openBrowser"] and not no_browser:
            webbrowser.open(f"http://127.0.0.1:{runtime['port']}/", new=1)
        if smoke:
            return 0
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        stack.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
        if instance not in (True, False):
            ctypes.windll.kernel32.CloseHandle(instance)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="观页专业版零配置启动器")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--bootstrap-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.bootstrap_only:
            root = app_root() if args.root is None else args.root.resolve()
            install_bootstrap(root)
            return 0
        return launch(args.root, smoke=args.smoke, no_browser=args.no_browser)
    except Exception as exc:
        root = app_root() if args.root is None else args.root.resolve()
        try:
            _log(root, "launcher failed: " + type(exc).__name__)
        except OSError:
            pass
        print(f"观页专业版启动失败：{type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
