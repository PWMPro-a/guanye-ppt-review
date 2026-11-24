"""Windows-only settings for the professional GPT Image worker."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from cloud.professional_worker_secrets import _load_payload, _path, _save_payload, _singleton_name
from cloud.windows_image_worker import ExeClient, LocalState, WorkerConfig, _release_shell_name

MAX_READY_BYTES = 16 * 1024
DEFAULT_WORKERS = 16


@dataclass(frozen=True)
class ImageSettings:
    worker_token: str = field(repr=False)
    ready_file: str
    exe_token: str = field(default="", repr=False)

    def __post_init__(self):
        if not isinstance(self.worker_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.worker_token):
            raise ValueError("生图 Worker 令牌长度或格式无效")
        if not isinstance(self.ready_file, str) or not 1 <= len(self.ready_file) <= 1200 or any(ord(c) < 32 for c in self.ready_file):
            raise ValueError("必须指定有效的 EXE 就绪文件")
        ready = Path(self.ready_file)
        if not ready.is_absolute() or ready.name not in {"desktop-ready.json", "backend-ready.json"}:
            raise ValueError("必须指定创作 EXE 的 desktop-ready.json 或 backend-ready.json 绝对路径")
        if not isinstance(self.exe_token, str) or (self.exe_token and not re.fullmatch(r"[\x21-\x7e]{1,1024}", self.exe_token)):
            raise ValueError("本机 EXE 凭据格式无效")
        if ready.name == "backend-ready.json" and not self.exe_token:
            raise ValueError("后端直连需要本机 EXE 凭据")
        if ready.name == "desktop-ready.json" and self.exe_token:
            raise ValueError("桌面代理自行注入凭据，不接收额外 EXE 凭据")


def image_fingerprint(config):
    return sha256(f"guanye-professional-image-v1\0{config['url']}\0{config['workspaceId']}".encode()).digest()


def settings_path(config):
    return _path(config).with_name("image-" + image_fingerprint(config).hex() + ".dpapi")


def load_settings(config):
    value = _load_payload(settings_path(config), image_fingerprint(config))
    if not isinstance(value, dict) or set(value) != {"worker_token", "ready_file", "exe_token"}:
        raise ValueError("本机生图凭据字段无效")
    return ImageSettings(**value)


def save_settings(config, settings):
    _save_payload(settings_path(config), image_fingerprint(config), asdict(settings))


def image_singleton(config):
    return _singleton_name("Local\\GuanyeProfessionalImage-" + image_fingerprint(config).hex())


@dataclass(frozen=True)
class CoverWorkerSettings:
    email: str = field(repr=False)
    password: str = field(repr=False)
    worker_token: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.worker_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.worker_token):
            raise ValueError("试点生图 Worker 令牌长度或格式无效")
        if not isinstance(self.email, str) or not 1 <= len(self.email) <= 320 or "@" not in self.email:
            raise ValueError("试点评分账号邮箱无效")
        if not isinstance(self.password, str) or not 1 <= len(self.password) <= 4096:
            raise ValueError("缺少试点评分账号密码")


def cover_settings_path(config):
    root = Path(os.environ["LOCALAPPDATA"])
    if not root.is_absolute():
        raise ValueError("LOCALAPPDATA 必须是绝对路径")
    return root / "Guanye" / "CoverPilot" / ("image-" + image_fingerprint(config).hex() + ".dpapi")


def _cover_worker_path(config):
    return cover_settings_path(config).with_suffix(".cover-worker.dpapi")


def load_cover_worker_settings(config):
    data = _load_payload(_cover_worker_path(config), image_fingerprint(config) + b"\0cover-worker-v1")
    if not isinstance(data, dict) or set(data) != {"email", "password", "worker_token"}:
        raise ValueError("试点生图 Worker 凭据字段无效")
    return CoverWorkerSettings(**data)


def save_cover_worker_settings(config, settings):
    _save_payload(_cover_worker_path(config), image_fingerprint(config) + b"\0cover-worker-v1", asdict(settings))


def build_config(config, settings):
    ready_file = Path(settings.ready_file)
    with ready_file.open("rb") as handle:
        raw = handle.read(MAX_READY_BYTES + 1)
    if not 1 <= len(raw) <= MAX_READY_BYTES:
        raise ValueError("创作 EXE 就绪文件大小无效")
    ready = json.loads(raw.decode("utf-8-sig"))
    url = ready.get("url") if isinstance(ready, dict) else None
    if not isinstance(url, str) or not re.fullmatch(r"http://(?:127\.0\.0\.1|\[::1\]):[0-9]{1,5}/?", url):
        raise ValueError("创作 EXE 必须使用明确的 IPv4/IPv6 回环地址和端口")
    if not urlsplit(url).port:
        raise ValueError("创作 EXE 端口无效")
    desktop = ready_file.name == "desktop-ready.json"
    state_dir = settings_path(config).with_suffix("")
    return WorkerConfig(
        gateway_base=config["url"], gateway_token="", worker_token=settings.worker_token,
        workspace_id=config["workspaceId"], exe_base=url.rstrip("/"), exe_token=settings.exe_token,
        worker_id="guanye-image-" + image_fingerprint(config).hex()[:20],
        workers=DEFAULT_WORKERS, state_dir=state_dir, exe_desktop_proxy=desktop,
        exe_endpoint_source="desktop-ready" if desktop else "backend-ready",
        exe_ready_file=ready_file.name, exe_release_shell=_release_shell_name(ready),
    )


def check_exe(worker):
    # Read-only compatibility probes, not a model run or an executable identity check.
    return ExeClient(
        worker.exe_base, worker.exe_token, LocalState(worker.state_dir),
        desktop_proxy=worker.exe_desktop_proxy, endpoint_source=worker.exe_endpoint_source,
        ready_file=worker.exe_ready_file, release_shell=worker.exe_release_shell,
    ).preflight()
