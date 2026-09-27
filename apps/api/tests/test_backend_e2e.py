"""Backend Black-box HTTP / SSE E2E Verification Suites (§24-§30)."""

from __future__ import annotations

import asyncio
import json
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


def parse_sse_events(raw_text: str) -> list[tuple[str, dict]]:
    events = []
    lines = raw_text.strip().split("\n")
    current_event = None
    for line in lines:
        line = line.strip()
        if line.startswith("event:"):
            current_event = line.replace("event:", "").strip()
        elif line.startswith("data:") and current_event:
            data_str = line.replace("data:", "").strip()
            try:
                data = json.loads(data_str)
                events.append((current_event, data))
            except json.JSONDecodeError:
                pass
            current_event = None
    return events


# ==============================================================================
# 1. Section 24: Core Send, Stream, Steer, and ONE Message Finalization E2E
# ==============================================================================
@pytest.mark.asyncio
async def test_e2e_core_send_stream_steer_and_single_message_finalization():
    """
    §24:
    POST Conversation
    ↓
    POST first Message
    ↓
    Initial Turn created by existing Dispatcher
    ↓
    SSE receives live delta/events
    ↓
    another Send while Turn running -> native steer degradation path
    ↓
    completion -> exactly ONE persistent Agent Message in Store
    ↓
    GET history returns correct result
    """
    store = SQLiteStore(":memory:")
    mbx = MailboxManager()
    coord = HarnessCoordinator(store, mbx)
    disp = Dispatcher(store, coord, mbx)
    adapter = SessionMockAdapter(
        capabilities=AgentCapabilities(
            supports_stream=True,
            supports_resume=True,
            supports_cancel=True,
            steer_mode="native",
        )
    )
    coord.register_adapter("kanaloa", adapter)

    app = create_app()
    app.state.store = store
    app.state.mailbox_manager = mbx
    app.state.coordinator = coord
    app.state.dispatcher = disp
    app.state.kanaloa_adapter = adapter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. POST Conversation
        res = await client.post("/api/v1/conversations", json={"title": "E2E Research"})
        assert res.status_code == 200
        conv_id = res.json()["conversation_id"]

        # 2. POST first Message -> initial turn auto-created
        res = await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "Analyze dataset"},
        )
        assert res.status_code == 200
        turn_id = res.json()["turn"]["turn_id"]
        assert res.json()["turn"]["status"] == "running"

        # 3. Simulate Agent streaming deltas and handling in-flight steer
        async def agent_workflow():
            # Wait for client to connect to stream
            while turn_id not in coord._listeners:
                await asyncio.sleep(0.01)
            await coord.emit_delta(turn_id, "Analyzing chunk A. ")
            await asyncio.sleep(0.05)
            await coord.emit_delta(turn_id, "Chunk B done. ")
            await asyncio.sleep(0.05)
            await coord.emit_message_complete(turn_id)

        workflow_task = asyncio.create_task(agent_workflow())

        # Mid-flight Send while Turn is running -> Steer degradation
        async def steer_workflow():
            while turn_id not in coord._listeners:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.03)
            res_steer = await client.post(
                f"/api/v1/conversations/{conv_id}/messages",
                json={"content": "Focus on 2026 data only", "turn_id": turn_id},
            )
            assert res_steer.status_code == 200
            assert len(adapter.steer_calls) == 1
            assert adapter.steer_calls[0]["message"].content == "Focus on 2026 data only"

        steer_task = asyncio.create_task(steer_workflow())

        # Stream SSE until completion
        lines = []
        async with client.stream("GET", f"/api/v1/turns/{turn_id}/stream") as stream_resp:
            assert stream_resp.status_code == 200
            async for line in stream_resp.aiter_lines():
                if line:
                    lines.append(line)
                if "finished" in line:
                    break

        await workflow_task
        await steer_task

        # Parse SSE
        events = parse_sse_events("\n".join(lines))
        deltas = [e[1]["payload"]["delta"] for e in events if e[0] == "delta"]
        assert "Analyzing chunk A. " in deltas
        assert "Chunk B done. " in deltas

        # Verification of Store: EXACTLY ONE permanent Agent Message in store for this reply (§15 & §22)
        all_msgs = store.get_messages(conv_id)
        agent_msgs = [m for m in all_msgs if m.sender == "agent"]
        assert len(agent_msgs) == 1
        assert agent_msgs[0].content == "Analyzing chunk A. Chunk B done. "

        # GET history returns correct result
        res_hist = await client.get(f"/api/v1/conversations/{conv_id}/messages")
        assert res_hist.status_code == 200
        hist = res_hist.json()
        assert len(hist) == 3  # User msg 1, User steer msg, Agent final reply
        assert hist[0]["content"] == "Analyze dataset"
        assert hist[1]["content"] == "Focus on 2026 data only"
        assert hist[2]["content"] == "Analyzing chunk A. Chunk B done. "


