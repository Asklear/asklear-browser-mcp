"""Local Connector lifecycle helpers.

The Connector is a small, shared local daemon.  The MCP stdio adapter only
starts it on demand and probes it; the adapter never owns or terminates the
daemon.  This mirrors the simple Kimi WebBridge model: a state directory with
one PID file, one log file, and one private process-token file.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import httpx

CONNECTOR_TOKEN_ENV = "ASKLEAR_BROWSER_CONNECTOR_TOKEN"
API_KEY_ENV = "ASKLEAR_BROWSER_API_KEY"  # retained for import compatibility; local mode never reads it
API_ORIGIN_ENV = "ASKLEAR_BROWSER_API_ORIGIN"
AUTOSTART_ENV = "ASKLEAR_BROWSER_CONNECTOR_AUTOSTART"
CONNECTOR_HOME_ENV = "ASKLEAR_BROWSER_CONNECTOR_HOME"

DEFAULT_API_ORIGIN = "https://api.asklear.cn"
DEFAULT_CONNECTOR_HOME = Path.home() / ".asklear" / "browser-connector"
PID_FILENAME = "connector.pid"
LOG_FILENAME = "connector.log"
START_LOCK_FILENAME = ".start.lock"
START_LOCK_STALE_SECONDS = 2.0
STARTUP_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 2.0
SHUTDOWN_GRACE_SECONDS = 5.0
_CONNECTOR_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PYTHONPATH",
        "ASKLEAR_BROWSER_ALLOWED_EXTENSION_IDS",
        "ASKLEAR_BROWSER_REQUEST_DEADLINE_SECONDS",
        CONNECTOR_HOME_ENV,
    }
)


class ConnectorUnavailable(RuntimeError):
    """Connector 不可用,且附带一条用户可操作的说明。"""

    def __init__(self, message: str, *, code: str = "connector_unavailable") -> None:
        super().__init__(message)
        self.code = code


def connector_state_dir(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    configured = os.environ.get(CONNECTOR_HOME_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CONNECTOR_HOME


def _canonical_loopback_host(host: str) -> str:
    return "127.0.0.1" if host == "localhost" else host


def process_token_path(host: str, port: int, *, root: Path | None = None) -> Path:
    return connector_state_dir(root) / f"process-token-{_canonical_loopback_host(host)}-{port}"


def connector_pid_path(*, root: Path | None = None) -> Path:
    return connector_state_dir(root) / PID_FILENAME


def connector_log_path(*, root: Path | None = None) -> Path:
    return connector_state_dir(root) / LOG_FILENAME


def connector_start_lock_path(*, root: Path | None = None) -> Path:
    return connector_state_dir(root) / START_LOCK_FILENAME


def _private_file_flags() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        os.chmod(path.parent, 0o700)
    staging = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with suppress(FileNotFoundError):
        staging.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _private_file_flags()
    fd = os.open(staging, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        with suppress(OSError):
            os.chmod(staging, 0o600)
        staging.replace(path)
    except Exception:
        with suppress(OSError):
            staging.unlink()
        raise


def _read_private_text(path: Path, *, max_bytes: int = 4096) -> str | None:
    flags = os.O_RDONLY | _private_file_flags()
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = handle.read(max_bytes + 1)
    except (OSError, UnicodeError):
        return None
    return value if len(value) <= max_bytes else None


def read_published_process_token(
    host: str, port: int, *, root: Path | None = None
) -> str | None:
    """读取 Connector 发布的本机 token;不存在或不可读返回 None。"""
    value = _read_private_text(process_token_path(host, port, root=root))
    value = value.strip() if value is not None else ""
    return value or None


def publish_process_token(
    host: str, port: int, token: str, *, root: Path | None = None
) -> Path:
    """把 process token 原子写入用户私有状态目录。"""
    path = process_token_path(host, port, root=root)
    _write_private_text(path, token)
    return path


def _remove_published_process_token(host: str, port: int, token: str, *, root: Path | None) -> None:
    path = process_token_path(host, port, root=root)
    if read_published_process_token(host, port, root=root) != token:
        return
    with suppress(OSError):
        path.unlink()


def _read_pid(*, root: Path | None = None) -> int | None:
    value = _read_private_text(connector_pid_path(root=root), max_bytes=64)
    if value is None:
        return None
    try:
        pid = int(value.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def _write_pid(pid: int, *, root: Path | None = None) -> None:
    _write_private_text(connector_pid_path(root=root), str(pid))


def write_connector_pid(pid: int, *, root: Path | None = None) -> None:
    _write_pid(pid, root=root)


def _clear_pid(pid: int | None, *, root: Path | None = None) -> None:
    path = connector_pid_path(root=root)
    current = _read_pid(root=root)
    if pid is not None and current != pid:
        return
    with suppress(OSError):
        path.unlink()


def clear_connector_pid(pid: int | None, *, root: Path | None = None) -> None:
    _clear_pid(pid, root=root)


def _pid_is_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _loopback_parts(origin: str) -> tuple[str, int]:
    parsed = urlsplit(origin)
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
    return parsed.hostname, parsed.port or 80


def validate_loopback_endpoint(host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("browser Connector must bind to loopback")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("browser Connector port is invalid")


def _is_connector_health(body: object) -> bool:
    if not isinstance(body, dict) or body.get("status") != "ok":
        return False
    if type(body.get("pid")) is not int or body["pid"] <= 0:
        return False
    capabilities = body.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    browser = capabilities.get("browser")
    return isinstance(browser, dict) and browser.get("execution") == "local"


def _connector_health(host: str, port: int, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> dict[str, Any] | None:
    opener = build_opener(ProxyHandler({}))
    request = Request(f"http://{host}:{port}/health", method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                return None
            body = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except (HTTPError, OSError, URLError, TimeoutError, ValueError, UnicodeError):
        return None
    return body if _is_connector_health(body) else None


def _connector_command() -> list[str]:
    """Start the Connector from the same installed Python environment."""
    return [sys.executable, "-m", "asklear_browser_mcp.connector"]


def _connector_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name in _CONNECTOR_ENV_ALLOWLIST
    }


def _detached_popen(
    command: Sequence[str], *, log_path: Path, env: dict[str, str]
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        os.chmod(log_path.parent, 0o700)
    log_handle = log_path.open("ab")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": env,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(list(command), **kwargs)
    finally:
        log_handle.close()


def _terminate_pid(pid: int, *, timeout: float = SHUTDOWN_GRACE_SECONDS) -> None:
    if not _pid_is_running(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_is_running(pid):
        time.sleep(0.05)
    if _pid_is_running(pid) and os.name != "nt":
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def _wait_for_health(pid: int, host: str, port: int, *, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = _connector_health(host, port)
        if health is not None and health.get("pid") == pid:
            return health
        if not _pid_is_running(pid):
            return None
        time.sleep(0.1)
    return None


def _acquire_start_lock(root: Path, *, timeout: float) -> Path:
    path = connector_start_lock_path(root=root)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _private_file_flags(),
                0o600,
            )
        except FileExistsError:
            owner = _read_private_text(path, max_bytes=64)
            try:
                owner_pid = int(owner.strip()) if owner else None
            except ValueError:
                owner_pid = None
            if (
                (owner_pid is not None and not _pid_is_running(owner_pid))
                or (owner_pid is None and _start_lock_is_stale(path))
            ):
                with suppress(OSError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise ConnectorUnavailable("已有一个 Connector 正在启动,请稍后重试。")
            time.sleep(0.05)
            continue
        except OSError as error:
            raise ConnectorUnavailable(f"无法创建 Connector 启动锁: {error}") from error
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        return path


def _release_start_lock(path: Path) -> None:
    with suppress(OSError):
        path.unlink()


def _start_lock_is_stale(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age >= START_LOCK_STALE_SECONDS


def start_connector(
    *,
    host: str,
    port: int,
    api_origin: str,
    root: Path | None = None,
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    command: Sequence[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Start or reuse the detached Connector and return ``(health, started)``."""
    validate_loopback_endpoint(host, port)
    state_root = connector_state_dir(root)
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        os.chmod(state_root, 0o700)
    lock_path = _acquire_start_lock(state_root, timeout=startup_timeout)
    try:
        existing = _connector_health(host, port)
        if existing is not None:
            return existing, False
        existing_pid = _read_pid(root=state_root)
        if _pid_is_running(existing_pid):
            raise ConnectorUnavailable(
                f"Connector 进程 {existing_pid} 正在启动但尚未就绪,请稍后重试。"
            )

        token = read_published_process_token(host, port, root=state_root) or secrets.token_urlsafe(32)
        publish_process_token(host, port, token, root=state_root)
        environment = _connector_environment()
        # run 从私有 token 文件读取;不把 token 放入环境或命令行。
        child_command = list(command or _connector_command()) + [
            "run",
            "--api-origin",
            api_origin,
            "--host",
            host,
            "--port",
            str(port),
            "--state-dir",
            str(state_root),
        ]
        try:
            child = _detached_popen(
                child_command,
                log_path=connector_log_path(root=state_root),
                env=environment,
            )
        except OSError as error:
            _remove_published_process_token(host, port, token, root=state_root)
            raise ConnectorUnavailable(f"无法启动 Connector: {error}") from error

        _write_pid(child.pid, root=state_root)
        health = _wait_for_health(child.pid, host, port, timeout=startup_timeout)
        if health is None:
            _terminate_pid(child.pid)
            _clear_pid(child.pid, root=state_root)
            _remove_published_process_token(host, port, token, root=state_root)
            raise ConnectorUnavailable(
                f"Connector 已启动但 {startup_timeout:.0f} 秒内未就绪。"
                f"请确认 Chrome 扩展已安装并完成绑定、端口 {port} 未被占用。"
            )
        return health, True
    finally:
        _release_start_lock(lock_path)


