from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.adapters.connector_adapter import ConnectorAdapter
from app.domain.models import AgentCapabilities, Conversation, Message, Turn
from app.harness.coordinator import HarnessCoordinator
from app.harness.mailbox import MailboxManager
from app.store.sqlite_store import SQLiteStore


class FakeSocket:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


def test_agent_settings_disconnect_revoke_remove_and_preserve_history():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from app.main import create_app

    store = SQLiteStore(":memory:")
    app = create_app()
    app.state.store = store
    with TestClient(app) as client:
        url = "/api/v1/agents/settings-test"
        first = client.post("/api/v1/agents/pairings", json={"agent_id": "settings-test", "display_name": "Before"}).json()
        second = client.post("/api/v1/agents/pairings", json={"agent_id": "settings-test", "display_name": "Before"}).json()
        assert second["expires_in_seconds"] is None
        assert len(app.state.connector_pairings) == 1
        assert all("expires_at" not in item for item in app.state.connector_pairings.values())
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": f"Pairing {first['pairing_code']}"}):
                pass
        hello = {"protocol": "kane-connector", "version": "0.1", "type": "connector.hello", "payload": {"agent_id": "settings-test", "connector_id": "settings-install", "capabilities": {}, "sessions": []}}
        with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": f"Pairing {second['pairing_code']}"}) as socket:
            socket.send_json(hello)
            token = socket.receive_json()["payload"]["connector_token"]
            assert client.patch(url, json={"display_name": "After"}).status_code == 200
            assert store.get_agent_binding("settings-test").display_name == "After"
            assert client.patch(url, json={"display_name": " ", "agent_id": "different"}).status_code == 422
            conversation = Conversation(bound_agent_id="settings-test")
            store.save_conversation(conversation)
            turn = Turn(conversation_id=conversation.conversation_id, bound_agent_id="settings-test")
            store.save_turn(turn)
            assert client.delete(url).status_code == 409
            assert client.post(url + "/disconnect").status_code == 204
            assert store.get_turn(turn.turn_id).status == "interrupted"
            assert not store.get_agent_binding("settings-test").is_active
            assert "connector_token_sha256" not in store.get_agent_binding("settings-test").config
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": f"Bearer {token}"}):
                pass
        assert client.delete(url).status_code == 204
        assert store.get_agent_binding("settings-test") is None
        assert store.get_conversation(conversation.conversation_id) == conversation
        assert not any(item["agent_id"] == "settings-test" for item in client.get("/api/v1/agents").json())
        assert client.delete("/api/v1/agents/kanaloa").status_code == 409
    store.close()


@pytest.mark.asyncio
async def test_connector_command_stream_completion_and_duplicate_event_are_idempotent():
    store = SQLiteStore(":memory:")
    coordinator = HarnessCoordinator(store, MailboxManager())
    adapter = ConnectorAdapter("agent-a", AgentCapabilities(), coordinator)
    socket = FakeSocket()
    await adapter.attach(socket, AgentCapabilities(), [])

    conversation = Conversation(bound_agent_id="agent-a")
    turn = Turn(conversation_id=conversation.conversation_id, bound_agent_id="agent-a")
    user_message = Message(conversation_id=conversation.conversation_id, turn_id=turn.turn_id, sender="user", content="hello")
    store.save_conversation(conversation)
    store.save_turn(turn)
    store.append_message(user_message)

    sending = asyncio.create_task(adapter.send(turn, user_message, []))
    await asyncio.sleep(0)
    command = socket.frames[0]
    assert command["type"] == "turn.send"
    await adapter.handle_frame({
        "type": "command.result",
        "request_id": command["request_id"],
        "payload": {"status": "accepted"},
    })
    await sending

    await adapter.handle_frame({
        "type": "reply.delta", "event_id": "evt-1",
        "payload": {"turn_id": turn.turn_id, "reply_id": "reply-1", "text": "PONG"},
    })
    completed = {
        "type": "reply.completed", "event_id": "evt-2",
        "payload": {"turn_id": turn.turn_id, "reply_id": "reply-1", "turn_finished": True},
    }
    await adapter.handle_frame(completed)
    await adapter.handle_frame(completed)

    restarted_coordinator = HarnessCoordinator(store, MailboxManager())
    restarted_adapter = ConnectorAdapter("agent-a", AgentCapabilities(), restarted_coordinator)
    await restarted_adapter.handle_frame({
        **completed,
        "event_id": "evt-duplicate-after-restart",
    })

    saved = store.get_messages(conversation.conversation_id)
    assert [(item.sender, item.content) for item in saved] == [("user", "hello"), ("agent", "PONG")]
    assert store.get_turn(turn.turn_id).status == "finished"
    await adapter.detach()
    assert not adapter.is_alive()
    store.close()


@pytest.mark.asyncio
async def test_connector_disconnect_preserves_partial_output_and_interrupts_turn():
    store = SQLiteStore(":memory:")
    coordinator = HarnessCoordinator(store, MailboxManager())
    adapter = ConnectorAdapter("agent-a", AgentCapabilities(), coordinator)
    conversation = Conversation(bound_agent_id="agent-a")
    turn = Turn(conversation_id=conversation.conversation_id, bound_agent_id="agent-a")
    store.save_conversation(conversation)
    store.save_turn(turn)

    await adapter.handle_frame({
        "type": "reply.delta",
        "event_id": "delta-before-disconnect",
        "payload": {"turn_id": turn.turn_id, "reply_id": "reply-1", "text": "partial"},
    })
    await coordinator.emit_interrupted(turn.turn_id, "connector_disconnected")

    saved = store.get_turn(turn.turn_id)
    assert saved.status == "interrupted"
    assert saved.partial_output == "partial"
    assert store.get_messages(conversation.conversation_id) == []
    store.close()


