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

import httpx
import pytest

from asklear_browser_mcp import supervisor as supervisor_module
from asklear_browser_mcp.supervisor import (
    ConnectorSupervisor,
    connector_start_lock_path,
    process_token_path,
    start_connector,
    status_connector,
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


def test_stop_does_not_kill_pid_when_health_belongs_to_another_process(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "connector-state"
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    reported_pid = unrelated.pid + 1

    class ForeignHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = json.dumps(
                {
                    "status": "ok",
                    "pid": reported_pid,
                    "extension_connected": False,
                    "instance_id": None,
                    "capabilities": {
                        "browser": {
                            "execution": "local",
                            "operations": [],
                            "max_active_sessions": 4,
                        }
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), ForeignHealthHandler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
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


def test_status_does_not_claim_a_managed_connector_when_health_pid_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "connector-state"
    managed_pid = 12345
    write_connector_pid(managed_pid, root=state_dir)
    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda pid: pid == managed_pid)
    monkeypatch.setattr(
        supervisor_module,
        "_connector_health",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "pid": 67890,
            "capabilities": {"browser": {"execution": "local"}},
        },
    )

    status = status_connector(host="127.0.0.1", port=8765, root=state_dir)

    assert status["running"] is False


def test_wait_for_health_rejects_health_from_another_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        supervisor_module,
        "_connector_health",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "pid": 67890,
            "capabilities": {"browser": {"execution": "local"}},
        },
    )
    monkeypatch.setattr(supervisor_module, "_pid_is_running", lambda pid: False)

    health = supervisor_module._wait_for_health(
        12345, "127.0.0.1", 8765, timeout=0.2
    )

    assert health is None


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
    monkeypatch.setenv("AUTH_HEADER", "bearer-must-not-be-inherited")
    monkeypatch.setenv("TAVILY_API_KEY", "search-key-must-not-be-inherited")
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
            "AUTH_HEADER",
            "TAVILY_API_KEY",
        )
    )


def test_async_probe_rejects_non_connector_health() -> None:
    class FakeClient:
        async def get(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

    supervisor = ConnectorSupervisor(
        connector_origin="http://127.0.0.1:8765",
        process_token="process-token",
        client=FakeClient(),  # type: ignore[arg-type]
    )

    import asyncio

    assert asyncio.run(supervisor.probe()) is None


def test_token_probe_rejects_unexpected_http_response() -> None:
    class FakeClient:
        async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

    supervisor = ConnectorSupervisor(
        connector_origin="http://127.0.0.1:8765",
        process_token="process-token",
        client=FakeClient(),  # type: ignore[arg-type]
    )

    import asyncio

    assert asyncio.run(supervisor._token_accepted()) is False


def test_process_token_path_canonicalizes_loopback_aliases(tmp_path: Path) -> None:
    assert process_token_path("127.0.0.1", 8765, root=tmp_path) == process_token_path(
        "localhost", 8765, root=tmp_path
    )


def test_run_refuses_to_replace_existing_connector_state(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"

    started = _run_connector(state_dir, port, "start")
    assert started.returncode == 0, started.stderr
    pid_path = state_dir / "connector.pid"
    token_path = process_token_path("127.0.0.1", port, root=state_dir)
    pid = int(pid_path.read_text(encoding="utf-8"))
    token = token_path.read_text(encoding="utf-8")
    try:
        duplicate = _run_connector(state_dir, port, "run")
        assert duplicate.returncode != 0
        assert int(pid_path.read_text(encoding="utf-8")) == pid
        assert token_path.read_text(encoding="utf-8") == token
        os.kill(pid, 0)
    finally:
        _run_connector(state_dir, port, "stop")
        _wait_for_exit(pid)


def test_stale_start_lock_is_reclaimed(tmp_path: Path) -> None:
    lock_path = connector_start_lock_path(root=tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not-a-pid", encoding="utf-8")
    stale_time = time.time() - 10
    os.utime(lock_path, (stale_time, stale_time))

    acquired = supervisor_module._acquire_start_lock(tmp_path, timeout=0.2)
    try:
        assert acquired == lock_path
    finally:
        supervisor_module._release_start_lock(acquired)


def test_stop_waits_for_an_in_progress_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "connector-state"
    state_dir.mkdir()
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lock_path = supervisor_module._acquire_start_lock(state_dir, timeout=1)
    health_called = threading.Event()
    terminate_called = threading.Event()
    monkeypatch.setattr(
        supervisor_module,
        "_connector_health",
        lambda *_args, **_kwargs: (
            health_called.set()
            or {
                "status": "ok",
                "pid": unrelated.pid,
                "capabilities": {"browser": {"execution": "local"}},
            }
        ),
    )
    monkeypatch.setattr(
        supervisor_module,
        "_terminate_pid",
        lambda *_args, **_kwargs: terminate_called.set(),
    )
    write_connector_pid(unrelated.pid, root=state_dir)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(
                stop_connector,
                host="127.0.0.1",
                port=8765,
                root=state_dir,
            )
            assert not health_called.wait(timeout=0.2)
            assert not result.done()
            supervisor_module._release_start_lock(lock_path)
            assert result.result(timeout=2) is True
            assert terminate_called.is_set()
    finally:
        supervisor_module._release_start_lock(lock_path)
        if unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=5)
