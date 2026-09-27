"""Tests for Backend HTTP Control Plane Routes (§4-§13, §18)."""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.mock_adapter import MockAdapter
from app.domain.models import AgentCapabilities, Conversation, Message, Turn
from app.harness.coordinator import HarnessCoordinator
from app.harness.dispatcher import Dispatcher
from app.harness.mailbox import MailboxManager
from app.main import create_app
from app.store.sqlite_store import SQLiteStore


class SessionMockAdapter(MockAdapter):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._turn_sessions: dict[str, str] = {}

    def get_native_session(self, turn_id: str) -> str | None:
        return self._turn_sessions.get(turn_id)

    async def send(self, turn: Turn, message: Message, history: list[Message]) -> None:
        sess_id = turn.native_session_ref or f"sess_{turn.turn_id}"
        self._turn_sessions[turn.turn_id] = sess_id
        turn.native_session_ref = sess_id
        await super().send(turn, message, history)


@pytest.fixture
def app_env():
    """Setup an isolated test app with mock adapter and in-memory SQLite store."""
    store = SQLiteStore(":memory:")
    mbx = MailboxManager()
    coord = HarnessCoordinator(store, mbx)
    disp = Dispatcher(store, coord, mbx)

    adapter = SessionMockAdapter(
        capabilities=AgentCapabilities(
            supports_stream=True,
            supports_resume=True,
            supports_cancel=True,
            supports_approval=True,
            supports_parallel_sessions=True,
            steer_mode="native",
            branch_mode="replay",
        )
    )
    coord.register_adapter("kanaloa", adapter)

    app = create_app()
    app.state.store = store
    app.state.mailbox_manager = mbx
    app.state.coordinator = coord
    app.state.dispatcher = disp
    app.state.kanaloa_adapter = adapter

    yield app, store, coord, disp, adapter
    store.close()


@pytest.mark.asyncio
async def test_conversation_crud_and_initial_message_flow(app_env):
    """§4 & §5: POST/GET Conversation and initial message Send creates initial Turn."""
    app, store, coord, disp, adapter = app_env

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. POST /api/v1/conversations
        res = await client.post("/api/v1/conversations", json={"title": "My Research", "bound_agent_id": "kanaloa"})
        assert res.status_code == 200
        conv = res.json()
        assert conv["title"] == "My Research"
        assert conv["bound_agent_id"] == "kanaloa"
        conv_id = conv["conversation_id"]

        # 2. GET /api/v1/conversations
        res = await client.get("/api/v1/conversations")
        assert res.status_code == 200
        convs = res.json()
        assert len(convs) == 1
        assert convs[0]["conversation_id"] == conv_id

        # 3. GET /api/v1/conversations/{id}
        res = await client.get(f"/api/v1/conversations/{conv_id}")
        assert res.status_code == 200
        assert res.json()["conversation_id"] == conv_id

        # 4. GET 404 for unknown conversation
        res = await client.get("/api/v1/conversations/non_existent_conv")
        assert res.status_code == 404

        # 5. POST /api/v1/conversations/{id}/messages (Initial message creates Turn)
        res = await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "Hello agent"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["message"]["content"] == "Hello agent"
        assert body["message"]["sender"] == "user"
        assert body["turn"]["status"] == "running"
        turn_id = body["turn"]["turn_id"]

        # Verify adapter received send
        assert len(adapter.sent_calls) == 1
        assert adapter.sent_calls[0]["turn"].turn_id == turn_id

        # 6. GET /api/v1/conversations/{id}/messages
        res = await client.get(f"/api/v1/conversations/{conv_id}/messages")
        assert res.status_code == 200
        msgs = res.json()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello agent"


