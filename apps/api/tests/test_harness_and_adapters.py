"""Unit tests for Kane vNext Phase 3:
- Turn Mailbox (inbound only)
- Harness Coordinator (outbound facts, causal isolation, delta accumulation, message finalization)
- Deterministic Dispatcher (zero AI guessing, steer degradation modes, turn resumption)
- Generic Bidirectional Adapter Contract with MockAdapter
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
import pytest

from app.adapters.kanaloa_adapter import KanaloaAdapter
from app.adapters.mock_adapter import MockAdapter
from app.domain.models import AgentCapabilities, Conversation, Message, Turn
from app.harness.coordinator import HarnessCoordinator
from app.harness.dispatcher import Dispatcher
from app.harness.mailbox import MailboxItem, MailboxManager
from app.store.sqlite_store import SQLiteStore


@pytest.fixture
def harness_env():
    """Setup an in-memory SQLite store, MailboxManager, Coordinator, and Dispatcher."""
    store = SQLiteStore(":memory:")
    mbx_mgr = MailboxManager()
    coord = HarnessCoordinator(store, mbx_mgr)
    dispatcher = Dispatcher(store, coord, mbx_mgr)
    return store, mbx_mgr, coord, dispatcher


@pytest.mark.asyncio
async def test_mailbox_causal_isolation(harness_env):
    """Verify Mailbox is strictly for inbound items and never contains outbound agent events."""
    store, mbx_mgr, coord, dispatcher = harness_env

    # Setup conversation and turn
    conv = Conversation(conversation_id="conv_iso", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="turn_iso",
        conversation_id="conv_iso",
        bound_agent_id="mock_agent",
        status="running",
    )
    store.save_turn(turn)

    mock_adapter = MockAdapter()
    coord.register_adapter("mock_agent", mock_adapter)

    # 1. Simulate agent producing various outbound events
    await mock_adapter.simulate_thinking_and_tool(
        "turn_iso", "Planning steps...", "run_script", {"file": "main.py"}
    )
    await mock_adapter.simulate_stream_and_complete(
        "turn_iso", ["Chunk 1 ", "Chunk 2"]
    )

    # 2. Assert mailbox remains strictly empty (Outbound events bypassed mailbox completely)
    mailbox = mbx_mgr.get_mailbox("turn_iso")
    assert mailbox.is_empty
    assert mailbox.pending_count == 0

    # 3. Inbound item can be put into mailbox
    inbound_item = MailboxItem(
        turn_id="turn_iso",
        item_type="steer",
        payload={"instruction": "Adjust strategy"},
    )
    await mailbox.put(inbound_item)
    assert mailbox.pending_count == 1
    retrieved = await mailbox.get()
    assert retrieved.item_type == "steer"
    assert retrieved.payload["instruction"] == "Adjust strategy"


@pytest.mark.asyncio
async def test_streaming_delta_aggregation_and_single_message_conservation(harness_env):
    """
    Verify:
    - Transport chunks != Messages.
    - Deltas accumulate in turn.partial_output buffer.
    - Only emit_message_complete produces ONE logical Message in permanent store.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="conv_stream", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="turn_stream",
        conversation_id="conv_stream",
        bound_agent_id="mock_agent",
        status="running",
    )
    store.save_turn(turn)

    mock_adapter = MockAdapter()
    coord.register_adapter("mock_agent", mock_adapter)

    # Stream 3 chunks
    await coord.emit_delta("turn_stream", "Hello, ")
    await coord.emit_delta("turn_stream", "world of ")
    await coord.emit_delta("turn_stream", "Kanaloa!")

    # Check that partial_output is accumulated in Turn buffer
    t_running = store.get_turn("turn_stream")
    assert t_running.partial_output == "Hello, world of Kanaloa!"

    # Chat message store must still contain ZERO messages (deltas are not messages!)
    messages = store.get_messages("conv_stream")
    assert len(messages) == 0

    # Now signal completion
    msg = await coord.emit_message_complete("turn_stream")
    assert msg.content == "Hello, world of Kanaloa!"
    assert msg.sender == "agent"
    assert msg.sender_id == "mock_agent"

    # Turn partial_output buffer is cleared and status is finished
    t_finished = store.get_turn("turn_stream")
    assert t_finished.partial_output is None
    assert t_finished.status == "finished"
    assert t_finished.finished_at is not None

    # Chat message store contains exactly ONE message
    messages_after = store.get_messages("conv_stream")
    assert len(messages_after) == 1
    assert messages_after[0].message_id == msg.message_id
    assert messages_after[0].content == "Hello, world of Kanaloa!"


@pytest.mark.asyncio
async def test_partial_output_survives_interruption(harness_env):
    """Verify that when a turn is interrupted, partial_output buffer is preserved."""
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="conv_int", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="turn_int",
        conversation_id="conv_int",
        bound_agent_id="mock_agent",
        status="running",
    )
    store.save_turn(turn)

    mock_adapter = MockAdapter()
    coord.register_adapter("mock_agent", mock_adapter)

    # Stream partial text
    await coord.emit_delta("turn_int", "Executing step 1: downloaded dataset...")

    # Interruption occurs (network drop / kill)
    await coord.emit_interrupted("turn_int", reason="connection_lost")

    # Turn status is interrupted and partial_output is NOT wiped out
    t_int = store.get_turn("turn_int")
    assert t_int.status == "interrupted"
    assert t_int.interrupt_reason == "connection_lost"
    assert t_int.partial_output == "Executing step 1: downloaded dataset..."

    # Still no finalized chat message
    assert len(store.get_messages("conv_int")) == 0


