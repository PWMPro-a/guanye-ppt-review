"""Build the single-file Guanye professional Setup.exe from a staged app directory."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def make_payload(app_dir: Path, payload: Path) -> Path:
    app_dir = app_dir.resolve()
    if not (app_dir / "guanye-professional.exe").is_file():
        raise ValueError("staged app 缺少 guanye-professional.exe")
    payload = payload.resolve()
    if payload.exists():
        raise ValueError("payload.zip 已存在，禁止覆盖")
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [d for d in dirs if d not in {"data", "__pycache__"}]
            for name in files:
                source = Path(root) / name
                relative = Path("app") / source.relative_to(app_dir)
                archive.write(source, relative.as_posix())
    return payload


def build(app_dir: Path, output: Path, *, stage_only: bool = False) -> dict:
    app_dir = app_dir.resolve()
    output = output.resolve()
    if output.exists():
        raise ValueError("输出目录已存在，禁止覆盖旧安装包")
    output.mkdir(parents=True)
    payload = make_payload(app_dir, output / "payload.zip")
    if not stage_only:
        work = output / "pyinstaller-work"
        spec = output / "pyinstaller-spec"
        command = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--onefile", "--windowed", "--name", "观页专业版-Setup",
            "--distpath", str(output / "dist"), "--workpath", str(work), "--specpath", str(spec),
            "--add-data", str(payload) + ";.",
            "--hidden-import", "win32com.client",
            "--hidden-import", "pythoncom", "--hidden-import", "pywintypes",
            str(Path(__file__).with_name("guanye_installer.py").resolve()),
        ]
        completed = subprocess.run(command, cwd=str(Path(__file__).resolve().parents[1]),
                                   text=True, encoding="utf-8", errors="replace")
        if completed.returncode:
            raise RuntimeError(f"安装器 PyInstaller 构建失败，退出码 {completed.returncode}")
        exe = output / "dist" / "观页专业版-Setup.exe"
        if not exe.is_file():
            raise RuntimeError("PyInstaller 未生成 Setup.exe")
    manifest = {
        "format": "guanye-professional-setup", "version": 1,
        "payloadBytes": payload.stat().st_size,
        "stageOnly": stage_only,
        "output": str(output),
    }
    (output / "BUILD-MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="构建观页专业版 Setup.exe")
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.app_dir, args.output, stage_only=args.stage_only), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"构建安装包失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