@pytest.mark.asyncio
async def test_new_task_and_focus_turn_apis(app_env):
    """§7 & §9: Explicit New Task (turns) and Focus Turn APIs."""
    app, store, coord, disp, adapter = app_env

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create conversation
        res = await client.post("/api/v1/conversations", json={"title": "Multi-turn Conv"})
        conv_id = res.json()["conversation_id"]

        # Initial turn via new turn API
        res = await client.post(f"/api/v1/conversations/{conv_id}/turns", json={"title": "Task Alpha"})
        assert res.status_code == 200
        turn_a = res.json()
        assert turn_a["title"] == "Task Alpha"
        assert turn_a["conversation_id"] == conv_id
        turn_a_id = turn_a["turn_id"]

        # Create second turn via new turn API
        res = await client.post(f"/api/v1/conversations/{conv_id}/turns", json={"title": "Task Beta"})
        assert res.status_code == 200
        turn_b = res.json()
        assert turn_b["title"] == "Task Beta"
        turn_b_id = turn_b["turn_id"]

        # List turns
        res = await client.get(f"/api/v1/conversations/{conv_id}/turns")
        assert res.status_code == 200
        turns = res.json()
        assert len(turns) == 2
        assert {t["turn_id"] for t in turns} == {turn_a_id, turn_b_id}

        # Query single turn
        res = await client.get(f"/api/v1/turns/{turn_a_id}")
        assert res.status_code == 200
        assert res.json()["turn_id"] == turn_a_id

        # Query unknown turn -> 404
        res = await client.get("/api/v1/turns/unknown_turn_999")
        assert res.status_code == 404

        # Switch focus back to Turn A
        res = await client.post(f"/api/v1/conversations/{conv_id}/focus", json={"turn_id": turn_a_id})
        assert res.status_code == 200
        assert res.json() == {"conversation_id": conv_id, "focus_turn_id": turn_a_id}

        # Verify conversation focus updated
        conv = store.get_conversation(conv_id)
        assert conv.focus_turn_id == turn_a_id

        # Invalid focus turn -> 400
        res = await client.post(f"/api/v1/conversations/{conv_id}/focus", json={"turn_id": "non_existent"})
        assert res.status_code == 400


@pytest.mark.asyncio
async def test_branch_creation_api(app_env):
    """§10: POST /api/v1/conversations/{id}/branches returns minimal Branch DTO."""
    app, store, coord, disp, adapter = app_env

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create conversation and first message
        res = await client.post("/api/v1/conversations", json={"title": "Branch Test Conv"})
        conv_id = res.json()["conversation_id"]

        res = await client.post(f"/api/v1/conversations/{conv_id}/messages", json={"content": "Msg 1"})
        msg1_id = res.json()["message"]["message_id"]

        # Branch from Msg 1
        res = await client.post(
            f"/api/v1/conversations/{conv_id}/branches",
            json={"message_id": msg1_id, "name": "Feature Branch"},
        )
        assert res.status_code == 200
        dto = res.json()
        assert dto["conversation_id"] == conv_id
        assert dto["branch_point_message_id"] == msg1_id
        assert "branch_id" in dto
        assert "initial_turn_id" in dto

        # Branch Turn exists in store
        branch_turn = store.get_turn(dto["initial_turn_id"])
        assert branch_turn is not None
        assert branch_turn.branch_id == dto["branch_id"]

        # Unknown message -> 400
        res = await client.post(
            f"/api/v1/conversations/{conv_id}/branches",
            json={"message_id": "invalid_msg_id"},
        )
        assert res.status_code == 400


@pytest.mark.asyncio
async def test_turn_controls_cancel_resume_stop_loop(app_env):
    """§11: Cancel, Resume, and Stop Loop endpoints."""
    app, store, coord, disp, adapter = app_env

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create conversation and turn
        conv = Conversation(conversation_id="conv_ctrl", bound_agent_id="kanaloa")
        store.save_conversation(conv)
        turn = Turn(turn_id="turn_ctrl", conversation_id="conv_ctrl", bound_agent_id="kanaloa", status="running")
        store.save_turn(turn)

        # 1. Cancel
        res = await client.post(f"/api/v1/turns/{turn.turn_id}/cancel")
        assert res.status_code == 200
        assert res.json()["status"] == "interrupted"
        assert len(adapter.cancel_calls) == 1

        # 2. Resume
        res = await client.post(f"/api/v1/turns/{turn.turn_id}/resume")
        assert res.status_code == 200
        assert res.json()["status"] == "running"
        assert len(adapter.resume_calls) == 1

        # 3. Stop Loop
        res = await client.post(f"/api/v1/turns/{turn.turn_id}/stop-loop")
        assert res.status_code == 200
        assert len(adapter.stop_loop_calls) == 1

        # 4. Unknown turn -> 404
        res = await client.post("/api/v1/turns/unknown_turn_ctrl/cancel")
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_approval_respond_permission_endpoint(app_env):
    """§12: Approval response endpoint with session and error enforcement."""
    app, store, coord, disp, adapter = app_env

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conv = Conversation(conversation_id="c_appr", bound_agent_id="kanaloa")
        store.save_conversation(conv)
        turn = Turn(turn_id="t_appr", conversation_id="c_appr", bound_agent_id="kanaloa", status="waiting_user", native_session_ref="sess_appr")
        store.save_turn(turn)

        # Agent requests permission
        await adapter.simulate_permission_request(
            turn_id="t_appr",
            request_id="perm_101",
            tool_name="bash_exec",
            session_id="sess_appr",
        )
        assert store.get_turn("t_appr").status == "waiting_user"

        # 1. Resolve permission
        res = await client.post(
            "/api/v1/turns/t_appr/permissions/perm_101/respond",
            json={"decision": "allow-once"},
        )
        assert res.status_code == 200
        assert res.json() == {
            "turn_id": "t_appr",
            "request_id": "perm_101",
            "decision": "allow-once",
            "status": "resolved",
        }
        # Turn transitioned to running via emit_resumed
        assert store.get_turn("t_appr").status == "running"

        # 2. Duplicate response -> 400 Unknown permission request
        res = await client.post(
            "/api/v1/turns/t_appr/permissions/perm_101/respond",
            json={"decision": "allow-once"},
        )
        assert res.status_code == 400

        # 3. Invalid decision value -> 400
        await adapter.simulate_permission_request(turn_id="t_appr", request_id="perm_102", tool_name="fs_write", session_id="sess_appr")
        res = await client.post(
            "/api/v1/turns/t_appr/permissions/perm_102/respond",
            json={"decision": "invalid-option"},
        )
        assert res.status_code == 400


