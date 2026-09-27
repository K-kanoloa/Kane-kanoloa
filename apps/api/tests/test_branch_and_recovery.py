"""Kane vNext: Comprehensive Branch Replay, Startup Reconciliation, and Multi-Task Isolation Unit & Integration Tests.

Covers:
A. Branch Replay (§16, §17, §18, §37.E, §40):
   - Branch created from historical Message inside the same Conversation.
   - Sliced history stops at branch point; original history unmodified.
   - Isolated new DSH native session under same logical agent identity.
   - Two-way history isolation (main does not leak into branch, branch does not leak into main).
   - Replay strictly enforces reconstruction safety (passive context only, no side-effect re-execution).
   - Branch survives Store restart.
   - KanaloaAdapter honest capability declaration: branch_mode = 'replay'.

B. Startup Reconciliation & Recovery (§30, §37.I, §37.J, §42 Rules 21 & 22):
   - Thin physical liveness probe based on native session / process / transport truth.
   - Recoverable turn remains running / resumes.
   - Unrecoverable turn transitions to interrupted with unrecoverable reason.
   - Partial output strictly preserved across crash / restart.
   - Zero blind automatic reruns of user prompt.
   - waiting_user turns remain waiting_user; stale permissions fail-closed.
   - Terminal turns (finished, failed, interrupted) remain unchanged.
   - Simulated full process restart integration test.

C. Multi-Task Isolation (§37.D, §41, §42 Rules 7 & 8):
   - Two concurrent Turns under same Conversation.
   - Isolated native sessions.
   - Targeted Steer does not affect other Turn.
   - Cancel on one Turn does not affect other Turn.
   - Approval on one Turn does not affect other Turn.
   - Streaming deltas and partial_output isolated.

D. Focus Turn Discipline (§11, §14):
   - set_focus_turn explicitly updates focus_turn_id.
   - Explicit target_turn_id dispatch does NOT implicitly change conversation focus_turn_id.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.base import AgentCapabilities
from app.adapters.kanaloa_adapter import KanaloaAdapter
from app.adapters.mock_adapter import MockAdapter
from app.domain.models import BranchBoundary, Conversation, Message, Turn
from app.harness.coordinator import HarnessCoordinator
from app.harness.dispatcher import Dispatcher
from app.harness.mailbox import MailboxManager
from app.store.sqlite_store import SQLiteStore


# --- Fixtures ---
@pytest.fixture
def harness_env():
    store = SQLiteStore(":memory:")
    mbx_mgr = MailboxManager()
    coord = HarnessCoordinator(store, mbx_mgr)
    dispatcher = Dispatcher(store, coord, mbx_mgr)
    coord.register_adapter("kanaloa", MockAdapter(
        capabilities=AgentCapabilities(branch_mode="replay")
    ))
    coord.register_adapter("mock", MockAdapter(
        capabilities=AgentCapabilities(branch_mode="replay")
    ))
    return store, mbx_mgr, coord, dispatcher


class WireBranchMockKanaloaAdapter(KanaloaAdapter):
    """Test helper for wire-level Kanaloa ACP branch, replay, and recovery tests."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.session_new_calls: list[dict[str, Any]] = []
        self.prompt_requests: list[dict[str, Any]] = []
        self.resume_requests: list[dict[str, Any]] = []
        self._new_session_counter = 1
        self.is_process_alive_mock = True
        self.resumable_sessions: set[str] = set()

    def is_alive(self) -> bool:
        return self.is_process_alive_mock

    async def _ensure_process(self) -> None:
        self._is_initialized = True

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "initialize":
            return {"result": {"agentInfo": {"name": "dsh-acp", "version": "0.0.1"}}}
        if method == "session/new":
            sess_id = f"sess_native_{self._new_session_counter}"
            self._new_session_counter += 1
            self.session_new_calls.append({"sessionId": sess_id, "params": params})
            self._active_sessions.add(sess_id)
            return {"result": {"sessionId": sess_id}}
        if method == "session/prompt":
            self.prompt_requests.append(params or {})
            return {"result": {"stopReason": "endTurn"}}
        if method == "session/resume":
            self.resume_requests.append(params or {})
            sess_id = params.get("sessionId") if params else None
            if sess_id and sess_id in self.resumable_sessions:
                self._active_sessions.add(sess_id)
                return {"result": {}}
            return {"error": {"code": -32001, "message": "Session not found or expired"}}
        if method == "session/cancel":
            return {"result": {}}
        return {"result": {}}


# ==============================================================================
# A. Branch Replay Tests (§16, §17, §18, §37.E, §40)
# ==============================================================================

