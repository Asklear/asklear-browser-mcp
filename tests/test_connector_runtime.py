from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from asklear_browser_mcp.connector import BrowserConnectorApplication


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

    with TestClient(connector.app) as client:
        with client.websocket_connect(
            "/v1/browser/ws",
            headers={"origin": "chrome-extension://ankkcefnhidgdggjdbkefehhahgofebe"},
        ) as extension:
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
