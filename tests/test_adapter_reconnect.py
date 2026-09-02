from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import httpx
import websockets

from asklear_browser_mcp.adapter import BrowserAgentAdapter
from asklear_browser_mcp.connector import ASKLEAR_EXTENSION_ID
from asklear_browser_mcp.supervisor import (
    ConnectorSupervisor,
    ConnectorUnavailable,
    read_published_process_token,
    restart_connector,
    start_connector,
    stop_connector,
)


class RecordingSupervisor:
    def __init__(self) -> None:
        self.calls = 0
        self.process_token = "old-token"

    async def ensure_running(self) -> None:
        self.calls += 1
        if self.calls == 2:
            self.process_token = "new-token"


class FailingReconnectSupervisor(RecordingSupervisor):
    async def ensure_running(self) -> None:
        await super().ensure_running()
        if self.calls == 2:
            raise ConnectorUnavailable("reconnect failed")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _connect_extension(port: int, instance_id: str):
    websocket = await websockets.connect(
        f"ws://127.0.0.1:{port}/v1/browser/ws",
        origin=f"chrome-extension://{ASKLEAR_EXTENSION_ID}",
    )
    await websocket.send(
        json.dumps(
            {
                "type": "authenticate",
                "protocol_version": 1,
                "instance_id": instance_id,
                "instance_token": "extension-test-token-1234",
            }
        )
    )
    authenticated = json.loads(await websocket.recv())
    assert authenticated["type"] == "authenticated"
    return websocket


async def _respond_to_navigate(websocket) -> dict[str, Any]:
    command = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
    assert command["type"] == "command"
    assert command["operation"] == "navigate"
    await websocket.send(
        json.dumps(
            {
                "type": "response",
                "request_id": command["request_id"],
                "session_id": command["session_id"],
                "ok": True,
                "result": {
                    "source": {"origin": "https://example.com", "path": "/"}
                },
            }
        )
    )
    return command


def test_agent_rehandshakes_and_retries_once_after_connector_token_rotation() -> None:
    requests: list[tuple[str | None, dict[str, Any]]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.headers.get("X-Asklear-Connector-Token"), payload))
        if len(requests) == 1:
            return httpx.Response(
                401,
                json={"error": {"code": "unauthorized"}},
                request=request,
            )
        return httpx.Response(
            200,
            json={"operation": "navigate", "session_id": "session_1"},
            request=request,
        )

    supervisor = RecordingSupervisor()

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = BrowserAgentAdapter(
                connector_origin="http://127.0.0.1:8765",
                process_token="old-token",
                client=client,
                supervisor=supervisor,  # type: ignore[arg-type]
            )
            return await adapter._call(
                "rpc-1",
                "navigate",
                {"request_id": "req_agent_test", "url": "https://example.com"},
            )

    response = asyncio.run(run())

    assert supervisor.calls == 2
    assert [token for token, _payload in requests] == ["old-token", "new-token"]
    assert requests[0][1] == requests[1][1]
    assert response["result"]["structuredContent"] == {
        "operation": "navigate",
        "session_id": "session_1",
    }


def test_reconnect_failure_does_not_resend_the_browser_command() -> None:
    request_count = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            401,
            json={"error": {"code": "unauthorized"}},
            request=request,
        )

    supervisor = FailingReconnectSupervisor()

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = BrowserAgentAdapter(
                connector_origin="http://127.0.0.1:8765",
                process_token="old-token",
                client=client,
                supervisor=supervisor,  # type: ignore[arg-type]
            )
            return await adapter._call(
                "rpc-1",
                "navigate",
                {"request_id": "req_agent_test", "url": "https://example.com"},
            )

    response = asyncio.run(run())

    assert request_count == 1
    assert supervisor.calls == 2
    assert response["result"]["isError"] is True
    assert json.loads(response["result"]["content"][0]["text"]) == {
        "code": "connector_unavailable",
        "message": "reconnect failed",
    }


def test_non_401_connector_response_is_not_retried() -> None:
    request_count = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            409,
            json={"error": {"code": "browser_extension_offline"}},
            request=request,
        )

    supervisor = RecordingSupervisor()

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = BrowserAgentAdapter(
                connector_origin="http://127.0.0.1:8765",
                process_token="old-token",
                client=client,
                supervisor=supervisor,  # type: ignore[arg-type]
            )
            return await adapter._call(
                "rpc-1",
                "navigate",
                {"request_id": "req_agent_test", "url": "https://example.com"},
            )

    response = asyncio.run(run())

    assert request_count == 1
    assert supervisor.calls == 1
    assert json.loads(response["result"]["content"][0]["text"]) == {
        "code": "browser_extension_offline"
    }


