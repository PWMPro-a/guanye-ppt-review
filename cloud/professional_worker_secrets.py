"""Protect credentials with Windows current-user DPAPI; store no plaintext files."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

MAX_PROTECTED_BYTES = 64 * 1024


class _Blob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte)))


def _blob(data):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _Blob(len(data), buffer), buffer


def _fingerprint(config):
    identity = f"guanye-professional-review-v1\0{config['url']}\0{config['workspaceId']}"
    return hashlib.sha256(identity.encode("utf-8")).digest()


def _path(config):
    root = Path(os.environ["LOCALAPPDATA"])
    if not root.is_absolute():
        raise ValueError("LOCALAPPDATA 必须是绝对路径")
    return root / "GuanyeProfessional" / ("review-" + _fingerprint(config).hex() + ".dpapi")


def status_path(config):
    return _path(config).with_suffix(".status.txt")


def _transform(data, entropy, *, decrypt):
    if os.name != "nt":
        raise OSError("预评凭据只支持 Windows 用户 DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(_Blob)
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    fn.argtypes = [pointer, ctypes.c_void_p if decrypt else wintypes.LPCWSTR, pointer,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, pointer]
    fn.restype = wintypes.BOOL
    free = kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    output = _Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; deliberately no CRYPTPROTECT_LOCAL_MACHINE.
        if not fn(ctypes.byref(input_blob), None, ctypes.byref(entropy_blob), None, None, 1,
                  ctypes.byref(output)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not 1 <= output.cbData <= MAX_PROTECTED_BYTES:
            raise ValueError("凭据数据大小无效")
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(input_buffer, 0, len(input_buffer))
        ctypes.memset(entropy_buffer, 0, len(entropy_buffer))
        if output.pbData:
            if decrypt:
                ctypes.memset(output.pbData, 0, output.cbData)
            free(output.pbData)


def _save_payload(path, entropy, values):
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted = _transform(payload, entropy, decrypt=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.stem + "-", suffix=".tmp", delete=False) as handle:
            name = handle.name
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def _load_payload(path, entropy):
    with path.open("rb") as source:
        data = source.read(MAX_PROTECTED_BYTES + 1)
    if not 1 <= len(data) <= MAX_PROTECTED_BYTES:
        raise ValueError("本机 Worker 凭据文件无效")
    return json.loads(_transform(data, entropy, decrypt=True).decode("utf-8"))


def save_secrets(config, secrets):
    _save_payload(_path(config), _fingerprint(config), asdict(secrets))


def load_secrets(config):
    from cloud.professional_review_worker import Credentials

    values = _load_payload(_path(config), _fingerprint(config))
    if not isinstance(values, dict) or set(values) != {"email", "password", "worker_token", "model_key"}:
        raise ValueError("本机预评凭据字段无效")
    return Credentials(**values)


@contextmanager
def _singleton_name(name):
    if os.name != "nt":
        raise OSError("专业版后台 Worker 只支持 Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateMutexW
    create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = create(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:  # ERROR_ALREADY_EXISTS: do not start a duplicate worker.
            raise RuntimeError("已有相同工作区的同类专业版 Worker")
        yield
    finally:
        close(handle)


def singleton(config):
    return _singleton_name("Local\\GuanyeProfessionalReview-" + _fingerprint(config).hex())
