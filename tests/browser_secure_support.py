"""Test client for the local Connector's encrypted WebSocket protocol."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecureTestSocket:
    def __init__(self, socket, key_hex: str, instance_id: str):
        self.socket = socket
        self.portal = socket.portal
        self._send_rx = socket._send_rx
        self._key = bytes.fromhex(key_hex)
        self._sent = 0
        self._received = 0
        client_nonce = secrets.token_hex(32)
        socket.send_json({"type": "secure_hello", "protocol_version": 2, "instance_id": instance_id, "nonce": client_nonce})
        challenge = socket.receive_json()
        transcript = json.dumps(["asklear-local-v2", instance_id, client_nonce, challenge["nonce"]], separators=(",", ":"))
        def digest(label):
            return hmac.new(self._key, f"{label}\0{transcript}".encode(), hashlib.sha256).digest()
        assert hmac.compare_digest(bytes.fromhex(challenge["proof"]), digest("server"))
        self._send = AESGCM(digest("c2s"))
        self._receive = AESGCM(digest("s2c"))
        socket.send_json({"type": "secure_proof", "proof": digest("client").hex()})

    @staticmethod
    def _nonce(direction: str, sequence: int) -> bytes:
        return direction.encode() + b"\0" + sequence.to_bytes(8, "big")

    @staticmethod
    def _aad(direction: str, sequence: int) -> bytes:
        return f"asklear-local-v2:{direction}:{sequence}".encode()

    def encode(self, frame):
        self._sent += 1
        sequence = self._sent
        plaintext = json.dumps(frame, separators=(",", ":")).encode()
        ciphertext = self._send.encrypt(self._nonce("c2s", sequence), plaintext, self._aad("c2s", sequence))
        return {"type": "secure_frame", "seq": sequence, "data": base64.b64encode(ciphertext).decode()}

    def send_json(self, frame):
        self.socket.send_json(self.encode(frame))

    def decrypt(self, frame):
        sequence = frame["seq"]
        assert frame["type"] == "secure_frame" and sequence == self._received + 1
        plaintext = self._receive.decrypt(self._nonce("s2c", sequence), base64.b64decode(frame["data"]), self._aad("s2c", sequence))
        self._received = sequence
        return json.loads(plaintext)

    def receive_json(self):
        return self.decrypt(self.socket.receive_json())

    def _raise_on_close(self, message):
        self.socket._raise_on_close(message)


class AsyncSecureTestSocket:
    @classmethod
    async def connect(cls, socket, key_hex, instance_id):
        client_nonce = secrets.token_hex(32)
        await socket.send(json.dumps({"type": "secure_hello", "protocol_version": 2, "instance_id": instance_id, "nonce": client_nonce}))
        challenge = json.loads(await socket.recv())
        transcript = json.dumps(["asklear-local-v2", instance_id, client_nonce, challenge["nonce"]], separators=(",", ":"))
        key = bytes.fromhex(key_hex)
        def digest(label):
            return hmac.new(key, f"{label}\0{transcript}".encode(), hashlib.sha256).digest()
        assert hmac.compare_digest(bytes.fromhex(challenge["proof"]), digest("server"))
        await socket.send(json.dumps({"type": "secure_proof", "proof": digest("client").hex()}))
        result = cls()
        result.socket = socket
        result.frames = SecureTestSocket.__new__(SecureTestSocket)
        result.frames._sent = result.frames._received = 0
        result.frames._send = AESGCM(digest("c2s"))
        result.frames._receive = AESGCM(digest("s2c"))
        return result

    async def send(self, value):
        await self.socket.send(json.dumps(self.frames.encode(json.loads(value))))

    async def recv(self):
        return json.dumps(self.frames.decrypt(json.loads(await self.socket.recv())))

    async def close(self):
        await self.socket.close()
