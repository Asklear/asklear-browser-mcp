from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from asklear_browser_mcp import supervisor as supervisor_module
from asklear_browser_mcp.supervisor import (
    ConnectorSupervisor,
    start_connector,
    stop_connector,
    write_connector_pid,
)

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_connector(state_dir: Path, port: int, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "asklear_browser_mcp.connector",
            *args,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--state-dir",
            str(state_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for_exit(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} did not exit")


def test_start_keeps_connector_alive_after_cli_exits(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"

    started = _run_connector(state_dir, port, "start")
    assert started.returncode == 0, started.stderr

    pid_path = state_dir / "connector.pid"
    token_path = state_dir / f"process-token-127.0.0.1-{port}"
    assert pid_path.exists()
    assert token_path.exists()
    pid = int(pid_path.read_text(encoding="utf-8"))
    assert pid != os.getpid()
    os.kill(pid, 0)

    status = _run_connector(state_dir, port, "status")
    assert status.returncode == 0, status.stderr
    body = json.loads(status.stdout)
    assert body["running"] is True
    assert body["pid"] == pid

    stopped = _run_connector(state_dir, port, "stop")
    assert stopped.returncode == 0, stopped.stderr
    _wait_for_exit(pid)


def test_start_reuses_the_existing_connector(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"

    first = _run_connector(state_dir, port, "start")
    assert first.returncode == 0, first.stderr
    pid = int((state_dir / "connector.pid").read_text(encoding="utf-8"))
    try:
        second = _run_connector(state_dir, port, "start")
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout)["started"] is False
        assert int((state_dir / "connector.pid").read_text(encoding="utf-8")) == pid
    finally:
        _run_connector(state_dir, port, "stop")
        _wait_for_exit(pid)


@pytest.mark.skipif(os.name == "nt", reason="ps command differs on Windows")
def test_connector_process_token_is_not_in_the_run_command_line(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"

    started = _run_connector(state_dir, port, "start")
    assert started.returncode == 0, started.stderr
    pid = int((state_dir / "connector.pid").read_text(encoding="utf-8"))
    token = (state_dir / f"process-token-127.0.0.1-{port}").read_text(encoding="utf-8").strip()
    try:
        command_line = subprocess.check_output(
            ["ps", "-o", "command=", "-p", str(pid)], text=True
        )
        assert token not in command_line
    finally:
        _run_connector(state_dir, port, "stop")
        _wait_for_exit(pid)


def test_adapter_shutdown_does_not_stop_the_shared_connector(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"

    started = _run_connector(state_dir, port, "start")
    assert started.returncode == 0, started.stderr
    pid = int((state_dir / "connector.pid").read_text(encoding="utf-8"))
    try:
        async def close_adapter_supervisor() -> None:
            supervisor = ConnectorSupervisor(
                connector_origin=f"http://127.0.0.1:{port}",
                process_token="not-the-published-token",
                autostart=False,
                token_root=state_dir,
            )
            await supervisor.ensure_running()
            await supervisor.aclose()

        import asyncio

        asyncio.run(close_adapter_supervisor())
        os.kill(pid, 0)
    finally:
        _run_connector(state_dir, port, "stop")
        _wait_for_exit(pid)


def test_supervisor_autostart_uses_the_shared_daemon(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"

    async def ensure_connector() -> None:
        supervisor = ConnectorSupervisor(
            connector_origin=f"http://127.0.0.1:{port}",
            process_token="adapter-bootstrap-token",
            token_root=state_dir,
        )
        health = await supervisor.ensure_running()
        assert health["status"] == "ok"
        await supervisor.aclose()

    import asyncio

    asyncio.run(ensure_connector())
    pid = int((state_dir / "connector.pid").read_text(encoding="utf-8"))
    try:
        os.kill(pid, 0)
    finally:
        _run_connector(state_dir, port, "stop")
        _wait_for_exit(pid)


def test_stop_does_not_kill_a_reused_pid_without_connector_health(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        write_connector_pid(unrelated.pid, root=state_dir)
        assert _run_connector(state_dir, port, "stop").returncode == 0
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_stop_does_not_kill_a_pid_for_non_connector_health(tmp_path: Path) -> None:
    state_dir = tmp_path / "connector-state"
    class NonConnectorHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), NonConnectorHealthHandler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        write_connector_pid(unrelated.pid, root=state_dir)
        assert (
            stop_connector(
                host="127.0.0.1", port=server.server_port, root=state_dir
            )
            is False
        )
        assert unrelated.poll() is None
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_concurrent_starts_share_one_connector(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _value: _run_connector(state_dir, port, "start"),
                (1, 2),
            )
        )

    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    started_flags = [json.loads(result.stdout)["started"] for result in results]
    managed_pid = int((state_dir / "connector.pid").read_text(encoding="utf-8"))
    try:
        assert sum(started_flags) == 1
        assert managed_pid > 0
    finally:
        _run_connector(state_dir, port, "stop")
        _wait_for_exit(managed_pid)


def test_start_does_not_pass_api_credentials_to_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("ASKLEAR_API_KEY", "api-key-must-not-be-inherited")
    monkeypatch.setenv(
        "ASKLEAR_BROWSER_API_KEY", "legacy-api-key-must-not-be-inherited"
    )
    monkeypatch.setenv(
        "ASKLEAR_BROWSER_CONNECTOR_TOKEN", "connector-token-must-not-be-inherited"
    )
    monkeypatch.setattr(
        supervisor_module, "_connector_health", lambda *_args, **_kwargs: None
    )

    def fake_detached_popen(command, *, log_path, env):
        captured["command"] = command
        captured["env"] = env
        return SimpleNamespace(pid=999999)

    monkeypatch.setattr(supervisor_module, "_detached_popen", fake_detached_popen)
    monkeypatch.setattr(
        supervisor_module,
        "_wait_for_health",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    health, started = start_connector(
        host="127.0.0.1",
        port=_free_port(),
        api_origin="https://api.asklear.cn",
        root=tmp_path / "connector-state",
        command=("fake-connector",),
        startup_timeout=0.2,
    )

    assert health == {"status": "ok"}
    assert started is True
    child_environment = captured["env"]
    assert isinstance(child_environment, dict)
    assert all(
        name not in child_environment
        for name in (
            "ASKLEAR_API_KEY",
            "ASKLEAR_BROWSER_API_KEY",
            "ASKLEAR_BROWSER_CONNECTOR_TOKEN",
        )
    )