def test_branch_from_historical_message(harness_env):
    """
    Verify creating a branch from a historical message:
    - Same conversation_id.
    - Establishes history boundary ending at message_id (BranchBoundary).
    - Creates isolated Turn with branch_id = branch.branch_id.
    - Does not create a new Conversation object.
    - Decouples Branch from Turn (one Branch can host multiple subsequent Turns).
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="conv_main", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    turn_main = Turn(turn_id="turn_main_1", conversation_id="conv_main", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn_main)

    m1 = Message(message_id="m1", conversation_id="conv_main", turn_id="turn_main_1", sender="user", content="Step 1")
    m2 = Message(message_id="m2", conversation_id="conv_main", turn_id="turn_main_1", sender="agent", content="Done 1", parent_id="m1")
    m3 = Message(message_id="m3", conversation_id="conv_main", turn_id="turn_main_1", sender="user", content="Step 2", parent_id="m2")
    m4 = Message(message_id="m4", conversation_id="conv_main", turn_id="turn_main_1", sender="agent", content="Done 2", parent_id="m3")
    for m in [m1, m2, m3, m4]:
        store.append_message(m)

    # Branch from m3
    branch_turn = dispatcher.branch_from_message("conv_main", "m3", title="Alternative Step 2")

    assert branch_turn.conversation_id == "conv_main"
    assert branch_turn.branch_id != "main"
    branch = store.get_branch(branch_turn.branch_id)
    assert branch is not None
    assert branch.branch_point_message_id == "m3"
    assert branch.name == "Alternative Step 2"
    assert branch_turn.title == "Alternative Step 2"
    assert branch_turn.turn_id != "turn_main_1"
    assert branch_turn.status == "running"

    # Verify invalid message_id raises ValueError
    with pytest.raises(ValueError, match="Message 'nonexistent' not found"):
        dispatcher.branch_from_message("conv_main", "nonexistent")

    # Verify message belonging to different conversation raises ValueError
    other_conv = Conversation(conversation_id="conv_other", bound_agent_id="kanaloa")
    store.save_conversation(other_conv)
    m_other = Message(message_id="m_other", conversation_id="conv_other", sender="user", content="other")
    store.append_message(m_other)

    with pytest.raises(ValueError, match="belongs to conversation 'conv_other', not 'conv_main'"):
        dispatcher.branch_from_message("conv_main", "m_other")


def test_branch_visible_history_stops_at_branch_point(harness_env):
    """Verify that branch visible history strictly stops at branch_point_message_id."""
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c1", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    t_main = Turn(turn_id="t_main", conversation_id="c1", bound_agent_id="kanaloa")
    store.save_turn(t_main)

    m1 = Message(message_id="m1", conversation_id="c1", turn_id="t_main", sender="user", content="1")
    m2 = Message(message_id="m2", conversation_id="c1", turn_id="t_main", sender="agent", content="2", parent_id="m1")
    m3 = Message(message_id="m3", conversation_id="c1", turn_id="t_main", sender="user", content="3", parent_id="m2")
    m4 = Message(message_id="m4", conversation_id="c1", turn_id="t_main", sender="agent", content="4", parent_id="m3")
    m5 = Message(message_id="m5", conversation_id="c1", turn_id="t_main", sender="user", content="5", parent_id="m4")
    for m in [m1, m2, m3, m4, m5]:
        store.append_message(m)

    branch_turn = dispatcher.branch_from_message("c1", "m3")
    history = dispatcher.get_turn_history(branch_turn)

    assert [m.message_id for m in history] == ["m1", "m2", "m3"]
    assert "m4" not in [m.message_id for m in history]
    assert "m5" not in [m.message_id for m in history]


def test_branch_does_not_modify_original_history(harness_env):
    """Verify original history remains completely intact and append-only after branching."""
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c1", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    t_main = Turn(turn_id="t_main", conversation_id="c1", bound_agent_id="kanaloa")
    store.save_turn(t_main)

    m1 = Message(message_id="m1", conversation_id="c1", turn_id="t_main", sender="user", content="1")
    m2 = Message(message_id="m2", conversation_id="c1", turn_id="t_main", sender="agent", content="2", parent_id="m1")
    m3 = Message(message_id="m3", conversation_id="c1", turn_id="t_main", sender="user", content="3", parent_id="m2")
    m4 = Message(message_id="m4", conversation_id="c1", turn_id="t_main", sender="agent", content="4", parent_id="m3")
    for m in [m1, m2, m3, m4]:
        store.append_message(m)

    # Create branch
    dispatcher.branch_from_message("c1", "m2")

    # Original messages in store must be identical
    all_msgs = store.get_messages("c1")
    assert len(all_msgs) == 4
    assert [m.message_id for m in all_msgs] == ["m1", "m2", "m3", "m4"]
    assert all_msgs[3].content == "4"


@pytest.mark.asyncio
async def test_branch_uses_new_native_session(harness_env):
    """Verify branch creation uses a brand new isolated native session."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    conv = Conversation(conversation_id="c1", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    # 1. Main turn runs and gets native session 1
    t_main = Turn(turn_id="t_main", conversation_id="c1", bound_agent_id="kanaloa", status="running")
    store.save_turn(t_main)
    await dispatcher.dispatch_user_message("c1", "main msg 1", target_turn_id="t_main")
    await asyncio.sleep(0.05)

    sess_main = store.get_turn("t_main").native_session_ref
    assert sess_main == "sess_native_1"

    # Append agent reply to main
    m_main_user = store.get_messages("c1")[0]
    m_main_reply = Message(message_id="m_reply_1", conversation_id="c1", turn_id="t_main", sender="agent", content="reply 1", parent_id=m_main_user.message_id)
    store.append_message(m_main_reply)

    # 2. Branch from m_main_reply
    branch_turn = dispatcher.branch_from_message("c1", "m_reply_1")
    assert branch_turn.native_session_ref is None

    # 3. Dispatch user message to branch turn
    await dispatcher.dispatch_user_message("c1", "branch alternative", target_turn_id=branch_turn.turn_id)
    await asyncio.sleep(0.05)

    sess_branch = store.get_turn(branch_turn.turn_id).native_session_ref
    assert sess_branch is not None
    assert sess_branch == "sess_native_2"
    assert sess_branch != sess_main

    # Two separate native sessions created
    assert len(kanaloa.session_new_calls) == 2


