#!/usr/bin/env python3
"""Offline smoke test for the Guanye AI gateway image.

The container runs with ``--network none``.  Health is checked from inside the
same container over loopback, so this verifies the real default entrypoint
without exposing a host port or leaving a path to an external upstream.

The same Python file is streamed into the container over stdin for the document
portion: it creates a two-page Chinese flat-ODP fixture, uses the image's
LibreOffice to save it as PPTX, then calls ``document_import.convert`` and
checks page count, text markers, pixels, and page distinctness.

``/healthz`` is liveness only.  Supabase, storage, and model services are not
contacted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree

DEFAULT_IMAGE = "guanye-ai-gateway:smoke"
EXPECTED_USER = "10001:10001"
EXPECTED_UID = 10001
EXPECTED_GID = 10001
EXPECTED_ENTRYPOINT = ["python", "-B", "-m", "cloud.ai_gateway"]
HEALTH_SERVICE = "guanye-ai-gateway"
PAGE_MARKERS = ("SMOKE_PAGE_ONE", "SMOKE_PAGE_TWO")
HEALTH_CODE = (
    "import json,urllib.request;"
    "r=urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=3);"
    "p=json.loads(r.read());"
    "assert r.status==200 and p.get('ok') is True and p.get('service')=='guanye-ai-gateway';"
    "print(json.dumps(p,sort_keys=True))"
)


class SmokeError(RuntimeError):
    """A concrete smoke-test failure with operator-facing context."""


def _clip(value: bytes | str, limit: int = 3000) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    text = text.strip()
    return text if len(text) <= limit else text[-limit:]


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SmokeError(f"Executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SmokeError(f"Command timed out after {timeout:.0f}s: {' '.join(command[:4])}") from exc

    if check and completed.returncode != 0:
        details = []
        if completed.stdout:
            details.append(f"stdout:\n{_clip(completed.stdout)}")
        if completed.stderr:
            details.append(f"stderr:\n{_clip(completed.stderr)}")
        suffix = "\n" + "\n".join(details) if details else ""
        raise SmokeError(
            f"Command failed with exit code {completed.returncode}: {' '.join(command[:4])}{suffix}"
        )
    return completed


def _verify_image_config(docker: str, image: str) -> dict:
    completed = _run([docker, "image", "inspect", image], timeout=30)
    try:
        image_data = json.loads(completed.stdout.decode("utf-8"))[0]
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError, TypeError) as exc:
        raise SmokeError("docker image inspect did not return one image record") from exc

    config = image_data.get("Config") if isinstance(image_data, dict) else None
    if not isinstance(config, dict):
        raise SmokeError("image has no Config object")
    user = str(config.get("User") or "")
    entrypoint = config.get("Entrypoint")
    if user != EXPECTED_USER:
        raise SmokeError(f"image User is {user!r}, expected {EXPECTED_USER!r}")
    if entrypoint != EXPECTED_ENTRYPOINT:
        raise SmokeError(f"image Entrypoint is {entrypoint!r}, expected {EXPECTED_ENTRYPOINT!r}")
    return {"user": user, "entrypoint": entrypoint}


def _start_container(docker: str, image: str, name: str) -> None:
    environment = {
        "HOME": "/tmp",
        "GUANYE_HOST": "0.0.0.0",
        "GUANYE_PORT": "8787",
        # Synthetic, non-secret values. /healthz never calls these services.
        "GUANYE_SUPABASE_URL": "https://example.invalid",
        "GUANYE_SUPABASE_ANON_KEY": "smoke-only-anon",
        "GUANYE_SUPABASE_SERVICE_ROLE_KEY": "smoke-only-service",
        "GUANYE_WORKSPACE_ID": "00000000-0000-4000-8000-000000000001",
        "GUANYE_MODEL_GATEWAY_URL": "https://example.invalid/v1",
    }
    command = [
        docker,
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--shm-size=256m",
    ]
    for key, value in environment.items():
        command.extend(["-e", f"{key}={value}"])
    command.append(image)
    _run(command, timeout=60)


def _wait_for_health(docker: str, name: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        probe = _run(
            [docker, "exec", name, "python", "-B", "-c", HEALTH_CODE],
            timeout=10,
            check=False,
        )
        if probe.returncode == 0:
            lines = [line for line in probe.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
            if not lines:
                raise SmokeError("/healthz probe returned no JSON")
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError as exc:
                raise SmokeError(f"/healthz probe returned invalid JSON: {lines[-1]!r}") from exc
        last_error = _clip(probe.stderr or probe.stdout)
        time.sleep(0.5)
    raise SmokeError(f"/healthz did not become ready within {timeout:.0f}s: {last_error}")


def _container_logs(docker: str, name: str) -> str:
    completed = _run([docker, "logs", "--tail", "100", name], timeout=30, check=False)
    return _clip((completed.stdout or b"") + (completed.stderr or b""))


def _remove_container(docker: str, name: str) -> None:
    _run([docker, "rm", "--force", name], timeout=30, check=False)


def _build_fodp_fixture(path: Path) -> None:
    """Write a compact two-slide flat ODP (FODP) using only the stdlib."""
    page_data = (
        (1, "SMOKE_PAGE_ONE", "观页中文双页导入，第 1 页", "dp1", "accent1"),
        (2, "SMOKE_PAGE_TWO", "观页中文双页导入，第 2 页", "dp2", "accent2"),
    )
    pages = "".join(
        f"""
  <draw:page draw:name="SmokePage{number}" draw:style-name="{page_style}" draw:master-page-name="Default">
    <draw:rect draw:style-name="{accent}" svg:x="0in" svg:y="0in" svg:width="13.333in" svg:height="0.45in"/>
    <draw:rect draw:style-name="{accent}" svg:x="0.8in" svg:y="6.75in" svg:width="11.7in" svg:height="0.18in"/>
    <draw:frame draw:style-name="TextFrame" svg:x="0.9in" svg:y="1.2in" svg:width="11.5in" svg:height="3.5in">
      <draw:text-box>
        <text:p text:style-name="Marker">{marker}</text:p>
        <text:p text:style-name="Chinese">{subtitle}</text:p>
      </draw:text-box>
    </draw:frame>
  </draw:page>"""
        for number, marker, subtitle, page_style, accent in page_data
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
 xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
 office:mimetype="application/vnd.oasis.opendocument.presentation"
 office:version="1.2">
  <office:font-face-decls>
    <style:font-face style:name="Noto Sans CJK SC" svg:font-family="'Noto Sans CJK SC'"/>
  </office:font-face-decls>
  <office:automatic-styles>
    <style:page-layout style:name="pm1">
      <style:page-layout-properties fo:page-width="13.333in" fo:page-height="7.5in" style:print-orientation="landscape"/>
    </style:page-layout>
    <style:style style:name="dp1" style:family="drawing-page">
      <style:drawing-page-properties draw:fill="solid" draw:fill-color="#F5F7FF"/>
    </style:style>
    <style:style style:name="dp2" style:family="drawing-page">
      <style:drawing-page-properties draw:fill="solid" draw:fill-color="#FFF7ED"/>
    </style:style>
    <style:style style:name="accent1" style:family="graphic">
      <style:graphic-properties draw:fill="solid" draw:fill-color="#3457D5" draw:stroke="none"/>
    </style:style>
    <style:style style:name="accent2" style:family="graphic">
      <style:graphic-properties draw:fill="solid" draw:fill-color="#C2410C" draw:stroke="none"/>
    </style:style>
    <style:style style:name="TextFrame" style:family="graphic">
      <style:graphic-properties draw:fill="none" draw:stroke="none"/>
    </style:style>
    <style:style style:name="Marker" style:family="paragraph">
      <style:paragraph-properties fo:text-align="center" fo:margin-bottom="0.22in"/>
      <style:text-properties style:font-name="Noto Sans CJK SC" style:font-name-asian="Noto Sans CJK SC" fo:font-size="40pt" fo:font-weight="bold" fo:color="#102A43"/>
    </style:style>
    <style:style style:name="Chinese" style:family="paragraph">
      <style:paragraph-properties fo:text-align="center"/>
      <style:text-properties style:font-name="Noto Sans CJK SC" style:font-name-asian="Noto Sans CJK SC" fo:font-size="24pt" fo:color="#334E68"/>
    </style:style>
  </office:automatic-styles>
  <office:master-styles>
    <style:master-page style:name="Default" style:page-layout-name="pm1"/>
  </office:master-styles>
  <office:body>
    <office:presentation>{pages}
    </office:presentation>
  </office:body>
</office:document>
"""
    path.write_text(xml, encoding="utf-8")
    ElementTree.fromstring(xml)