@pytest.mark.asyncio
async def test_deterministic_dispatcher_turn_resolution(harness_env):
    """
    Verify 100% deterministic target turn resolution:
    - Initial turn created only when 0 turns exist.
    - If turns already exist but no focus/explicit turn can be resolved, raise target_turn_required.
    - Explicit create_new_turn or is_new_task=True creates independent turn.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    # 1. Brand new conversation (0 turns) -> creates initial Turn and sets focus_turn_id
    turn1 = dispatcher.resolve_target_turn("conv_new")
    assert turn1 is not None
    assert turn1.conversation_id == "conv_new"
    assert turn1.status == "running"
    assert turn1.title == "Initial Turn"

    conv = store.get_conversation("conv_new")
    assert conv.focus_turn_id == turn1.turn_id

    # 2. Subsequent call to same conversation with focus_turn_id resolves to existing focus_turn
    turn2 = dispatcher.resolve_target_turn("conv_new")
    assert turn2.turn_id == turn1.turn_id

    # 3. If focus_turn_id is cleared and no explicit turn provided, DO NOT silently create new Turn
    conv.focus_turn_id = None
    store.save_conversation(conv)

    with pytest.raises(ValueError, match="target_turn_required"):
        dispatcher.resolve_target_turn("conv_new")

    # 4. Explicit turn resolves directly
    explicit_turn = Turn(
        turn_id="turn_explicit",
        conversation_id="conv_new",
        bound_agent_id="mock_agent",
        status="running",
    )
    store.save_turn(explicit_turn)

    resolved = dispatcher.resolve_target_turn("conv_new", explicit_turn_id="turn_explicit")
    assert resolved.turn_id == "turn_explicit"

    # 5. Explicit turn for wrong conversation raises ValueError
    with pytest.raises(ValueError, match="belongs to conversation"):
        dispatcher.resolve_target_turn("conv_other", explicit_turn_id="turn_explicit")

    # 6. Explicitly requesting a New Task creates an additional independent turn
    new_task_turn = dispatcher.create_new_turn("conv_new", title="Dedicated Task 2")
    assert new_task_turn.turn_id != turn1.turn_id
    assert new_task_turn.title == "Dedicated Task 2"
    assert store.get_conversation("conv_new").focus_turn_id == new_task_turn.turn_id


@pytest.mark.asyncio
async def test_turn_resumption_from_finished_failed_interrupted(harness_env):
    """
    Verify §8 & §39: finished, failed, and interrupted turns resume back
    to 'running' when receiving user follow-up input.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="conv_cont", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="turn_cont",
        conversation_id="conv_cont",
        bound_agent_id="mock_agent",
        status="finished",
        finished_at="2026-01-01T00:00:00Z",
    )
    store.save_turn(turn)
    conv.focus_turn_id = "turn_cont"
    store.save_conversation(conv)

    mock_adapter = MockAdapter()
    coord.register_adapter("mock_agent", mock_adapter)

    # 1. Follow-up to finished turn resumes it to running
    user_msg, target_turn = await dispatcher.dispatch_user_message(
        conversation_id="conv_cont",
        content="Please continue and run tests",
    )
    assert target_turn.turn_id == "turn_cont"
    assert target_turn.status == "running"
    assert target_turn.finished_at is None
    assert len(mock_adapter.sent_calls) == 1
    assert mock_adapter.sent_calls[0]["message"].content == "Please continue and run tests"

    # 2. Follow-up to failed turn resumes it to running
    target_turn.status = "failed"
    target_turn.interrupt_reason = "Out of memory"
    store.save_turn(target_turn)

    _, resumed_turn = await dispatcher.dispatch_user_message(
        conversation_id="conv_cont",
        content="Try running with half batch size",
    )
    assert resumed_turn.status == "running"
    assert resumed_turn.interrupt_reason is None

    # 3. Follow-up to interrupted turn resumes it to running
    resumed_turn.status = "interrupted"
    resumed_turn.interrupt_reason = "cancelled_by_user"
    store.save_turn(resumed_turn)

    _, resumed_again = await dispatcher.dispatch_user_message(
        conversation_id="conv_cont",
        content="Resume execution",
    )
    assert resumed_again.status == "running"
    assert resumed_again.interrupt_reason is None


