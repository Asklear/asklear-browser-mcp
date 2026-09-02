"""stdio JSON-RPC adapter for Agents without a native browser surface.

The adapter is deliberately a thin local proxy.  It knows only the loopback
Connector process token; the Asklear API key belongs to that Connector and is
never accepted here or sent with a page command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from collections.abc import Mapping
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx

from .constants import MAX_FILL_VALUE_BYTES, PROCESS_TOKEN_HEADER
from .supervisor import (
    AUTOSTART_ENV,
    ConnectorSupervisor,
    ConnectorUnavailable,
)

CONNECTOR_TOKEN_ENV = "ASKLEAR_BROWSER_CONNECTOR_TOKEN"
CONNECTOR_ORIGIN_ENV = "ASKLEAR_BROWSER_CONNECTOR_ORIGIN"
DEFAULT_CONNECTOR_ORIGIN = "http://127.0.0.1:8765"
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{1,124}$")
ADAPTER_OPERATIONS = ("navigate", "observe", "click", "fill", "scroll", "extract")


def _error(request_id: Any, code: int, message: str, *, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        body["error"]["data"] = data
    return body


def _operation_schema(operation: str) -> dict[str, Any]:
    common = {
        "request_id": {"type": "string"},
        "session_id": {"type": "string"},
    }
    if operation == "navigate":
        properties = {
            **common,
            "url": {"type": "string", "minLength": 1},
            "visibility": {
                "type": "string",
                "enum": ["silent", "visible", "foreground"],
                "default": "silent",
            },
        }
        required = ["url"]
    elif operation in {"observe", "extract"}:
        properties = common
        required = ["session_id"]
    elif operation == "scroll":
        properties = {
            **common,
            "pixels": {"type": "integer", "minimum": 1, "maximum": 4000},
        }
        required = ["session_id", "pixels"]
    elif operation == "click":
        properties = {
            **common,
            "snapshot_id": {"type": "string"},
            "element_ref": {"type": "string"},
        }
        required = ["session_id", "snapshot_id", "element_ref"]
    else:
        properties = {
            **common,
            "snapshot_id": {"type": "string"},
            "element_ref": {"type": "string"},
            "value": {"type": "string", "maxLength": MAX_FILL_VALUE_BYTES},
        }
        required = ["session_id", "snapshot_id", "element_ref", "value"]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": operation,
            "description": (
                f"Run the local Chrome browser {operation} operation. "
                "Page results stay local to the Agent."
            ),
            "inputSchema": _operation_schema(operation),
        }
        for operation in ADAPTER_OPERATIONS
    ]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _new_request_id() -> str:
    return f"req_agent_{secrets.token_hex(12)}"


def _validate_connector_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        raise ValueError("browser Connector origin is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("browser Connector origin must be loopback HTTP")
    return value.rstrip("/")


class BrowserAgentAdapter:
    """Translate six stdio MCP tools into local Connector HTTP commands."""

    def __init__(
        self,
        *,
        connector_origin: str = DEFAULT_CONNECTOR_ORIGIN,
        process_token: str,
        client: httpx.AsyncClient | None = None,
        supervisor: ConnectorSupervisor | None = None,
    ) -> None:
        if not isinstance(process_token, str) or not process_token.strip():
            raise ValueError("browser Connector process token is required")
        self.connector_origin = _validate_connector_origin(connector_origin)
        self.process_token = process_token
        self._client = client or httpx.AsyncClient(timeout=120, trust_env=False)
        self._owns_client = client is None
        # 监管器负责"探活 → 复用或拉起";复用来的 Connector 不归它收尾。
        self._supervisor = supervisor
        self._ensured = False

    async def _ensure_connector(self) -> None:
        """首次实际调用前确保 Connector 可用。

        放在首次调用而不是构造时,是为了让 adapter 在 Connector 尚不可用时
        也能完成 MCP 握手与 tools/list——否则 Agent 客户端会直接判定该
        MCP 启动失败,用户连错误说明都看不到。
        """
        if self._ensured or self._supervisor is None:
            return
        await self._supervisor.ensure_running()
        # 复用已在运行的 Connector 时,监管器会换成对方发布的 token;
        # 后续请求必须跟着换,否则每次调用都是 401。
        self.process_token = self._supervisor.process_token
        self._ensured = True

    async def close(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.aclose()
        if self._owns_client:
            await self._client.aclose()

    async def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid Request")
        request_id = message.get("id")
        method = message.get("method")
        # JSON-RPC 通知一律不作答(不只 initialized:cancelled/progress 是
        # 客户端常规消息,回 id:null 的错误违反 2.0 规范并制造客户端噪音);
        # ping 按 MCP 规范必须回空 result。
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "asklear-local-browser", "version": "0.1.1"},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _tool_definitions()}}
        if method != "tools/call":
            return _error(request_id, -32601, "Method not found")

        params = message.get("params")
        if not isinstance(params, Mapping):
            return _error(request_id, -32602, "Invalid params")
        operation = params.get("name")
        arguments = params.get("arguments", {})
        if operation not in ADAPTER_OPERATIONS or not isinstance(arguments, Mapping):
            return _error(request_id, -32602, "Invalid tool arguments")
        if "operation" in arguments:
            return _error(request_id, -32602, "operation is controlled by the tool name")

        payload = dict(arguments)
        command_request_id = payload.get("request_id")
        if command_request_id is None:
            command_request_id = _new_request_id()
            payload["request_id"] = command_request_id
        if not isinstance(command_request_id, str) or not _REQUEST_ID.fullmatch(command_request_id):
            return _error(request_id, -32602, "request_id is invalid")

        return await self._call(request_id, operation, payload)

    async def _call(
        self, request_id: Any, operation: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            await self._ensure_connector()
        except ConnectorUnavailable as error:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": _json_text(
                        {"code": error.code, "message": str(error)}
                    )}],
                },
            }
        try:
            response = await self._client.post(
                f"{self.connector_origin}/v1/browser/command",
                headers={
                    PROCESS_TOKEN_HEADER: self.process_token,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json={"operation": operation, **dict(payload)},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": _json_text({"code": "connector_unavailable", "message": str(error)})}],
                },
            }

        if not response.is_success:
            error_body = body.get("error") if isinstance(body, Mapping) else body
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": _json_text(error_body)}],
                },
            }
        if not isinstance(body, Mapping):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": _json_text({"code": "connector_protocol_error"})}],
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": _json_text(body)}],
                "structuredContent": dict(body),
            },
        }


async def _run_stdio(adapter: BrowserAgentAdapter, stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _error(None, -32700, "Parse error")
        else:
            response = await adapter.handle(message)
        if response is not None:
            stdout.write(_json_text(response) + "\n")
            stdout.flush()


async def _run_adapter(adapter: BrowserAgentAdapter, stdin: TextIO, stdout: TextIO) -> None:
    try:
        await _run_stdio(adapter, stdin, stdout)
    finally:
        await adapter.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Asklear local browser Agent adapter")
    parser.add_argument(
        "--connector-origin",
        default=os.environ.get(CONNECTOR_ORIGIN_ENV, DEFAULT_CONNECTOR_ORIGIN),
    )
    parser.add_argument("--process-token", default=os.environ.get(CONNECTOR_TOKEN_ENV, ""))
    parser.add_argument(
        "--autostart",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get(AUTOSTART_ENV, "").strip().lower() not in {"0", "false", "no"},
        help="自动拉起 Connector(--no-autostart 关闭;命令行优先于环境变量)",
    )
    args = parser.parse_args(argv)

    autostart = args.autostart
    process_token = args.process_token
    if not process_token:
        if not autostart:
            # 不自启时必须由用户提供,否则无法与已在运行的 Connector 互认。
            parser.error(f"{CONNECTOR_TOKEN_ENV} is required when autostart is disabled")
        # 自启时不需要用户配置令牌;这里仅给 adapter 一个临时占位值,
        # Supervisor 会从常驻 Connector 的私有状态文件读取真正的令牌。
        process_token = secrets.token_urlsafe(32)

    supervisor = ConnectorSupervisor(
        connector_origin=args.connector_origin,
        process_token=process_token,
        autostart=autostart,
    )
    adapter = BrowserAgentAdapter(
        connector_origin=args.connector_origin,
        process_token=process_token,
        supervisor=supervisor,
    )
    asyncio.run(_run_adapter(adapter, sys.stdin, sys.stdout))
    return 0


__all__ = [
    "CONNECTOR_ORIGIN_ENV",
    "CONNECTOR_TOKEN_ENV",
    "DEFAULT_CONNECTOR_ORIGIN",
    "BrowserAgentAdapter",
    "main",
]