def _assert_runtime_identity() -> dict:
    if os.getuid() != EXPECTED_UID or os.getgid() != EXPECTED_GID:
        raise SmokeError(f"runtime uid/gid is {os.getuid()}:{os.getgid()}, expected {EXPECTED_UID}:{EXPECTED_GID}")
    if Path.cwd() != Path("/app"):
        raise SmokeError(f"runtime working directory is {Path.cwd()}, expected /app")

    status = Path("/proc/1/status").read_text(encoding="utf-8").splitlines()
    uid_line = next((line for line in status if line.startswith("Uid:")), "")
    gid_line = next((line for line in status if line.startswith("Gid:")), "")
    process_uid = int(uid_line.split()[1]) if uid_line else -1
    process_gid = int(gid_line.split()[1]) if gid_line else -1
    if process_uid != EXPECTED_UID or process_gid != EXPECTED_GID:
        raise SmokeError(f"service process uid/gid is {process_uid}:{process_gid}, expected {EXPECTED_UID}:{EXPECTED_GID}")

    argv = [part.decode("utf-8", errors="replace") for part in Path("/proc/1/cmdline").read_bytes().split(b"\0") if part]
    normalized = [Path(argv[0]).name, *argv[1:]] if argv else []
    if normalized != EXPECTED_ENTRYPOINT:
        raise SmokeError(f"service process argv is {normalized!r}, expected {EXPECTED_ENTRYPOINT!r}")
    return {"uid": process_uid, "gid": process_gid, "cwd": str(Path.cwd()), "entrypoint_argv": normalized}