def test_second_401_is_returned_without_a_second_reconnect() -> None:
    request_tokens: list[str | None] = []

    def transport(request: httpx.Request) -> httpx.Response:
        request_tokens.append(request.headers.get("X-Asklear-Connector-Token"))
        return httpx.Response(
            401,
            json={"error": {"code": "unauthorized"}},
            request=request,
        )

    supervisor = RecordingSupervisor()

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = BrowserAgentAdapter(
                connector_origin="http://127.0.0.1:8765",
                process_token="old-token",
                client=client,
                supervisor=supervisor,  # type: ignore[arg-type]
            )
            return await adapter._call(
                "rpc-1",
                "navigate",
                {"request_id": "req_agent_test", "url": "https://example.com"},
            )

    response = asyncio.run(run())

    assert request_tokens == ["old-token", "new-token"]
    assert supervisor.calls == 2
    assert response["result"]["isError"] is True
    assert json.loads(response["result"]["content"][0]["text"]) == {
        "code": "unauthorized"
    }


def test_401_without_supervisor_is_not_retried_with_the_stale_token() -> None:
    request_count = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                401,
                json={"error": {"code": "unauthorized"}},
                request=request,
            )
        return httpx.Response(200, json={"unexpected": "retry"}, request=request)

    async def run() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            adapter = BrowserAgentAdapter(
                connector_origin="http://127.0.0.1:8765",
                process_token="old-token",
                client=client,
            )
            return await adapter._call(
                "rpc-1",
                "navigate",
                {"request_id": "req_agent_test", "url": "https://example.com"},
            )

    response = asyncio.run(run())

    assert request_count == 1
    assert response["result"]["isError"] is True
    assert json.loads(response["result"]["content"][0]["text"]) == {
        "code": "unauthorized"
    }


def test_same_agent_recovers_after_real_connector_restart(tmp_path: Path) -> None:
    port = _free_port()
    state_dir = tmp_path / "connector-state"
    started = start_connector(
        host="127.0.0.1",
        port=port,
        api_origin="https://api.asklear.cn",
        root=state_dir,
        startup_timeout=10,
    )
    assert started[1] is True
    token_before = read_published_process_token("127.0.0.1", port, root=state_dir)
    assert token_before

    async def run() -> tuple[dict[str, Any], dict[str, Any], str | None]:
        supervisor = ConnectorSupervisor(
            connector_origin=f"http://127.0.0.1:{port}",
            process_token=token_before,
            autostart=False,
            token_root=state_dir,
        )
        adapter = BrowserAgentAdapter(
            connector_origin=f"http://127.0.0.1:{port}",
            process_token=token_before,
            supervisor=supervisor,
        )
        first_extension = await _connect_extension(port, "inst_restart_first")
        try:
            first_task = asyncio.create_task(
                adapter.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": "rpc-first",
                        "method": "tools/call",
                        "params": {
                            "name": "navigate",
                            "arguments": {
                                "request_id": "req_restart_first",
                                "url": "https://example.com",
                            },
                        },
                    }
                )
            )
            first_command = await _respond_to_navigate(first_extension)
            first_result = await first_task
        finally:
            await first_extension.close()

        await asyncio.to_thread(
            restart_connector,
            host="127.0.0.1",
            port=port,
            api_origin="https://api.asklear.cn",
            root=state_dir,
        )
        token_after = read_published_process_token("127.0.0.1", port, root=state_dir)
        second_extension = await _connect_extension(port, "inst_restart_second")
        try:
            second_task = asyncio.create_task(
                adapter.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": "rpc-second",
                        "method": "tools/call",
                        "params": {
                            "name": "navigate",
                            "arguments": {
                                "request_id": "req_restart_second",
                                "url": "https://example.com",
                            },
                        },
                    }
                )
            )
            second_command = await _respond_to_navigate(second_extension)
            second_result = await second_task
        finally:
            await second_extension.close()
            await adapter.close()
        assert first_command["request_id"] == "req_restart_first"
        assert second_command["request_id"] == "req_restart_second"
        return first_result, second_result, token_after

    try:
        first_result, second_result, token_after = asyncio.run(run())
    finally:
        stop_connector(host="127.0.0.1", port=port, root=state_dir)

    assert token_after and token_after != token_before
    assert first_result["result"]["structuredContent"]["operation"] == "navigate"
    assert second_result["result"]["structuredContent"]["operation"] == "navigate"