def status_connector(*, host: str, port: int, root: Path | None = None) -> dict[str, Any]:
    validate_loopback_endpoint(host, port)
    pid = _read_pid(root=root)
    health = _connector_health(host, port)
    alive = _pid_is_running(pid)
    body: dict[str, Any] = {
        "running": health is not None
        and (pid is None or (alive and health.get("pid") == pid)),
        "managed": alive,
        "pid": pid if alive else None,
        "port": port,
    }
    if health is not None:
        body["health"] = health
    return body


def stop_connector(*, host: str, port: int, root: Path | None = None) -> bool:
    validate_loopback_endpoint(host, port)
    state_root = connector_state_dir(root)
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        os.chmod(state_root, 0o700)
    lock_path = _acquire_start_lock(state_root, timeout=STARTUP_TIMEOUT_SECONDS)
    try:
        pid = _read_pid(root=state_root)
        if pid is None or not _pid_is_running(pid):
            _clear_pid(pid, root=state_root)
            return False
        # A stale PID must never be enough to terminate a process.  Require the
        # expected Connector health endpoint and matching PID before sending a
        # signal; otherwise just discard the stale bookkeeping.
        health = _connector_health(host, port)
        if health is None or health.get("pid") != pid:
            _clear_pid(pid, root=state_root)
            return False
        token = read_published_process_token(host, port, root=state_root)
        _terminate_pid(pid)
        _clear_pid(pid, root=state_root)
        if token:
            _remove_published_process_token(host, port, token, root=state_root)
        return True
    finally:
        _release_start_lock(lock_path)


