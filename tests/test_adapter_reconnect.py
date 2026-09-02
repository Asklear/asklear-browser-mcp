from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from asklear_browser_mcp.adapter import BrowserAgentAdapter
from asklear_browser_mcp.supervisor import ConnectorUnavailable


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