@pytest.mark.asyncio
async def test_steer_degradation_modes(harness_env):
    """
    Verify steer degradation according to AgentCapabilities.steer_mode:
    1. native: calls adapter.steer() immediately
    2. safe_boundary: enqueues steer into TurnMailbox for agent safe-step polling
    3. follow_up_only: enqueues message into TurnMailbox for auto-dispatch when turn completes
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    # --- Mode 1: Native ---
    mock_native = MockAdapter(
        capabilities=AgentCapabilities(steer_mode="native")
    )
    coord.register_adapter("agent_native", mock_native)

    conv1 = Conversation(conversation_id="c_native", bound_agent_id="agent_native")
    store.save_conversation(conv1)
    turn1 = Turn(
        turn_id="t_native",
        conversation_id="c_native",
        bound_agent_id="agent_native",
        status="running",
    )
    store.save_turn(turn1)
    conv1.focus_turn_id = "t_native"
    store.save_conversation(conv1)

    await dispatcher.dispatch_user_message("c_native", "Native steer message")
    assert len(mock_native.steer_calls) == 1
    assert mock_native.steer_calls[0]["message"].content == "Native steer message"
    assert mbx_mgr.get_mailbox("t_native").is_empty

    # --- Mode 2: Safe Boundary (Adapter boundary signal driven) ---
    mock_safe = MockAdapter(
        capabilities=AgentCapabilities(steer_mode="safe_boundary")
    )
    coord.register_adapter("agent_safe", mock_safe)

    conv2 = Conversation(conversation_id="c_safe", bound_agent_id="agent_safe")
    store.save_conversation(conv2)
    turn2 = Turn(
        turn_id="t_safe",
        conversation_id="c_safe",
        bound_agent_id="agent_safe",
        status="running",
    )
    store.save_turn(turn2)
    conv2.focus_turn_id = "t_safe"
    store.save_conversation(conv2)

    await dispatcher.dispatch_user_message("c_safe", "Safe boundary steer")
    # Native steer must NOT have been called yet
    assert len(mock_safe.steer_calls) == 0
    # Enqueued into Kane Mailbox (Agent does not know about Mailbox)
    mbx_safe = mbx_mgr.get_mailbox("t_safe")
    assert mbx_safe.pending_count == 1

    # When Adapter reaches a native safe execution boundary, it signals Kane
    await mock_safe.simulate_reach_safe_boundary("t_safe")

    # Kane dequeues the steer and feeds it directly into adapter.steer()
    assert mbx_safe.pending_count == 0
    assert len(mock_safe.steer_calls) == 1
    assert mock_safe.steer_calls[0]["message"].content == "Safe boundary steer"

    # --- Mode 3: Follow Up Only ---
    mock_followup = MockAdapter(
        capabilities=AgentCapabilities(steer_mode="follow_up_only")
    )
    coord.register_adapter("agent_followup", mock_followup)

    conv3 = Conversation(conversation_id="c_followup", bound_agent_id="agent_followup")
    store.save_conversation(conv3)
    turn3 = Turn(
        turn_id="t_followup",
        conversation_id="c_followup",
        bound_agent_id="agent_followup",
        status="running",
    )
    store.save_turn(turn3)
    conv3.focus_turn_id = "t_followup"
    store.save_conversation(conv3)

    # Dispatch mid-flight input while running
    await dispatcher.dispatch_user_message("c_followup", "Next task when you finish")
    assert len(mock_followup.sent_calls) == 0
    assert len(mock_followup.steer_calls) == 0

    mbx_fu = mbx_mgr.get_mailbox("t_followup")
    assert mbx_fu.pending_count == 1

    # 3a. Normal logical completion -> can auto-dispatch queued follow-up
    await coord.emit_delta("t_followup", "First task done.")
    await coord.emit_message_complete("t_followup")

    # Give event loop a tick to process background followup dispatch
    await asyncio.sleep(0.01)

    # Mailbox should have been drained and follow-up dispatched to adapter!
    assert mbx_fu.pending_count == 0
    assert len(mock_followup.sent_calls) == 1
    assert mock_followup.sent_calls[0]["message"].content == "Next task when you finish"

    # 3b. If a turn is interrupted or failed -> DO NOT auto-dispatch queued follow-up!
    turn3_fail = Turn(
        turn_id="t_followup_fail",
        conversation_id="c_followup",
        bound_agent_id="agent_followup",
        status="running",
    )
    store.save_turn(turn3_fail)
    conv3.focus_turn_id = "t_followup_fail"
    store.save_conversation(conv3)

    # Queue a follow-up
    await dispatcher.dispatch_user_message("c_followup", "Task to hold upon failure")
    mbx_fail = mbx_mgr.get_mailbox("t_followup_fail")
    assert mbx_fail.pending_count == 1

    # Turn fails
    await coord.emit_failed("t_followup_fail", reason="Engine error")
    await asyncio.sleep(0.01)

    # Must NOT auto-dispatch; preserved in mailbox waiting for explicit user resumption
    assert mbx_fail.pending_count == 1


@pytest.mark.asyncio
async def test_completion_deterministic_precedence(harness_env):
    """
    Verify deterministic precedence in emit_message_complete:
    - If explicit content provided, it takes precedence over partial_output.
    - If explicit content is None, partial_output is used.
    - partial_output is always cleared to prevent duplicate text accumulation.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c_prec", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_prec",
        conversation_id="c_prec",
        bound_agent_id="mock_agent",
        status="running",
    )
    store.save_turn(turn)

    mock_agent = MockAdapter()
    coord.register_adapter("mock_agent", mock_agent)

    # Stream some deltas
    await coord.emit_delta("t_prec", "Partial chunk 1")
    t = store.get_turn("t_prec")
    assert t.partial_output == "Partial chunk 1"

    # 1. Calling emit_message_complete with explicit override content
    msg1 = await coord.emit_message_complete("t_prec", content="Explicit final message")
    assert msg1.content == "Explicit final message"
    # partial_output cleared
    assert store.get_turn("t_prec").partial_output is None

    # 2. Calling emit_message_complete without explicit content (content=None)
    # Resume turn and stream again
    turn.status = "running"
    store.save_turn(turn)
    await coord.emit_delta("t_prec", "Accumulated stream text")
    msg2 = await coord.emit_message_complete("t_prec", content=None)
    assert msg2.content == "Accumulated stream text"
    assert store.get_turn("t_prec").partial_output is None


@pytest.mark.asyncio
async def test_control_commands_cancel_and_resume(harness_env):
    """Verify cancel_turn and resume_turn control flows."""
    store, mbx_mgr, coord, dispatcher = harness_env

    mock_agent = MockAdapter()
    coord.register_adapter("mock_agent", mock_agent)

    conv = Conversation(conversation_id="c_ctrl", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_ctrl",
        conversation_id="c_ctrl",
        bound_agent_id="mock_agent",
        status="running",
    )
    store.save_turn(turn)

    # Cancel
    cancelled = await dispatcher.cancel_turn("t_ctrl", reason="cancelled_by_user")
    assert cancelled.status == "interrupted"
    assert cancelled.interrupt_reason == "cancelled_by_user"
    assert len(mock_agent.cancel_calls) == 1

    # Resume
    resumed = await dispatcher.resume_turn("t_ctrl")
    assert resumed.status == "running"
    assert resumed.interrupt_reason is None
    assert len(mock_agent.resume_calls) == 1


@pytest.mark.asyncio
async def test_live_event_subscription(harness_env):
    """Verify live TurnEvent subscription mechanism for UI/SSE streaming."""
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c_sub", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_sub",
        conversation_id="c_sub",
        bound_agent_id="mock_agent",
        status="running",
    )
    store.save_turn(turn)

    mock_agent = MockAdapter()
    coord.register_adapter("mock_agent", mock_agent)

    # Subscribe to live events
    queue = coord.subscribe("t_sub")

    # Emit delta and thinking
    await coord.emit_event("t_sub", "thinking", {"thought": "Processing..."})
    await coord.emit_delta("t_sub", "Delta 1")

    # Verify subscriber received both events
    evt1 = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert evt1.event_type == "thinking"
    assert evt1.payload["thought"] == "Processing..."

    evt2 = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert evt2.event_type == "delta"
    assert evt2.payload["delta"] == "Delta 1"

    # Unsubscribe
    coord.unsubscribe("t_sub", queue)
    await coord.emit_delta("t_sub", "Delta 2")
    assert queue.empty()