def restart_connector(
    *, host: str, port: int, api_origin: str, root: Path | None = None
) -> tuple[dict[str, Any], bool]:
    stop_connector(host=host, port=port, root=root)
    return start_connector(host=host, port=port, api_origin=api_origin, root=root)


class ConnectorSupervisor:
    """探活 → 复用或 detached 拉起;adapter 退出时只关闭自己的 HTTP client。"""

    def __init__(
        self,
        *,
        connector_origin: str,
        process_token: str,
        api_origin: str | None = None,
        autostart: bool = True,
        client: httpx.AsyncClient | None = None,
        token_root: Path | None = None,
    ) -> None:
        self.connector_origin = connector_origin.rstrip("/")
        self._host, self._port = _loopback_parts(self.connector_origin)
        self._process_token = process_token
        self._api_origin = api_origin or os.environ.get(API_ORIGIN_ENV) or DEFAULT_API_ORIGIN
        self._autostart = autostart
        self._client = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, trust_env=False)
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._token_root = token_root

    @property
    def owns_connector(self) -> bool:
        """常驻 Connector 不归 adapter 所有,因此永远不会由 adapter 收尾。"""
        return False

    @property
    def process_token(self) -> str:
        return self._process_token

    async def _token_accepted(self) -> bool:
        from .constants import PROCESS_TOKEN_HEADER

        try:
            response = await self._client.post(
                f"{self.connector_origin}/v1/browser/command",
                headers={PROCESS_TOKEN_HEADER: self._process_token},
                json={},
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except (httpx.HTTPError, RuntimeError):
            return False
        if response.status_code != 422:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        error = body.get("error") if isinstance(body, dict) else None
        return isinstance(error, dict) and error.get("code") == "malformed_input"

    async def probe(self) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                f"{self.connector_origin}/health", timeout=PROBE_TIMEOUT_SECONDS
            )
        except (httpx.HTTPError, RuntimeError):
            return None
        if not response.is_success:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if _is_connector_health(body) else None

    async def ensure_running(self) -> dict[str, Any]:
        """确保 Connector 可用:优先复用,必要时 detached 拉起。"""
        async with self._lock:
            existing = await self.probe()
            if existing is not None:
                published = read_published_process_token(
                    self._host, self._port, root=self._token_root
                )
                if published:
                    self._process_token = published
                if not await self._token_accepted():
                    raise ConnectorUnavailable(
                        f"端口 {self._port} 上已有一个 Connector 在运行,但它不认本进程的令牌,无法复用。"
                        f"请使用同一个 {CONNECTOR_TOKEN_ENV} 或检查本机 Connector 状态。"
                    )
                return existing

            if not self._autostart:
                raise ConnectorUnavailable(self._manual_hint("未检测到正在运行的 Connector"))

            command = _connector_command() + [
                "start",
                "--api-origin",
                self._api_origin,
                "--host",
                self._host,
                "--port",
                str(self._port),
            ]
            if self._token_root is not None:
                command += ["--state-dir", str(self._token_root)]
            environment = _connector_environment()
            launcher: asyncio.subprocess.Process | None = None
            try:
                launcher = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                )
                stdout, stderr = await asyncio.wait_for(
                    launcher.communicate(), timeout=STARTUP_TIMEOUT_SECONDS + 5
                )
            except (TimeoutError, OSError) as error:
                if launcher is not None:
                    with suppress(ProcessLookupError):
                        launcher.kill()
                raise ConnectorUnavailable(
                    f"无法启动 Connector。{self._manual_hint('')}"
                ) from error
            if launcher.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
                message = detail[:300] if detail else "Connector start 命令失败"
                raise ConnectorUnavailable(f"{message}。{self._manual_hint('')}")

            published = read_published_process_token(
                self._host, self._port, root=self._token_root
            )
            if published:
                self._process_token = published
            health = await self.probe()
            if health is None or not await self._token_accepted():
                raise ConnectorUnavailable(
                    f"Connector 已启动但无法完成本地握手。{self._manual_hint('')}"
                )
            return health

    def _manual_hint(self, prefix: str) -> str:
        head = f"{prefix}。" if prefix else ""
        return (
            f"{head}可直接启动本地 Connector:"
            f"asklear-browser-connector start --host {self._host} --port {self._port}。"
            "它不需要 API Key、OAuth 授权或单独安装 Connector。"
        )

    async def aclose(self) -> None:
        """只关闭 adapter 自己的 client,不停止常驻 Connector。"""
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "API_KEY_ENV",
    "API_ORIGIN_ENV",
    "AUTOSTART_ENV",
    "CONNECTOR_HOME_ENV",
    "CONNECTOR_TOKEN_ENV",
    "ConnectorSupervisor",
    "ConnectorUnavailable",
    "_connector_command",
    "clear_connector_pid",
    "connector_log_path",
    "connector_pid_path",
    "connector_start_lock_path",
    "connector_state_dir",
    "process_token_path",
    "publish_process_token",
    "read_published_process_token",
    "restart_connector",
    "start_connector",
    "status_connector",
    "stop_connector",
    "validate_loopback_endpoint",
    "write_connector_pid",
]