@pytest.mark.asyncio
async def test_branch_keeps_same_agent_identity(harness_env):
    """Verify branch retains the same bound logical agent identity."""
    store, mbx_mgr, coord, dispatcher = harness_env
    conv = Conversation(conversation_id="c_id", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    t_main = Turn(turn_id="t_main", conversation_id="c_id", bound_agent_id="kanaloa")
    store.save_turn(t_main)
    m1 = Message(message_id="m1", conversation_id="c_id", turn_id="t_main", sender="user", content="hello")
    store.append_message(m1)

    branch_turn = dispatcher.branch_from_message("c_id", "m1")
    assert branch_turn.bound_agent_id == "kanaloa"


@pytest.mark.asyncio
async def test_branch_new_messages_do_not_pollute_main(harness_env):
    """Verify new messages in branch turn do not pollute main turn visible history."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    conv = Conversation(conversation_id="c_poll", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    # Main turn
    t_main = Turn(turn_id="t_main", conversation_id="c_poll", bound_agent_id="kanaloa", status="running")
    store.save_turn(t_main)
    await dispatcher.dispatch_user_message("c_poll", "M1", target_turn_id="t_main")
    await asyncio.sleep(0.02)
    m1 = store.get_messages("c_poll")[0]

    # Branch from M1
    t_branch = dispatcher.branch_from_message("c_poll", m1.message_id)
    await dispatcher.dispatch_user_message("c_poll", "M2_Branch", target_turn_id=t_branch.turn_id)
    await asyncio.sleep(0.02)

    # Main history check: must NOT contain M2_Branch
    main_hist = dispatcher.get_turn_history(t_main)
    assert any(m.content == "M1" for m in main_hist)
    assert "M2_Branch" not in [m.content for m in main_hist]


@pytest.mark.asyncio
async def test_main_new_messages_do_not_pollute_branch(harness_env):
    """Verify subsequent main turn messages do not leak into branch turn history."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    conv = Conversation(conversation_id="c_cross", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    t_main = Turn(turn_id="t_main", conversation_id="c_cross", bound_agent_id="kanaloa", status="running")
    store.save_turn(t_main)
    await dispatcher.dispatch_user_message("c_cross", "M1_Main", target_turn_id="t_main")
    await asyncio.sleep(0.02)
    m1 = store.get_messages("c_cross")[0]

    # Branch from M1
    t_branch = dispatcher.branch_from_message("c_cross", m1.message_id)

    # Main continues with M2_Main and M3_Main
    await dispatcher.dispatch_user_message("c_cross", "M2_Main", target_turn_id="t_main")
    await asyncio.sleep(0.02)
    await dispatcher.dispatch_user_message("c_cross", "M3_Main", target_turn_id="t_main")
    await asyncio.sleep(0.02)

    # Branch visible history check: must strictly contain M1_Main, no M2_Main or M3_Main
    branch_hist = dispatcher.get_turn_history(t_branch)
    assert any(m.content == "M1_Main" for m in branch_hist)
    assert "M2_Main" not in [m.content for m in branch_hist]
    assert "M3_Main" not in [m.content for m in branch_hist]


@pytest.mark.asyncio
async def test_branch_reconstruction_is_passive_context_only(harness_env):
    """
    Verify reconstruction safety (§18, §31):
    Historical messages M1..M2 are passed as passive context blocks in prompt payload,
    only the new message is an executable prompt.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    conv = Conversation(conversation_id="c_reconst", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    t_main = Turn(turn_id="t_main", conversation_id="c_reconst", bound_agent_id="kanaloa", status="running")
    store.save_turn(t_main)
    await dispatcher.dispatch_user_message("c_reconst", "Historical prompt 1", target_turn_id="t_main")
    await asyncio.sleep(0.02)
    m1 = store.get_messages("c_reconst")[0]

    # Branch from M1
    t_branch = dispatcher.branch_from_message("c_reconst", m1.message_id)
    await dispatcher.dispatch_user_message("c_reconst", "Branch prompt 2", target_turn_id=t_branch.turn_id)
    await asyncio.sleep(0.02)

    # Check payload sent for the branch session
    assert len(kanaloa.prompt_requests) == 2
    branch_payload = kanaloa.prompt_requests[1]
    prompt_blocks = branch_payload["prompt"]

    # Historical messages are passed as passive context blocks (read-only transcript)
    assert any("[USER CONTEXT]: Historical prompt 1" in b.get("text", "") for b in prompt_blocks[:-1])
    # Active executable prompt is strictly the new message
    assert prompt_blocks[-1]["text"] == "Branch prompt 2"


def test_branch_survives_store_restart():
    """Verify branch metadata, branch point, and lineage walk survive full Store restart."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "kane_branch.db")

        # 1. First Store instance
        store1 = SQLiteStore(db_path)
        conv = Conversation(conversation_id="c_persist", bound_agent_id="kanaloa")
        store1.save_conversation(conv)

        t_main = Turn(turn_id="t_main", conversation_id="c_persist", bound_agent_id="kanaloa", status="running")
        store1.save_turn(t_main)

        m1 = Message(message_id="m1", conversation_id="c_persist", turn_id="t_main", sender="user", content="root")
        m2 = Message(message_id="m2", conversation_id="c_persist", turn_id="t_main", sender="agent", content="root reply", parent_id="m1")
        m3 = Message(message_id="m3", conversation_id="c_persist", turn_id="t_main", sender="user", content="main continuation", parent_id="m2")
        for m in [m1, m2, m3]:
            store1.append_message(m)

        # Branch from m2: BranchBoundary + Turn
        branch_obj = BranchBoundary(
            branch_id="branch_persist_1",
            conversation_id="c_persist",
            branch_point_message_id="m2",
            name="Persist Branch",
        )
        store1.save_branch(branch_obj)

        t_branch = Turn(
            turn_id="t_branch",
            conversation_id="c_persist",
            bound_agent_id="kanaloa",
            branch_id="branch_persist_1",
            status="running",
        )
        store1.save_turn(t_branch)

        m_branch = Message(
            message_id="mb1",
            conversation_id="c_persist",
            turn_id="t_branch",
            sender="user",
            content="branch continuation",
            parent_id="m2",
        )
        store1.append_message(m_branch)

        # Close store 1
        store1.close()

        # 2. Re-open second Store instance from same SQLite DB
        store2 = SQLiteStore(db_path)
        mbx2 = MailboxManager()
        coord2 = HarnessCoordinator(store2, mbx2)
        disp2 = Dispatcher(store2, coord2, mbx2)

        # Verify turns and branches
        loaded_main = store2.get_turn("t_main")
        loaded_branch = store2.get_turn("t_branch")
        assert loaded_main is not None
        assert loaded_branch is not None
        assert loaded_branch.branch_id == "branch_persist_1"

        loaded_branch_boundary = store2.get_branch("branch_persist_1")
        assert loaded_branch_boundary is not None
        assert loaded_branch_boundary.branch_point_message_id == "m2"

        # Verify lineage histories
        branch_history = disp2.get_turn_history(loaded_branch)
        assert [m.message_id for m in branch_history] == ["m1", "m2", "mb1"]
        assert "m3" not in [m.message_id for m in branch_history]

        main_history = disp2.get_turn_history(loaded_main)
        assert [m.message_id for m in main_history] == ["m1", "m2", "m3"]
        assert "mb1" not in [m.message_id for m in main_history]

        store2.close()


def test_one_branch_can_host_multiple_turns(harness_env):
    """
    Verify architectural invariant (Branch != Turn, §16, §17):
    Conversation
    └─ Branch B
       ├─ Turn X: research code
       └─ Turn Y: New Task download files
    Both Turn X and Turn Y operate in Branch B, sharing Branch B's history worldline,
    while completely isolated from Main and from any other branches.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    conv = Conversation(conversation_id="c_multi_turn", bound_agent_id="mock")
    store.save_conversation(conv)

    t_main = dispatcher.create_new_turn("c_multi_turn", title="Main Task 1")
    # Main messages
    m1 = Message(message_id="m1", conversation_id="c_multi_turn", turn_id=t_main.turn_id, sender="user", content="Step 1")
    m2 = Message(message_id="m2", conversation_id="c_multi_turn", turn_id=t_main.turn_id, sender="agent", content="Step 1 Reply", parent_id="m1")
    m3 = Message(message_id="m3", conversation_id="c_multi_turn", turn_id=t_main.turn_id, sender="user", content="Step 2 Main", parent_id="m2")
    for m in [m1, m2, m3]:
        store.append_message(m)

    # User branches from m2
    turn_x = dispatcher.branch_from_message("c_multi_turn", "m2", title="Turn X: Research")
    branch_id = turn_x.branch_id
    assert branch_id != "main"

    # Turn X produces messages in Branch B
    mx1 = Message(message_id="mx1", conversation_id="c_multi_turn", turn_id=turn_x.turn_id, sender="user", content="Researching code", parent_id="m2")
    mx2 = Message(message_id="mx2", conversation_id="c_multi_turn", turn_id=turn_x.turn_id, sender="agent", content="Code analysis done", parent_id="mx1")
    store.append_message(mx1)
    store.append_message(mx2)

    # User creates "New Task" inside Branch B -> Turn Y
    turn_y = dispatcher.create_new_turn("c_multi_turn", branch_id=branch_id, title="Turn Y: Download files")
    assert turn_y.branch_id == branch_id
    assert turn_y.turn_id != turn_x.turn_id

    # Turn Y produces message in Branch B
    my1 = Message(message_id="my1", conversation_id="c_multi_turn", turn_id=turn_y.turn_id, sender="user", content="Download dataset", parent_id="mx2")
    store.append_message(my1)

    # Verify Turn Y sees: [m1, m2] (from base lineage) + [mx1, mx2] (Turn X) + [my1] (Turn Y)
    hist_y = dispatcher.get_turn_history(turn_y)
    assert [m.message_id for m in hist_y] == ["m1", "m2", "mx1", "mx2", "my1"]
    assert "m3" not in [m.message_id for m in hist_y]

    # Verify Main Turn sees only [m1, m2, m3], never mx1, mx2, my1
    hist_main = dispatcher.get_turn_history(t_main)
    assert [m.message_id for m in hist_main] == ["m1", "m2", "m3"]
    assert "mx1" not in [m.message_id for m in hist_main]
    assert "my1" not in [m.message_id for m in hist_main]


def test_branch_lineage_no_rowid_cross_pollution(harness_env):
    """
    Verify user directive:
    M1 -> M2
    M2 -> Branch X -> MX  (written first into DB)
    M2 -> Main -> M3      (written later into DB)
    Now branch from M3 -> Branch Y.
    Must strictly follow parent_id lineage: Branch Y must NEVER include MX,
    even though MX was physically inserted before M3.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    conv = Conversation(conversation_id="c_tree_iso", bound_agent_id="mock")
    store.save_conversation(conv)

    t_main = dispatcher.create_new_turn("c_tree_iso", title="Main")

    # M1 and M2
    m1 = Message(message_id="m1", conversation_id="c_tree_iso", turn_id=t_main.turn_id, sender="user", content="M1")
    m2 = Message(message_id="m2", conversation_id="c_tree_iso", turn_id=t_main.turn_id, sender="agent", content="M2", parent_id="m1")
    store.append_message(m1)
    store.append_message(m2)

    # Branch X from M2, written FIRST into DB
    t_x = dispatcher.branch_from_message("c_tree_iso", "m2", title="Branch X")
    mx = Message(message_id="mx", conversation_id="c_tree_iso", turn_id=t_x.turn_id, sender="user", content="MX in branch X", parent_id="m2")
    store.append_message(mx)

    # Main continues with M3, written LATER into DB
    m3 = Message(message_id="m3", conversation_id="c_tree_iso", turn_id=t_main.turn_id, sender="user", content="M3 in main", parent_id="m2")
    store.append_message(m3)

    # Now branch from M3 -> Branch Y
    t_y = dispatcher.branch_from_message("c_tree_iso", "m3", title="Branch Y")
    my = Message(message_id="my", conversation_id="c_tree_iso", turn_id=t_y.turn_id, sender="user", content="MY in branch Y", parent_id="m3")
    store.append_message(my)

    # Branch Y visible history check:
    # Must strictly be [m1, m2, m3, my]. MX must NEVER be present!
    hist_y = dispatcher.get_turn_history(t_y)
    assert [m.message_id for m in hist_y] == ["m1", "m2", "m3", "my"]
    assert "mx" not in [m.message_id for m in hist_y]

    # Branch X visible history check:
    # Must strictly be [m1, m2, mx]. Neither m3 nor my must be present!
    hist_x = dispatcher.get_turn_history(t_x)
    assert [m.message_id for m in hist_x] == ["m1", "m2", "mx"]
    assert "m3" not in [m.message_id for m in hist_x]
    assert "my" not in [m.message_id for m in hist_x]


# ==============================================================================
# B. Startup Reconciliation & Recovery Tests (§30, §37.I, §37.J, §42)
# ==============================================================================

@pytest.mark.asyncio
async def test_startup_reconcile_running_turn_with_live_native_session(harness_env):
    """Verify startup reconciliation preserves 'running' when native session is verified alive."""
    store, mbx_mgr, coord, dispatcher = harness_env
    adapter = MockAdapter()
    coord.register_adapter("mock_agent", adapter)

    # Setup running turn with active session in memory
    turn = Turn(turn_id="t_live", conversation_id="c_rec", bound_agent_id="mock_agent", status="running", native_session_ref="sess_active_1")
    store.save_turn(turn)
    adapter.live_sessions.add("sess_active_1")

    # Reconciliation pass
    reconciled = await coord.reconcile_startup_turns()

    assert reconciled.get("t_live") == "running"
    assert store.get_turn("t_live").status == "running"


@pytest.mark.asyncio
async def test_startup_reconcile_running_turn_with_resumable_session(harness_env):
    """Verify startup reconciliation successfully recovers resumable session over ACP."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    turn = Turn(turn_id="t_resumable", conversation_id="c_rec", bound_agent_id="kanaloa", status="running", native_session_ref="sess_saved_100")
    store.save_turn(turn)
    kanaloa.resumable_sessions.add("sess_saved_100")

    reconciled = await coord.reconcile_startup_turns()

    assert reconciled.get("t_resumable") == "running"
    assert store.get_turn("t_resumable").status == "running"
    assert len(kanaloa.resume_requests) == 1
    assert kanaloa.resume_requests[0]["sessionId"] == "sess_saved_100"


@pytest.mark.asyncio
async def test_startup_reconcile_running_turn_with_lost_session(harness_env):
    """
    Verify startup reconciliation marks turn 'interrupted' when native session is lost.
    Preserves partial_output, does NOT rerun.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    turn = Turn(
        turn_id="t_lost",
        conversation_id="c_rec",
        bound_agent_id="kanaloa",
        status="running",
        native_session_ref="sess_nonexistent",
        partial_output="Analyzing progress up to step 4...",
    )
    store.save_turn(turn)

    reconciled = await coord.reconcile_startup_turns()

    assert reconciled.get("t_lost") == "interrupted"
    t_after = store.get_turn("t_lost")
    assert t_after.status == "interrupted"
    assert t_after.interrupt_reason == "unrecoverable:session_lost_on_startup"
    # Rule 22: partial_output must survive interruption
    assert t_after.partial_output == "Analyzing progress up to step 4..."
    # Rule 21: No rerun prompt
    assert len(kanaloa.prompt_requests) == 0


@pytest.mark.asyncio
async def test_startup_reconcile_running_turn_when_process_exited(harness_env):
    """Verify startup reconciliation marks turn 'interrupted' when agent process has exited."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    kanaloa.is_process_alive_mock = False  # Subprocess exited / dead
    coord.register_adapter("kanaloa", kanaloa)

    turn = Turn(turn_id="t_dead_proc", conversation_id="c_rec", bound_agent_id="kanaloa", status="running", native_session_ref="sess_1")
    store.save_turn(turn)

    reconciled = await coord.reconcile_startup_turns()

    assert reconciled.get("t_dead_proc") == "interrupted"
    assert store.get_turn("t_dead_proc").status == "interrupted"


@pytest.mark.asyncio
async def test_startup_reconcile_resume_failure_marks_interrupted(harness_env):
    """Verify failed session/resume RPC marks turn as interrupted."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    turn = Turn(turn_id="t_fail_res", conversation_id="c_rec", bound_agent_id="kanaloa", status="running", native_session_ref="sess_fail")
    store.save_turn(turn)

    reconciled = await coord.reconcile_startup_turns()
    assert reconciled.get("t_fail_res") == "interrupted"
    assert store.get_turn("t_fail_res").status == "interrupted"


@pytest.mark.asyncio
async def test_startup_reconcile_waiting_user_is_not_blindly_resumed(harness_env):
    """Verify waiting_user turns remain waiting_user during reconciliation pass."""
    store, mbx_mgr, coord, dispatcher = harness_env
    turn = Turn(turn_id="t_wait", conversation_id="c_rec", bound_agent_id="mock", status="waiting_user")
    store.save_turn(turn)

    reconciled = await coord.reconcile_startup_turns()

    assert "t_wait" not in reconciled
    assert store.get_turn("t_wait").status == "waiting_user"


@pytest.mark.asyncio
async def test_startup_reconcile_terminal_turns_unchanged(harness_env):
    """Verify terminal turns (finished, failed, interrupted) are never altered during reconciliation."""
    store, mbx_mgr, coord, dispatcher = harness_env

    t_fin = Turn(turn_id="t_fin", conversation_id="c1", bound_agent_id="kanaloa", status="finished")
    t_fail = Turn(turn_id="t_fail", conversation_id="c1", bound_agent_id="kanaloa", status="failed")
    t_int = Turn(turn_id="t_int", conversation_id="c1", bound_agent_id="kanaloa", status="interrupted", partial_output="half")
    for t in [t_fin, t_fail, t_int]:
        store.save_turn(t)

    reconciled = await coord.reconcile_startup_turns()
    assert len(reconciled) == 0

    assert store.get_turn("t_fin").status == "finished"
    assert store.get_turn("t_fail").status == "failed"
    assert store.get_turn("t_int").status == "interrupted"
    assert store.get_turn("t_int").partial_output == "half"


@pytest.mark.asyncio
async def test_startup_reconcile_real_restart_lifecycle():
    """Integration test simulating crash and restart with separate instances."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "kane_reconcile.db")

        # 1. Before crash: Turn running with unrecoverable session
        store1 = SQLiteStore(db_path)
        t_crash = Turn(
            turn_id="t_crashed_task",
            conversation_id="conv_crash",
            bound_agent_id="kanaloa",
            status="running",
            native_session_ref="sess_crashed_pid",
            partial_output="Work in progress before crash",
        )
        store1.save_turn(t_crash)
        store1.close()

        # 2. After restart: New store, coordinator, adapter
        store2 = SQLiteStore(db_path)
        mbx2 = MailboxManager()
        coord2 = HarnessCoordinator(store2, mbx2)
        adapter2 = WireBranchMockKanaloaAdapter(event_handler=coord2)
        # adapter2 does NOT know sess_crashed_pid
        coord2.register_adapter("kanaloa", adapter2)

        # Run startup reconciliation
        reconciled = await coord2.reconcile_startup_turns()

        assert reconciled.get("t_crashed_task") == "interrupted"
        t_recovered = store2.get_turn("t_crashed_task")
        assert t_recovered.status == "interrupted"
        assert t_recovered.interrupt_reason == "unrecoverable:session_lost_on_startup"
        assert t_recovered.partial_output == "Work in progress before crash"
        assert len(adapter2.prompt_requests) == 0  # No blind rerun

        store2.close()


# ==============================================================================
# C. Multi-Task Isolation Tests (§37.D, §41, §42 Rules 7 & 8)
# ==============================================================================

@pytest.mark.asyncio
async def test_multi_task_two_turns_isolated_native_sessions(harness_env):
    """Verify two concurrent Turns have strictly isolated native sessions and Mailboxes."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = WireBranchMockKanaloaAdapter(event_handler=coord)
    coord.register_adapter("kanaloa", kanaloa)

    conv = Conversation(conversation_id="c_multi", bound_agent_id="kanaloa")
    store.save_conversation(conv)

    # Explicitly create two turns in same conversation
    turn_a = dispatcher.create_new_turn("c_multi", title="Task A")
    turn_b = dispatcher.create_new_turn("c_multi", title="Task B")

    await dispatcher.dispatch_user_message("c_multi", "Start A", target_turn_id=turn_a.turn_id)
    await asyncio.sleep(0.02)
    await dispatcher.dispatch_user_message("c_multi", "Start B", target_turn_id=turn_b.turn_id)
    await asyncio.sleep(0.02)

    sess_a = store.get_turn(turn_a.turn_id).native_session_ref
    sess_b = store.get_turn(turn_b.turn_id).native_session_ref

    assert sess_a is not None
    assert sess_b is not None
    assert sess_a != sess_b
    assert sess_a == "sess_native_1"
    assert sess_b == "sess_native_2"

    # Mailboxes are distinct
    mbx_a = mbx_mgr.get_mailbox(turn_a.turn_id)
    mbx_b = mbx_mgr.get_mailbox(turn_b.turn_id)
    assert mbx_a is not mbx_b


@pytest.mark.asyncio
async def test_multi_task_steer_one_turn_does_not_affect_other(harness_env):
    """Verify steering Turn A targets Turn A only, leaving Turn B untouched."""
    store, mbx_mgr, coord, dispatcher = harness_env
    adapter = MockAdapter()
    coord.register_adapter("mock", adapter)

    conv = Conversation(conversation_id="c_steer_iso", bound_agent_id="mock")
    store.save_conversation(conv)

    turn_a = dispatcher.create_new_turn("c_steer_iso", title="Turn A")
    turn_b = dispatcher.create_new_turn("c_steer_iso", title="Turn B")

    # Steer Turn A
    steer_msg = Message(conversation_id="c_steer_iso", turn_id=turn_a.turn_id, sender="user", content="Steer A")
    await adapter.steer(turn_a, steer_msg)

    assert len(adapter.steer_calls) == 1
    assert adapter.steer_calls[0]["turn"].turn_id == turn_a.turn_id
    assert adapter.steer_calls[0]["message"].content == "Steer A"

    # Turn B has no steer calls
    assert all(c["turn"].turn_id != turn_b.turn_id for c in adapter.steer_calls)


@pytest.mark.asyncio
async def test_multi_task_cancel_one_turn_does_not_affect_other(harness_env):
    """Verify cancelling Turn A transitions Turn A to interrupted while Turn B remains running."""
    store, mbx_mgr, coord, dispatcher = harness_env
    adapter = MockAdapter()
    coord.register_adapter("mock", adapter)

    conv = Conversation(conversation_id="c_cancel_iso", bound_agent_id="mock")
    store.save_conversation(conv)

    turn_a = dispatcher.create_new_turn("c_cancel_iso", title="Turn A")
    turn_b = dispatcher.create_new_turn("c_cancel_iso", title="Turn B")

    # Cancel Turn A
    await dispatcher.cancel_turn(turn_a.turn_id, reason="user_cancelled_task_a")

    assert store.get_turn(turn_a.turn_id).status == "interrupted"
    assert store.get_turn(turn_a.turn_id).interrupt_reason == "user_cancelled_task_a"

    # Turn B remains running
    assert store.get_turn(turn_b.turn_id).status == "running"


@pytest.mark.asyncio
async def test_multi_task_approval_one_turn_does_not_affect_other(harness_env):
    """Verify permission request on Turn A pauses Turn A in waiting_user while Turn B continues running."""
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c_appr_iso", bound_agent_id="mock")
    store.save_conversation(conv)

    turn_a = dispatcher.create_new_turn("c_appr_iso", title="Turn A")
    turn_b = dispatcher.create_new_turn("c_appr_iso", title="Turn B")

    # Turn A requests permission
    await coord.emit_waiting_user(turn_a.turn_id, prompt="Allow shell command?")

    assert store.get_turn(turn_a.turn_id).status == "waiting_user"
    # Turn B must remain running!
    assert store.get_turn(turn_b.turn_id).status == "running"


@pytest.mark.asyncio
async def test_multi_task_output_stream_isolated(harness_env):
    """Verify streaming deltas for Turn A and Turn B accumulate into their respective buffers."""
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c_stream_iso", bound_agent_id="mock")
    store.save_conversation(conv)

    turn_a = dispatcher.create_new_turn("c_stream_iso", title="Turn A")
    turn_b = dispatcher.create_new_turn("c_stream_iso", title="Turn B")

    await coord.emit_delta(turn_a.turn_id, "Chunk A1. ")
    await coord.emit_delta(turn_b.turn_id, "Chunk B1. ")
    await coord.emit_delta(turn_a.turn_id, "Chunk A2.")
    await coord.emit_delta(turn_b.turn_id, "Chunk B2.")

    assert store.get_turn(turn_a.turn_id).partial_output == "Chunk A1. Chunk A2."
    assert store.get_turn(turn_b.turn_id).partial_output == "Chunk B1. Chunk B2."


# ==============================================================================
# D. Focus Turn Discipline (§11, §14)
# ==============================================================================

def test_set_focus_turn_explicit_choice(harness_env):
    """Verify set_focus_turn explicitly updates conversation focus_turn_id."""
    store, mbx_mgr, coord, dispatcher = harness_env

    conv = Conversation(conversation_id="c_foc", bound_agent_id="mock")
    store.save_conversation(conv)

    t1 = dispatcher.create_new_turn("c_foc", title="Turn 1")
    t2 = dispatcher.create_new_turn("c_foc", title="Turn 2")

    # Initially focus is t2 (most recently created)
    assert store.get_conversation("c_foc").focus_turn_id == t2.turn_id

    # User explicitly sets focus to t1
    dispatcher.set_focus_turn("c_foc", t1.turn_id)
    assert store.get_conversation("c_foc").focus_turn_id == t1.turn_id


@pytest.mark.asyncio
async def test_explicit_turn_dispatch_does_not_implicitly_change_focus(harness_env):
    """
    Verify user directive:
    Dispatching to an explicit target_turn_id does NOT implicitly change the conversation focus_turn_id.
    Focus is only changed when explicitly requested.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    adapter = MockAdapter()
    coord.register_adapter("mock", adapter)

    conv = Conversation(conversation_id="c_no_imp", bound_agent_id="mock")
    store.save_conversation(conv)

    t_focus = dispatcher.create_new_turn("c_no_imp", title="Focus Turn")
    t_background = dispatcher.create_new_turn("c_no_imp", title="Background Turn")

    # Set focus explicitly to t_focus
    dispatcher.set_focus_turn("c_no_imp", t_focus.turn_id)
    assert store.get_conversation("c_no_imp").focus_turn_id == t_focus.turn_id

    # Dispatch to background turn with explicit_turn_id
    await dispatcher.dispatch_user_message(
        conversation_id="c_no_imp",
        content="Background message",
        target_turn_id=t_background.turn_id,
    )

    # Focus must remain t_focus!
    assert store.get_conversation("c_no_imp").focus_turn_id == t_focus.turn_id


@pytest.mark.asyncio
async def test_cancel_and_inspect_do_not_implicitly_change_focus(harness_env):
    """
    Verify user directive:
    Explicit actions on background turn (such as cancel, inspect) do NOT implicitly modify focus_turn_id.
    Only explicit set_focus_turn changes focus.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    adapter = MockAdapter()
    coord.register_adapter("mock", adapter)

    conv = Conversation(conversation_id="c_cancel_focus", bound_agent_id="mock")
    store.save_conversation(conv)

    t_focus = dispatcher.create_new_turn("c_cancel_focus", title="Focus Turn")
    t_background = dispatcher.create_new_turn("c_cancel_focus", title="Background Turn")

    dispatcher.set_focus_turn("c_cancel_focus", t_focus.turn_id)
    assert store.get_conversation("c_cancel_focus").focus_turn_id == t_focus.turn_id

    # Cancel background turn
    await dispatcher.cancel_turn(t_background.turn_id, reason="cancel_bg")

    # Focus must remain t_focus!
    assert store.get_conversation("c_cancel_focus").focus_turn_id == t_focus.turn_id