# ==============================================================================
# 2. Section 25: Multi-task HTTP E2E
# ==============================================================================
@pytest.mark.asyncio
async def test_e2e_multitask_isolation():
    """
    §25:
    Conversation
    ↓
    Turn A running
    ↓
    POST New Task -> Turn B
    ↓
    A/B independent
    ↓
    target Send to A -> B unchanged
    ↓
    cancel B -> A unchanged
    """
    store = SQLiteStore(":memory:")
    mbx = MailboxManager()
    coord = HarnessCoordinator(store, mbx)
    disp = Dispatcher(store, coord, mbx)
    adapter = SessionMockAdapter()
    coord.register_adapter("kanaloa", adapter)

    app = create_app()
    app.state.store = store
    app.state.mailbox_manager = mbx
    app.state.coordinator = coord
    app.state.dispatcher = disp
    app.state.kanaloa_adapter = adapter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create conversation
        res = await client.post("/api/v1/conversations", json={"title": "Parallel Conv"})
        conv_id = res.json()["conversation_id"]

        # Initial message -> Turn A
        res = await client.post(f"/api/v1/conversations/{conv_id}/messages", json={"content": "Task A prompt"})
        turn_a_id = res.json()["turn"]["turn_id"]

        # POST New Task -> Turn B
        res = await client.post(f"/api/v1/conversations/{conv_id}/turns", json={"title": "Parallel Task B"})
        assert res.status_code == 200
        turn_b_id = res.json()["turn_id"]
        assert turn_a_id != turn_b_id

        # Send to Turn B
        await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "Task B prompt", "turn_id": turn_b_id},
        )

        # Independent session mapping & isolation:
        assert turn_a_id in adapter._turn_sessions
        assert turn_b_id in adapter._turn_sessions
        assert adapter._turn_sessions[turn_a_id] != adapter._turn_sessions[turn_b_id]

        # Targeted steer to A -> B unchanged
        await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "Steer for A", "turn_id": turn_a_id},
        )
        assert len(adapter.steer_calls) == 1
        assert adapter.steer_calls[0]["turn"].turn_id == turn_a_id
        assert store.get_turn(turn_b_id).status == "running"

        # Cancel B -> A remains running and unaffected
        res_cancel = await client.post(f"/api/v1/turns/{turn_b_id}/cancel")
        assert res_cancel.status_code == 200
        assert store.get_turn(turn_b_id).status == "interrupted"
        assert store.get_turn(turn_a_id).status == "running"


