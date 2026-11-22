"""Run the existing Windows Image2PPT service through the professional queue."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import sys
import warnings

from cloud.local_professional import load_browser_config
from cloud.professional_image_client import ProfessionalImageClient
from cloud.professional_image_settings import (
    ImageSettings,
    build_config,
    check_exe,
    image_singleton,
    load_settings,
    save_settings,
    settings_path,
)
from cloud.professional_worker_secrets import load_secrets
from cloud.windows_image_worker import SupabaseAuthConfig, SupabaseTokenProvider, WindowsWorker


def status_path(config):
    return settings_path(config).with_suffix(".status.txt")


def build_worker(config, secrets, image_settings):
    worker_config = build_config(config, image_settings)
    auth = SupabaseAuthConfig(config["url"], config["anonKey"], secrets.email, secrets.password)
    tokens = SupabaseTokenProvider(auth)
    gateway = ProfessionalImageClient(
        config["url"], config["workspaceId"], config["anonKey"], tokens, image_settings.worker_token,
    )
    return WindowsWorker(worker_config, gateway=gateway)


def _read_secret(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _report(message, *, log=None, stream=None):
    if stream is not None:
        print(message, file=stream, flush=True)
    if log is not None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.write_text(f"{stamp} {message}\n", encoding="utf-8")


def _safe_detail(exc):
    if isinstance(exc, (ValueError, RuntimeError)):
        text = str(exc).strip()
        if text and len(text) <= 240 and all(marker not in text for marker in ("Bearer ", "token=", "password", "password=")):
            return text
    return "请检查本机 EXE、专业版配置和当前 Windows 用户凭据。"


def main(argv=None):
    parser = argparse.ArgumentParser(description="观页专业版 Windows GPT Image Worker")
    parser.add_argument("action", choices=("configure", "doctor", "preflight", "run", "once"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, help="desktop-ready.json 或 backend-ready.json 的绝对路径（仅 configure）")
    parser.add_argument("--backend", action="store_true", help="backend-ready.json 还需要本机 EXE 凭据")
    parser.add_argument("--max-pages", type=int, default=0)
    args = parser.parse_args(argv)
    log = None
    try:
        config = load_browser_config(args.config)
        log = status_path(config)
        if args.action == "configure":
            if args.ready_file is None:
                raise ValueError("配置生图前必须用 --ready-file 指定 EXE 就绪文件的绝对路径。")
            if args.max_pages:
                raise ValueError("configure 不接受 --max-pages。")
            ready = args.ready_file.expanduser().resolve(strict=True)
            exe_token = _read_secret("本机 EXE 访问凭据（桌面代理模式直接回车）：").strip() if args.backend else ""
            if args.backend and not exe_token:
                raise ValueError("backend-ready.json 必须配置本机 EXE 凭据。")
            if not args.backend and exe_token:
                raise ValueError("桌面代理模式不接收额外 EXE 凭据。")
            worker_token = _read_secret("图片工作节点令牌（GUANYE_IMAGE_WORKER_TOKEN）：").strip()
            save_settings(config, ImageSettings(worker_token, str(ready), exe_token))
            print("已用当前 Windows 用户 DPAPI 保存图片工作配置。")
            return 0
        if args.ready_file is not None or args.backend:
            raise ValueError("只有 configure 接受 EXE 就绪文件和 --backend。")
        if args.max_pages < 0:
            raise ValueError("--max-pages 不能为负数。")
        with image_singleton(config):
            image_settings = load_settings(config)
            worker = build_worker(config, load_secrets(config), image_settings)
            if args.action in ("doctor", "preflight"):
                result = check_exe(worker.config)
                try:
                    worker.gateway.authorize()
                    cloud_identity = True
                except Exception:
                    # doctor stays local-only; preflight is the explicit cloud gate.
                    if args.action == "preflight":
                        raise
                    cloud_identity = False
                result["isolatedState"] = str(worker.config.state_dir)
                result["cloudIdentityVerified"] = cloud_identity
                result["paidRequests"] = 0
                result["scope"] = "只验证本机 DPAPI、EXE 回环协议和设置文件；未领取云端任务。"
                if args.action == "doctor":
                    print("本机生图凭据、EXE 回环协议和 16 路配置可读取；尚未领取云端任务或生成图片。" if not cloud_identity else "本机生图凭据、EXE 回环协议、云端评分师身份和 16 路配置已验证；尚未生成图片。")
                else:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            _report("专业版生图 Worker 已启动；尚未生成图片。", log=log, stream=sys.stdout)
            if args.action == "once":
                worker.run(max_pages=args.max_pages, once=True)
                result = worker.last_run
                if result.claim_failed or result.failed or (result.claimed == 0 and not result.empty):
                    status, code = "failed", 2
                elif result.empty:
                    status, code = "empty", 3
                else:
                    status, code = "succeeded", 0
                print(json.dumps({"mode": "once", "status": status, "claimed": result.claimed,
                                  "processed": result.processed, "failed": result.failed},
                                 ensure_ascii=False, separators=(",", ":")))
                return code
            worker.run(max_pages=args.max_pages)
            return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        message = "无法初始化观页专业版生图 Worker：" + _safe_detail(exc)
        try:
            _report(message, log=log, stream=sys.stderr)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