def _png_stats(path: Path, fitz_module) -> dict:
    pixmap = fitz_module.Pixmap(str(path))
    if pixmap.width <= 0 or pixmap.height <= 0 or pixmap.n < 3:
        raise SmokeError(f"rendered page has invalid pixel dimensions: {path.name}")

    width, height, components, stride = pixmap.width, pixmap.height, pixmap.n, pixmap.stride
    samples = pixmap.samples
    step_x = max(1, width // 120)
    step_y = max(1, height // 120)
    sampled = nonwhite = low = 0
    high = 0
    colors: set[tuple[int, int, int]] = set()
    for y in range(0, height, step_y):
        row = y * stride
        for x in range(0, width, step_x):
            offset = row + x * components
            red, green, blue = samples[offset], samples[offset + 1], samples[offset + 2]
            sampled += 1
            colors.add((red, green, blue))
            if red < 245 or green < 245 or blue < 245:
                nonwhite += 1
            luminance = (red * 299 + green * 587 + blue * 114) // 1000
            low = min(low, luminance)
            high = max(high, luminance)

    ratio = nonwhite / sampled if sampled else 0.0
    if ratio < 0.05 or len(colors) < 4 or high - low < 30:
        raise SmokeError(
            f"rendered page is visually trivial: {path.name} "
            f"(nonwhite={ratio:.3f}, colors={len(colors)}, luminance_range={high - low})"
        )
    return {
        "file": path.name,
        "size": path.stat().st_size,
        "width": width,
        "height": height,
        "nonwhite_ratio": round(ratio, 4),
        "sample_colors": len(colors),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _container_document_smoke() -> int:
    """Run inside the target image, streamed over docker exec stdin."""
    runtime = _assert_runtime_identity()
    soffice = shutil.which("soffice")
    if not soffice:
        raise SmokeError("soffice is not available in the image")

    with tempfile.TemporaryDirectory(prefix="guanye-gateway-smoke-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)

        fixture = root / "two-pages.fodp"
        _build_fodp_fixture(fixture)
        profile = root / "lo-pptx-profile"
        pptx = root / "two-pages.pptx"
        conversion = _run(
            [
                soffice,
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless",
                "--convert-to",
                "pptx",
                "--outdir",
                str(root),
                str(fixture),
            ],
            timeout=150,
        )
        if not pptx.is_file() or not zipfile.is_zipfile(pptx):
            raise SmokeError(f"LibreOffice did not create a valid PPTX: {_clip(conversion.stdout)}")
        with zipfile.ZipFile(pptx) as archive:
            if "ppt/presentation.xml" not in archive.namelist():
                raise SmokeError("LibreOffice-created PPTX is missing ppt/presentation.xml")

        import document_import
        import fitz

        rendered = root / "rendered"
        document_import.convert(pptx, rendered)
        result = json.loads((rendered / "result.json").read_text(encoding="utf-8"))
        pages = result.get("pages")
        expected_files = ["page-0001.png", "page-0002.png"]
        actual_files = [entry.get("file") for entry in pages] if isinstance(pages, list) else []
        if actual_files != expected_files:
            raise SmokeError(f"document_import returned pages {actual_files!r}, expected {expected_files!r}")

        with fitz.open(rendered / "converted.pdf") as document:
            if len(document) != 2:
                raise SmokeError(f"rendered PDF has {len(document)} pages, expected 2")
            page_texts = [document[index].get_text("text") for index in range(2)]

        for index, marker in enumerate(PAGE_MARKERS):
            if marker not in page_texts[index]:
                raise SmokeError(f"page {index + 1} is missing text marker {marker!r}")
            if "观页" not in page_texts[index]:
                raise SmokeError(f"page {index + 1} is missing the Chinese marker 观页")

        stats = [_png_stats(rendered / name, fitz) for name in expected_files]
        if stats[0]["sha256"] == stats[1]["sha256"]:
            raise SmokeError("the two rendered pages are byte-identical")

        print(
            json.dumps(
                {
                    "result": "passed",
                    "runtime": runtime,
                    "pages": stats,
                    "text_markers": list(PAGE_MARKERS),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


def _run_container_document_smoke(docker: str, name: str, timeout: float) -> dict:
    completed = _run(
        [docker, "exec", "-i", name, "python", "-B", "-", "--container-document-smoke"],
        timeout=timeout,
        input_bytes=Path(__file__).resolve().read_bytes(),
    )
    lines = [line for line in completed.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if not lines:
        raise SmokeError("container document smoke returned no JSON result")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise SmokeError(f"container document smoke returned invalid JSON: {lines[-1]!r}") from exc
    if payload.get("result") != "passed":
        raise SmokeError(f"container document smoke did not pass: {payload!r}")
    return payload


def _host_main(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    docker = args.docker
    image = args.image
    if args.build:
        _run(
            [docker, "build", "--file", "cloud/Dockerfile", "--tag", image, "."],
            cwd=repo_root,
            timeout=1800,
        )

    image_config = _verify_image_config(docker, image)
    name = f"guanye-gateway-smoke-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    started = False
    try:
        _start_container(docker, image, name)
        started = True
        health = _wait_for_health(docker, name, args.health_timeout)
        document = _run_container_document_smoke(docker, name, args.document_timeout)
    except Exception:
        if started:
            logs = _container_logs(docker, name)
            if logs:
                print(f"container logs (tail):\n{logs}", file=sys.stderr)
        raise
    finally:
        if started:
            _remove_container(docker, name)

    print(
        json.dumps(
            {
                "result": "passed",
                "image": image,
                "network": "none",
                "image_config": image_config,
                "runtime": document["runtime"],
                "healthz": health,
                "health_scope": "liveness only; Supabase/auth/storage/model were not contacted",
                "document_import": document,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"local image reference (default: {DEFAULT_IMAGE})")
    parser.add_argument("--build", action="store_true", help="build cloud/Dockerfile locally before testing")
    parser.add_argument("--docker", default="docker", help="Docker CLI executable (default: docker)")
    parser.add_argument("--health-timeout", type=float, default=90.0, help="seconds to wait for in-container /healthz")
    parser.add_argument("--document-timeout", type=float, default=240.0, help="seconds for the in-container document smoke")
    parser.add_argument("--container-document-smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.container_document_smoke:
        return _container_document_smoke()
    return _host_main(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except SmokeError as exc:
        print(f"smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