# ==============================================================================
# 3. Section 26: Branch HTTP E2E
# ==============================================================================
@pytest.mark.asyncio
async def test_e2e_branch_history_isolation():
    """
    §26:
    M1 → M2 → M3 → M4
             ↓
    POST Branch from M2
             ↓
    Branch B
             ↓
    Send new branch message
    Verify:
    - same conversation_id
    - distinct branch_id
    - isolated Turn/session
    - Branch sees M1/M2 + branch messages
    - Branch does NOT see M3/M4
    - Main does NOT see branch messages
    """
    store = SQLiteStore(":memory:")
    mbx = MailboxManager()
    coord = HarnessCoordinator(store, mbx)
    disp = Dispatcher(store, coord, mbx)
    adapter = SessionMockAdapter()
    coord.register_adapter("kanaloa", adapter)

    app = create_app()
    app.state.store = store
    app.state.mailbox_manager = mbx
    app.state.coordinator = coord
    app.state.dispatcher = disp
    app.state.kanaloa_adapter = adapter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create conversation
        res = await client.post("/api/v1/conversations", json={"title": "Branch Worldline"})
        conv_id = res.json()["conversation_id"]

        # M1 on main
        res = await client.post(f"/api/v1/conversations/{conv_id}/messages", json={"content": "M1"})
        m1_id = res.json()["message"]["message_id"]
        main_turn_id = res.json()["turn"]["turn_id"]
        await coord.emit_message_complete(main_turn_id, content="Agent reply to M1")

        # M2 on main
        res = await client.post(f"/api/v1/conversations/{conv_id}/messages", json={"content": "M2", "turn_id": main_turn_id})
        m2_id = res.json()["message"]["message_id"]
        await coord.emit_message_complete(main_turn_id, content="Agent reply to M2")

        # M3 on main
        res = await client.post(f"/api/v1/conversations/{conv_id}/messages", json={"content": "M3", "turn_id": main_turn_id})
        m3_id = res.json()["message"]["message_id"]
        await coord.emit_message_complete(main_turn_id, content="Agent reply to M3")

        # M4 on main
        res = await client.post(f"/api/v1/conversations/{conv_id}/messages", json={"content": "M4", "turn_id": main_turn_id})
        m4_id = res.json()["message"]["message_id"]
        await coord.emit_message_complete(main_turn_id, content="Agent reply to M4")

        # POST Branch from M2
        res_branch = await client.post(
            f"/api/v1/conversations/{conv_id}/branches",
            json={"message_id": m2_id, "name": "Alternative Exploration"},
        )
        assert res_branch.status_code == 200
        branch_dto = res_branch.json()
        assert branch_dto["conversation_id"] == conv_id
        assert branch_dto["branch_point_message_id"] == m2_id
        branch_id = branch_dto["branch_id"]
        branch_turn_id = branch_dto["initial_turn_id"]

        # Send new message on Branch
        res_b_msg = await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "Branch Explore Prompt", "turn_id": branch_turn_id},
        )
        assert res_b_msg.status_code == 200

        # Check turn history visible to Branch turn:
        branch_turn = store.get_turn(branch_turn_id)
        visible_to_branch = disp.get_turn_history(branch_turn)
        branch_msg_contents = [m.content for m in visible_to_branch]

        # Branch sees M1, reply to M1, M2, and branch message
        assert "M1" in branch_msg_contents
        assert "M2" in branch_msg_contents
        assert "Branch Explore Prompt" in branch_msg_contents

        # Branch DOES NOT see M3 or M4
        assert "M3" not in branch_msg_contents
        assert "M4" not in branch_msg_contents

        # Main turn DOES NOT see branch messages
        main_turn = store.get_turn(main_turn_id)
        visible_to_main = disp.get_turn_history(main_turn)
        main_msg_contents = [m.content for m in visible_to_main]
        assert "M1" in main_msg_contents
        assert "M2" in main_msg_contents
        assert "M3" in main_msg_contents
        assert "M4" in main_msg_contents
        assert "Branch Explore Prompt" not in main_msg_contents