@pytest.mark.asyncio
async def test_atomic_finalize_turn_completion(harness_env):
    """
    Verify that finalize_turn_completion executes in a single ACID SQLite transaction:
    - Message inserted
    - Turn updated with partial_output = None, status = finished, finished_at recorded
    - TurnEvent recorded
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c_atomic", bound_agent_id="mock_agent")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_atomic",
        conversation_id="c_atomic",
        bound_agent_id="mock_agent",
        status="running",
        partial_output="Pending stream buffer",
    )
    store.save_turn(turn)

    mock_agent = MockAdapter()
    coord.register_adapter("mock_agent", mock_agent)

    # Call emit_message_complete which uses store.finalize_turn_completion atomically
    msg = await coord.emit_message_complete("t_atomic")
    assert msg.content == "Pending stream buffer"

    # Verify atomic state in store
    t_db = store.get_turn("t_atomic")
    assert t_db.status == "finished"
    assert t_db.partial_output is None
    assert t_db.finished_at is not None

    messages = store.get_messages("c_atomic")
    assert len(messages) == 1
    assert messages[0].message_id == msg.message_id


@pytest.mark.asyncio
async def test_kanaloa_adapter_capabilities_and_session_binding(harness_env):
    """
    Verify KanaloaAdapter:
    - Honest capability declaration based on verified DSH ACP transport
    - Session binding to Turn native_session_ref
    """
    from app.adapters.kanaloa_adapter import KanaloaAdapter

    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = KanaloaAdapter()
    coord.register_adapter("kanaloa", kanaloa)

    caps = kanaloa.capabilities()
    assert caps.supports_cancel is True
    assert caps.supports_stream is True
    assert caps.supports_resume is True
    assert caps.supports_approval is True
    assert caps.supports_parallel_sessions is True
    assert caps.max_parallel_sessions is None
    assert caps.steer_mode == "native"
    assert caps.branch_mode == "unsupported"

    # Session binding
    conv = Conversation(conversation_id="c_kan", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_kan",
        conversation_id="c_kan",
        bound_agent_id="kanaloa",
        status="running",
    )
    store.save_turn(turn)

    kanaloa.bind_session("t_kan", "sess_kan_123")
    assert kanaloa.get_native_session("t_kan") == "sess_kan_123"


@pytest.mark.asyncio
async def test_kanaloa_adapter_event_normalization(harness_env):
    """
    Verify KanaloaAdapter deterministic normalization of ACP session/update into Kane stable facts:
    - content block text -> emit_delta -> partial_output buffer
    - thought / thinking -> coarse live event {"status": "thinking"} (zero raw thought bloat into DB)
    - tool events -> coarse tool event
    - permission request -> emit_waiting_user -> turn.status = waiting_user
    - prompt complete -> emit_message_complete -> single Message in store (sender_id="kanaloa")
    - cancelled outcome -> emit_interrupted -> turn.status = interrupted
    """
    from app.adapters.kanaloa_adapter import KanaloaAdapter

    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = KanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    conv = Conversation(conversation_id="c_norm", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_norm",
        conversation_id="c_norm",
        bound_agent_id="kanaloa",
        native_session_ref="sess_norm_1",
        status="running",
    )
    store.save_turn(turn)
    kanaloa.bind_session("t_norm", "sess_norm_1")

    # 1. Delta normalization via session/update
    await kanaloa._handle_session_update({
        "sessionId": "sess_norm_1",
        "update": {
            "type": "content",
            "content": {"type": "text", "text": "Step 1: Analyzing repo with DSH..."},
        },
    })
    assert store.get_turn("t_norm").partial_output == "Step 1: Analyzing repo with DSH..."

    # 2. Thinking normalization (coarse live status, not bloating turn_events)
    await kanaloa._handle_session_update({
        "sessionId": "sess_norm_1",
        "update": {
            "type": "thought",
            "thought": "Deep reasoning intermediate steps...",
        },
    })
    events = store.list_events("t_norm")
    assert any(e.event_type == "thinking" and e.payload.get("status") == "thinking" for e in events)

    # 3. Tool event normalization
    await kanaloa._handle_session_update({
        "sessionId": "sess_norm_1",
        "update": {
            "type": "tool_call",
            "toolName": "bash",
            "status": "executing",
        },
    })
    events = store.list_events("t_norm")
    assert any(e.event_type == "tool_start" and e.payload.get("tool") == "bash" for e in events)

    # 4. Permission request normalization
    await kanaloa._handle_permission_request({
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_norm_1",
            "message": "Confirm file edit?",
        },
    })
    assert store.get_turn("t_norm").status == "waiting_user"

    # 5. Completion normalization
    await kanaloa.event_handler.emit_message_complete(
        turn_id="t_norm",
        sender_id="kanaloa",
    )
    t_fin = store.get_turn("t_norm")
    assert t_fin.status == "finished"
    assert t_fin.partial_output is None
    msgs = store.get_messages("c_norm")
    assert len(msgs) == 1
    assert msgs[0].content == "Step 1: Analyzing repo with DSH..."
    assert msgs[0].sender_id == "kanaloa"

    # 6. Interrupted / Cancel normalization
    turn.status = "running"
    store.save_turn(turn)
    await kanaloa.event_handler.emit_interrupted("t_norm", reason="cancelled_by_acp")
    assert store.get_turn("t_norm").status == "interrupted"


@pytest.mark.asyncio
async def test_deterministic_message_turn_binding_and_reply_routing(harness_env):
    """
    Verify Message -> Turn deterministic binding:
    - User message gets turn_id bound upon dispatch
    - reply_to_message_id resolves deterministically to the target Turn via store.get_turn_id_by_message_id
    - Zero NLP, zero heuristic guessing
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    mock_agent = MockAdapter()
    coord.register_adapter("mock_agent", mock_agent)

    conv = Conversation(conversation_id="c_determ", bound_agent_id="mock_agent")
    store.save_conversation(conv)

    turn_a = Turn(turn_id="turn_a", conversation_id="c_determ", bound_agent_id="mock_agent", status="running")
    turn_b = Turn(turn_id="turn_b", conversation_id="c_determ", bound_agent_id="mock_agent", status="running")
    store.save_turn(turn_a)
    store.save_turn(turn_b)

    # 1. Dispatch explicit message to turn_a
    msg_a, res_a = await dispatcher.dispatch_user_message("c_determ", "Message for A", target_turn_id="turn_a")
    assert res_a.turn_id == "turn_a"
    assert msg_a.turn_id == "turn_a"
    assert store.get_turn_id_by_message_id(msg_a.message_id) == "turn_a"

    # 2. Dispatch explicit message to turn_b
    msg_b, res_b = await dispatcher.dispatch_user_message("c_determ", "Message for B", target_turn_id="turn_b")
    assert res_b.turn_id == "turn_b"
    assert msg_b.turn_id == "turn_b"
    assert store.get_turn_id_by_message_id(msg_b.message_id) == "turn_b"

    # 3. Dispatch replying to msg_a without target_turn_id -> Must route deterministically to turn_a!
    msg_reply_a, res_reply_a = await dispatcher.dispatch_user_message(
        "c_determ", "Followup replying to A", reply_to_message_id=msg_a.message_id
    )
    assert res_reply_a.turn_id == "turn_a"
    assert msg_reply_a.turn_id == "turn_a"

    # 4. Dispatch replying to msg_b without target_turn_id -> Must route deterministically to turn_b!
    msg_reply_b, res_reply_b = await dispatcher.dispatch_user_message(
        "c_determ", "Followup replying to B", reply_to_message_id=msg_b.message_id
    )
    assert res_reply_b.turn_id == "turn_b"
    assert msg_reply_b.turn_id == "turn_b"


