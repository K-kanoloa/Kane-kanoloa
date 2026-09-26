"""Unit tests for Kane vNext Phase 3:
- Turn Mailbox (inbound only)
- Harness Coordinator (outbound facts, causal isolation, delta accumulation, message finalization)
- Deterministic Dispatcher (zero AI guessing, steer degradation modes, turn resumption)
- Generic Bidirectional Adapter Contract with MockAdapter
"""

from __future__ import annotations

import asyncio
import pytest

from app.adapters.mock_adapter import MockAdapter
from app.domain.models import AgentCapabilities, Conversation, Turn
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
    assert turn1.title == "Initial Task"

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
