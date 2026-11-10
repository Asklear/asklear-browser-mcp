from __future__ import annotations

import os
import base64
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from asklear_browser_mcp.connector import BrowserConnectorApplication
from browser_secure_support import SecureTestSocket


def test_connector_rejects_an_unpaired_plaintext_extension() -> None:
    connector = BrowserConnectorApplication(host="127.0.0.1", port=8765, process_token="process-token")
    with TestClient(connector.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/browser/ws") as extension:
                extension.send_json({
                    "type": "authenticate", "protocol_version": 1,
                    "instance_id": "inst_fake", "instance_token": "unpaired-token-1234",
                })
                extension.receive_json()
        assert client.get("/health").json()["extension_connected"] is False


def test_connector_rejects_wrong_pairing_proof() -> None:
    connector = BrowserConnectorApplication(host="127.0.0.1", port=8765, process_token="process-token")
    with TestClient(connector.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/browser/ws") as extension:
                extension.send_json({"type": "secure_hello", "protocol_version": 2, "instance_id": "inst_fake", "nonce": "00" * 32})
                assert extension.receive_json()["type"] == "secure_challenge"
                extension.send_json({"type": "secure_proof", "proof": "00" * 32})
                extension.receive_json()
        assert client.get("/health").json()["extension_connected"] is False


@pytest.mark.parametrize("attack", ["replay", "tamper"])
def test_connector_rejects_replayed_or_modified_frames(attack: str) -> None:
    connector = BrowserConnectorApplication(host="127.0.0.1", port=8765, process_token="process-token")
    with TestClient(connector.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/browser/ws") as raw:
                extension = SecureTestSocket(raw, connector.pairing_key, "inst_browser")
                extension.send_json({"type": "authenticate", "protocol_version": 1, "instance_id": "inst_browser", "instance_token": "t" * 48})
                assert extension.receive_json()["type"] == "authenticated"
                frame = extension.encode({"type": "heartbeat", "protocol_version": 1})
                if attack == "tamper":
                    data = bytearray(base64.b64decode(frame["data"]))
                    data[0] ^= 1
                    frame["data"] = base64.b64encode(data).decode()
                    raw.send_json(frame)
                else:
                    raw.send_json(frame)
                    raw.send_json(frame)
                raw.receive_json()
        assert client.get("/health").json()["extension_connected"] is False


@pytest.mark.parametrize(
    "origin", ["https://untrusted.example", "http://127.0.0.1:1234", "null", "chrome-extension://other"]
)
def test_connector_rejects_unapproved_websocket_origins(origin: str) -> None:
    connector = BrowserConnectorApplication(host="127.0.0.1", port=8765, process_token="process-token")
    with TestClient(connector.app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/v1/browser/ws", headers={"origin": origin}):
                pass
        assert error.value.code == 1008
        assert client.get("/health").json()["extension_connected"] is False


def test_connector_accepts_local_clients_without_origin() -> None:
    connector = BrowserConnectorApplication(host="127.0.0.1", port=8765, process_token="process-token")
    with TestClient(connector.app) as client, client.websocket_connect("/v1/browser/ws") as extension:
        extension = SecureTestSocket(extension, connector.pairing_key, "inst_local")
        extension.send_json({
            "type": "authenticate", "protocol_version": 1,
            "instance_id": "inst_local", "instance_token": "local-test-token-1234",
        })
        assert extension.receive_json()["type"] == "authenticated"


def test_connector_health_is_loopback_and_process_token_protected() -> None:
    connector = BrowserConnectorApplication(
        host="127.0.0.1",
        port=8765,
        process_token="process-token",
    )

    with TestClient(connector.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["extension_connected"] is False
        assert health.json()["pid"] == os.getpid()

        unauthorized = client.post(
            "/v1/browser/command",
            headers={"X-Asklear-Connector-Token": "wrong-token"},
            json={"operation": "navigate", "request_id": "req_test"},
        )
        assert unauthorized.status_code == 401


def test_connector_reports_extension_offline_without_exposing_page_data() -> None:
    connector = BrowserConnectorApplication(
        host="127.0.0.1",
        port=8765,
        process_token="process-token",
    )

    with TestClient(connector.app) as client:
        response = client.post(
            "/v1/browser/command",
            headers={"X-Asklear-Connector-Token": "process-token"},
            json={
                "operation": "navigate",
                "request_id": "req_test",
                "url": "https://example.com/private?token=do-not-echo",
                "visibility": "silent",
            },
        )

        assert response.status_code == 409
        assert response.json() == {
            "error": {
                "code": "browser_extension_offline",
                "message": "browser extension is offline",
            }
        }
        assert "do-not-echo" not in response.text


def test_one_extension_connection_serves_multiple_agent_commands() -> None:
    connector = BrowserConnectorApplication(
        host="127.0.0.1",
        port=8765,
        process_token="process-token",
    )

    with TestClient(connector.app) as client, client.websocket_connect(
        "/v1/browser/ws",
        headers={"origin": "chrome-extension://ankkcefnhidgdggjdbkefehhahgofebe"},
    ) as extension:
            extension = SecureTestSocket(extension, connector.pairing_key, "inst_browser")
            extension.send_json(
                {
                    "type": "authenticate",
                    "protocol_version": 1,
                    "instance_id": "inst_browser",
                    "instance_token": "bound-extension-token",
                }
            )
            assert extension.receive_json()["type"] == "authenticated"

            def call_agent(request_id: str):
                return client.post(
                    "/v1/browser/command",
                    headers={"X-Asklear-Connector-Token": "process-token"},
                    json={
                        "operation": "navigate",
                        "request_id": request_id,
                        "url": "https://example.com/",
                        "visibility": "silent",
                    },
                )

            with ThreadPoolExecutor(max_workers=1) as executor:
                first_future = executor.submit(call_agent, "req_agent_one")
                first_command = extension.receive_json()
                assert first_command["type"] == "command"
                assert first_command["operation"] == "navigate"
                extension.send_json(
                    {
                        "type": "response",
                        "request_id": first_command["request_id"],
                        "session_id": first_command["session_id"],
                        "ok": True,
                        "result": {
                            "operation": "navigate",
                            "session_id": first_command["session_id"],
                            "source": {"origin": "https://example.com", "path": "/"},
                        },
                    }
                )
                assert first_future.result().json()["session_id"] == first_command["session_id"]

                second_future = executor.submit(call_agent, "req_agent_two")
                second_command = extension.receive_json()
                extension.send_json(
                    {
                        "type": "response",
                        "request_id": second_command["request_id"],
                        "session_id": second_command["session_id"],
                        "ok": True,
                        "result": {
                            "operation": "navigate",
                            "session_id": second_command["session_id"],
                            "source": {"origin": "https://example.com", "path": "/"},
                        },
                    }
                )
                assert second_future.result().json()["session_id"] == second_command["session_id"]
                assert first_command["session_id"] != second_command["session_id"]
