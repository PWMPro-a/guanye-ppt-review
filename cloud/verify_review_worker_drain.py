#!/usr/bin/env python3
"""Verify review-worker draining under a real Docker stop.

Docker mode uses the already-built image, its exec-form entrypoint, and
``--init`` like Compose. A synthetic fixture is started by ``docker exec`` in
the same ``--network none`` namespace and serves Supabase-like REST/RPC,
storage, and model responses over loopback. Each lane is held at its model
request until a real ``docker stop`` has produced the worker's normal drain
marker. The fixture then releases both attempts.

Local mode runs the real CLI main function with an in-process SIGTERM. It is a
Windows protocol check only, not Linux container evidence.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_IMAGE = "guanye-ai-gateway:smoke"
DEFAULT_PORT = 18787
ENTRYPOINT = ["python", "-B", "-m", "cloud.ai_gateway"]
USER = "10001:10001"
WORKSPACE = "00000000-0000-4000-8000-000000000001"
JUDGE = "00000000-0000-4000-8000-000000000002"
DECKS = {
    "uploaded": "00000000-0000-4000-8000-000000000101",
    "generated": "00000000-0000-4000-8000-000000000102",
}
JOBS = {
    "uploaded": "00000000-0000-4000-8000-000000000201",
    "generated": "00000000-0000-4000-8000-000000000202",
}
DECK_VERSION = "deck-v1"
RUBRIC_VERSION = "rubric-v1"
DRAIN_MARKER = "Review worker stop requested; draining in-flight attempts"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nYQAAAAASUVORK5CYII="
)
MODEL_RESULT = {
    "scores": {"visual": 4, "layout": 4, "match": 4},
    "reasons": ["synthetic drain fixture"],
    "summary": "synthetic drain fixture",
    "confidence": 0.9,
    "classification": {"label": "fixture"},
}


class SmokeError(RuntimeError):
    pass


_HOST: Any = None


def _host() -> Any:
    global _HOST
    if _HOST is None:
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import cloud.verify_gateway_image as host
        _HOST = host
    return _HOST


def _run(command: Sequence[str], *, cwd: Path | None = None, timeout: float = 60) -> subprocess.CompletedProcess[bytes]:
    try:
        return _host()._run(list(command), cwd=cwd, timeout=timeout)
    except _host().SmokeError as exc:
        raise SmokeError(str(exc)) from exc


def _wait(label: str, timeout: float, probe: Callable[[], Any], ok: Callable[[Any], bool]) -> Any:
    deadline = time.monotonic() + max(0.1, timeout)
    last: Any = None
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            error = None
            if ok(last):
                return last
        except Exception as exc:
            error = exc
        time.sleep(0.1)
    if error is not None:
        raise SmokeError(f"Timed out waiting for {label}: {error}") from error
    raise SmokeError(f"Timed out waiting for {label}: {last!r}")


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"Cannot read fixture state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SmokeError("Fixture state must be a JSON object")
    return value


class Fixture:
    """One fixed fixture: lock-protected state plus a blocking model gate."""

    def __init__(self, port: int = 0, state_file: Path | None = None, release_file: Path | None = None):
        self.port = int(port)
        self.state_file = state_file
        self.release_file = release_file
        self.lock = threading.Lock()
        self.release_event = threading.Event()
        self.state: dict[str, Any] = {
            "ready": False,
            "claims": [],
            "issued": [],
            "started": [],
            "inflight": [],
            "finishes": [],
            "released": False,
            "errors": [],
        }
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def save(self) -> None:
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(f".{self.state_file.name}.tmp")
        temporary.write_text(json.dumps(self.state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_file)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def claim(self, kind: str) -> dict[str, Any] | None:
        with self.lock:
            issued = kind in DECKS and kind not in self.state["issued"]
            self.state["claims"].append({"kind": kind, "job": JOBS.get(kind) if issued else None})
            if issued:
                self.state["issued"].append(kind)
            self.save()
        if not issued:
            return None
        return {
            "id": JOBS[kind],
            "sourceKind": kind,
            "source_kind": kind,
            "variant": "baseline",
            "deckId": DECKS[kind],
            "deckVersion": DECK_VERSION,
            "rubricVersion": RUBRIC_VERSION,
            "scope": "deck",
            "attempts": 1,
        }

    def finish(self, params: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.state["finishes"].append(
                {
                    "job": params.get("target_job"),
                    "attempt": params.get("p_attempt"),
                    "status": params.get("p_status"),
                    "result": params.get("p_result"),
                    "error": params.get("p_error"),
                    "terminal": params.get("p_terminal"),
                }
            )
            self.save()
        return {"id": params.get("target_job"), "status": params.get("p_status")}

    def enter_model(self, kind: str) -> None:
        with self.lock:
            self.state["started"].append(kind)
            self.state["inflight"].append(kind)
            self.save()

    def leave_model(self, kind: str) -> None:
        with self.lock:
            if kind in self.state["inflight"]:
                self.state["inflight"].remove(kind)
            self.save()

    def release(self) -> None:
        with self.lock:
            self.state["released"] = True
            self.save()
            self.release_event.set()

    def wait_release(self, timeout: float = 180) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.release_file is not None and self.release_file.exists():
                self.release()
            if self.release_event.wait(0.1):
                return True
        return False

    def start(self) -> int:
        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        server.fixture = self  # type: ignore[attr-defined]
        self.server = server
        self.port = int(server.server_address[1])
        with self.lock:
            self.state["ready"] = True
            self.state["port"] = self.port
            self.save()
        self.thread = threading.Thread(target=server.serve_forever, name="fixture-http", daemon=True)
        self.thread.start()
        return self.port

    def close(self) -> None:
        self.release()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)


class Handler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    @property
    def fixture(self) -> Fixture:
        return self.server.fixture  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeError("Fixture request is not JSON") from exc
        if not isinstance(value, dict):
            raise SmokeError("Fixture request must be JSON object")
        return value

    def _send(self, status: int, value: Any, content_type: str = "application/json") -> None:
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            if method == "GET" and path == "/rest/v1/workspaces":
                self._send(200, [{"id": WORKSPACE, "dedicated_judge_id": JUDGE,
                                  "settings": {"maxScore": 5, "dimensions": [{"id": "visual"}]}}])
            elif method == "GET" and path == "/rest/v1/ai_review_settings":
                self._send(200, [{"vision_weight": 40, "layout_weight": 30, "match_weight": 30}])
            elif method == "GET" and path == "/rest/v1/decks":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                wanted = (query.get("id") or [""])[0].removeprefix("eq.")
                kind = next((name for name, deck in DECKS.items() if deck == wanted), "")
                self._send(200, [] if not kind else [{
                    "id": wanted, "source_kind": kind,
                    "data": {"images": [{"id": f"{kind}-page", "name": "fixture", "position": 1,
                                         "path": f"{WORKSPACE}/{kind}/fixture.png"}]},
                }])
            elif method == "GET" and path.startswith("/storage/v1/object/ppt-library/"):
                self._send(200, PNG, "image/png")
            elif method == "POST" and path.startswith("/rest/v1/rpc/"):
                name = path.rsplit("/", 1)[-1]
                params = self._body()
                if name == "ai_review_claim_jobs":
                    job = self.fixture.claim(str(params.get("p_kind") or ""))
                    self._send(200, [job] if job else [])
                elif name == "ai_review_deck_version":
                    self._send(200, DECK_VERSION)
                elif name == "ai_review_rubric_version":
                    self._send(200, RUBRIC_VERSION)
                elif name == "ai_review_finish_attempt":
                    self._send(200, self.fixture.finish(params))
                else:
                    raise SmokeError(f"Unexpected RPC: {name}")
            elif method == "POST" and path == "/v1/chat/completions":
                request = self._body()
                text = json.dumps(request)
                found = re.search(r"sourceKind.{0,8}(uploaded|generated)", text)
                kind = found.group(1) if found else ""
                if kind not in DECKS:
                    raise SmokeError("Model request did not identify its review lane")
                self.fixture.enter_model(kind)
                try:
                    if not self.fixture.wait_release():
                        raise SmokeError("Fixture release timed out")
                    content = json.dumps(MODEL_RESULT, separators=(",", ":"))
                    self._send(200, {"choices": [{"message": {"content": content}}]})
                finally:
                    self.fixture.leave_model(kind)
            else:
                raise SmokeError(f"Unexpected request: {method} {path}")
        except Exception as exc:
            with self.fixture.lock:
                self.fixture.state["errors"].append(str(exc)[:500])
                self.fixture.save()
            self._send(500, {"message": str(exc)})

def _validate(state: dict[str, Any], claims_at_marker: int) -> dict[str, Any]:
    claims = state.get("claims") or []
    starts = state.get("started") or []
    finishes = state.get("finishes") or []
    if state.get("errors"):
        raise SmokeError(f"Fixture errors: {state['errors']!r}")
    if state.get("released") is not True:
        raise SmokeError("Fixture release was not consumed")
    if claims_at_marker != 2 or len(claims) != claims_at_marker:
        raise SmokeError(f"Expected exactly two pre-release claims, got marker={claims_at_marker}, final={claims!r}")
    if sorted(item.get("kind") for item in claims) != ["generated", "uploaded"]:
        raise SmokeError(f"Claim lanes are not one each: {claims!r}")
    if set(starts) != {"uploaded", "generated"}:
        raise SmokeError(f"Both lanes did not enter model attempts: {starts!r}")
    if len(finishes) != 2:
        raise SmokeError(f"Expected two finish RPCs: {finishes!r}")
    by_job = {job: kind for kind, job in JOBS.items()}
    seen = set()
    for item in finishes:
        kind = by_job.get(item.get("job"))
        if not kind or item.get("job") in seen:
            raise SmokeError(f"Finish RPC target is invalid or duplicated: {item!r}")
        seen.add(item["job"])
        result = item.get("result")
        if (
            item.get("attempt") != 1
            or item.get("status") != "completed"
            or item.get("error") is not None
            or item.get("terminal") is not False
            or not isinstance(result, dict)
            or result.get("sourceKind") != kind
        ):
            raise SmokeError(f"Finish RPC is not an exact completed write-back: {item!r}")
    return {
        "claims": [{"kind": item["kind"], "job": item["job"]} for item in claims],
        "model_started": sorted(set(starts)),
        "claims_at_marker": claims_at_marker,
        "no_claim_after_drain_marker": True,
        "finish_writeback": [
            {"job": item["job"], "sourceKind": item["result"]["sourceKind"], "status": item["status"]}
            for item in finishes
        ],
    }


class MarkerHandler(logging.Handler):
    def __init__(self, event: threading.Event):
        super().__init__()
        self.event = event
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        self.messages.append(message)
        if DRAIN_MARKER in message:
            self.event.set()


def local_check(timeout: float) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import cloud.ai_gateway as gateway

    fixture = Fixture()
    fixture.start()
    marker = threading.Event()
    handler = MarkerHandler(marker)
    previous_level = gateway.LOGGER.level
    gateway.LOGGER.addHandler(handler)
    gateway.LOGGER.setLevel(logging.INFO)
    env = {
        "HOME": tempfile.gettempdir(),
        "GUANYE_SUPABASE_URL": f"http://127.0.0.1:{fixture.port}",
        "GUANYE_SUPABASE_ANON_KEY": "fixture-anon",
        "GUANYE_SUPABASE_SERVICE_ROLE_KEY": "fixture-service",
        "GUANYE_WORKSPACE_ID": WORKSPACE,
        "GUANYE_MODEL_API_KEY": "fixture-model",
        "GUANYE_MODEL_GATEWAY_URL": f"http://127.0.0.1:{fixture.port}/v1",
        "GUANYE_MODEL_MAX_CONCURRENCY": "2",
        "GUANYE_REQUEST_TIMEOUT_SECONDS": "15",
        "GUANYE_MODEL_TIMEOUT_SECONDS": "180",
    }
    old_env = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    errors: list[BaseException] = []
    claims_at_marker: list[int] = []

    def trigger() -> None:
        try:
            _wait("both local lanes in flight", timeout, fixture.snapshot,
                  lambda state: set(state["inflight"]) == {"uploaded", "generated"})
            signal.raise_signal(signal.SIGTERM)
            if not marker.wait(timeout):
                raise SmokeError("Local worker did not consume the drain marker")
            claims_at_marker.append(len(fixture.snapshot()["claims"]))
        except BaseException as exc:
            errors.append(exc)
            try:
                signal.raise_signal(signal.SIGTERM)
            except Exception:
                pass
        finally:
            fixture.release()

    thread = threading.Thread(target=trigger, name="local-sigterm")
    result = 1
    try:
        thread.start()
        result = gateway.main(["--review-worker"])
    finally:
        fixture.release()
        thread.join(timeout=5)
        gateway.LOGGER.removeHandler(handler)
        gateway.LOGGER.setLevel(previous_level)
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        state = fixture.snapshot()
        fixture.close()
    if thread.is_alive() or errors:
        raise SmokeError(f"Local SIGTERM protocol check failed: {errors!r}")
    if result != 0 or not marker.is_set() or not claims_at_marker:
        raise SmokeError(f"Local CLI exit={result}, marker={marker.is_set()}, claims={claims_at_marker}")
    evidence = _validate(state, claims_at_marker[0])
    evidence.update({
        "mode": "local-fixture-check",
        "worker_exit_code": result,
        "boundary": "In-process SIGTERM on real CLI main; protocol only, not Docker/Linux PID 1 evidence.",
    })
    return evidence


def _inspect(docker: str, target: str) -> dict[str, Any]:
    completed = _run([docker, "inspect", target], timeout=30)
    try:
        data = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"docker inspect returned invalid JSON for {target}") from exc
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise SmokeError(f"docker inspect returned no object for {target}")
    return data[0]


def docker_check(args: argparse.Namespace) -> dict[str, Any]:
    host = _host()
    docker, image = args.docker, args.image
    if args.build:
        _run([docker, "build", "--file", "cloud/Dockerfile", "--tag", image, "."],
             cwd=Path(__file__).resolve().parents[1], timeout=1800)
    config = (_inspect(docker, image).get("Config") or {})
    if config.get("User") != USER or config.get("Entrypoint") != ENTRYPOINT or config.get("StopSignal") != "SIGTERM":
        raise SmokeError(f"Unexpected image config: user={config.get('User')!r}, entrypoint={config.get('Entrypoint')!r}, stop={config.get('StopSignal')!r}")

    parent = Path(tempfile.mkdtemp(prefix="guanye-review-drain-"))
    shared = parent / "shared"
    shared.mkdir()
    # mkdtemp is private (0700); expose only this empty child for fixture I/O.
    os.chmod(shared, 0o777)
    state_file, release_file = shared / "state.json", shared / "release"
    name = f"guanye-review-drain-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    created = False
    stop_process: subprocess.Popen[str] | None = None
    try:
        env = {
            "HOME": "/tmp",
            "GUANYE_SUPABASE_URL": f"http://127.0.0.1:{args.fixture_port}",
            "GUANYE_SUPABASE_ANON_KEY": "fixture-anon",
            "GUANYE_SUPABASE_SERVICE_ROLE_KEY": "fixture-service",
            "GUANYE_WORKSPACE_ID": WORKSPACE,
            "GUANYE_MODEL_API_KEY": "fixture-model",
            "GUANYE_MODEL_GATEWAY_URL": f"http://127.0.0.1:{args.fixture_port}/v1",
            "GUANYE_MODEL_MAX_CONCURRENCY": "2",
            "GUANYE_REQUEST_TIMEOUT_SECONDS": "15",
            "GUANYE_MODEL_TIMEOUT_SECONDS": "180",
        }
        command = [
            docker, "run", "--detach", "--name", name, "--init", "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--mount", f"type=bind,source={shared},target=/fixture-state",
            "--mount", f"type=bind,source={Path(__file__).resolve()},target=/fixture-runner.py,readonly",
        ]
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])
        _run(command + [image, "--review-worker"], timeout=60)
        created = True
        started = _inspect(docker, name)
        host_config = started.get("HostConfig") or {}
        if host_config.get("Init") is not True or host_config.get("NetworkMode") != "none":
            raise SmokeError(f"Worker must use Compose-style init and network none: {host_config!r}")

        _run([docker, "exec", "--detach", name, "python", "-B", "/fixture-runner.py",
              "--fixture-server", "--fixture-port", str(args.fixture_port),
              "--state-file", "/fixture-state/state.json",
              "--release-file", "/fixture-state/release"], timeout=30)
        _wait("fixture readiness", args.wait_timeout, lambda: _read_state(state_file),
              lambda state: state.get("ready") is True)
        _wait("both container lanes in flight", args.wait_timeout, lambda: _read_state(state_file),
              lambda state: set(state.get("inflight") or []) == {"uploaded", "generated"})
        stop_process = subprocess.Popen(
            [docker, "stop", "--timeout", str(args.stop_timeout), name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        _wait("post-stop drain marker", args.wait_timeout, lambda: host._container_logs(docker, name),
              lambda logs: DRAIN_MARKER in logs)
        claims_at_marker = len(_read_state(state_file).get("claims") or [])
        release_file.write_text("release\n", encoding="utf-8")
        stop_stdout, stop_stderr = stop_process.communicate(timeout=args.stop_timeout + 30)
        if stop_process.returncode != 0:
            raise SmokeError(f"docker stop failed ({stop_process.returncode}): {stop_stdout}{stop_stderr}")
        final = _wait("two finish RPC write-backs", args.wait_timeout, lambda: _read_state(state_file),
                      lambda state: len(state.get("finishes") or []) == 2 and not (state.get("inflight") or []))
        container_state = _inspect(docker, name).get("State") or {}
        if container_state.get("ExitCode") != 0 or container_state.get("OOMKilled") is True:
            raise SmokeError(f"Unexpected worker container state: {container_state!r}")
        evidence = _validate(final, claims_at_marker)
        evidence.update({
            "mode": "docker-image-smoke",
            "image": image,
            "image_config": {"user": USER, "entrypoint": ENTRYPOINT, "stop_signal": "SIGTERM"},
            "init": True,
            "network": "none",
            "docker_stop_exit_code": stop_process.returncode,
            "worker_exit_code": container_state.get("ExitCode"),
            "drain_marker_seen": True,
            "boundary": "Synthetic loopback REST/RPC, storage, and model only; no real upstream contacted.",
        })
        return evidence
    except Exception:
        try:
            logs = host._container_logs(docker, name)
        except Exception:
            logs = ""
        if logs:
            print(f"worker logs:\n{logs}", file=sys.stderr)
        if state_file.exists():
            try:
                print(f"fixture state:\n{json.dumps(_read_state(state_file), indent=2)}", file=sys.stderr)
            except Exception as exc:
                print(f"fixture state unavailable: {exc}", file=sys.stderr)
        raise
    finally:
        if stop_process is not None and stop_process.poll() is None:
            stop_process.kill()
        if created:
            host._remove_container(docker, name)
        shutil.rmtree(parent, ignore_errors=True)


def fixture_server(args: argparse.Namespace) -> int:
    fixture = Fixture(
        port=args.fixture_port,
        state_file=Path(args.state_file) if args.state_file else None,
        release_file=Path(args.release_file) if args.release_file else None,
    )
    fixture.start()
    try:
        while True:
            time.sleep(3600)
    finally:
        fixture.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--fixture-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--wait-timeout", type=float, default=60)
    parser.add_argument("--stop-timeout", type=int, default=120)
    parser.add_argument("--local-fixture-check", action="store_true", help="run the Windows/local protocol check")
    parser.add_argument("--fixture-server", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-file", help=argparse.SUPPRESS)
    parser.add_argument("--release-file", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.fixture_server:
            return fixture_server(args)
        result = local_check(args.wait_timeout) if args.local_fixture_check else docker_check(args)
        print(json.dumps({"result": "passed", **result}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"drain smoke failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())