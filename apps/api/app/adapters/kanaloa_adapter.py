"""Kanaloa Adapter for DeepSeek Harness (DSH).

Kanaloa uses the Official DeepSeek Harness ACP (Agent Client Protocol) JSON-RPC 2.0 stdio
as its sole official transport.

Disciplines:
- Strictly responsible for:
  1. Protocol translation (ACP JSON-RPC 2.0 stdio <-> Kane BaseAdapter contract)
  2. Session binding (Turn <-> ACP native sessionId)
  3. Honest capability declaration verified by real tests against official DSH ACP:
     - supports_cancel = True (verified via session/cancel notification)
     - supports_resume = True (verified via session/resume with cwd)
     - supports_stream = True (verified via session/update notifications)
     - supports_approval = True (verified: session/request_permission -> waiting_user -> respond_permission roundtrip over ACP stdio)
     - supports_parallel_sessions = True (verified via multiple sessions on single connection)
     - max_parallel_sessions = None (not statically capped by adapter; bound by DSH/system resources)
     - steer_mode = 'native' (verified: session/steer extension submits steering to active native session mid-flight)
     - branch_mode = 'unsupported'
  4. Two-state Prompt semantics with Non-Negotiable Side-Effect Boundary:
     - State A (Active Session): incremental prompt only
     - State B (Bootstrap / Session Rebuild): reconstruction != replay historical execution
       Historical messages are passed ONLY as passive context transcript blocks;
       guaranteeing 0 mechanical replay / 0 command queue replay.
       Only current new message is executed as the active prompt.
       External side-effect idempotency and approval policies remain governed by the Agent/Tool/Host layers.
  5. Process & transport liveness over official DSH ACP profile (`npx @deepseek-ai/dsh --profile acp`).
  6. Thinking & stream persistence boundary:
     - Raw streaming deltas stay in memory / partial_output buffer (zero raw chunk dump into turn_events).
     - Thinking events map to coarse live status ({"status": "thinking", "summary": ...}) for UI observation only.
- ZERO in-tree vendoring, ZERO core pollution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import sys
import time
from typing import Any, Literal

from ..domain.models import AgentCapabilities, Message, Turn, current_iso
from .base import AgentEventHandler, BaseAdapter
from .kanaloa_runtime import KanaloaRuntime

logger = logging.getLogger(__name__)

_UNSET = object()


@dataclass
class PendingPermissionRequest:
    request_id: str | int  # JSON-RPC request id from DSH ACP
    session_id: str
    turn_id: str | None
    tool_call: dict[str, Any]
    options: list[dict[str, Any]]
    created_at: float
    status: Literal["pending", "resolved", "cancelled", "expired", "failed"] = "pending"
    decision: str | None = None


class KanaloaAdapter(BaseAdapter):
    """
    Adapter bridging Kane Harness to the Official DeepSeek Harness (DSH)
    over standard Agent Client Protocol (ACP) JSON-RPC 2.0 stdio.
    """

    def __init__(
        self,
        command: list[str] | None = None,
        cwd: str | None = None,
        event_handler: AgentEventHandler | None = None,
    ) -> None:
        super().__init__(event_handler)
        self.cwd = cwd or os.getcwd()

        if command:
            self.command = command
        else:
            # Deterministic downstream resolution: prefer project-local DSH if present, fallback to npx
            local_dsh = os.path.join(self.cwd, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
            if not os.path.exists(local_dsh):
                repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
                candidate = os.path.join(repo_root, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
                if os.path.exists(candidate):
                    local_dsh = candidate

            if os.path.exists(local_dsh):
                self.command = ["node", local_dsh, "--profile", "acp"]
            else:
                default_npx = "npx.cmd" if sys.platform == "win32" else "npx"
                self.command = [default_npx, "--yes", "@deepseek-ai/dsh", "--profile", "acp"]

        self._process: asyncio.subprocess.Process | None = None
        self._turn_sessions: dict[str, str] = {}  # turn_id -> native_sessionId
        self._session_turns: dict[str, str] = {}  # native_sessionId -> turn_id
        self._active_sessions: set[str] = set()    # active sessionIds
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._req_counter = 1
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._is_initialized = False
        self._lock = asyncio.Lock()
        self._pending_permissions: dict[str, PendingPermissionRequest] = {}
        self.runtime = KanaloaRuntime()
        self._active_loops: dict[str, dict[str, Any]] = {}
        self._iteration_outputs: dict[str, list[str]] = {}

        # Audit properties for testing & verification
        self.last_sent_payload: dict[str, Any] | None = None
        self.last_stop_reason: str | None = None

    def capabilities(self) -> AgentCapabilities:
        """
        Honest capability declaration verified by real tests against official DSH ACP profile:
        - supports_cancel = True (verified: session/cancel notification cancels in-flight prompt)
        - supports_resume = True (verified: session/resume restores session config & context)
        - supports_stream = True (verified: session/update delivers stream content blocks)
        - supports_approval = True (verified: session/request_permission -> waiting_user -> respond_permission roundtrip over ACP stdio)
        - supports_parallel_sessions = True (verified: multiple sessions on single connection)
        - max_parallel_sessions = None (not statically capped by adapter; actual concurrency bound by DSH/system resources)
        - steer_mode = 'native' (verified: session/steer extension submits steering to active native session mid-flight)
        - branch_mode = 'unsupported'
        """
        return AgentCapabilities(
            supports_stream=True,
            supports_resume=True,
            supports_cancel=True,
            supports_approval=True,
            supports_parallel_sessions=True,
            max_parallel_sessions=None,
            steer_mode="native",
            branch_mode="unsupported",
        )

    # --- Session Binding ---
    def get_native_session(self, turn_id: str) -> str | None:
        return self._turn_sessions.get(turn_id)

    def bind_session(self, turn_id: str, native_session_id: str) -> None:
        self._turn_sessions[turn_id] = native_session_id
        self._session_turns[native_session_id] = turn_id

    # --- Transport & Process Liveness ---
    def is_alive(self) -> bool:
        if self._process is None:
            return False
        return self._process.returncode is None

    async def _ensure_process(self) -> None:
        """Ensure ACP subprocess is launched and initialized."""
        async with self._lock:
            if self.is_alive() and self._is_initialized:
                return

            logger.info("KanaloaAdapter: launching ACP subprocess: %s", " ".join(self.command))
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._read_stdout_loop())
            self._stderr_task = asyncio.create_task(self._read_stderr_loop())

            # Perform ACP initialize handshake
            init_resp = await self._send_request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "kane-harness", "version": "2.0.0"},
                    "capabilities": {},
                },
            )
            if "error" in init_resp:
                raise RuntimeError(f"ACP initialization failed: {init_resp['error']}")
            self._is_initialized = True
            logger.info("KanaloaAdapter: initialized successfully with agentInfo: %s", init_resp.get("result", {}).get("agentInfo"))

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send JSON-RPC request and await response."""
        assert self._process is not None and self._process.stdin is not None
        req_id = self._req_counter
        self._req_counter += 1

        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future

        data = json.dumps(msg, ensure_ascii=False) + "\n"
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

        return await future

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send JSON-RPC notification (no id, no response expected)."""
        assert self._process is not None and self._process.stdin is not None
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        data = json.dumps(msg, ensure_ascii=False) + "\n"
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_stdout_loop(self) -> None:
        """Background loop reading JSON-RPC lines from ACP server."""
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                msg = json.loads(line_str)
                await self._handle_incoming_rpc(msg)
            except json.JSONDecodeError:
                logger.warning("KanaloaAdapter: received non-JSON line from stdout: %s", line_str)

        # Fail-closed cleanup when ACP subprocess stdout closes / process disconnects
        await self._handle_disconnect(reason="acp_process_disconnected")

    async def _read_stderr_loop(self) -> None:
        """Background loop logging stderr."""
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            logger.debug("[ACP STDERR] %s", line.decode("utf-8", errors="replace").strip())

    async def _handle_incoming_rpc(self, msg: dict[str, Any]) -> None:
        """Route incoming JSON-RPC response or notification."""
        # 1. Response to a pending request
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending_requests:
            future = self._pending_requests.pop(msg_id)
            if not future.done():
                future.set_result(msg)
            return

        # 2. Server -> Client Notification
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "session/update":
            await self._handle_session_update(params)
        elif method == "session/request_permission":
            await self._handle_permission_request(msg)
        else:
            logger.debug("KanaloaAdapter: unhandled ACP notification: %s", method)

    async def _handle_session_update(self, params: dict[str, Any]) -> None:
        """Normalize ACP session/update into Kane AgentEventHandler facts."""
        session_id = params.get("sessionId")
        turn_id = self._session_turns.get(session_id or "")
        if not turn_id:
            return

        update = params.get("update", {})
        update_type = update.get("type")

        if update_type == "content":
            content_block = update.get("content", {})
            if content_block.get("type") == "text":
                text = content_block.get("text", "")
                if text:
                    await self.event_handler.emit_delta(turn_id, text)
                    if turn_id in self._iteration_outputs:
                        self._iteration_outputs[turn_id].append(text)
        elif update_type in ("thought", "thinking"):
            # Coarse live thinking status/event for UI; does NOT dump massive raw reasoning chains into turn_events
            thought_text = update.get("thought") or update.get("content", "")
            thinking_payload: dict[str, Any] = {"status": "thinking"}
            if isinstance(thought_text, str) and thought_text.strip():
                thinking_payload["summary"] = thought_text[:120].strip()
            await self.event_handler.emit_event(turn_id, "thinking", thinking_payload)
        elif update_type in ("tool_call", "tool_start", "tool_result", "tool_end"):
            # Coarse tool event for UI observation (mapped to canonical EventType)
            norm_event_type = "tool_start" if update_type in ("tool_call", "tool_start") else "tool_end"
            tool_name = update.get("toolName") or update.get("name") or update.get("tool", "")
            tool_payload: dict[str, Any] = {
                "type": update_type,
                "tool": str(tool_name),
            }
            if "status" in update:
                tool_payload["status"] = update["status"]
            await self.event_handler.emit_event(turn_id, norm_event_type, tool_payload)

    async def _handle_permission_request(self, msg: dict[str, Any]) -> None:
        """Handle permission request from ACP agent (e.g. tool execution / sandbox escalation)."""
        rpc_id = msg.get("id")
        params = msg.get("params", {})
        session_id = params.get("sessionId")
        turn_id = self._session_turns.get(session_id or "")

        if rpc_id is not None and session_id:
            perm_key = str(rpc_id)
            self._pending_permissions[perm_key] = PendingPermissionRequest(
                request_id=rpc_id,
                session_id=session_id,
                turn_id=turn_id,
                tool_call=params.get("toolCall", {}),
                options=params.get("options", []),
                created_at=time.time(),
                status="pending",
            )
            logger.info(
                "KanaloaAdapter: recorded pending permission request %s for session %s (turn: %s)",
                rpc_id,
                session_id,
                turn_id,
            )

        if turn_id:
            prompt_text = params.get("message")
            if not prompt_text:
                tool_call = params.get("toolCall", {})
                tool_name = tool_call.get("toolName") or tool_call.get("name") or "Tool"
                prompt_text = f"Permission required for {tool_name} (request_id: {rpc_id})"
            await self.event_handler.emit_waiting_user(
                turn_id,
                prompt=prompt_text,
            )

    def get_pending_permission(self, request_id: str | int) -> PendingPermissionRequest | None:
        """Retrieve a pending permission request by its exact request ID."""
        return self._pending_permissions.get(str(request_id))

    def list_pending_permissions(self, session_id: str | None = None) -> list[PendingPermissionRequest]:
        """List active pending permission requests, optionally filtered by session ID."""
        perms = [p for p in self._pending_permissions.values() if p.status == "pending"]
        if session_id:
            perms = [p for p in perms if p.session_id == session_id]
        return perms

    async def respond_permission(
        self,
        request_id: str | int,
        decision: Literal["allow-once", "reject-once", "cancelled"] | str,
        session_id: str | None = None,
    ) -> None:
        """
        Respond to an inbound DSH session/request_permission RPC call.

        Enforces:
        - Strict binding: must specify the exact request_id from the original permission request.
        - Session safety: if session_id is provided, asserts match to prevent cross-session crosstalk.
        - Lifecycle safety: rejects unknown, already resolved, expired, or cancelled requests.
        - Conformance: formats standard ACP SelectedPermissionOutcome or cancelled outcome.
        - Turn resumption: transitions waiting_user turn back to running upon resolution.
        """
        perm_key = str(request_id)
        perm = self._pending_permissions.get(perm_key)
        if not perm:
            raise ValueError(f"Unknown permission request '{request_id}'")

        if perm.status != "pending":
            raise ValueError(
                f"Permission request '{request_id}' is already {perm.status} (cannot be resolved)"
            )

        if session_id is not None and perm.session_id != session_id:
            raise ValueError(
                f"Session mismatch for permission request '{request_id}': "
                f"expected '{perm.session_id}', got '{session_id}'"
            )

        # Validate decision against allowed options or cancelled
        valid_options = {
            opt.get("optionId")
            for opt in perm.options
            if isinstance(opt, dict) and "optionId" in opt
        }
        if not valid_options:
            valid_options = {"allow-once", "reject-once"}

        if decision == "cancelled":
            outcome: dict[str, Any] = {"outcome": "cancelled"}
        elif decision in valid_options or decision in ("allow-once", "reject-once"):
            outcome = {
                "outcome": "selected",
                "optionId": decision,
            }
        else:
            raise ValueError(
                f"Invalid permission decision '{decision}'. "
                f"Valid options: {sorted(valid_options | {'cancelled'})}"
            )

        if not self._process or self._process.stdin is None or self._process.returncode is not None:
            perm.status = "failed"
            raise RuntimeError("ACP subprocess is not running")

        # Format exact JSON-RPC 2.0 response to DSH ACP
        resp_msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": perm.request_id,
            "result": {
                "outcome": outcome,
            },
        }
        data = json.dumps(resp_msg, ensure_ascii=False) + "\n"
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

        perm.status = "resolved"
        perm.decision = decision
        logger.info(
            "KanaloaAdapter: sent permission response %s for request %s (session %s)",
            decision,
            perm.request_id,
            perm.session_id,
        )

        # Resume Turn in Kane: transition waiting_user back to running
        turn_id = perm.turn_id
        if turn_id and hasattr(self.event_handler, "store"):
            store = getattr(self.event_handler, "store")
            turn = store.get_turn(turn_id)
            if turn and turn.status == "waiting_user":
                turn.status = "running"
                turn.last_event_at = current_iso()
                store.save_turn(turn)
                await self.event_handler.emit_event(
                    turn_id,
                    "status_change",
                    {
                        "status": "running",
                        "resumed_from": "permission_response",
                        "permission_request_id": str(request_id),
                        "decision": decision,
                    },
                )

    async def _handle_disconnect(
        self,
        session_id: str | None = None,
        reason: str = "acp_disconnected",
        purge: bool = False,
    ) -> None:
        """
        Approval disconnect fail-closed & runtime cleanup.
        Invalidates pending permissions matching session_id (or all if None).
        Transitions any waiting_user turn pending those requests to interrupted.
        Cancels active loops and releases session-scoped IPython runtimes.
        Zero state restoration across process restart.
        """
        turns_to_interrupt = set()
        keys_to_purge = []

        for req_key, perm in self._pending_permissions.items():
            if session_id is None or perm.session_id == session_id:
                if perm.status == "pending":
                    perm.status = "expired" if reason == "adapter_closed" else "cancelled"
                    if perm.turn_id:
                        turns_to_interrupt.add(perm.turn_id)
                if purge:
                    keys_to_purge.append(req_key)

        for k in keys_to_purge:
            self._pending_permissions.pop(k, None)

        if hasattr(self.event_handler, "store") and self.event_handler.store:
            store = getattr(self.event_handler, "store")
            for turn_id in turns_to_interrupt:
                turn = store.get_turn(turn_id)
                if turn and turn.status == "waiting_user":
                    await self.event_handler.emit_interrupted(
                        turn_id,
                        reason=f"permission_invalidated:{reason}",
                    )

        if session_id:
            self.runtime.close_ipython_session(session_id)
        else:
            self.runtime.close_all()

    async def close(self) -> None:
        """Clean up active sessions and shutdown ACP process."""
        # 1. Invalidate active loops and purge pending permissions & runtimes fail-closed
        for loop_meta in self._active_loops.values():
            loop_meta["cancelled"] = True
        self._active_loops.clear()
        self._iteration_outputs.clear()

        await self._handle_disconnect(reason="adapter_closed", purge=False)

        # 2. Close active sessions gracefully
        for session_id in list(self._active_sessions):
            try:
                await self._send_request("session/close", {"sessionId": session_id})
            except Exception as e:
                logger.debug("Failed closing session %s: %s", session_id, e)
        self._active_sessions.clear()
        self._turn_sessions.clear()
        self._session_turns.clear()

        # 2. Terminate subprocess
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except Exception:
                if self._process.returncode is None:
                    self._process.kill()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

        self._is_initialized = False

    # --- BaseAdapter Methods ---
    async def send(
        self,
        turn: Turn,
        message: Message,
        history: list[Message],
        max_iterations: Any = _UNSET,
    ) -> None:
        """
        Send prompt request over standard ACP JSON-RPC.

        Supports two execution modes:
        - Normal Mode (max_iterations is _UNSET): default single prompt execution over ACP.
        - Loop Mode (max_iterations is not _UNSET): governed strictly by single field max_iterations (int | None).
          - max_iterations = 5: Default
          - max_iterations = None: Unlimited
          - max_iterations = N: Custom (positive int)
          - Invalid values (0, negative, float, string): fail-closed immediately.

        Two deterministic prompt payload states:
        - State A (Active Session): If native sessionId exists and session is active,
          send ONLY the current incremental message as ContentBlock.
        - State B (Bootstrap / Session Rebuild): If session is new or rebuilt,
          RECONSTRUCTION != REPLAY HISTORICAL EXECUTION.
          Historical messages are passed ONLY as passive context transcript blocks;
          guaranteeing 0 mechanical replay / 0 command queue replay.
          Only the current new message (message.content) is executed as the active prompt.
          External side-effect idempotency and approval policies remain governed by the Agent/Tool/Host layers.
        """
        if max_iterations is not _UNSET:
            if max_iterations is not None:
                if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations <= 0:
                    raise ValueError(
                        f"Invalid max_iterations '{max_iterations}': must be None or a positive integer"
                    )

        await self._ensure_process()

        session_id = turn.native_session_ref or self.get_native_session(turn.turn_id)

        # State B: Session New / Rebuild
        if not session_id or session_id not in self._active_sessions:
            # 1. Create native ACP session
            new_resp = await self._send_request("session/new", {
                "cwd": self.cwd,
                "mcpServers": [],
            })
            if "error" in new_resp:
                raise RuntimeError(f"ACP session/new failed: {new_resp['error']}")

            session_id = new_resp["result"]["sessionId"]
            turn.native_session_ref = session_id
            self.bind_session(turn.turn_id, session_id)
            self._active_sessions.add(session_id)
            if hasattr(self.event_handler, "store") and self.event_handler.store:
                self.event_handler.store.save_turn(turn)

            # 2. Construct ACP prompt blocks:
            # Passive history context transcript blocks (read-only, NOT executed as commands)
            prompt_blocks: list[dict[str, Any]] = []
            for m in history:
                if m.message_id != message.message_id:
                    prompt_blocks.append({
                        "type": "text",
                        "text": f"[{m.sender.upper()} CONTEXT]: {m.content}",
                    })

            # ACTIVE EXECUTABLE PROMPT: Strictly the single new user message
            prompt_blocks.append({
                "type": "text",
                "text": message.content,
            })

            payload = {
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": prompt_blocks,
                },
            }
            logger.debug(
                "KanaloaAdapter: bootstrapped ACP session %s with %d context blocks",
                session_id,
                len(prompt_blocks) - 1,
            )
        else:
            turn.native_session_ref = session_id
            # State A: Active native session -> incremental message block only
            prompt_blocks = [{"type": "text", "text": message.content}]
            payload = {
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": prompt_blocks,
                },
            }
            logger.debug(
                "KanaloaAdapter: sending incremental prompt to active ACP session %s",
                session_id,
            )

        self.last_sent_payload = payload

        # Dispatch prompt in background so send() returns while prompt streams
        if max_iterations is not _UNSET:
            asyncio.create_task(self._execute_loop(turn, session_id, prompt_blocks, max_iterations))
        else:
            asyncio.create_task(self._execute_prompt(turn.turn_id, session_id, prompt_blocks))

    async def _execute_prompt(self, turn_id: str, session_id: str, prompt_blocks: list[dict[str, Any]]) -> None:
        """Execute session/prompt and normalize completion/cancellation outcome in Normal Mode."""
        try:
            resp = await self._send_request("session/prompt", {
                "sessionId": session_id,
                "prompt": prompt_blocks,
            })
            if "error" in resp:
                err_msg = resp["error"].get("message", "ACP prompt error")
                await self.event_handler.emit_failed(turn_id, reason=str(err_msg))
                return

            result = resp.get("result", {})
            stop_reason = result.get("stopReason")
            self.last_stop_reason = stop_reason

            if stop_reason == "cancelled":
                await self.event_handler.emit_interrupted(turn_id, reason="cancelled_by_acp")
            else:
                # Normal completion
                await self.event_handler.emit_message_complete(
                    turn_id=turn_id,
                    sender_id="kanaloa",
                )
        except Exception as e:
            logger.error("KanaloaAdapter prompt execution exception: %s", e)
            await self.event_handler.emit_failed(turn_id, reason=str(e))

    async def _execute_loop(
        self,
        turn: Turn,
        session_id: str,
        initial_prompt_blocks: list[dict[str, Any]],
        max_iterations: int | None,
    ) -> None:
        """
        Optional Loop Mode execution over DSH ACP stdio.
        Governed strictly by single field max_iterations: int | None.
        Runs work iterations until COMPLETE, max_iterations reached, or interrupted/cancelled.
        Emits single emit_message_complete upon completion.
        """
        turn_id = turn.turn_id
        current_prompt = initial_prompt_blocks
        iteration = 0
        loop_meta: dict[str, Any] = {
            "session_id": session_id,
            "max_iterations": max_iterations,
            "current_iteration": 0,
            "cancelled": False,
        }
        self._active_loops[turn_id] = loop_meta

        try:
            while True:
                # 1. Check if cancelled before starting iteration
                if loop_meta.get("cancelled"):
                    await self.event_handler.emit_interrupted(turn_id, reason="cancelled")
                    return

                iteration += 1
                loop_meta["current_iteration"] = iteration
                logger.info(
                    "KanaloaAdapter: starting loop iteration %d (max: %s) for turn %s (session %s)",
                    iteration,
                    max_iterations,
                    turn_id,
                    session_id,
                )

                # Reset iteration output accumulator
                self._iteration_outputs[turn_id] = []

                # 2. Execute one work iteration via session/prompt
                resp = await self._send_request("session/prompt", {
                    "sessionId": session_id,
                    "prompt": current_prompt,
                })

                if "error" in resp:
                    err_msg = resp["error"].get("message", "ACP prompt error in loop")
                    await self.event_handler.emit_failed(turn_id, reason=str(err_msg))
                    return

                result = resp.get("result", {})
                stop_reason = result.get("stopReason")
                self.last_stop_reason = stop_reason

                # Check if cancelled mid-flight
                if stop_reason == "cancelled" or loop_meta.get("cancelled"):
                    await self.event_handler.emit_interrupted(turn_id, reason="cancelled_by_acp")
                    return

                # Check turn status in store in case it was interrupted/failed externally
                if hasattr(self.event_handler, "store") and self.event_handler.store:
                    t = self.event_handler.store.get_turn(turn_id)
                    if t and t.status in ("interrupted", "failed"):
                        return

                # 3. Check for early completion: output contains 'COMPLETE'
                iteration_text = "".join(self._iteration_outputs.get(turn_id, []))
                store = getattr(self.event_handler, "store", None)
                turn_obj = store.get_turn(turn_id) if store else turn
                full_output = (turn_obj.partial_output or "") if turn_obj else ""

                is_complete = "COMPLETE" in iteration_text or "COMPLETE" in full_output

                if is_complete:
                    logger.info(
                        "KanaloaAdapter: loop early complete detected at iteration %d for turn %s",
                        iteration,
                        turn_id,
                    )
                    break

                # 4. Check if max_iterations limit reached
                if max_iterations is not None and iteration >= max_iterations:
                    logger.info(
                        "KanaloaAdapter: loop reached max_iterations (%d) for turn %s",
                        max_iterations,
                        turn_id,
                    )
                    break

                # 5. Prepare prompt for next iteration
                pending_steer = loop_meta.pop("pending_steer", None)
                if pending_steer:
                    current_prompt = [{"type": "text", "text": pending_steer}]
                else:
                    current_prompt = [{
                        "type": "text",
                        "text": "Continue with next step. Output 'COMPLETE' when finished.",
                    }]

            # Loop finished normally
            await self.event_handler.emit_message_complete(
                turn_id=turn_id,
                sender_id="kanaloa",
            )
        except Exception as e:
            logger.error("KanaloaAdapter: loop execution exception: %s", e)
            await self.event_handler.emit_failed(turn_id, reason=str(e))
        finally:
            self._active_loops.pop(turn_id, None)
            self._iteration_outputs.pop(turn_id, None)

    async def steer(
        self,
        turn: Turn,
        message: Message,
    ) -> None:
        """
        Submit native steering for the running turn directly to DSH ACP
        via the session/steer extension on the same stdio connection.
        Preserves the same Turn and native sessionId without restart.
        """
        session_id = turn.native_session_ref or self.get_native_session(turn.turn_id)
        if not session_id:
            raise ValueError(f"Cannot steer turn '{turn.turn_id}' without active native session")

        if turn.status in ("completed", "finished", "failed", "interrupted"):
            raise RuntimeError(
                f"Cannot steer turn '{turn.turn_id}': turn has terminal status '{turn.status}'"
            )

        if turn.turn_id in self._active_loops:
            self._active_loops[turn.turn_id]["pending_steer"] = message.content

        resp = await self._send_request("session/steer", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": message.content}],
        })
        if "error" in resp:
            err_msg = resp["error"].get("message", str(resp["error"]))
            raise RuntimeError(f"DSH steer failed: {err_msg}")
        logger.info(
            "KanaloaAdapter: successfully submitted native steer to session %s for turn %s",
            session_id,
            turn.turn_id,
        )

    async def cancel(
        self,
        turn: Turn,
    ) -> None:
        """
        Verified: ACP cancellation is an RPC notification 'session/cancel' with sessionId.
        """
        session_id = turn.native_session_ref or self.get_native_session(turn.turn_id)
        if not session_id:
            logger.warning("KanaloaAdapter: cancel requested but no native_session_ref on turn %s", turn.turn_id)
            return

        # Mark active loop as cancelled
        if turn.turn_id in self._active_loops:
            self._active_loops[turn.turn_id]["cancelled"] = True

        # Invalidate any pending permissions for this session fail-closed
        await self._handle_disconnect(session_id=session_id, reason="session_cancelled")

        await self._send_notification("session/cancel", {
            "sessionId": session_id,
        })
        logger.info("KanaloaAdapter: sent session/cancel notification for session %s", session_id)

    async def resume(
        self,
        turn: Turn,
        history: list[Message],
    ) -> None:
        """
        Verified: ACP session/resume restores an existing persisted session.
        """
        session_id = turn.native_session_ref or self.get_native_session(turn.turn_id)
        if not session_id:
            raise ValueError(f"Cannot resume turn '{turn.turn_id}' without native_session_ref")

        await self._ensure_process()

        resp = await self._send_request("session/resume", {
            "sessionId": session_id,
            "cwd": self.cwd,
        })
        if "error" in resp:
            raise RuntimeError(f"ACP session/resume failed: {resp['error']}")

        self._active_sessions.add(session_id)
        logger.info("KanaloaAdapter: resumed session %s successfully", session_id)
