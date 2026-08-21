"""按需拉起并托管本地 Connector 的监管器。

设计背景见 docs/superpowers/specs/2026-08-18-browser-connector-lifecycle-design.md。

要点:
- **先探活、再拉起**:已有 Connector(用户手工起的,或另一个 Agent 拉起的)必须复用,
  绝不抢占。这保留了"多个 Agent 共享同一浏览器绑定"的能力。
- **只托管自己拉起的那个**:复用来的进程不归我们管,退出时不得终止它。
- **随 adapter 退出而收尾**:自己拉起的子进程必须一并终止,否则会留下占着 8765 的孤儿。
- **有界重试**:拉起失败不无限重试,直接返回可操作的错误。
- API Key 只从环境透传给子进程,adapter 自身不持有、不记录。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import shutil
import sys
from typing import Any
from urllib.parse import urlsplit

import httpx



API_KEY_ENV = "ASKLEAR_BROWSER_API_KEY"
CONNECTOR_TOKEN_ENV = "ASKLEAR_BROWSER_CONNECTOR_TOKEN"
API_ORIGIN_ENV = "ASKLEAR_BROWSER_API_ORIGIN"
AUTOSTART_ENV = "ASKLEAR_BROWSER_CONNECTOR_AUTOSTART"

DEFAULT_API_ORIGIN = "https://api.asklear.cn"
# 拉起后等待 Connector 就绪的上限。超过即判失败并回收子进程。
STARTUP_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 2.0
# 优雅终止的等待时间,超时才强杀。
SHUTDOWN_GRACE_SECONDS = 5.0


class ConnectorUnavailable(RuntimeError):
    """Connector 不可用,且附带一条用户可操作的说明。"""

    def __init__(self, message: str, *, code: str = "connector_unavailable") -> None:
        super().__init__(message)
        self.code = code


def _loopback_parts(origin: str) -> tuple[str, int]:
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("browser Connector origin must be loopback HTTP")
    return parsed.hostname, parsed.port or 80


def process_token_path(host: str, port: int, *, root: Path | None = None) -> Path:
    base = root or (Path.home() / ".asklear" / "browser-connector")
    return base / f"process-token-{host}-{port}"


def read_published_process_token(
    host: str, port: int, *, root: Path | None = None
) -> str | None:
    """读取正在运行的 Connector 发布的 process token;不存在或不可读返回 None。"""
    try:
        token = process_token_path(host, port, root=root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def publish_process_token(
    host: str, port: int, token: str, *, root: Path | None = None
) -> Path:
    """把 process token 发布到本机 0600 文件,供复用方(其他 adapter)互认。

    这是"多个 Agent 共享同一 Connector"能力的握手通道:没有它,后来者只能
    自造随机 token,对已在运行的 Connector 的每次调用都会 401。
    """
    path = process_token_path(host, port, root=root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    staging = path.with_suffix(".tmp")
    staging.unlink(missing_ok=True)
    # O_EXCL + O_NOFOLLOW:不跟随符号链接,杜绝本机他人预置链接窃取。
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    staging.replace(path)
    return path


class ConnectorSupervisor:
    """探活 → 复用或拉起 → 随自身退出收尾。"""

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
        self._api_origin = api_origin or os.environ.get("ASKLEAR_BROWSER_API_ORIGIN") or DEFAULT_API_ORIGIN
        self._autostart = autostart
        self._client = client or httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS, trust_env=False)
        self._owns_client = client is None
        self._child: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        # 测试注入用:process token 发布/读取的根目录(默认 ~/.asklear/browser-connector)。
        self._token_root = token_root

    @property
    def owns_connector(self) -> bool:
        """True 表示 Connector 是我们拉起的(因此归我们收尾)。"""
        return self._child is not None

    @property
    def process_token(self) -> str:
        """当前生效的 process token。复用已在运行的 Connector 时会被换成对方发布的值。"""
        return self._process_token

    async def _token_accepted(self) -> bool:
        """向需鉴权端点发一个空命令,只看是不是 401——鉴权在读 body 之前。"""
        from .constants import PROCESS_TOKEN_HEADER

        try:
            response = await self._client.post(
                f"{self.connector_origin}/v1/browser/command",
                headers={PROCESS_TOKEN_HEADER: self._process_token},
                json={},
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            return False
        return response.status_code != 401

    async def probe(self) -> dict[str, Any] | None:
        """探活。返回 health body 表示可用;None 表示连不上。"""
        try:
            response = await self._client.get(
                f"{self.connector_origin}/health", timeout=PROBE_TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            return None
        if not response.is_success:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def ensure_running(self) -> dict[str, Any]:
        """确保 Connector 可用:优先复用,必要时拉起。

        返回 health body。不可用时抛 ConnectorUnavailable,消息里带可操作指引。
        """
        async with self._lock:
            existing = await self.probe()
            if existing is not None:
                # 复用不是探活成功就完事:对方的 process token 与我们手里的
                # 大概率不同(各 adapter 启动时就地生成),必须先互认,否则
                # 后续每次命令调用都是 401——而且是没有任何指引的 401。
                published = read_published_process_token(
                    self._host, self._port, root=self._token_root
                )
                if published:
                    self._process_token = published
                if not await self._token_accepted():
                    raise ConnectorUnavailable(
                        f"端口 {self._port} 上已有一个 Connector 在运行,但它不认本进程的"
                        f"令牌,无法复用。请停掉它后重试(自动拉起会用一致的令牌),"
                        f"或在双方配置里提供同一个 {CONNECTOR_TOKEN_ENV}。"
                    )
                return existing

            if not self._autostart:
                raise ConnectorUnavailable(self._manual_hint("未检测到正在运行的 Connector"))

            # 本地模式的 Connector 是纯本地执行器,自动拉起不再需要任何账号凭据——
            # Agent 直接驱动插件采集。云端模式(ASKLEAR_BROWSER_CONNECTOR_MODE=cloud)
            # 的凭据由子进程从继承的环境变量自取,在实际调用时给出可操作错误。
            executable = shutil.which("asklear-browser-connector")
            command: list[str]
            if executable:
                command = [executable]
            else:
                # 未安装 console script 时退回模块调用,保证开发环境也能用。
                command = ["asklear-browser-connector"]  # fallback if installed via pip
            command += [
                "--api-origin", self._api_origin,
                "--host", self._host,
                "--port", str(self._port),
            ]

            environment = dict(os.environ)
            environment[CONNECTOR_TOKEN_ENV] = self._process_token
            # 不再由监管器透传 API Key:本地模式无需凭据;云端模式(灰度回滚)下
            # 子进程从继承的环境变量自取 API Key / OAuth 凭据。
            # 统一网关(asklear-mcp)的远程透传 key 是 ASKLEAR_API_KEY——它属于
            # 网关进程,Connector 既不读它也不该在环境里持有它(红线口径:
            # adapter/Connector 不见 key),spawn 前剔除。
            environment.pop("ASKLEAR_API_KEY", None)

            try:
                child = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    # 子进程输出不能混进 adapter 的 stdio JSON-RPC 流。
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=environment,
                )
            except OSError as error:
                raise ConnectorUnavailable(
                    f"无法启动 Connector:{error}。{self._manual_hint('')}"
                ) from error

            self._child = child
            health = await self._await_ready(child)
            if health is None:
                # 只回收自己拉起的子进程。绝不能连共享的探活 client 一起关:
                # httpx 关闭后再用抛的是 RuntimeError(不是 httpx.HTTPError),
                # 会穿透所有 except,把下一次工具调用连同 adapter 进程一起打死。
                await self._terminate_child()
                raise ConnectorUnavailable(
                    f"Connector 已启动但 {STARTUP_TIMEOUT_SECONDS:.0f} 秒内未就绪。"
                    f"常见原因:{API_KEY_ENV} 无效或账号缺少 browser 权限、"
                    f"端口 {self._port} 被其他程序占用。{self._manual_hint('')}"
                )
            publish_process_token(
                self._host, self._port, self._process_token, root=self._token_root
            )
            return health

    async def _await_ready(self, child: asyncio.subprocess.Process) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_TIMEOUT_SECONDS
        while loop.time() < deadline:
            if child.returncode is not None:
                # 子进程自己退了,继续等没有意义。
                return None
            health = await self.probe()
            if health is not None:
                return health
            await asyncio.sleep(0.25)
        return None

    def _manual_hint(self, prefix: str) -> str:
        head = f"{prefix}。" if prefix else ""
        return (
            f"{head}可手动启动(推荐先授权,之后无需任何 Key):"
            f"asklear-browser-connector login --api-origin {self._api_origin};"
            f"然后 asklear-browser-connector --api-origin {self._api_origin} "
            f"--host {self._host} --port {self._port}。"
            f"如必须用 API Key,别把它写在命令行上(会进 shell history):"
            f"先 read -rs {API_KEY_ENV} && export {API_KEY_ENV},再运行上面的启动命令。"
        )

    async def _terminate_child(self) -> None:
        """只收自己拉起的子进程;复用来的进程不动。不关 client,监管器仍可用。"""
        child = self._child
        self._child = None
        if child is not None and child.returncode is None:
            with suppress(ProcessLookupError):
                child.terminate()
            try:
                await asyncio.wait_for(child.wait(), timeout=SHUTDOWN_GRACE_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                with suppress(ProcessLookupError):
                    child.kill()
                with suppress(Exception):
                    await child.wait()

    async def aclose(self) -> None:
        """最终收尾:回收子进程,并关闭自有 client。此后监管器不可再用。"""
        await self._terminate_child()
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "API_KEY_ENV",
    "API_ORIGIN_ENV",
    "AUTOSTART_ENV",
    "ConnectorSupervisor",
    "ConnectorUnavailable",
]
