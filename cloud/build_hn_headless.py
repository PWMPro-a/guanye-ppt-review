"""Stage the HN PPT2 runtime and build a headless HN launcher.

The official runtime zip remains the source of backend/frontend/runtime; this
script overlays the current local backend and builds only the small launcher.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def stage(source: Path, runtime_zip: Path, output: Path, site_overlay: Path | None = None) -> Path:
    source = source.resolve(); runtime_zip = runtime_zip.resolve(); output = output.resolve()
    if output.exists():
        raise ValueError("HN 输出目录已存在，禁止覆盖")
    resources = output / "resources"
    output.mkdir(parents=True)
    extract_root = output / ".runtime-extract"
    with zipfile.ZipFile(runtime_zip) as archive:
        archive.extractall(extract_root)
    children = list(extract_root.iterdir())
    package_root = children[0] if len(children) == 1 and children[0].is_dir() else extract_root
    for child in package_root.iterdir():
        shutil.move(str(child), resources / child.name)
    shutil.rmtree(extract_root)
    backend = source / "src" / "local_backend.py"
    if not backend.is_file():
        raise ValueError("HN 源码缺少 src/local_backend.py")
    shutil.copy2(backend, resources / "local_backend.py")
    web = source / "web"
    if web.is_dir():
        shutil.copytree(web, resources / "frontend", dirs_exist_ok=True)
    repair = source / "repair"
    if repair.is_dir():
        shutil.copytree(repair, resources / "repair", dirs_exist_ok=True)
    if site_overlay is not None:
        overlay = site_overlay.resolve()
        if not overlay.is_dir():
            raise ValueError("Python site-packages overlay 目录不存在")
        shutil.copytree(overlay, resources / "python" / "Lib" / "site-packages", dirs_exist_ok=True)
    return resources


def build_launcher(source: Path, output: Path) -> Path:
    source = source.resolve(); output = output.resolve()
    pyi_work = output / "pyinter-work"
    pyi_spec = output / "pyinter-spec"
    dist = output / "launcher-dist"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "HN-PPT2",
        "--paths", str(source / "src"), "--distpath", str(dist),
        "--workpath", str(pyi_work), "--specpath", str(pyi_spec),
        "--collect-all", "webview", "--collect-all", "fitz",
        "--collect-all", "PIL", "--hidden-import", "light_client_shell",
        "--hidden-import", "local_render", "--hidden-import", "local_desktop",
        "--hidden-import", "sqlite3", "--hidden-import", "requests",
        "--hidden-import", "win32com.client", "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes",
        str(source / "src" / "local_desktop.py"),
    ]
    completed = subprocess.run(command, cwd=str(source), text=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise RuntimeError(f"HN PPT2 启动器构建失败，退出码 {completed.returncode}")
    exe = dist / "HN-PPT2.exe"
    if not exe.is_file():
        raise RuntimeError("未生成 HN-PPT2.exe")
    return exe


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="构建 HN PPT2 零配置运行时")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-overlay", type=Path)
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        resources = stage(args.source, args.runtime_zip, args.output, args.site_overlay)
        result = {"resources": str(resources), "launcher": ""}
        if not args.stage_only:
            result["launcher"] = str(build_launcher(args.source, args.output))
        (args.output / "BUILD-MANIFEST.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"HN 构建失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