class MockWireKanaloaAdapter(KanaloaAdapter):
    """Test helper for wire-level Kanaloa ACP interactions without spawning real subprocess."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cancel_notifs: list[dict[str, Any] | None] = []
        self.resumed_sessions: list[dict[str, Any] | None] = []
        self.steer_requests: list[dict[str, Any] | None] = []
        self.sent_requests: list[tuple[str, dict[str, Any] | None]] = []
        self._new_session_id_counter = 1

    async def _ensure_process(self) -> None:
        self._is_initialized = True

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if method == "session/cancel":
            self.cancel_notifs.append(params)

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent_requests.append((method, params))
        if method == "initialize":
            return {"result": {"agentInfo": {"name": "dsh-acp", "version": "0.0.1"}}}
        if method == "session/new":
            sess_id = f"acp_sess_{self._new_session_id_counter}"
            self._new_session_id_counter += 1
            return {"result": {"sessionId": sess_id}}
        if method == "session/resume":
            self.resumed_sessions.append(params)
            return {"result": {"configOptions": []}}
        if method == "session/prompt":
            return {"result": {"stopReason": "end_turn"}}
        if method == "session/steer":
            self.steer_requests.append(params)
            return {"result": {"accepted": True}}
        if method == "session/close":
            return {"result": {}}
        return {"result": {}}


@pytest.mark.asyncio
async def test_strict_capability_gates_cancel_and_resume(harness_env):
    """
    Verify capability gates strictly block/raise instead of silently pretending success:
    - supports_cancel = False raises RuntimeError on cancel_turn
    - supports_resume = False raises RuntimeError on resume_turn
    - KanaloaAdapter steer() raises NotImplementedError (honest follow_up_only declaration)
    """
    from app.adapters.kanaloa_adapter import KanaloaAdapter

    store, mbx_mgr, coord, dispatcher = harness_env

    # 1. Adapter without cancel support
    no_cancel_agent = MockAdapter(
        capabilities=AgentCapabilities(supports_cancel=False, supports_resume=True)
    )
    coord.register_adapter("agent_no_cancel", no_cancel_agent)

    conv1 = Conversation(conversation_id="c_gate1", bound_agent_id="agent_no_cancel")
    store.save_conversation(conv1)
    turn1 = Turn(turn_id="t_gate1", conversation_id="c_gate1", bound_agent_id="agent_no_cancel", status="running")
    store.save_turn(turn1)

    with pytest.raises(RuntimeError, match="does not support cancellation"):
        await dispatcher.cancel_turn("t_gate1")

    # Turn status must NOT be modified to interrupted
    assert store.get_turn("t_gate1").status == "running"

    # 2. Adapter without resume support
    no_resume_agent = MockAdapter(
        capabilities=AgentCapabilities(supports_cancel=True, supports_resume=False)
    )
    coord.register_adapter("agent_no_resume", no_resume_agent)

    conv2 = Conversation(conversation_id="c_gate2", bound_agent_id="agent_no_resume")
    store.save_conversation(conv2)
    turn2 = Turn(turn_id="t_gate2", conversation_id="c_gate2", bound_agent_id="agent_no_resume", status="waiting_user")
    store.save_turn(turn2)

    with pytest.raises(RuntimeError, match="does not support native resume"):
        await dispatcher.resume_turn("t_gate2")

    # 3. Direct steer call on KanaloaAdapter:
    kanaloa = KanaloaAdapter()
    assert kanaloa.capabilities().steer_mode == "native"
    # Fails if session is not bound
    with pytest.raises(ValueError, match="without active native session"):
        await kanaloa.steer(turn2, Message(conversation_id="c_gate2", sender="user", content="steer"))
    # Fails if turn is in terminal status
    terminal_turn = Turn(turn_id="t_term", conversation_id="c_gate2", bound_agent_id="kanaloa", status="interrupted", native_session_ref="sess_term")
    with pytest.raises(RuntimeError, match="terminal status"):
        await kanaloa.steer(terminal_turn, Message(conversation_id="c_gate2", sender="user", content="steer"))


@pytest.mark.asyncio
async def test_kanaloa_adapter_incremental_vs_bootstrap_prompt(harness_env):
    """
    Verify KanaloaAdapter dual-state prompt payload over ACP:
    - Bootstrap state (new session or after reset): sends prompt + visible history context blocks
    - Incremental state (active session): sends prompt ONLY (no history blocks repeated)
    - Session reset / rebuild: re-attaches visible history context blocks
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = MockWireKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa_proto", kanaloa)

    conv = Conversation(conversation_id="c_proto", bound_agent_id="kanaloa_proto")
    store.save_conversation(conv)

    turn = Turn(
        turn_id="t_proto",
        conversation_id="c_proto",
        bound_agent_id="kanaloa_proto",
        status="finished",
    )
    store.save_turn(turn)
    conv.focus_turn_id = "t_proto"
    store.save_conversation(conv)

    # Prior historical message
    m0 = Message(conversation_id="c_proto", turn_id="t_proto", sender="user", content="System instruction: be concise")
    store.append_message(m0)

    # 1. First send: Bootstrap State (session is new, not yet active in memory)
    m1, _ = await dispatcher.dispatch_user_message("c_proto", "First question")
    assert kanaloa.last_sent_payload is not None
    payload1 = kanaloa.last_sent_payload
    assert payload1["method"] == "session/prompt"
    assert payload1["params"]["sessionId"] == "acp_sess_1"
    prompt_blocks1 = payload1["params"]["prompt"]
    # Visible history contains prior message m0 as passive context block
    assert any("[USER CONTEXT]: System instruction: be concise" in b.get("text", "") for b in prompt_blocks1)
    # The active prompt is the new question
    assert prompt_blocks1[-1]["text"] == "First question"

    # Simulate completion
    await kanaloa.event_handler.emit_message_complete(turn_id="t_proto", sender_id="kanaloa")

    # 2. Follow-up send on existing active session: Incremental State
    m2, _ = await dispatcher.dispatch_user_message("c_proto", "Follow-up question")
    payload2 = kanaloa.last_sent_payload
    assert payload2["method"] == "session/prompt"
    assert payload2["params"]["sessionId"] == "acp_sess_1"
    # No history repeated! Only the incremental new message block
    prompt_blocks2 = payload2["params"]["prompt"]
    assert len(prompt_blocks2) == 1
    assert prompt_blocks2[0]["text"] == "Follow-up question"

    # Simulate completion before shutdown
    await kanaloa.event_handler.emit_message_complete(turn_id="t_proto", sender_id="kanaloa")

    # 3. Simulate process crash / session reset via close()
    await kanaloa.close()
    assert not kanaloa.is_alive()

    # 4. Next send triggers Session Rebuild State with visible history context blocks
    m3, _ = await dispatcher.dispatch_user_message("c_proto", "Question after restart")
    payload3 = kanaloa.last_sent_payload
    assert payload3["method"] == "session/prompt"
    prompt_blocks3 = payload3["params"]["prompt"]
    assert prompt_blocks3[-1]["text"] == "Question after restart"
    assert any("[USER CONTEXT]:" in b.get("text", "") for b in prompt_blocks3)