# ==============================================================================
# 4. Section 27: Approval HTTP/SSE E2E
# ==============================================================================
@pytest.mark.asyncio
async def test_e2e_approval_roundtrip_and_safety():
    """
    §27:
    Agent requests permission
    ↓
    SSE exposes waiting_user/approval fact
    ↓
    POST permission response
    ↓
    existing Adapter validates request/session
    ↓
    emit_resumed
    ↓
    Turn running
    ↓
    Agent continues
    """
    store = SQLiteStore(":memory:")
    mbx = MailboxManager()
    coord = HarnessCoordinator(store, mbx)
    disp = Dispatcher(store, coord, mbx)
    adapter = SessionMockAdapter()
    coord.register_adapter("kanaloa", adapter)

    app = create_app()
    app.state.store = store
    app.state.mailbox_manager = mbx
    app.state.coordinator = coord
    app.state.dispatcher = disp
    app.state.kanaloa_adapter = adapter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        conv = Conversation(conversation_id="c_appr_e2e", bound_agent_id="kanaloa")
        store.save_conversation(conv)
        turn = Turn(
            turn_id="t_appr_e2e",
            conversation_id="c_appr_e2e",
            bound_agent_id="kanaloa",
            status="running",
            native_session_ref="sess_safe_99",
        )
        store.save_turn(turn)

        # Background task simulates permission request then response continuation
        async def agent_flow():
            await asyncio.sleep(0.05)
            await adapter.simulate_permission_request(
                turn_id="t_appr_e2e",
                request_id="perm_req_77",
                tool_name="rm -rf /tmp/data",
                session_id="sess_safe_99",
            )
            # Wait until resumed
            while store.get_turn("t_appr_e2e").status == "waiting_user":
                await asyncio.sleep(0.02)
            # Emit continuation output after approval
            await coord.emit_delta("t_appr_e2e", "Action approved, completed successfully.")
            await coord.emit_message_complete("t_appr_e2e")

        agent_task = asyncio.create_task(agent_flow())

        # Wait for waiting_user status in store
        while store.get_turn("t_appr_e2e").status != "waiting_user":
            await asyncio.sleep(0.02)

        # 1. POST Approval response
        res_resp = await client.post(
            "/api/v1/turns/t_appr_e2e/permissions/perm_req_77/respond",
            json={"decision": "allow-once"},
        )
        assert res_resp.status_code == 200
        assert res_resp.json()["status"] == "resolved"

        await agent_task

        # Verify completed cleanly
        assert store.get_turn("t_appr_e2e").status == "finished"
        msgs = store.get_messages("c_appr_e2e")
        assert len(msgs) == 1
        assert msgs[0].content == "Action approved, completed successfully."

        # Stale request fails closed
        res_stale = await client.post(
            "/api/v1/turns/t_appr_e2e/permissions/perm_req_77/respond",
            json={"decision": "allow-once"},
        )
        assert res_stale.status_code == 400


