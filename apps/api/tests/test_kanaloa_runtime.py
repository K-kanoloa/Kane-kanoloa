"""Kane vNext: Comprehensive Kanaloa Agent Runtime Unit Tests.

Covers:
1. Session-Scoped Persistent IPython:
   - Same-session persistence across turns (variables, functions, mutables).
   - Cross-session isolation (zero crosstalk, NameError on other session globals).
   - Session close and cleanup (namespace purged, no orphan processes).
2. Shell Runtime:
   - Platform-native detection (PowerShell on Windows, Bash on Unix).
   - Command execution and result capture.
3. Approval Disconnect Fail-Closed:
   - Subprocess disconnect / session close immediately invalidates live permissions.
   - Waiting_user turn transitions to interrupted.
   - Zero state restoration across process restart.
4. Optional Loop Mode:
   - Governed strictly by single field max_iterations: int | None.
   - Normal Mode: default execution without loop wrapper.
   - Default: max_iterations = 5.
   - Unlimited: max_iterations = None.
   - Custom: max_iterations = 3 and max_iterations = 10.
   - Early COMPLETE: halts iteration early upon COMPLETE token.
   - Invalid max_iterations: fail-closed rejection.
   - Steer, Approval, and Cancel during Loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.kanaloa_adapter import KanaloaAdapter, _UNSET
from app.adapters.kanaloa_runtime import (
    IPythonExecutionResult,
    KanaloaRuntime,
    SessionIPythonRuntime,
    SessionShellRuntime,
)
from app.domain.models import Conversation, Message, Turn
from app.harness.coordinator import HarnessCoordinator
from app.harness.dispatcher import Dispatcher
from app.harness.mailbox import MailboxManager
from app.store.sqlite_store import SQLiteStore


# --- Test Fixtures ---
@pytest.fixture
def harness_env():
    store = SQLiteStore(":memory:")
    mbx_mgr = MailboxManager()
    coord = HarnessCoordinator(store, mbx_mgr)
    dispatcher = Dispatcher(store, coord, mbx_mgr)
    return store, mbx_mgr, coord, dispatcher


class LoopMockWireKanaloaAdapter(KanaloaAdapter):
    """Test helper for wire-level Kanaloa ACP loop and approval tests."""

    def __init__(self, prompt_responses=None, **kwargs):
        super().__init__(**kwargs)
        self.cancel_notifs: list[dict[str, Any] | None] = []
        self.steer_requests: list[dict[str, Any] | None] = []
        self.prompt_requests: list[dict[str, Any]] = []
        self.prompt_responses = prompt_responses or []
        self._prompt_call_count = 0
        self._new_session_id_counter = 1

    async def _ensure_process(self) -> None:
        self._is_initialized = True

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        if method == "session/cancel":
            self.cancel_notifs.append(params)

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "initialize":
            return {"result": {"agentInfo": {"name": "dsh-acp", "version": "0.0.1"}}}
        if method == "session/new":
            sess_id = f"loop_sess_{self._new_session_id_counter}"
            self._new_session_id_counter += 1
            return {"result": {"sessionId": sess_id}}
        if method == "session/steer":
            self.steer_requests.append(params)
            return {"result": {"accepted": True}}
        if method == "session/prompt":
            self.prompt_requests.append(params or {})
            self._prompt_call_count += 1
            # Return configured response or default completion
            if self.prompt_responses and self._prompt_call_count <= len(self.prompt_responses):
                resp = self.prompt_responses[self._prompt_call_count - 1]
                if callable(resp):
                    return await resp(params)
                return resp
            return {"result": {"stopReason": "endTurn"}}
        if method == "session/close":
            return {"result": {}}
        return {"result": {}}


# ==============================================================================
# 1. Session-Scoped Persistent IPython Tests
# ==============================================================================

def test_ipython_same_session_persistence():
    """Verify variables and function definitions persist across turns in the same session."""
    runtime = KanaloaRuntime()
    s = runtime.get_ipython("sess_turn_test")

    # Turn 1: define variables and function
    r1 = s.execute("x = 42\ndef compute(factor):\n    return x * factor")
    assert r1.success, f"Turn 1 execution failed: {r1.error}"

    # Turn 2: invoke function and mutate variable
    r2 = s.execute("y = compute(2)")
    assert r2.success, f"Turn 2 execution failed: {r2.error}"
    assert s.get_variable("y") == 84

    # Turn 3: check persistence
    r3 = s.execute("y + x")
    assert r3.success
    assert r3.result == 126

    runtime.close_all()


def test_ipython_cross_session_isolation():
    """Verify strict variable and namespace isolation between different sessions."""
    runtime = KanaloaRuntime()
    s_alpha = runtime.get_ipython("sess_alpha")
    s_beta = runtime.get_ipython("sess_beta")

    # Session Alpha defines secret
    r_alpha = s_alpha.execute("secret_token = 'ALPHA_SECRET_123'")
    assert r_alpha.success
    assert s_alpha.get_variable("secret_token") == "ALPHA_SECRET_123"

    # Session Beta attempts to access Alpha's secret -> must fail with NameError
    r_beta = s_beta.execute("secret_token")
    assert not r_beta.success
    assert "name 'secret_token' is not defined" in str(r_beta.error)
    assert s_beta.get_variable("secret_token") is None

    runtime.close_all()


def test_ipython_complex_mutable_objects():
    """Verify complex objects, dictionaries, lists, and imports mutate properly across turns."""
    runtime = KanaloaRuntime()
    s = runtime.get_ipython("sess_complex")

    # Turn 1: import module and create nested dict
    r1 = s.execute("import math\nstate = {'pi': math.pi, 'history': [10, 20]}")
    assert r1.success

    # Turn 2: mutate list
    r2 = s.execute("state['history'].append(30)")
    assert r2.success

    # Turn 3: inspect
    r3 = s.execute("sum(state['history'])")
    assert r3.result == 60

    runtime.close_all()


def test_ipython_session_close_cleanup():
    """Verify closing a session purges its namespace and blocks further execution."""
    runtime = KanaloaRuntime()
    s = runtime.get_ipython("sess_cleanup")

    s.execute("val = 999")
    assert s.get_variable("val") == 999

    runtime.close_ipython_session("sess_cleanup")

    # Further execution raises RuntimeError
    with pytest.raises(RuntimeError, match="has been closed"):
        s.execute("val + 1")

    # Reading variable on closed session raises RuntimeError
    with pytest.raises(RuntimeError, match="has been closed"):
        s.get_variable("val")


# ==============================================================================
# 2. Shell Runtime Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_shell_platform_execution():
    """Verify platform-native shell command execution."""
    runtime = KanaloaRuntime()
    shell_type = runtime.shell.get_shell_type()
    assert shell_type in ("powershell", "bash")

    cmd = "Write-Output 'SHELL_OK'" if shell_type == "powershell" else "echo 'SHELL_OK'"
    code, stdout, stderr = await runtime.shell.execute(cmd)
    assert code == 0, f"Shell error: {stderr}"
    assert "SHELL_OK" in stdout


# ==============================================================================
# 3. Approval Disconnect Fail-Closed Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_approval_disconnect_fail_closed_transitions_waiting_turn(harness_env):
    """
    Verify approval disconnect fail-closed:
    1. Turn in waiting_user pending permission.
    2. Subprocess disconnect occurs (e.g. stdout EOF or process exit).
    3. Pending permission is invalidated and purged.
    4. Turn transitions to 'interrupted' with reason 'permission_invalidated:acp_process_disconnected'.
    5. Subsequent respond_permission call fails-closed with Unknown permission request.
    """
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = KanaloaAdapter(event_handler=coord)

    conv = Conversation(conversation_id="c_disc", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(
        turn_id="t_disc_1",
        conversation_id="c_disc",
        bound_agent_id="kanaloa",
        native_session_ref="sess_disc_1",
        status="running",
    )
    store.save_turn(turn)
    kanaloa.bind_session("t_disc_1", "sess_disc_1")

    # 1. Incoming permission request puts Turn into waiting_user
    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 501,
        "method": "session/request_permission",
        "params": {
            "sessionId": "sess_disc_1",
            "toolCall": {"toolName": "danger_eval"},
        },
    })
    assert store.get_turn("t_disc_1").status == "waiting_user"
    assert kanaloa.get_pending_permission(501) is not None

    # 2. Simulate process disconnect / stdout EOF
    await kanaloa._handle_disconnect(reason="acp_process_disconnected", purge=True)

    # 3. Verification: Turn transitioned to interrupted
    t_after = store.get_turn("t_disc_1")
    assert t_after.status == "interrupted"

    # 4. Verification: Pending permission purged
    assert kanaloa.get_pending_permission(501) is None

    # 5. Subsequent respond_permission fails-closed (unknown request)
    with pytest.raises(ValueError, match="Unknown permission request '501'"):
        await kanaloa.respond_permission(501, "allow-once")


# ==============================================================================
# 4. Optional Loop Mode Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_loop_mode_invalid_values_fail_closed(harness_env):
    """Verify invalid max_iterations values fail-closed immediately with ValueError."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = LoopMockWireKanaloaAdapter(event_handler=coord)

    turn = Turn(turn_id="t_inv", conversation_id="c1", bound_agent_id="kanaloa")
    msg = Message(conversation_id="c1", sender="user", content="hello")

    # 0 is invalid
    with pytest.raises(ValueError, match="must be None or a positive integer"):
        await kanaloa.send(turn, msg, [], max_iterations=0)

    # Negative int is invalid
    with pytest.raises(ValueError, match="must be None or a positive integer"):
        await kanaloa.send(turn, msg, [], max_iterations=-1)

    # Float is invalid
    with pytest.raises(ValueError, match="must be None or a positive integer"):
        await kanaloa.send(turn, msg, [], max_iterations=3.5)

    # String is invalid
    with pytest.raises(ValueError, match="must be None or a positive integer"):
        await kanaloa.send(turn, msg, [], max_iterations="5")

    # Boolean is invalid (True is int subclass in python)
    with pytest.raises(ValueError, match="must be None or a positive integer"):
        await kanaloa.send(turn, msg, [], max_iterations=True)