@pytest.mark.asyncio
async def test_session_rebuild_context_reconstruction_without_side_effect_replay(harness_env):
    """
    Verify Non-Negotiable Session Rebuild Discipline:
    Reconstruction != Replay historical execution.
    - Historical messages containing real-world side effects (file edits, git commits, API calls)
      MUST ONLY be reconstructed as passive context / transcript entries ({role, content}).
    - Historical messages MUST NEVER be placed in the executable prompt field or invoked as a command queue.
    - ONLY the single, newly dispatched user message is executed as the prompt.
    - 0 mechanical replay / 0 command queue replay.
    - External side-effect idempotency and approval policies remain governed by the Agent/Tool/Host layers.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = MockWireKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa_rebuild", kanaloa)

    conv = Conversation(conversation_id="c_side_effect", bound_agent_id="kanaloa_rebuild")
    store.save_conversation(conv)

    turn = Turn(
        turn_id="t_side_effect",
        conversation_id="c_side_effect",
        bound_agent_id="kanaloa_rebuild",
        status="finished",
    )
    store.save_turn(turn)
    conv.focus_turn_id = "t_side_effect"
    store.save_conversation(conv)

    # 1. Historical messages that performed real-world side effects
    m1 = Message(
        conversation_id="c_side_effect",
        turn_id="t_side_effect",
        sender="user",
        content="rm -rf /tmp/build_cache && git push origin main",
    )
    store.append_message(m1)
    m2 = Message(
        conversation_id="c_side_effect",
        turn_id="t_side_effect",
        sender="agent",
        sender_id="kanaloa",
        content="Cleaned cache and pushed commit 92fff32 to origin.",
    )
    store.append_message(m2)

    # 2. Simulate native session loss / process restart (State B: Rebuild)
    await kanaloa.close()
    assert not kanaloa.is_alive()

    # 3. User sends a new read-only inspection query
    current_input = "Show me the current git log"
    current_msg, resumed_turn = await dispatcher.dispatch_user_message(
        "c_side_effect",
        current_input,
    )

    payload = kanaloa.last_sent_payload
    assert payload is not None
    assert payload["method"] == "session/prompt"

    prompt_blocks = payload["params"]["prompt"]

    # CRITICAL CHECK 1: The executable prompt is STRICTLY and ONLY the current new input
    assert prompt_blocks[-1]["text"] == current_input
    assert "rm -rf" not in prompt_blocks[-1]["text"]
    assert "git push" not in prompt_blocks[-1]["text"]

    # CRITICAL CHECK 2: Historical messages with side effects are strictly passive transcript blocks
    assert prompt_blocks[0]["text"] == "[USER CONTEXT]: rm -rf /tmp/build_cache && git push origin main"
    assert prompt_blocks[1]["text"] == "[AGENT CONTEXT]: Cleaned cache and pushed commit 92fff32 to origin."

    # CRITICAL CHECK 3: No execution loop or queue re-executed historical messages
    assert resumed_turn.status == "running"


@pytest.mark.asyncio
async def test_kanaloa_adapter_dispatcher_control_flows(harness_env):
    """
    Verify Dispatcher cancel and resume work seamlessly with KanaloaAdapter:
    - Since supports_cancel=True, cancel_turn does NOT raise and completes cleanly via session/cancel
    - Since supports_resume=True, resume_turn does NOT raise and invokes adapter.resume via session/resume
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    mock_kanaloa = MockWireKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa_ctrl", mock_kanaloa)

    conv = Conversation(conversation_id="c_ctrl", bound_agent_id="kanaloa_ctrl")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_ctrl",
        conversation_id="c_ctrl",
        bound_agent_id="kanaloa_ctrl",
        native_session_ref="sess_wire_1",
        status="running",
    )
    store.save_turn(turn)
    mock_kanaloa.bind_session("t_ctrl", "sess_wire_1")

    # 1. Cancel active turn
    cancelled = await dispatcher.cancel_turn("t_ctrl", reason="user_stop")
    assert cancelled.status == "interrupted"
    assert len(mock_kanaloa.cancel_notifs) == 1
    assert mock_kanaloa.cancel_notifs[0]["sessionId"] == "sess_wire_1"

    # 2. Resume turn natively
    resumed = await dispatcher.resume_turn("t_ctrl")
    assert resumed.status == "running"
    assert len(mock_kanaloa.resumed_sessions) == 1
    assert mock_kanaloa.resumed_sessions[0]["sessionId"] == "sess_wire_1"