# ==============================================================================
# 5. Section 28: Recovery Backend Lifespan E2E
# ==============================================================================
@pytest.mark.asyncio
async def test_e2e_startup_reconciliation_lifespan():
    """
    §28:
    persisted running Turn exists before app startup
    ↓
    app lifespan starts
    ↓
    reconcile_startup_turns()
    ↓
    recoverable → remains running
    unrecoverable → transitions to interrupted
    ↓
    THEN requests are served
    partial_output preserved, no blind rerun
    """
    store = SQLiteStore(":memory:")
    mbx = MailboxManager()
    coord = HarnessCoordinator(store, mbx)
    disp = Dispatcher(store, coord, mbx)
    adapter = SessionMockAdapter()
    coord.register_adapter("kanaloa", adapter)

    # Pre-populate store with turns before app starts:
    conv = Conversation(conversation_id="c_rec", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    # 1. Recoverable turn (session is live in adapter)
    t_rec = Turn(
        turn_id="t_live",
        conversation_id="c_rec",
        bound_agent_id="kanaloa",
        status="running",
        native_session_ref="sess_live",
        partial_output="Partial work done before restart. ",
    )
    store.save_turn(t_rec)
    adapter.live_sessions.add("sess_live")

    # 2. Unrecoverable turn (session was lost in crash)
    t_lost = Turn(
        turn_id="t_lost",
        conversation_id="c_rec",
        bound_agent_id="kanaloa",
        status="running",
        native_session_ref="sess_dead",
        partial_output="Dead work preserved. ",
    )
    store.save_turn(t_lost)

    app = create_app()
    app.state.store = store
    app.state.mailbox_manager = mbx
    app.state.coordinator = coord
    app.state.dispatcher = disp
    app.state.kanaloa_adapter = adapter

    # When app starts with lifespan:
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Recoverable turn remains running and partial_output is preserved
            res_live = await client.get("/api/v1/turns/t_live")
            assert res_live.status_code == 200
            live_turn = res_live.json()
            assert live_turn["status"] == "running"
            assert live_turn["partial_output"] == "Partial work done before restart. "

            # 2. Unrecoverable turn is marked interrupted and partial_output is preserved (§31)
            res_lost = await client.get("/api/v1/turns/t_lost")
            assert res_lost.status_code == 200
            lost_turn = res_lost.json()
            assert lost_turn["status"] == "interrupted"
            assert lost_turn["interrupt_reason"] == "unrecoverable:session_lost_on_startup"
            assert lost_turn["partial_output"] == "Dead work preserved. "


# ==============================================================================
# 6. Section 30: Long-running Conversation Usability
# ==============================================================================
@pytest.mark.asyncio
async def test_e2e_long_running_conversation_usability():
    """
    §30:
    Turn A running
    ↓
    GET Conversation works
    GET history works
    GET Turns works
    New Task works
    targeted Send works
    Branch works
    SSE works
    Conversation is NEVER locked while Agent is actively working.
    """
    store = SQLiteStore(":memory:")
    mbx = MailboxManager()
    coord = HarnessCoordinator(store, mbx)
    disp = Dispatcher(store, coord, mbx)
    adapter = SessionMockAdapter()
    coord.register_adapter("kanaloa", adapter)

    app = create_app()
    app.state.store = store
    app.state.mailbox_manager = mbx
    app.state.coordinator = coord
    app.state.dispatcher = disp
    app.state.kanaloa_adapter = adapter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create conversation
        res = await client.post("/api/v1/conversations", json={"title": "Long Running Conv"})
        conv_id = res.json()["conversation_id"]

        # Start Turn A (running)
        res = await client.post(f"/api/v1/conversations/{conv_id}/messages", json={"content": "Long task prompt"})
        turn_a_id = res.json()["turn"]["turn_id"]
        assert store.get_turn(turn_a_id).status == "running"

        # While Turn A is running:
        # 1. GET Conversation works
        res_conv = await client.get(f"/api/v1/conversations/{conv_id}")
        assert res_conv.status_code == 200

        # 2. GET messages works
        res_msgs = await client.get(f"/api/v1/conversations/{conv_id}/messages")
        assert res_msgs.status_code == 200

        # 3. GET turns works
        res_turns = await client.get(f"/api/v1/conversations/{conv_id}/turns")
        assert res_turns.status_code == 200
        assert len(res_turns.json()) == 1

        # 4. POST New Task works
        res_new_turn = await client.post(f"/api/v1/conversations/{conv_id}/turns", json={"title": "Side Job"})
        assert res_new_turn.status_code == 200
        turn_b_id = res_new_turn.json()["turn_id"]

        # 5. Targeted send to Turn B works
        res_send_b = await client.post(
            f"/api/v1/conversations/{conv_id}/messages",
            json={"content": "Work on B", "turn_id": turn_b_id},
        )
        assert res_send_b.status_code == 200

        # 6. Branch from Turn A's message works
        msg_id = res_msgs.json()[0]["message_id"]
        res_branch = await client.post(
            f"/api/v1/conversations/{conv_id}/branches",
            json={"message_id": msg_id, "name": "Branch while A runs"},
        )
        assert res_branch.status_code == 200
        assert "branch_id" in res_branch.json()

        # Turn A is still running
        assert store.get_turn(turn_a_id).status == "running"
