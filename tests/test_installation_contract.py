from __future__ import annotations

import subprocess
import sys
import tomllib
import json
import zipfile
from pathlib import Path

import asklear_browser_mcp
from asklear_browser_mcp.supervisor import ConnectorSupervisor, _connector_command

ROOT = Path(__file__).resolve().parents[1]


def test_distribution_exposes_adapter_and_connector_commands() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"] == {
        "asklear-browser-agent": "asklear_browser_mcp.adapter:main",
        "asklear-browser-connector": "asklear_browser_mcp.connector:main",
    }


def test_distribution_version_matches_the_mcp_server_info() -> None:
    import asyncio

    from asklear_browser_mcp.adapter import BrowserAgentAdapter

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    async def initialize():
        adapter = BrowserAgentAdapter(connector_origin="http://127.0.0.1:8765", process_token="test")
        try:
            return await adapter.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        finally:
            await adapter.close()

    response = asyncio.run(initialize())
    assert project["project"]["version"] == asklear_browser_mcp.__version__
    assert response["result"]["serverInfo"]["version"] == asklear_browser_mcp.__version__


def test_readme_links_the_install_guide_and_explains_local_connection() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://docs.asklear.cn/browser" in readme
    assert "bound to your account" not in readme
    assert "uv tool upgrade asklear-browser-mcp" in readme
    assert "asklear-browser-connector restart" in readme
    assert "asklear-browser-connector pair" in readme
    assert readme.index("asklear-browser-connector start") < readme.index("asklear-browser-connector pair")
    assert "wait until the local service is connected" in " ".join(readme.split())


def test_bundled_extensions_support_the_connector_protocol() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "downloads/asklear-browser-extraction-global-test.zip" not in readme
    assert not (ROOT / "downloads/asklear-browser-extraction-global-test.zip").exists()
    for environment in ("test", "production", "global-production"):
        relative = f"downloads/asklear-browser-extraction-{environment}.zip"
        assert f"]({relative})" in readme
        with zipfile.ZipFile(ROOT / relative) as package:
            manifest = json.loads(package.read("manifest.json"))
            config = json.loads(package.read("config.json"))
            worker = package.read("service-worker.js")
        assert manifest["version"] == "0.3.3"
        assert config["environment"] == environment
        assert b"secure_hello" in worker
        assert b"close(1002)" not in worker


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