@pytest.mark.asyncio
async def test_kanaloa_native_steer_wire_protocol(harness_env):
    """
    Verify KanaloaAdapter Native Steer wire protocol over ACP:
    - Same Turn, same native sessionId (sess_steer_wire)
    - Execution remains running, no cancel+restart
    - session/steer request sent with formatted ContentBlock prompt
    - Multiple steers accepted in order
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    mock_kanaloa = MockWireKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa_steer", mock_kanaloa)

    conv = Conversation(conversation_id="c_steer", bound_agent_id="kanaloa_steer")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_steer",
        conversation_id="c_steer",
        bound_agent_id="kanaloa_steer",
        native_session_ref="sess_steer_wire",
        status="running",
    )
    store.save_turn(turn)
    mock_kanaloa.bind_session("t_steer", "sess_steer_wire")

    # 1. Dispatch user message while turn is actively running -> Dispatcher invokes native steer directly
    msg1, returned_turn = await dispatcher.dispatch_user_message(
        "c_steer",
        "Stop previous plan, switch to test suite generation.",
        target_turn_id="t_steer",
    )
    assert returned_turn.turn_id == "t_steer"
    assert returned_turn.status == "running"
    assert returned_turn.native_session_ref == "sess_steer_wire"

    # 2. Assert wire request was sent
    assert len(mock_kanaloa.steer_requests) == 1
    steer_req1 = mock_kanaloa.steer_requests[0]
    assert steer_req1["sessionId"] == "sess_steer_wire"
    assert steer_req1["prompt"] == [{"type": "text", "text": "Stop previous plan, switch to test suite generation."}]

    # 3. Dispatch second steer message to verify multiple sequential steers
    msg2, returned_turn2 = await dispatcher.dispatch_user_message(
        "c_steer",
        "Also include property-based tests.",
        target_turn_id="t_steer",
    )
    assert returned_turn2.turn_id == "t_steer"
    assert returned_turn2.status == "running"
    assert len(mock_kanaloa.steer_requests) == 2
    assert mock_kanaloa.steer_requests[1]["prompt"] == [{"type": "text", "text": "Also include property-based tests."}]

    # 4. Zero cancellations occurred!
    assert len(mock_kanaloa.cancel_notifs) == 0


@pytest.mark.asyncio
async def test_kanaloa_approval_roundtrip_allow_and_reject(harness_env):
    """
    Verify complete Approval bidirectional roundtrip over ACP:
    1. DSH ACP sends session/request_permission with id=101
    2. Turn transitions to waiting_user
    3. User invokes respond_permission with 'allow-once'
    4. Exact JSON-RPC response formatted per ACP spec written to stdio
    5. Turn in store automatically resumes to 'running'
    6. Repeated response to same request rejected (fail-closed)
    7. Second request with 'reject-once' verified
    """
    from unittest.mock import AsyncMock, MagicMock
    store, mbx_mgr, coord, dispatcher = harness_env

    kanaloa = MockWireKanaloaAdapter(event_handler=coord)
    # Mock subprocess stdin
    written_data: list[str] = []
    mock_stdin = AsyncMock()
    mock_stdin.write = MagicMock(side_effect=lambda b: written_data.append(b.decode("utf-8")))
    mock_stdin.drain = AsyncMock()
    mock_process = MagicMock()
    mock_process.stdin = mock_stdin
    mock_process.returncode = None
    kanaloa._process = mock_process
    kanaloa._is_initialized = True

    conv = Conversation(conversation_id="c_appr", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_appr_1",
        conversation_id="c_appr",
        bound_agent_id="kanaloa",
        native_session_ref="sess_appr_1",
        status="running",
    )
    store.save_turn(turn)
    kanaloa.bind_session("t_appr_1", "sess_appr_1")

    # 1. DSH sends permission request 101 (e.g. bash file edit)
    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 101,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_appr_1",
            "toolCall": {"toolCallId": "call_edit_1", "toolName": "edit_file"},
            "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ],
            "message": "Allow edit to config.json?",
        },
    })

    # Verify Turn transitioned to waiting_user
    assert store.get_turn("t_appr_1").status == "waiting_user"
    pending = kanaloa.get_pending_permission(101)
    assert pending is not None
    assert pending.session_id == "sess_appr_1"
    assert pending.turn_id == "t_appr_1"
    assert pending.status == "pending"

    # 2. User responds with 'allow-once'
    await kanaloa.respond_permission(101, "allow-once", session_id="sess_appr_1")

    # Verify Turn transitioned back to running
    assert store.get_turn("t_appr_1").status == "running"
    assert pending.status == "resolved"
    assert pending.decision == "allow-once"

    # Verify exact JSON-RPC response format
    assert len(written_data) == 1
    resp_101 = json.loads(written_data[0].strip())
    assert resp_101["jsonrpc"] == "2.0"
    assert resp_101["id"] == 101
    assert resp_101["result"]["outcome"] == {
        "outcome": "selected",
        "optionId": "allow-once",
    }

    # 3. Repeated response to resolved request must raise ValueError
    with pytest.raises(ValueError, match="already resolved"):
        await kanaloa.respond_permission(101, "allow-once")

    # 4. Second permission request with 'reject-once'
    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 102,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_appr_1",
            "toolCall": {"toolCallId": "call_rm_1", "toolName": "delete_all"},
            "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ],
            "message": "Allow delete_all?",
        },
    })
    assert store.get_turn("t_appr_1").status == "waiting_user"
    await kanaloa.respond_permission(102, "reject-once", session_id="sess_appr_1")
    assert store.get_turn("t_appr_1").status == "running"

    resp_102 = json.loads(written_data[1].strip())
    assert resp_102["id"] == 102
    assert resp_102["result"]["outcome"] == {
        "outcome": "selected",
        "optionId": "reject-once",
    }


@pytest.mark.asyncio
async def test_kanaloa_approval_parallel_sessions_no_crosstalk(harness_env):
    """
    Verify multiple concurrent sessions handling permission requests:
    - Session A (Turn A) and Session B (Turn B) receive distinct permission requests.
    - Mismatched session response strictly fails.
    - Correct responses routed strictly by request_id and session_id without crosstalk.
    """
    from unittest.mock import AsyncMock, MagicMock
    store, mbx_mgr, coord, dispatcher = harness_env

    kanaloa = MockWireKanaloaAdapter(event_handler=coord)
    written_data: list[str] = []
    mock_stdin = AsyncMock()
    mock_stdin.write = MagicMock(side_effect=lambda b: written_data.append(b.decode("utf-8")))
    mock_stdin.drain = AsyncMock()
    mock_process = MagicMock()
    mock_process.stdin = mock_stdin
    mock_process.returncode = None
    kanaloa._process = mock_process
    kanaloa._is_initialized = True

    # Setup 2 turns on separate sessions
    for i in (1, 2):
        conv = Conversation(conversation_id=f"c_p_{i}", bound_agent_id="kanaloa")
        store.save_conversation(conv)
        turn = Turn(
            turn_id=f"t_p_{i}",
            conversation_id=f"c_p_{i}",
            bound_agent_id="kanaloa",
            native_session_ref=f"sess_p_{i}",
            status="running",
        )
        store.save_turn(turn)
        kanaloa.bind_session(f"t_p_{i}", f"sess_p_{i}")

    # Session 1 receives Req 201; Session 2 receives Req 202
    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 201,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_p_1",
            "toolCall": {"toolCallId": "c_1", "toolName": "bash"},
            "options": [{"optionId": "allow-once"}, {"optionId": "reject-once"}],
        },
    })
    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 202,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_p_2",
            "toolCall": {"toolCallId": "c_2", "toolName": "deploy"},
            "options": [{"optionId": "allow-once"}, {"optionId": "reject-once"}],
        },
    })

    assert store.get_turn("t_p_1").status == "waiting_user"
    assert store.get_turn("t_p_2").status == "waiting_user"

    # Cross-session injection attempt: Trying to answer Req 201 using sess_p_2 -> must fail
    with pytest.raises(ValueError, match="Session mismatch"):
        await kanaloa.respond_permission(201, "allow-once", session_id="sess_p_2")

    # Correct response for Req 201 on Session 1
    await kanaloa.respond_permission(201, "allow-once", session_id="sess_p_1")
    assert store.get_turn("t_p_1").status == "running"
    assert store.get_turn("t_p_2").status == "waiting_user"  # Session 2 still waiting!

    # Correct response for Req 202 on Session 2
    await kanaloa.respond_permission(202, "reject-once", session_id="sess_p_2")
    assert store.get_turn("t_p_2").status == "running"

    resp1 = json.loads(written_data[0].strip())
    resp2 = json.loads(written_data[1].strip())
    assert resp1["id"] == 201 and resp1["result"]["outcome"]["optionId"] == "allow-once"
    assert resp2["id"] == 202 and resp2["result"]["outcome"]["optionId"] == "reject-once"


@pytest.mark.asyncio
async def test_kanaloa_approval_expired_and_unknown_requests(harness_env):
    """
    Verify rejection of invalid, expired, cancelled, or unknown permission requests.
    """
    from unittest.mock import AsyncMock, MagicMock
    store, mbx_mgr, coord, dispatcher = harness_env

    kanaloa = MockWireKanaloaAdapter(event_handler=coord)
    mock_process = MagicMock()
    mock_process.stdin = AsyncMock()
    mock_process.returncode = None
    kanaloa._process = mock_process
    kanaloa._is_initialized = True

    # 1. Unknown request ID raises ValueError
    with pytest.raises(ValueError, match="Unknown permission request '999'"):
        await kanaloa.respond_permission(999, "allow-once")

    # 2. Setup turn and request
    conv = Conversation(conversation_id="c_exp", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_exp",
        conversation_id="c_exp",
        bound_agent_id="kanaloa",
        native_session_ref="sess_exp",
        status="running",
    )
    store.save_turn(turn)
    kanaloa.bind_session("t_exp", "sess_exp")

    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 301,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_exp",
            "toolCall": {"toolCallId": "c_3", "toolName": "bash"},
        },
    })

    # 3. Cancel turn -> marks pending permission cancelled
    await kanaloa.cancel(turn)
    assert kanaloa.get_pending_permission(301).status == "cancelled"

    with pytest.raises(ValueError, match="already cancelled"):
        await kanaloa.respond_permission(301, "allow-once")

    # 4. Another request, then close adapter -> marks pending permission expired
    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 302,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_exp",
            "toolCall": {"toolCallId": "c_4", "toolName": "bash"},
        },
    })
    await kanaloa.close()
    assert kanaloa.get_pending_permission(302).status == "expired"

    with pytest.raises(ValueError, match="already expired"):
        await kanaloa.respond_permission(302, "allow-once")