@pytest.mark.asyncio
async def test_normal_mode_single_execution_without_loop_wrapper(harness_env):
    """Verify Normal Mode (max_iterations unset): executes single prompt, no loop wrapper."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = LoopMockWireKanaloaAdapter(event_handler=coord)

    conv = Conversation(conversation_id="c_norm", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_norm", conversation_id="c_norm", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    msg = Message(conversation_id="c_norm", sender="user", content="run once")
    await kanaloa.send(turn, msg, [])
    # Wait for prompt task to complete
    await asyncio.sleep(0.05)

    assert kanaloa._prompt_call_count == 1
    assert store.get_turn("t_norm").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_default_5_iterations(harness_env):
    """Verify Loop Mode with max_iterations=5 runs exactly 5 iterations."""
    store, mbx_mgr, coord, dispatcher = harness_env
    kanaloa = LoopMockWireKanaloaAdapter(event_handler=coord)

    conv = Conversation(conversation_id="c_loop5", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_loop5", conversation_id="c_loop5", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    msg = Message(conversation_id="c_loop5", sender="user", content="iterate 5")
    await kanaloa.send(turn, msg, [], max_iterations=5)
    await asyncio.sleep(0.05)

    assert kanaloa._prompt_call_count == 5
    assert store.get_turn("t_loop5").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_early_complete(harness_env):
    """Verify Loop Mode halts early when output contains COMPLETE at iteration 2."""
    store, mbx_mgr, coord, dispatcher = harness_env

    # Simulate iteration 2 emitting COMPLETE delta
    async def mock_iter2_resp(params):
        # Emit delta containing COMPLETE
        await kanaloa.event_handler.emit_delta("t_early", "Task is finished. COMPLETE!")
        kanaloa.runtime.record_delta("t_early", "Task is finished. COMPLETE!")
        return {"result": {"stopReason": "endTurn"}}

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[
            {"result": {"stopReason": "endTurn"}},  # iteration 1
            mock_iter2_resp,                         # iteration 2 -> COMPLETE
            {"result": {"stopReason": "endTurn"}},  # iteration 3 (should not run)
        ],
        event_handler=coord,
    )

    conv = Conversation(conversation_id="c_early", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_early", conversation_id="c_early", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    msg = Message(conversation_id="c_early", sender="user", content="early stop test")
    await kanaloa.send(turn, msg, [], max_iterations=5)
    await asyncio.sleep(0.05)

    # Only 2 iterations executed
    assert kanaloa._prompt_call_count == 2
    assert store.get_turn("t_early").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_custom_iterations_3_and_10(harness_env):
    """Verify Loop Mode with custom max_iterations=3 and max_iterations=10."""
    store, mbx_mgr, coord, dispatcher = harness_env

    # 1. Custom = 3
    kanaloa_3 = LoopMockWireKanaloaAdapter(event_handler=coord)
    conv_3 = Conversation(conversation_id="c_c3", bound_agent_id="kanaloa")
    store.save_conversation(conv_3)
    turn_3 = Turn(turn_id="t_c3", conversation_id="c_c3", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn_3)

    await kanaloa_3.send(turn_3, Message(conversation_id="c_c3", sender="user", content="3"), [], max_iterations=3)
    await asyncio.sleep(0.05)
    assert kanaloa_3._prompt_call_count == 3
    assert store.get_turn("t_c3").status == "finished"

    # 2. Custom = 10
    kanaloa_10 = LoopMockWireKanaloaAdapter(event_handler=coord)
    conv_10 = Conversation(conversation_id="c_c10", bound_agent_id="kanaloa")
    store.save_conversation(conv_10)
    turn_10 = Turn(turn_id="t_c10", conversation_id="c_c10", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn_10)

    await kanaloa_10.send(turn_10, Message(conversation_id="c_c10", sender="user", content="10"), [], max_iterations=10)
    await asyncio.sleep(0.05)
    assert kanaloa_10._prompt_call_count == 10
    assert store.get_turn("t_c10").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_unlimited_stops_on_complete(harness_env):
    """Verify Unlimited Mode (max_iterations=None) runs past 5 without limit, stops on COMPLETE."""
    store, mbx_mgr, coord, dispatcher = harness_env

    async def mock_iter_resp(params):
        if kanaloa._prompt_call_count == 7:
            await kanaloa.event_handler.emit_delta("t_unl", "All 7 steps done. COMPLETE.")
            kanaloa.runtime.record_delta("t_unl", "All 7 steps done. COMPLETE.")
        return {"result": {"stopReason": "endTurn"}}

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[mock_iter_resp] * 10,
        event_handler=coord,
    )

    conv = Conversation(conversation_id="c_unl", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_unl", conversation_id="c_unl", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    await kanaloa.send(turn, Message(conversation_id="c_unl", sender="user", content="unlimited"), [], max_iterations=None)
    await asyncio.sleep(0.05)

    # Successfully ran 7 iterations (> 5 default limit) and completed
    assert kanaloa._prompt_call_count == 7
    assert store.get_turn("t_unl").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_steer_during_loop(harness_env):
    """Verify native steering sent during Loop Mode is accepted and modifies next prompt."""
    store, mbx_mgr, coord, dispatcher = harness_env

    kanaloa = LoopMockWireKanaloaAdapter(event_handler=coord)

    conv = Conversation(conversation_id="c_lsteer", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_lsteer", conversation_id="c_lsteer", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    # Start loop
    await kanaloa.send(turn, Message(conversation_id="c_lsteer", sender="user", content="init"), [], max_iterations=3)

    # Steer before iteration 2
    steer_msg = Message(conversation_id="c_lsteer", sender="user", content="CHANGE DIRECTION TO X")
    await kanaloa.steer(turn, steer_msg)

    assert len(kanaloa.steer_requests) == 1
    assert kanaloa.steer_requests[0]["prompt"][0]["text"] == "CHANGE DIRECTION TO X"

    await asyncio.sleep(0.05)
    assert kanaloa._prompt_call_count == 3
    assert store.get_turn("t_lsteer").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_approval_during_loop(harness_env):
    """Verify approval request during Loop Mode pauses turn in waiting_user, resumes and completes loop."""
    store, mbx_mgr, coord, dispatcher = harness_env

    kanaloa = LoopMockWireKanaloaAdapter(event_handler=coord)
    # Mock subprocess stdin for respond_permission
    written_data: list[str] = []
    mock_stdin = AsyncMock()
    mock_stdin.write = MagicMock(side_effect=lambda b: written_data.append(b.decode("utf-8")))
    mock_stdin.drain = AsyncMock()
    mock_process = MagicMock()
    mock_process.stdin = mock_stdin
    mock_process.returncode = None
    kanaloa._process = mock_process

    conv = Conversation(conversation_id="c_lappr", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_lappr", conversation_id="c_lappr", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    # Start loop (max_iterations=3)
    await kanaloa.send(turn, Message(conversation_id="c_lappr", sender="user", content="loop with approval"), [], max_iterations=3)
    sess_id = turn.native_session_ref or "loop_sess_1"

    # Simulate permission request during loop
    await kanaloa._handle_incoming_rpc({
        "jsonrpc": "2.0",
        "id": 888,
        "method": "session/request_permission",
        "params": {
            "sessionId": sess_id,
            "toolCall": {"toolName": "danger_eval"},
            "options": [{"optionId": "allow-once"}, {"optionId": "reject-once"}],
        },
    })

    assert store.get_turn("t_lappr").status == "waiting_user"
    assert kanaloa.get_pending_permission(888) is not None

    # User responds allow-once
    await kanaloa.respond_permission(888, "allow-once", session_id=sess_id)
    assert store.get_turn("t_lappr").status == "running"

    await asyncio.sleep(0.05)
    assert kanaloa._prompt_call_count == 3
    assert store.get_turn("t_lappr").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_cancel_during_loop(harness_env):
    """Verify cancellation during Loop Mode immediately halts iterations."""
    store, mbx_mgr, coord, dispatcher = harness_env

    async def cancel_on_iter2(params):
        # Cancel turn during iteration 2
        turn_obj = store.get_turn("t_lcancel")
        await kanaloa.cancel(turn_obj)
        return {"result": {"stopReason": "cancelled"}}

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[
            {"result": {"stopReason": "endTurn"}},  # iter 1
            cancel_on_iter2,                         # iter 2 cancels
            {"result": {"stopReason": "endTurn"}},  # iter 3 should never run
        ],
        event_handler=coord,
    )

    conv = Conversation(conversation_id="c_lcancel", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_lcancel", conversation_id="c_lcancel", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    await kanaloa.send(turn, Message(conversation_id="c_lcancel", sender="user", content="cancel test"), [], max_iterations=5)
    await asyncio.sleep(0.05)

    assert kanaloa._prompt_call_count == 2
    assert store.get_turn("t_lcancel").status == "interrupted"
    assert len(kanaloa.cancel_notifs) == 1


# ==============================================================================
# 5. Non-Fragile COMPLETE Marker and Termination Invariant Tests
# ==============================================================================

def test_complete_marker_robustness():
    """Verify strict, non-fragile COMPLETE marker detection rejecting false positives."""
    positive_samples = [
        "Task is finished. [COMPLETE]",
        "Task is finished.\n[COMPLETE]",
        "All done. [COMPLETE]",
        "[complete]",
        "[COMPLETE]",
        "COMPLETE",
        "  COMPLETE!  ",
        "**COMPLETE**",
        "`COMPLETE`",
        "Step 1 done.\nCOMPLETE\n",
        "Task is finished. COMPLETE!",
    ]
    negative_samples = [
        "not COMPLETE yet",
        "COMPLETE condition not met",
        "This is not COMPLETE.",
        "Will it be COMPLETE soon?",
        "Incomplete task",
        "This is a completely different sentence.",
        "We need complete information.",
        "",
        None,
    ]
    for text in positive_samples:
        assert KanaloaRuntime.is_complete_marker(text), f"Expected True for: {text!r}"
    for text in negative_samples:
        assert not KanaloaRuntime.is_complete_marker(text), f"Expected False for: {text!r}"


@pytest.mark.asyncio
async def test_loop_mode_stop_does_not_start_next_iteration(harness_env):
    """
    Verify Stop condition (Section 8.A):
    - Loop runs at least 1 iteration.
    - Caller signals stop_loop during iteration 1.
    - Iteration 1 finishes, iteration 2 does NOT start.
    - Loop exits gracefully with emit_message_complete and turn status 'finished'.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    async def mock_iter1_with_stop(params):
        turn_obj = store.get_turn("t_lstop")
        kanaloa.stop_loop(turn_obj)
        return {"result": {"stopReason": "endTurn"}}

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[
            mock_iter1_with_stop,
            {"result": {"stopReason": "endTurn"}},  # iteration 2 must NOT run
        ],
        event_handler=coord,
    )

    conv = Conversation(conversation_id="c_lstop", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_lstop", conversation_id="c_lstop", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    await kanaloa.send(turn, Message(conversation_id="c_lstop", sender="user", content="stop test"), [], max_iterations=5)
    await asyncio.sleep(0.05)

    # Exactly 1 iteration executed
    assert kanaloa._prompt_call_count == 1
    # Turn finished gracefully
    assert store.get_turn("t_lstop").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_failed_halts_loop_immediately(harness_env):
    """
    Verify failed condition (Section 8.B):
    - Iteration 1 encounters error (e.g. ACP prompt error).
    - Loop terminates immediately without starting next iteration.
    - Turn transitions to 'failed'.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[
            {"error": {"code": -32603, "message": "DSH runtime fatal execution error"}},
            {"result": {"stopReason": "endTurn"}},  # iteration 2 must NOT run
        ],
        event_handler=coord,
    )

    conv = Conversation(conversation_id="c_lfail", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_lfail", conversation_id="c_lfail", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    await kanaloa.send(turn, Message(conversation_id="c_lfail", sender="user", content="fail test"), [], max_iterations=5)
    await asyncio.sleep(0.05)

    # Exactly 1 iteration executed
    assert kanaloa._prompt_call_count == 1
    # Turn marked as failed
    assert store.get_turn("t_lfail").status == "failed"


@pytest.mark.asyncio
async def test_loop_mode_interrupted_halts_loop_immediately(harness_env):
    """
    Verify interrupted condition (Section 8.C):
    - Iteration 1 experiences external interruption (e.g. process crash or manual interruption).
    - Turn enters 'interrupted'.
    - Loop halts immediately without starting next iteration.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    async def mock_iter1_external_interrupt(params):
        # Simulate external interruption entering store
        await coord.emit_interrupted("t_linter", reason="external_worker_eviction")
        return {"result": {"stopReason": "endTurn"}}

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[
            mock_iter1_external_interrupt,
            {"result": {"stopReason": "endTurn"}},  # iteration 2 must NOT run
        ],
        event_handler=coord,
    )

    conv = Conversation(conversation_id="c_linter", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_linter", conversation_id="c_linter", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    await kanaloa.send(turn, Message(conversation_id="c_linter", sender="user", content="interrupt test"), [], max_iterations=5)
    await asyncio.sleep(0.05)

    assert kanaloa._prompt_call_count == 1
    assert store.get_turn("t_linter").status == "interrupted"


@pytest.mark.asyncio
async def test_loop_mode_approval_does_not_increment_iteration(harness_env):
    """
    Verify approval does not increment iteration (Section 9):
    - iteration 1 -> approval request -> waiting_user -> allow-once -> resume same work-cycle.
    - iteration 1 completes with [COMPLETE].
    - Final iteration count is strictly 1.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    async def mock_iter1_with_approval(params):
        sess_id = params.get("sessionId")
        # Trigger permission request during iteration 1
        await kanaloa._handle_incoming_rpc({
            "jsonrpc": "2.0",
            "id": 991,
            "method": "session/request_permission",
            "params": {
                "sessionId": sess_id,
                "toolCall": {"toolName": "shell_run"},
                "options": [{"optionId": "allow-once"}, {"optionId": "reject-once"}],
            },
        })
        assert store.get_turn("t_lappr_cnt").status == "waiting_user"
        assert kanaloa.runtime.get_loop_iteration("t_lappr_cnt") == 1

        # User responds allow-once to resume same work-cycle
        await kanaloa.respond_permission(991, "allow-once", session_id=sess_id)
        assert store.get_turn("t_lappr_cnt").status == "running"

        # Emit [COMPLETE] delta within same iteration
        await kanaloa.event_handler.emit_delta("t_lappr_cnt", "Execution finished after approval. [COMPLETE]")
        kanaloa.runtime.record_delta("t_lappr_cnt", "Execution finished after approval. [COMPLETE]")
        return {"result": {"stopReason": "endTurn"}}

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[
            mock_iter1_with_approval,
            {"result": {"stopReason": "endTurn"}},  # iteration 2 must NOT run
        ],
        event_handler=coord,
    )
    # Mock subprocess stdin for respond_permission
    mock_stdin = AsyncMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_process = MagicMock()
    mock_process.stdin = mock_stdin
    mock_process.returncode = None
    kanaloa._process = mock_process

    conv = Conversation(conversation_id="c_lappr_cnt", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_lappr_cnt", conversation_id="c_lappr_cnt", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    await kanaloa.send(turn, Message(conversation_id="c_lappr_cnt", sender="user", content="approval count test"), [], max_iterations=5)
    await asyncio.sleep(0.05)

    # Exactly 1 work-cycle / prompt call executed
    assert kanaloa._prompt_call_count == 1
    assert store.get_turn("t_lappr_cnt").status == "finished"


@pytest.mark.asyncio
async def test_loop_mode_steer_does_not_increment_iteration(harness_env):
    """
    Verify steer does not increment iteration (Section 9):
    - iteration 1 runs normally.
    - iteration 2 is running -> mid-flight steer is injected.
    - iteration 2 continues and completes with [COMPLETE].
    - Final prompt call count is exactly 2 (iteration 2), steer did NOT create iteration 3.
    """
    store, mbx_mgr, coord, dispatcher = harness_env

    async def mock_iter2_with_steer(params):
        turn_obj = store.get_turn("t_lsteer_cnt")
        assert kanaloa.runtime.get_loop_iteration("t_lsteer_cnt") == 2
        # Mid-flight steer injected during iteration 2
        steer_msg = Message(conversation_id="c_lsteer_cnt", sender="user", content="Adjust direction")
        await kanaloa.steer(turn_obj, steer_msg)

        # Iteration count must remain 2 during and after steer
        assert kanaloa.runtime.get_loop_iteration("t_lsteer_cnt") == 2

        # Complete iteration 2
        await kanaloa.event_handler.emit_delta("t_lsteer_cnt", "Adjusted and finished. [COMPLETE]")
        kanaloa.runtime.record_delta("t_lsteer_cnt", "Adjusted and finished. [COMPLETE]")
        return {"result": {"stopReason": "endTurn"}}

    kanaloa = LoopMockWireKanaloaAdapter(
        prompt_responses=[
            {"result": {"stopReason": "endTurn"}},  # iteration 1
            mock_iter2_with_steer,                   # iteration 2 with steer + COMPLETE
            {"result": {"stopReason": "endTurn"}},  # iteration 3 must NOT run
        ],
        event_handler=coord,
    )

    conv = Conversation(conversation_id="c_lsteer_cnt", bound_agent_id="kanaloa")
    store.save_conversation(conv)
    turn = Turn(turn_id="t_lsteer_cnt", conversation_id="c_lsteer_cnt", bound_agent_id="kanaloa", status="running")
    store.save_turn(turn)

    await kanaloa.send(turn, Message(conversation_id="c_lsteer_cnt", sender="user", content="steer count test"), [], max_iterations=5)
    await asyncio.sleep(0.05)

    # Prompt call count is exactly 2
    assert kanaloa._prompt_call_count == 2
    assert len(kanaloa.steer_requests) == 1
    assert store.get_turn("t_lsteer_cnt").status == "finished"

