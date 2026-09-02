"""Local loopback Connector used by the standalone browser MCP package.

The stdio adapter is started once per Agent.  This process is the shared
machine-local bridge: it owns the single WebSocket connection to the Asklear
Chrome extension and multiplexes commands from any number of adapters over
that connection.

No page content or account credential is sent to Asklear by this local mode.
The loopback process token only protects the local HTTP command endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .constants import PROCESS_TOKEN_HEADER

BROWSER_OPERATIONS = ("navigate", "observe", "click", "fill", "scroll", "extract")
CONNECTOR_TOKEN_ENV = "ASKLEAR_BROWSER_CONNECTOR_TOKEN"
ALLOWED_EXTENSION_IDS_ENV = "ASKLEAR_BROWSER_ALLOWED_EXTENSION_IDS"
REQUEST_DEADLINE_ENV = "ASKLEAR_BROWSER_REQUEST_DEADLINE_SECONDS"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DEADLINE_SECONDS = 120
MAX_ACTIVE_SESSIONS = 4
MAX_FILL_VALUE_BYTES = 8192
MAX_URL_BYTES = 8192
MAX_ERROR_MESSAGE_LENGTH = 512
STARTUP_API_ORIGIN = "https://api.asklear.cn"
ASKLEAR_EXTENSION_ID = "ankkcefnhidgdggjdbkefehhahgofebe"
_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9_-]{1,124}$")
_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9_-]{1,124}$")
_INSTANCE_ID = re.compile(r"^inst_[A-Za-z0-9_-]{1,123}$")
_SNAPSHOT_ID = re.compile(r"^snap_[A-Za-z0-9_-]{1,120}$")
_ELEMENT_REF = re.compile(r"^el_[A-Za-z0-9_-]{1,120}$")


class BrowserConnectorError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _deadline_seconds() -> int:
    raw = os.environ.get(REQUEST_DEADLINE_ENV, "").strip()
    if not raw:
        return DEFAULT_DEADLINE_SECONDS
    try:
        return max(1, min(300, int(raw)))
    except ValueError:
        return DEFAULT_DEADLINE_SECONDS


def _allowed_extension_origins() -> set[str] | None:
    raw = os.environ.get(ALLOWED_EXTENSION_IDS_ENV, "").strip()
    if raw == "*":
        return None
    ids = {ASKLEAR_EXTENSION_ID}
    ids.update(item.strip() for item in raw.split(",") if item.strip())
    return {f"chrome-extension://{item}" for item in ids}


def _session_id(request_id: str) -> str:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
    return f"ses_{digest}"


def _validate_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_URL_BYTES:
        raise BrowserConnectorError("browser URL is invalid", code="malformed_input")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except (TypeError, ValueError):
        raise BrowserConnectorError("browser URL is invalid", code="malformed_input") from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in hostname)
    ):
        raise BrowserConnectorError("browser URL is invalid", code="malformed_input")
    if hostname.casefold() in {"localhost", "localhost.localdomain"}:
        raise BrowserConnectorError("browser URL is invalid", code="malformed_input")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise BrowserConnectorError("browser URL is invalid", code="malformed_input")
    return value


def _reject_extra(payload: Mapping[str, Any], allowed: set[str]) -> None:
    if set(payload) - allowed:
        raise BrowserConnectorError("browser command is invalid", code="malformed_input")


def _validate_command(operation: object, payload: object) -> tuple[str, str, str | None, dict[str, Any]]:
    if operation not in BROWSER_OPERATIONS or not isinstance(payload, Mapping):
        raise BrowserConnectorError("browser command is invalid", code="malformed_input")
    data = dict(payload)
    request_id = data.pop("request_id", None)
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise BrowserConnectorError("request id is invalid", code="malformed_input")
    session_id = data.pop("session_id", None)
    if session_id is not None and (not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id)):
        raise BrowserConnectorError("session id is invalid", code="malformed_input")

    if operation == "navigate":
        _reject_extra(data, {"url", "visibility"})
        data["url"] = _validate_url(data.get("url"))
        visibility = data.get("visibility", "silent")
        if visibility not in {"silent", "visible", "foreground"}:
            raise BrowserConnectorError("browser command is invalid", code="malformed_input")
        data["visibility"] = visibility
    elif operation == "observe" or operation == "extract":
        _reject_extra(data, set())
    elif operation == "scroll":
        _reject_extra(data, {"pixels"})
        pixels = data.get("pixels")
        if type(pixels) is not int or not 1 <= pixels <= 4000:
            raise BrowserConnectorError("browser command is invalid", code="malformed_input")
    elif operation == "click":
        _reject_extra(data, {"snapshot_id", "element_ref"})
        if not isinstance(data.get("snapshot_id"), str) or not _SNAPSHOT_ID.fullmatch(data["snapshot_id"]):
            raise BrowserConnectorError("browser command is invalid", code="malformed_input")
        if not isinstance(data.get("element_ref"), str) or not _ELEMENT_REF.fullmatch(data["element_ref"]):
            raise BrowserConnectorError("browser command is invalid", code="malformed_input")
    else:
        _reject_extra(data, {"snapshot_id", "element_ref", "value"})
        if not isinstance(data.get("snapshot_id"), str) or not _SNAPSHOT_ID.fullmatch(data["snapshot_id"]):
            raise BrowserConnectorError("browser command is invalid", code="malformed_input")
        if not isinstance(data.get("element_ref"), str) or not _ELEMENT_REF.fullmatch(data["element_ref"]):
            raise BrowserConnectorError("browser command is invalid", code="malformed_input")
        value = data.get("value")
        if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > MAX_FILL_VALUE_BYTES:
            raise BrowserConnectorError("browser input is invalid", code="malformed_input")
    return str(operation), request_id, session_id, data


class _ExtensionConnection:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.instance_id = ""
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def execute(self, frame: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
        request_id = frame["request_id"]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            try:
                await self.websocket.send_json(dict(frame))
            except (ConnectionError, OSError, RuntimeError, WebSocketDisconnect) as error:
                raise BrowserConnectorError("browser extension is offline", code="browser_extension_offline") from error
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError as error:
                raise BrowserConnectorError(
                    "browser extension request timed out", code="browser_extension_request_timed_out"
                ) from error
        finally:
            self.pending.pop(request_id, None)

    def resolve(self, frame: Mapping[str, Any]) -> None:
        request_id = frame.get("request_id")
        if isinstance(request_id, str):
            future = self.pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(dict(frame))

    def fail(self, error: BaseException) -> None:
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        self.pending.clear()


class _ExtensionHub:
    def __init__(self) -> None:
        self.current: _ExtensionConnection | None = None
        self._lock = asyncio.Lock()

    async def claim(self, connection: _ExtensionConnection) -> None:
        async with self._lock:
            if self.current is not None:
                raise BrowserConnectorError(
                    "browser extension connection already active", code="browser_extension_busy"
                )
            self.current = connection

    async def release(self, connection: _ExtensionConnection) -> None:
        async with self._lock:
            if self.current is connection:
                self.current = None


class _BrowserCore:
    def __init__(self, hub: _ExtensionHub, *, timeout_seconds: int) -> None:
        self.hub = hub
        self.timeout_seconds = timeout_seconds
        self._sessions: set[str] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._replays: dict[str, tuple[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def invalidate_sessions(self) -> None:
        async with self._lock:
            self._sessions.clear()
            self._session_locks.clear()

    async def execute(self, *, operation: object, payload: object) -> dict[str, Any]:
        operation_name, request_id, requested_session_id, command = _validate_command(operation, payload)
        digest = hashlib.sha256(
            repr((operation_name, request_id, requested_session_id, sorted(command.items()))).encode("utf-8")
        ).hexdigest()
        async with self._lock:
            replay = self._replays.get(request_id)
            if replay is not None:
                if replay[0] != digest:
                    raise BrowserConnectorError("idempotency conflict", code="idempotency_conflict")
                return replay[1]
            creates_session = operation_name == "navigate" and requested_session_id is None
            session_id = requested_session_id or _session_id(request_id)
            if not creates_session and session_id not in self._sessions:
                raise BrowserConnectorError("browser session is unknown", code="session_mismatch")
            if creates_session and len(self._sessions) >= MAX_ACTIVE_SESSIONS:
                raise BrowserConnectorError("browser session capacity is full", code="browser_capacity")
            self._sessions.add(session_id)
            lock = self._session_locks.setdefault(session_id, asyncio.Lock())

        try:
            async with lock:
                connection = self.hub.current
                if connection is None:
                    raise BrowserConnectorError("browser extension is offline", code="browser_extension_offline")
                deadline = datetime.now(UTC) + timedelta(seconds=self.timeout_seconds)
                frame = {
                    "type": "command",
                    "request_id": request_id,
                    "session_id": session_id,
                    "operation": operation_name,
                    "deadline_at": deadline.isoformat().replace("+00:00", "Z"),
                    "payload": command,
                }
                response = await connection.execute(frame, timeout=self.timeout_seconds)
                body = self._response_body(response, operation_name, request_id, session_id)
                async with self._lock:
                    self._replays[request_id] = (digest, body)
                return body
        except Exception:
            if creates_session:
                async with self._lock:
                    self._sessions.discard(session_id)
                    self._session_locks.pop(session_id, None)
            raise

    @staticmethod
    def _response_body(
        response: Mapping[str, Any], operation: str, request_id: str, session_id: str
    ) -> dict[str, Any]:
        if response.get("type") != "response" or response.get("request_id") != request_id:
            raise BrowserConnectorError("extension response is invalid", code="browser_protocol_mismatch")
        if response.get("session_id") != session_id:
            raise BrowserConnectorError("browser session mismatch", code="session_mismatch")
        if response.get("ok") is not True:
            error = response.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
                message = error.get("message")
                if isinstance(code, str) and re.fullmatch(r"^[a-z][a-z0-9_]{2,63}$", code):
                    if isinstance(message, str) and message.strip():
                        raise BrowserConnectorError(message[:MAX_ERROR_MESSAGE_LENGTH], code=code)
                    raise BrowserConnectorError("browser operation failed", code=code)
            raise BrowserConnectorError("browser operation failed", code="browser_operation_failed")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise BrowserConnectorError("extension response is invalid", code="browser_protocol_mismatch")
        return {
            "request_id": request_id,
            "session_id": session_id,
            "operation": operation,
            "result": dict(result),
            "billing": {"settled_credits": 0, "billing_mode": "local"},
        }


class BrowserConnectorApplication:
    """FastAPI application bound to loopback only."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        process_token: str | None = None,
        request_timeout_seconds: int | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("browser Connector must bind to loopback")
        if not 1 <= port <= 65535:
            raise ValueError("browser Connector port is invalid")
        self.host = host
        self.port = port
        self.process_token = process_token or secrets.token_urlsafe(32)
        if not self.process_token.strip():
            raise ValueError("browser Connector process token is required")
        self.hub = _ExtensionHub()
        self.core = _BrowserCore(
            self.hub,
            timeout_seconds=request_timeout_seconds or _deadline_seconds(),
        )
        self.app = FastAPI(title="Asklear local browser Connector")
        self._allowed_origins = _allowed_extension_origins()
        self._mount_routes()

    def _mount_routes(self) -> None:
        @self.app.get("/health")
        async def health() -> dict[str, Any]:
            current = self.hub.current
            return {
                "status": "ok",
                "pid": os.getpid(),
                "extension_connected": current is not None,
                "instance_id": current.instance_id if current is not None else None,
                "capabilities": {
                    "browser": {
                        "execution": "local",
                        "operations": list(BROWSER_OPERATIONS),
                        "max_active_sessions": MAX_ACTIVE_SESSIONS,
                    }
                },
            }

        @self.app.post("/v1/browser/command")
        async def command(request: Request):
            provided = request.headers.get(PROCESS_TOKEN_HEADER, "")
            if not hmac.compare_digest(provided, self.process_token):
                return JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
            try:
                body = await request.json()
                if not isinstance(body, Mapping):
                    raise BrowserConnectorError("browser command is invalid", code="malformed_input")
                return await self.core.execute(
                    operation=body.get("operation"),
                    payload={key: value for key, value in body.items() if key != "operation"},
                )
            except BrowserConnectorError as error:
                status = 422 if error.code in {"malformed_input", "unauthorized"} else 409
                return JSONResponse(
                    {"error": {"code": error.code, "message": str(error)}},
                    status_code=status,
                )
            except Exception:  # noqa: BLE001 - keep the local HTTP endpoint fail-closed
                return JSONResponse(
                    {"error": {"code": "connector_error", "message": "browser Connector failed"}},
                    status_code=502,
                )

        @self.app.websocket("/v1/browser/ws")
        async def extension_socket(websocket: WebSocket):
            origin = websocket.headers.get("origin")
            if (
                self._allowed_origins is not None
                and origin is not None
                and origin.startswith("chrome-extension://")
                and origin not in self._allowed_origins
            ):
                await websocket.close(code=1008)
                return
            await websocket.accept()
            connection = _ExtensionConnection(websocket)
            claimed = False
            try:
                first = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                if (
                    not isinstance(first, Mapping)
                    or first.get("type") != "authenticate"
                    or first.get("protocol_version") != 1
                    or not isinstance(first.get("instance_id"), str)
                    or not _INSTANCE_ID.fullmatch(first["instance_id"])
                    or not isinstance(first.get("instance_token"), str)
                    or len(first["instance_token"]) < 16
                ):
                    raise BrowserConnectorError(
                        "extension authentication is invalid", code="authentication_required"
                    )
                connection.instance_id = first["instance_id"]
                await self.hub.claim(connection)
                claimed = True
                await websocket.send_json(
                    {
                        "type": "authenticated",
                        "protocol_version": 1,
                        "instance_id": connection.instance_id,
                        "server_time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    }
                )
                while True:
                    frame = await websocket.receive_json()
                    if not isinstance(frame, Mapping):
                        raise BrowserConnectorError("extension frame is invalid", code="malformed_input")
                    if frame.get("type") == "heartbeat":
                        continue
                    if frame.get("type") == "response":
                        connection.resolve(frame)
                        continue
                    raise BrowserConnectorError("unexpected extension frame", code="malformed_input")
            except WebSocketDisconnect:
                pass
            except Exception:  # noqa: BLE001 - close malformed extension sessions
                with suppress(Exception):
                    await websocket.close(code=1002)
            finally:
                if claimed:
                    await self.core.invalidate_sessions()
                connection.fail(
                    BrowserConnectorError("browser extension is offline", code="browser_extension_offline")
                )
                await self.hub.release(connection)

    def run(self, *, state_dir: Path | None = None) -> None:
        from .supervisor import (
            ConnectorUnavailable,
            _connector_health,
            _pid_is_running,
            _read_pid,
            clear_connector_pid,
            connector_state_dir,
            process_token_path,
            publish_process_token,
            validate_loopback_endpoint,
            write_connector_pid,
        )

        validate_loopback_endpoint(self.host, self.port)
        state_root = connector_state_dir(state_dir)
        existing_pid = _read_pid(root=state_root)
        if (
            (existing_pid not in {None, os.getpid()} and _pid_is_running(existing_pid))
            or _connector_health(self.host, self.port) is not None
        ):
            raise ConnectorUnavailable(
                f"端口 {self.port} 上已有一个 Connector 在运行,请使用 start/status/stop 管理。"
            )
        write_connector_pid(os.getpid(), root=state_root)
        try:
            publish_process_token(
                self.host, self.port, self.process_token, root=state_root
            )
            uvicorn.run(
                self.app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
            )
        finally:
            clear_connector_pid(os.getpid(), root=state_root)
            with suppress(OSError):
                token_path = process_token_path(self.host, self.port, root=state_root)
                current = token_path.read_text(encoding="utf-8")
                if current.strip() == self.process_token:
                    token_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Asklear local browser Connector")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("start", "run", "status", "stop", "restart"),
        default="run",
        help="生命周期命令(默认 run, start 会在后台运行)",
    )
    # Kept for compatibility with the unified gateway supervisor.  Local mode
    # does not call the control plane, so the value is intentionally unused.
    parser.add_argument("--api-origin", default=STARTUP_API_ORIGIN)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--process-token",
        default=os.environ.get(CONNECTOR_TOKEN_ENV, ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--state-dir", default=None, help="本地状态目录")
    args = parser.parse_args(argv)
    from .supervisor import (
        ConnectorUnavailable,
        restart_connector,
        start_connector,
        status_connector,
        stop_connector,
        validate_loopback_endpoint,
    )

    state_root = Path(args.state_dir) if args.state_dir else None
    if args.command == "run":
        try:
            validate_loopback_endpoint(args.host, args.port)
            token = args.process_token or os.environ.get(CONNECTOR_TOKEN_ENV, "")
            if not token:
                from .supervisor import read_published_process_token

                token = read_published_process_token(args.host, args.port, root=state_root) or secrets.token_urlsafe(32)
            BrowserConnectorApplication(
                host=args.host,
                port=args.port,
                process_token=token,
            ).run(state_dir=state_root)
            return 0
        except (ConnectorUnavailable, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 1

    try:
        if args.command == "start":
            health, started = start_connector(
                host=args.host,
                port=args.port,
                api_origin=args.api_origin,
                root=state_root,
            )
            print(json.dumps({"started": started, "health": health}, ensure_ascii=False))
            return 0
        if args.command == "restart":
            health, started = restart_connector(
                host=args.host,
                port=args.port,
                api_origin=args.api_origin,
                root=state_root,
            )
            print(json.dumps({"started": started, "health": health}, ensure_ascii=False))
            return 0
        if args.command == "status":
            status = status_connector(host=args.host, port=args.port, root=state_root)
            print(json.dumps(status, ensure_ascii=False))
            return 0 if status["running"] else 1
        stopped = stop_connector(host=args.host, port=args.port, root=state_root)
        print(json.dumps({"stopped": stopped}, ensure_ascii=False))
        return 0
    except (ConnectorUnavailable, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


__all__ = [
    "ASKLEAR_EXTENSION_ID",
    "BROWSER_OPERATIONS",
    "CONNECTOR_TOKEN_ENV",
    "BrowserConnectorApplication",
    "BrowserConnectorError",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
