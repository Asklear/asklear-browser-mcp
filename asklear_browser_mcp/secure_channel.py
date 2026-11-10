"""Authenticate and encrypt the local extension connection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import WebSocket

_HEX_32 = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE = re.compile(r"^inst_[A-Za-z0-9_-]{1,123}$")
_MAX_FRAME_BYTES = 2_000_000


def pairing_key(*, root: Path | None = None) -> str:
    configured = os.environ.get("ASKLEAR_BROWSER_CONNECTOR_HOME", "").strip()
    base = root if root is not None else Path(configured) if configured else Path.home() / ".asklear" / "browser-connector"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(base, 0o700)
    path = base / "extension-pairing-key"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=".pairing-", dir=base)
        try:
            with os.fdopen(temporary_fd, "w", encoding="ascii") as temporary:
                temporary.write(secrets.token_hex(32))
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary_name)
        fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="ascii") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
            raise ValueError("local pairing credentials are not private")
        value = handle.read(65)
    if not _HEX_32.fullmatch(value):
        raise ValueError("local pairing credentials are invalid")
    return value


def _digest(key: bytes, label: str, transcript: str) -> bytes:
    return hmac.new(key, f"{label}\0{transcript}".encode(), hashlib.sha256).digest()


class SecureChannel:
    def __init__(self, key: bytes, transcript: str) -> None:
        self._receive = AESGCM(_digest(key, "c2s", transcript))
        self._send = AESGCM(_digest(key, "s2c", transcript))
        self._receive_seq = 0
        self._send_seq = 0
        self._send_lock = asyncio.Lock()

    @staticmethod
    def _nonce(direction: str, seq: int) -> bytes:
        return direction.encode() + b"\0" + seq.to_bytes(8, "big")

    @staticmethod
    def _aad(direction: str, seq: int) -> bytes:
        return f"asklear-local-v2:{direction}:{seq}".encode()

    async def send(self, websocket: WebSocket, frame: dict[str, Any]) -> None:
        async with self._send_lock:
            self._send_seq += 1
            seq = self._send_seq
            plaintext = json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode()
            if len(plaintext) > _MAX_FRAME_BYTES:
                raise ValueError("secure frame too large")
            ciphertext = self._send.encrypt(self._nonce("s2c", seq), plaintext, self._aad("s2c", seq))
            await websocket.send_json({"type": "secure_frame", "seq": seq, "data": base64.b64encode(ciphertext).decode()})

    def receive(self, frame: Any) -> dict[str, Any]:
        if not isinstance(frame, dict) or set(frame) != {"type", "seq", "data"} or frame["type"] != "secure_frame":
            raise ValueError("invalid secure frame")
        seq, encoded = frame["seq"], frame["data"]
        if type(seq) is not int or seq != self._receive_seq + 1 or not isinstance(encoded, str) or len(encoded) > 2_700_000:
            raise ValueError("invalid secure sequence")
        ciphertext = base64.b64decode(encoded, validate=True)
        plaintext = self._receive.decrypt(self._nonce("c2s", seq), ciphertext, self._aad("c2s", seq))
        if len(plaintext) > _MAX_FRAME_BYTES:
            raise ValueError("secure frame too large")
        decoded = json.loads(plaintext)
        if not isinstance(decoded, dict):
            raise ValueError("invalid secure payload")
        self._receive_seq = seq
        return decoded


async def accept_secure_channel(websocket: WebSocket, key_hex: str, first: Any) -> tuple[SecureChannel, str]:
    if not _HEX_32.fullmatch(key_hex):
        raise ValueError("invalid pairing key")
    if not isinstance(first, dict) or set(first) != {"type", "protocol_version", "instance_id", "nonce"}:
        raise ValueError("secure handshake required")
    instance, nonce = first["instance_id"], first["nonce"]
    if (first["type"] != "secure_hello" or first["protocol_version"] != 2
            or not isinstance(instance, str) or not _INSTANCE.fullmatch(instance)
            or not isinstance(nonce, str) or not _HEX_32.fullmatch(nonce)):
        raise ValueError("invalid secure handshake")
    server_nonce = secrets.token_hex(32)
    transcript = json.dumps(["asklear-local-v2", instance, nonce, server_nonce], separators=(",", ":"))
    key = bytes.fromhex(key_hex)
    await websocket.send_json({"type": "secure_challenge", "nonce": server_nonce,
                               "proof": _digest(key, "server", transcript).hex()})
    proof = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    if (not isinstance(proof, dict) or set(proof) != {"type", "proof"}
            or proof["type"] != "secure_proof" or not isinstance(proof["proof"], str)
            or not hmac.compare_digest(proof["proof"], _digest(key, "client", transcript).hex())):
        raise ValueError("invalid pairing proof")
    return SecureChannel(key, transcript), instance