def test_pairing_code_becomes_scoped_reconnect_credential_and_identity_survives_disconnect():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.store.sqlite_store import SQLiteStore

    store = SQLiteStore(":memory:")
    app = create_app()
    app.state.store = store
    client = TestClient(app)
    hello = {
        "protocol": "kane-connector",
        "version": "0.1",
        "type": "connector.hello",
        "id": "hello-1",
        "payload": {
            "connector_id": "install-1",
            "agent_id": "wire-agent-test",
            "display_name": "Wire Agent Test",
            "capabilities": {"supports_stream": True},
            "sessions": [],
        },
    }

    with client:
        created = client.post("/api/v1/agents/pairings", json={"agent_id": "wire-agent-test", "display_name": "Wire Agent Test"})
        assert created.status_code == 200
        code = created.json()["pairing_code"]
        with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": f"Pairing {code}"}) as socket:
            socket.send_json(hello)
            ready = socket.receive_json()
            assert ready["type"] == "connector.ready"
            token = ready["payload"]["connector_token"]
            assert token
            assert next(item for item in client.get("/api/v1/agents").json() if item["agent_id"] == "wire-agent-test")["status"] == "ready"
        binding = store.get_agent_binding("wire-agent-test")
        assert binding is not None
        assert binding.config["connector_id"] == "install-1"
        assert binding.config["connector_token_sha256"]
        assert token not in str(binding.config)

        assert any(item["agent_id"] == "wire-agent-test" for item in client.get("/api/v1/agents").json())
        with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": f"Bearer {token}"}) as socket:
            socket.send_json(hello)
            assert socket.receive_json()["type"] == "connector.ready"
            socket.send_json({
                "protocol": "kane-connector", "version": "0.1", "type": "agent.availability",
                "id": "availability-1", "event_id": "availability-event-1",
                "payload": {"agent_id": "wire-agent-test", "status": "ready"},
            })
            assert socket.receive_json()["type"] == "event.ack"

        agents = client.get("/api/v1/agents").json()
        paired = next(item for item in agents if item["agent_id"] == "wire-agent-test")
        assert paired["status"] == "unavailable"
        assert paired["display_name"] == "Wire Agent Test"
    store.close()


def test_http_message_round_trips_over_paired_connector_and_persists_one_reply():
    from fastapi.testclient import TestClient

    from app.main import create_app

    store = SQLiteStore(":memory:")
    app = create_app()
    app.state.store = store
    client = TestClient(app)
    with client:
        pairing = client.post("/api/v1/agents/pairings", json={"agent_id": "wire-agent", "display_name": "Wire Agent"}).json()
        hello = {
            "protocol": "kane-connector",
            "version": "0.1",
            "type": "connector.hello",
            "id": "hello-wire",
            "payload": {
                "connector_id": "wire-install",
                "agent_id": "wire-agent",
                "display_name": "Wire Agent",
                "capabilities": {"supports_stream": True},
                "sessions": [],
            },
        }
        with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": f"Pairing {pairing['pairing_code']}"}) as socket:
            socket.send_json(hello)
            assert socket.receive_json()["type"] == "connector.ready"
            conversation = client.post("/api/v1/conversations", json={"title": "Wire test", "bound_agent_id": "wire-agent"}).json()
            with ThreadPoolExecutor(max_workers=1) as executor:
                sending = executor.submit(
                    client.post,
                    f"/api/v1/conversations/{conversation['conversation_id']}/messages",
                    json={"content": "hello over Kane"},
                )
                command = socket.receive_json()
                assert command["type"] == "turn.send"
                assert command["payload"]["content"] == "hello over Kane"
                socket.send_json({
                    "protocol": "kane-connector", "version": "0.1", "type": "command.result",
                    "id": "ack-send", "request_id": command["request_id"], "payload": {"status": "accepted"},
                })
                result = sending.result(timeout=5)
                assert result.status_code == 200
                turn_id = result.json()["turn"]["turn_id"]
                socket.send_json({
                    "protocol": "kane-connector", "version": "0.1", "type": "reply.delta",
                    "id": "delta-wire", "event_id": "evt-delta-wire",
                    "payload": {"conversation_id": conversation["conversation_id"], "turn_id": turn_id, "reply_id": "reply-wire", "text": "PONG"},
                })
                assert socket.receive_json()["type"] == "event.ack"
                socket.send_json({
                    "protocol": "kane-connector", "version": "0.1", "type": "reply.completed",
                    "id": "complete-wire", "event_id": "evt-complete-wire",
                    "payload": {"conversation_id": conversation["conversation_id"], "turn_id": turn_id, "reply_id": "reply-wire", "content": "PONG", "turn_finished": True},
                })
                assert socket.receive_json()["type"] == "event.ack"
                messages = client.get(f"/api/v1/conversations/{conversation['conversation_id']}/messages").json()
                assert [(item["sender"], item["content"]) for item in messages] == [
                    ("user", "hello over Kane"), ("agent", "PONG")
                ]
                assert client.get(f"/api/v1/turns/{turn_id}").json()["status"] == "finished"
    store.close()