@pytest.mark.asyncio
async def test_agent_capability_discovery_api(app_env):
    """§13: GET /api/v1/agents returns truthful capability projection."""
    app, store, coord, disp, adapter = app_env

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res = await client.get("/api/v1/agents")
        assert res.status_code == 200
        agents = res.json()
        assert len(agents) == 1
        agent = agents[0]
        assert agent["agent_id"] == "kanaloa"
        assert agent["supports_stream"] is True
        assert agent["supports_resume"] is True
        assert agent["supports_cancel"] is True
        assert agent["supports_approval"] is True
        assert agent["branch_mode"] == "replay"
        assert agent["steer_mode"] == "native"


@pytest.mark.asyncio
async def test_error_mapping_discipline(app_env):
    """§18: Error mapping discipline: 400 for target_turn_required, 404 for missing objects, 400 for unsupported caps."""
    app, store, coord, disp, adapter = app_env

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. 404 on missing conversation
        res = await client.post("/api/v1/conversations/non_existent_id/messages", json={"content": "Hi"})
        assert res.status_code == 404

        # 2. Setup conversation with 2 turns but unset focus_turn_id
        conv = Conversation(conversation_id="c_ambig", bound_agent_id="kanaloa", focus_turn_id=None)
        store.save_conversation(conv)
        t1 = Turn(turn_id="t1", conversation_id="c_ambig", bound_agent_id="kanaloa", status="running")
        t2 = Turn(turn_id="t2", conversation_id="c_ambig", bound_agent_id="kanaloa", status="running")
        store.save_turn(t1)
        store.save_turn(t2)

        # Sending message without explicit turn_id -> 400 target_turn_required
        res = await client.post("/api/v1/conversations/c_ambig/messages", json={"content": "Ambiguous message"})
        assert res.status_code == 400
        assert "target_turn_required" in res.json()["detail"]

        # 3. Unsupported cancel capability -> 400
        adapter.set_capabilities(AgentCapabilities(supports_cancel=False, supports_resume=False))
        res = await client.post("/api/v1/turns/t1/cancel")
        assert res.status_code == 400
        assert "does not support cancellation" in res.json()["detail"]

        # 4. Unsupported resume capability -> 400
        res = await client.post("/api/v1/turns/t1/resume")
        assert res.status_code == 400
        assert "does not support native resume" in res.json()["detail"]

        # 5. Missing turn on controls -> 404
        res = await client.post("/api/v1/turns/ghost_turn/cancel")
        assert res.status_code == 404
        res = await client.post("/api/v1/turns/ghost_turn/resume")
        assert res.status_code == 404
        res = await client.post("/api/v1/turns/ghost_turn/stop-loop")
        assert res.status_code == 404
        res = await client.post("/api/v1/turns/ghost_turn/permissions/req1/respond", json={"decision": "allow-once"})
        assert res.status_code == 404
