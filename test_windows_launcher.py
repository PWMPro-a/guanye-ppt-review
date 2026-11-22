#!/usr/bin/env python3
"""Windows launcher acceptance tests for launch.ps1 and the .cmd entry point."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REGISTERED_SERVERS: list[tuple[Path, int, int, int | None]] = []
PROJECT = Path(__file__).resolve().parent
LAUNCH = PROJECT / "launch.ps1"
CMD = PROJECT / "启动评分系统.cmd"
SERVER = PROJECT / "server.py"
DEFAULT_PWSH = Path(
    r"C:\Users\ylk\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass


def resolve_pwsh() -> Path:
    candidates = [
        DEFAULT_PWSH,
        Path(os.environ["PWSH_PATH"]) if os.environ.get("PWSH_PATH") else None,
        PROJECT / "pwsh.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PowerShell" / "7" / "pwsh.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    for name in ("pwsh.exe", "pwsh"):
        found = subprocess.run(
            ["where.exe", name] if os.name == "nt" else ["which", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if found.returncode == 0:
            first = found.stdout.splitlines()[0].strip()
            if first and Path(first).is_file():
                return Path(first).resolve()
    raise RuntimeError("PowerShell 7 (pwsh.exe) was not found.")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_pwsh(pwsh: Path, script: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(pwsh), "-NoLogo", "-NoProfile", "-Command", script],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_launcher(pwsh: Path, *arguments: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    command = [
        str(pwsh),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LAUNCH),
        *arguments,
    ]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=PROJECT,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
            stdout_file.seek(0)
            stderr_file.seek(0)
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout_file.read(),
                stderr_file.read(),
            )


def parse_json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = result.stdout.strip()
    if not text:
        raise AssertionError(f"No JSON output. stderr={result.stderr!r}")
    return json.loads(text)


def local_json(url: str, timeout: float = 1.0) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_health(port: int, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            payload = local_json(url, timeout=0.75)
            if payload.get("app") == "ppt-review":
                return payload
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"Health check timed out for {url}: {last_error}")


def query_json(pwsh: Path, script: str) -> list[dict[str, Any]]:
    result = run_pwsh(pwsh, script, timeout=20)
    if result.returncode != 0:
        raise AssertionError(f"PowerShell query failed with {result.returncode}: {result.stderr}")
    text = result.stdout.strip()
    if not text:
        return []
    value = json.loads(text)
    return value if isinstance(value, list) else [value]


def listeners_for_port(pwsh: Path, port: int) -> list[dict[str, Any]]:
    return query_json(
        pwsh,
        "@(Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object LocalAddress,LocalPort,OwningProcess) | ConvertTo-Json -Compress -AsArray" % port,
    )


def process_snapshot(pwsh: Path, pid: int) -> dict[str, Any] | None:
    rows = query_json(
        pwsh,
        "@(Get-CimInstance Win32_Process -Filter 'ProcessId=%d' | "
        "Select-Object ProcessId,ExecutablePath,CommandLine,CreationDate) | ConvertTo-Json -Compress -AsArray" % pid,
    )
    return rows[0] if rows else None


def processes_for_port(pwsh: Path, port: int) -> list[dict[str, Any]]:
    escaped = str(SERVER).replace("'", "''")
    return query_json(
        pwsh,
        "@(Get-CimInstance Win32_Process | Where-Object { "
        f"$_.CommandLine -and $_.CommandLine.Contains({ps_quote(escaped)}) -and "
        f"$_.CommandLine -match {ps_quote('--port\\s+' + str(port) + '(?:\\s|$)')} "
        "} | Select-Object ProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress -AsArray",
    )


def stop_verified_server(pwsh: Path, pid: int, port: int, interpreter_pid: int | None = None) -> None:
    snapshot = process_snapshot(pwsh, pid)
    if snapshot:
        command_line = str(snapshot.get("CommandLine") or "")
        if str(SERVER) not in command_line or f"--port {port}" not in command_line:
            raise AssertionError(f"Refusing to stop unverified PID {pid}: {command_line}")
        run_pwsh(pwsh, f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")
    if interpreter_pid and interpreter_pid != pid:
        parent = process_snapshot(pwsh, interpreter_pid)
        if parent:
            parent_line = str(parent.get("CommandLine") or "")
            if str(SERVER) in parent_line and f"--port {port}" in parent_line:
                run_pwsh(pwsh, f"Stop-Process -Id {interpreter_pid} -Force -ErrorAction SilentlyContinue")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        parent_alive = bool(interpreter_pid and process_snapshot(pwsh, interpreter_pid))
        listener_alive = bool(snapshot and process_snapshot(pwsh, pid))
        if not listeners_for_port(pwsh, port) and not listener_alive and not parent_alive:
            return
        time.sleep(0.2)
    raise AssertionError(f"Verified test server PID {pid} did not stop.")


def cleanup_registered_servers() -> None:
    if not REGISTERED_SERVERS:
        return
    try:
        pwsh = resolve_pwsh()
    except Exception:
        return
    for _, pid, port, interpreter_pid in list(REGISTERED_SERVERS):
        try:
            stop_verified_server(pwsh, pid, port, interpreter_pid)
        except Exception:
            pass
    REGISTERED_SERVERS.clear()


atexit.register(cleanup_registered_servers)


class OtherAppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/api/health":
            body = json.dumps({"app": "other-app", "version": 1}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_other_app(port: int) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", port), OtherAppHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="other-app", daemon=True)
    thread.start()
    return server, thread


def write_probe_sitecustomize(directory: Path) -> None:
    source = '''import atexit
import hashlib
import json
import os
from pathlib import Path
output = os.environ.get("GUANYE_LAUNCH_TEST_PROBE")
if output:
    names = [
        "GUANYE_LOCAL_PRO_ENABLED",
        "GUANYE_INSTANCE_CONFIG",
        "GUANYE_LAUNCH_TEST_VALUE",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
    ]
    Path(output).write_text(
        json.dumps({name: os.environ.get(name) for name in names}, ensure_ascii=False),
        encoding="utf-8",
    )
'''
    (directory / "sitecustomize.py").write_text(source, encoding="utf-8", newline="\n")


def write_professional_config(path: Path, workspace_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "url": "https://fixture.supabase.co",
                "anonKey": "sb_publishable_fixturetesting",
                "workspaceId": workspace_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

def cleanup_test_servers_for_root(pwsh: Path, root: Path) -> None:
    marker = root.name
    rows = query_json(
        pwsh,
        "@(Get-CimInstance Win32_Process | Where-Object { "
        f"$_.CommandLine -and $_.CommandLine.Contains({ps_quote(str(SERVER))}) -and "
        f"$_.CommandLine.Contains({ps_quote(marker)}) "
        "} | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress -AsArray",
    )
    for row in rows:
        process_id = int(row["ProcessId"])
        command_line = str(row.get("CommandLine") or "")
        if str(SERVER) not in command_line or marker not in command_line:
            continue
        run_pwsh(pwsh, f"Stop-Process -Id {process_id} -Force -ErrorAction SilentlyContinue")

def main() -> int:
    configure_stdio()
    pwsh = resolve_pwsh()
    version = run_pwsh(pwsh, "$PSVersionTable.PSVersion.ToString()")
    if version.returncode != 0:
        raise AssertionError(f"PowerShell version probe failed: {version.stderr}")
    ps_version = version.stdout.strip()
    if int(ps_version.split(".", 1)[0]) < 7:
        raise AssertionError(f"PowerShell 7 required, found {ps_version}")

    print(f"INFO pwsh={pwsh}")
    print(f"INFO powershell_version={ps_version}")
    print(f"INFO launch={LAUNCH}")
    print(f"INFO cmd={CMD}")

    syntax_script = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile({ps_quote(str(LAUNCH))},"
        "[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }; exit 0"
    )
    syntax = run_pwsh(pwsh, syntax_script)
    if syntax.returncode != 0:
        raise AssertionError(f"Syntax check failed: {syntax.stderr}")
    print(f"PASS syntax exit={syntax.returncode} file={LAUNCH}")

    with tempfile.TemporaryDirectory(prefix="guanye-launcher-test-") as temp_name:
        try:
            temp = Path(temp_name)
            source = temp / "源 图 库"
            source.mkdir()
            state = temp / "状态 state"
            cmd_state = temp / "cmd state"
            probe_dir = temp / "探针 probe"
            probe_dir.mkdir()
            probe_output = temp / "probe 输出.json"
            write_probe_sitecustomize(probe_dir)
            config = temp / "独立 实例.json"
            config.write_text(
                json.dumps(
                    {
                        "localPro": True,
                        "environment": {
                            "GUANYE_LAUNCH_TEST_VALUE": "fixture-value",
                            "GUANYE_LAUNCH_TEST_PROBE": str(probe_output),
                            "PYTHONPATH": str(probe_dir),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            professional_config = temp / "专业 配置.json"
            professional_config_alt = temp / "专业 配置 二.json"
            write_professional_config(professional_config, "11111111-1111-4111-8111-111111111111")
            write_professional_config(professional_config_alt, "22222222-2222-4222-8222-222222222222")
            port = free_port()
            if port == 8765:
                professional_config = temp / "专业 配置.json"
            professional_config_alt = temp / "专业 配置 二.json"
            write_professional_config(professional_config, "11111111-1111-4111-8111-111111111111")
            write_professional_config(professional_config_alt, "22222222-2222-4222-8222-222222222222")
            port = free_port()
            print(f"INFO isolated_port={port}")
            print(f"INFO isolated_state={state}")

            check_result = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Check",
                "-Port",
                str(port),
                "-StateDirectory",
                str(state),
                "-Source",
                str(source),
                "-InstanceConfig",
                str(config),
            )
            if check_result.returncode != 0:
                raise AssertionError(f"Check mode failed: {check_result.stderr}")
            plan = parse_json_stdout(check_result)
            assert plan["bindAddress"] == "127.0.0.1"
            assert plan["port"] == port
            assert plan["localProRequested"] is True
            assert plan["professionalWorkerStatus"] == "not-verified"
            assert "GUANYE_LOCAL_PRO_ENABLED" in plan["environmentNames"]
            assert "GUANYE_INSTANCE_CONFIG" in plan["environmentNames"]
            print(f"PASS check_mode exit={check_result.returncode} python={plan['pythonPath']} version={plan['pythonVersion']}")
            pro_check = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Check",
                "-Port",
                str(port),
                "-StateDirectory",
                str(temp / "专业检查 state"),
                "-Source",
                str(source),
                "-ProfessionalConfig",
                str(professional_config),
            )
            if pro_check.returncode != 0:
                raise AssertionError(f"Professional check failed: {pro_check.stderr}")
            pro_plan = parse_json_stdout(pro_check)
            expected_pro_hash = hashlib.sha256(professional_config.read_bytes()).hexdigest()
            assert pro_plan["mode"] == "single-judge"
            assert pro_plan["professionalConfig"] == str(professional_config.resolve())
            assert pro_plan["professionalConfigSha256"] == expected_pro_hash
            print(
                f"PASS professional_check exit={pro_check.returncode} "
                f"mode={pro_plan['mode']} config={pro_plan['professionalConfig']}"
            )

            cmd_line = (
                f'call "{CMD}" -NoBrowser -Check -Port {port} '
                f'-StateDirectory "{cmd_state}" -Source "{source}" -InstanceConfig "{config}" -ProfessionalConfig "{professional_config}"'
            )
            cmd_result = subprocess.run(
                "cmd.exe /d /c " + cmd_line,
                cwd=PROJECT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
            if cmd_result.returncode != 0:
                raise AssertionError(f"CMD check failed: stdout={cmd_result.stdout!r} stderr={cmd_result.stderr!r}")
            cmd_plan = parse_json_stdout(cmd_result)
            assert cmd_plan["port"] == port
            assert cmd_plan["mode"] == "single-judge"
            print(f"PASS cmd_entry exit={cmd_result.returncode} file={CMD} mode={cmd_plan['mode']}")

            first = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(port),
                "-StateDirectory",
                str(state),
                "-Source",
                str(source),
                "-InstanceConfig",
                str(config),
            )
            if first.returncode != 0:
                raise AssertionError(f"First start failed: stdout={first.stdout!r} stderr={first.stderr!r}")
            health = wait_health(port)
            assert "mode" not in health or health.get("mode") in (None, "legacy")
            pid_file = state / "server.pid"
            deadline = time.monotonic() + 5
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert pid_file.is_file(), "server.pid was not written"
            first_pid = int(pid_file.read_text(encoding="utf-8").strip())
            REGISTERED_SERVERS.append((pwsh, first_pid, port, None))

            listeners = listeners_for_port(pwsh, port)
            assert listeners, "No listener found"
            assert {str(row["LocalAddress"]) for row in listeners} == {"127.0.0.1"}
            assert {int(row["OwningProcess"]) for row in listeners} == {first_pid}
            snapshot = process_snapshot(pwsh, first_pid)
            assert snapshot is not None
            assert str(SERVER) in str(snapshot.get("CommandLine") or "")
            assert f"--port {port}" in str(snapshot.get("CommandLine") or "")

            deadline = time.monotonic() + 5
            while not probe_output.is_file() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert probe_output.is_file(), "Environment probe did not run"
            probe = json.loads(probe_output.read_text(encoding="utf-8"))
            assert probe["GUANYE_LOCAL_PRO_ENABLED"] == "1"
            assert probe["GUANYE_INSTANCE_CONFIG"] == str(config.resolve())
            assert probe["GUANYE_LAUNCH_TEST_VALUE"] == "fixture-value"
            assert probe["PYTHONUTF8"] == "1"
            assert str(probe["PYTHONIOENCODING"]).lower() == "utf-8"

            state_data = json.loads((state / "launcher-state.json").read_text(encoding="utf-8"))
            assert state_data["processId"] == first_pid
            assert state_data["professionalWorkerStatus"] == "not-verified"
            assert state_data["mode"] == "legacy"
            assert state_data["serverPythonPath"], state_data
            assert "GUANYE_LAUNCH_TEST_PROBE" in state_data["environmentNames"]
            state_raw = (state / "launcher-state.json").read_text(encoding="utf-8")
            assert "fixture-value" not in state_raw, "Launcher state must not persist environment values"
            print(f"PASS first_start exit={first.returncode} pid={first_pid} health={health['app']}")
            REGISTERED_SERVERS[-1] = (pwsh, first_pid, port, state_data.get("interpreterLauncherPid"))
            print(f"PASS environment_passthrough worker_status=not-verified state={state / 'launcher-state.json'}")

            second = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(port),
                "-StateDirectory",
                str(state),
                "-Source",
                str(source),
                "-InstanceConfig",
                str(config),
            )
            if second.returncode != 0:
                raise AssertionError(f"Second start failed: stdout={second.stdout!r} stderr={second.stderr!r}")
            second_pid = int(pid_file.read_text(encoding="utf-8").strip())
            assert second_pid == first_pid, (first_pid, second_pid)
            second_listeners = listeners_for_port(pwsh, port)
            assert len(second_listeners) == 1, second_listeners
            assert {int(row["OwningProcess"]) for row in second_listeners} == {first_pid}
            assert "reused" in second.stdout.lower()
            print(f"PASS second_start_reuse exit={second.returncode} pid={second_pid} listeners={len(second_listeners)}")
            legacy_to_pro = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(port),
                "-StateDirectory",
                str(temp / "跨模式 state"),
                "-Source",
                str(source),
                "-ProfessionalConfig",
                str(professional_config),
            )
            assert legacy_to_pro.returncode != 0, "Legacy server must not be reused as professional mode"
            assert "different mode" in legacy_to_pro.stderr.lower()
            assert wait_health(port).get("app") == "ppt-review"
            print(
                f"PASS legacy_to_pro_rejected exit={legacy_to_pro.returncode} "
                f"port={port} legacy_server_survived=yes"
            )

            pro_port = free_port()
            while pro_port in (8765, port):
                pro_port = free_port()
            pro_state = temp / "专业状态 state"
            pro_first = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(pro_port),
                "-StateDirectory",
                str(pro_state),
                "-Source",
                str(source),
                "-ProfessionalConfig",
                str(professional_config),
            )
            if pro_first.returncode != 0:
                raise AssertionError(f"Professional start failed: stdout={pro_first.stdout!r} stderr={pro_first.stderr!r}")
            pro_health = wait_health(pro_port)
            assert pro_health.get("mode") == "single-judge", pro_health
            pro_pid_file = pro_state / "server.pid"
            assert pro_pid_file.is_file(), "Professional server.pid was not written"
            pro_pid = int(pro_pid_file.read_text(encoding="utf-8").strip())
            REGISTERED_SERVERS.append((pwsh, pro_pid, pro_port, None))
            assert int(pro_health["pid"]) == pro_pid, (pro_health, pro_pid)
            pro_listeners = listeners_for_port(pwsh, pro_port)
            assert pro_listeners and {int(row["OwningProcess"]) for row in pro_listeners} == {pro_pid}
            pro_snapshot = process_snapshot(pwsh, pro_pid)
            assert pro_snapshot is not None
            pro_command = str(pro_snapshot.get("CommandLine") or "")
            assert "--professional-config" in pro_command
            assert str(professional_config.resolve()) in pro_command
            pro_state_data = json.loads((pro_state / "launcher-state.json").read_text(encoding="utf-8"))
            assert pro_state_data["mode"] == "single-judge"
            assert pro_state_data["professionalConfig"] == str(professional_config.resolve())
            assert pro_state_data["professionalConfigSha256"] == expected_pro_hash
            pro_state_raw = (pro_state / "launcher-state.json").read_text(encoding="utf-8")
            assert "sb_publishable_fixturetesting" not in pro_state_raw
            REGISTERED_SERVERS[-1] = (pwsh, pro_pid, pro_port, pro_state_data.get("interpreterLauncherPid"))
            print(
                f"PASS professional_start exit={pro_first.returncode} pid={pro_pid} "
                f"mode={pro_health['mode']} health_pid={pro_health['pid']}"
            )

            pro_second = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(pro_port),
                "-StateDirectory",
                str(pro_state),
                "-Source",
                str(source),
                "-ProfessionalConfig",
                str(professional_config),
            )
            assert pro_second.returncode == 0, pro_second.stderr
            assert int(pro_pid_file.read_text(encoding="utf-8").strip()) == pro_pid
            assert "reused" in pro_second.stdout.lower()
            print(f"PASS professional_reuse exit={pro_second.returncode} pid={pro_pid}")

            pro_alt = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(pro_port),
                "-StateDirectory",
                str(pro_state),
                "-Source",
                str(source),
                "-ProfessionalConfig",
                str(professional_config_alt),
            )
            assert pro_alt.returncode != 0, "Different professional config path must not be reused"
            assert "professional config" in pro_alt.stderr.lower()
            assert wait_health(pro_port).get("mode") == "single-judge"
            print(f"PASS professional_config_path_mismatch exit={pro_alt.returncode} port={pro_port}")

            write_professional_config(professional_config, "22222222-2222-4222-8222-222222222222")
            pro_changed = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(pro_port),
                "-StateDirectory",
                str(pro_state),
                "-Source",
                str(source),
                "-ProfessionalConfig",
                str(professional_config),
            )
            assert pro_changed.returncode != 0, "Changed professional config content must not be reused"
            assert "fingerprint" in pro_changed.stderr.lower()
            assert wait_health(pro_port).get("mode") == "single-judge"
            print(f"PASS professional_config_fingerprint_mismatch exit={pro_changed.returncode} port={pro_port}")

            pro_to_legacy = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(pro_port),
                "-StateDirectory",
                str(temp / "专业转旧 state"),
                "-Source",
                str(source),
            )
            assert pro_to_legacy.returncode != 0, "Professional server must not be reused as legacy mode"
            assert "different mode" in pro_to_legacy.stderr.lower()
            pro_health_after = wait_health(pro_port)
            assert pro_health_after.get("mode") == "single-judge"
            assert int(pro_health_after["pid"]) == pro_pid
            print(
                f"PASS pro_to_legacy_rejected exit={pro_to_legacy.returncode} "
                f"port={pro_port} professional_server_survived=yes"
            )

            stop_verified_server(pwsh, pro_pid, pro_port, pro_state_data.get("interpreterLauncherPid"))
            print(f"PASS professional_cleanup pid={pro_pid} port={pro_port}")

            other_port = free_port()
            if other_port in (8765, port):
                other_port = free_port()
            other_server, other_thread = start_other_app(other_port)
            conflict_state = temp / "conflict-state"
            try:
                assert local_json(f"http://127.0.0.1:{other_port}/api/health")["app"] == "other-app"
                conflict = run_launcher(
                    pwsh,
                    "-NoBrowser",
                    "-Port",
                    str(other_port),
                    "-StateDirectory",
                    str(conflict_state),
                    "-Source",
                    str(source),
                )
                assert conflict.returncode != 0, "Port conflict should fail"
                assert "occupied by another application" in conflict.stderr.lower()
                assert other_thread.is_alive(), "Conflict test server was terminated"
                assert local_json(f"http://127.0.0.1:{other_port}/api/health")["app"] == "other-app"
                other_listeners = listeners_for_port(pwsh, other_port)
                assert other_listeners and {int(row["OwningProcess"]) for row in other_listeners} == {os.getpid()}
                assert not (conflict_state / "server.pid").exists()
                print(
                    f"PASS foreign_port_conflict exit={conflict.returncode} "
                    f"port={other_port} owner_pid={os.getpid()} survived=yes"
                )
            finally:
                other_server.shutdown()
                other_server.server_close()
                other_thread.join(timeout=5)

            failure_port = free_port()
            if failure_port in (8765, port, other_port):
                failure_port = free_port()
            failure_state = temp / "失败 state"
            failure_config = temp / "失败 实例.json"
            failure_config.write_text(
                json.dumps(
                    {"environment": {"PYTHONHOME": str(temp / "missing-python-home")}},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            failed_start = run_launcher(
                pwsh,
                "-NoBrowser",
                "-Port",
                str(failure_port),
                "-StateDirectory",
                str(failure_state),
                "-Source",
                str(source),
                "-InstanceConfig",
                str(failure_config),
            )
            assert failed_start.returncode != 0, "Invalid interpreter environment should fail"
            assert not (failure_state / "server.pid").exists(), "Failed launch left a PID file"
            assert not listeners_for_port(pwsh, failure_port), "Failed launch left a listener"
            assert wait_health(port)["app"] == "ppt-review", "Failure cleanup affected the existing test server"
            print(
                f"PASS failed_start_cleanup exit={failed_start.returncode} "
                f"failed_port={failure_port} existing_server_survived=yes"
            )
            stop_verified_server(pwsh, first_pid, port, state_data.get("interpreterLauncherPid"))
            print(f"PASS cleanup pid={first_pid} port={port}")
        finally:
            cleanup_test_servers_for_root(pwsh, temp)

    print("ALL_WINDOWS_LAUNCHER_TESTS_PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
