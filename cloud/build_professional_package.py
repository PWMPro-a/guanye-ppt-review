"""Build a zero-config Guanye professional app directory.

Inputs are deliberately external to the repository: professional.json,
runtime.json, optional bootstrap.json, and the bundled HN PPT2 executable.
This keeps production secrets out of source control.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise ValueError(f"缺少构建输入目录：{source}")
    shutil.copytree(source, target)


def _copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise ValueError(f"缺少构建输入文件：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _validate_config(config_dir: Path) -> None:
    public = config_dir / "professional.json"
    runtime = config_dir / "runtime.json"
    for path in (public, runtime):
        if not path.is_file():
            raise ValueError(f"缺少构建配置：{path.name}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path.name} 不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} 必须是 JSON 对象")


def _run_pyinstaller(root: Path, output: Path, work: Path, spec: Path) -> None:
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", "guanye-professional",
        "--distpath", str(output / "app"), "--workpath", str(work), "--specpath", str(spec),
        "--paths", str(root),
        "--hidden-import", "document_import", "--hidden-import", "fitz",
        "--hidden-import", "PIL", "--hidden-import", "requests",
        "--hidden-import", "win32com.client", "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes", "--hidden-import", "cryptography",
        "--hidden-import", "cloud.professional_launcher",
        "--hidden-import", "cloud.local_professional",
        "--hidden-import", "cloud.professional_creation",
        "--hidden-import", "cloud.professional_image_client",
        "--hidden-import", "cloud.professional_image_settings",
        "--hidden-import", "cloud.professional_image_worker",
        "--hidden-import", "cloud.professional_review_worker",
        "--hidden-import", "cloud.professional_worker_secrets",
        "--hidden-import", "cloud.windows_image_worker",
        "--hidden-import", "cloud.ai_planning",
        "--hidden-import", "cloud.ai_generation",
        "--hidden-import", "cloud.ai_gateway",
        "--hidden-import", "cloud.ai_reviews",
        "--hidden-import", "cloud.professional_import",
        "--exclude-module", "playwright", "--exclude-module", "scipy",
        "--exclude-module", "cv2", "--exclude-module", "pandas",
        "--exclude-module", "matplotlib", "--exclude-module", "torch",
        "--exclude-module", "tensorflow", "--exclude-module", "onnxruntime",
        "--exclude-module", "pytest",
        str(root / "cloud" / "professional_launcher.py"),
    ]
    completed = subprocess.run(command, cwd=str(root), text=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise RuntimeError(f"PyInstaller 构建失败，退出码 {completed.returncode}")


def build(root: Path, config_dir: Path, output: Path, hn_ppt2: Path | None, *, stage_only: bool, hn_resources: Path | None = None) -> dict:
    root = root.resolve()
    config_dir = config_dir.resolve()
    output = output.resolve()
    _validate_config(config_dir)
    if output.exists():
        raise ValueError("输出目录已存在；请换一个全新的输出目录，禁止覆盖旧构建")
    output.mkdir(parents=True)
    _copy_tree(root / "public", output / "public")
    _copy_tree(config_dir, output / "config")
    (output / "data").mkdir()
    if hn_ppt2 is not None:
        hn_ppt2 = hn_ppt2.resolve()
        if not hn_ppt2.is_file():
            raise ValueError("HN PPT2 构建输入不是文件")
        _copy_file(hn_ppt2, output / "hn-ppt2" / hn_ppt2.name)
    if hn_resources is not None:
        _copy_tree(hn_resources, output / "hn-ppt2" / "resources")
    if not stage_only:
        work = output.parent / (output.name + ".pyinstaller-work")
        spec = output.parent / (output.name + ".pyinstaller-spec")
        if work.exists() or spec.exists():
            raise ValueError("PyInstaller 临时目录已存在；请清理后使用新输出目录")
        _run_pyinstaller(root, output, work, spec)
        app = output / "app" / "guanye-professional"
        if not app.is_dir():
            raise RuntimeError("PyInstaller 未生成预期的 app 目录")
        _copy_tree(root / "public", app / "public")
        _copy_tree(config_dir, app / "config")
        (app / "data").mkdir(exist_ok=True)
        if hn_ppt2 is not None:
            _copy_file(hn_ppt2, app / "hn-ppt2" / hn_ppt2.name)
        if hn_resources is not None:
            _copy_tree(hn_resources, app / "hn-ppt2" / "resources")
    manifest = {
        "format": "guanye-professional-build",
        "version": 1,
        "stageOnly": stage_only,
        "hasBootstrap": (config_dir / "bootstrap.json").is_file(),
        "hasHnPpt2": hn_ppt2 is not None,
        "output": str(output),
    }
    (output / "BUILD-MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="构建观页专业版 Windows 应用目录")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--hn-ppt2", type=Path)
    parser.add_argument("--hn-resources", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage-only", action="store_true", help="仅组装目录，不运行 PyInstaller")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args.root, args.config_dir, args.output, args.hn_ppt2, stage_only=args.stage_only, hn_resources=args.hn_resources), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
