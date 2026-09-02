from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from asklear_browser_mcp.supervisor import ConnectorSupervisor, _connector_command

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_exposes_adapter_and_connector_commands() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"] == {
        "asklear-browser-agent": "asklear_browser_mcp.adapter:main",
        "asklear-browser-connector": "asklear_browser_mcp.connector:main",
    }


def test_distribution_declares_websocket_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "websockets>=13" in project["project"]["dependencies"]


def test_readme_uses_the_agent_command_without_the_removed_command() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert '"command": "asklear-browser-agent"' in readme
    assert '"command": "asklear-browser-mcp"' not in readme


def test_connector_module_has_a_real_cli_entrypoint() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "asklear_browser_mcp.connector", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--host" in result.stdout
    assert "--port" in result.stdout


def test_manual_connector_hint_matches_the_standalone_local_contract() -> None:
    supervisor = ConnectorSupervisor(
        connector_origin="http://127.0.0.1:8765",
        process_token="process-token",
    )
    try:
        hint = supervisor._manual_hint("Connector unavailable")
    finally:
        # The hint is synchronous, but the supervisor owns an async client.
        import asyncio

        asyncio.run(supervisor.aclose())

    assert "asklear-browser-connector start --host 127.0.0.1 --port 8765" in hint
    assert "login" not in hint
    assert "API_KEY" not in hint


def test_autostart_uses_the_installed_python_module() -> None:
    assert _connector_command() == [
        sys.executable,
        "-m",
        "asklear_browser_mcp.connector",
    ]


def test_autostart_does_not_select_a_stale_connector_from_path(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/old-venv/bin")

    assert _connector_command() == [
        sys.executable,
        "-m",
        "asklear_browser_mcp.connector",
    ]
