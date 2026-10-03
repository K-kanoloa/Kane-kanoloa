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
     - branch_mode = 'replay'
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
import re
import shutil
import time
from typing import Any, Literal

from ..domain.models import AgentCapabilities, Message, Turn, current_iso
from .base import AgentEventHandler, BaseAdapter
from .kanaloa_runtime import KanaloaRuntime, normalize_acp_outcome

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
        runtime: KanaloaRuntime | None = None,
    ) -> None:
        super().__init__(event_handler)
        self.cwd = cwd or os.getcwd()

        self._default_command = command is None
        self._bundled_dsh: str | None = None
        if command:
            self.command = command
        else:
            # The pinned DSH package is bundled by npm install; never fetch a runtime on first send.
            local_dsh = os.path.join(self.cwd, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
            if not os.path.exists(local_dsh):
                repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
                candidate = os.path.join(repo_root, "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")
                if os.path.exists(candidate):
                    local_dsh = candidate

            if os.path.exists(local_dsh):
                self._bundled_dsh = local_dsh
                self.command = ["node", local_dsh, "--profile", "acp"]
            else:
                self.command = ["node", local_dsh, "--profile", "acp"]

        if self._default_command:
            persona = os.path.join(os.path.dirname(__file__), "kanaloa-persona.patch.yml")
            self.command.extend(["--patch", persona])

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
        self.runtime = runtime or KanaloaRuntime()
        self._active_thinking_turns: set[str] = set()

        # Audit properties for testing & verification
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
        - branch_mode = 'replay' (verified: creates new isolated native session with historical transcript as passive context without re-executing side-effects)
        """
        return AgentCapabilities(
            supports_stream=True,
            supports_resume=True,
            supports_cancel=True,
            supports_approval=True,
            supports_parallel_sessions=True,
            max_parallel_sessions=None,
            steer_mode="native",
            branch_mode="replay",
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

    def is_available(self) -> bool:
        if self._default_command and self._bundled_dsh is None:
            return False
        return bool(shutil.which(self.command[0]) or os.path.exists(self.command[0]))

    async def activate(self) -> None:
        """Start the bundled ACP runtime without creating a Conversation or Turn."""
        await self._ensure_process()

    async def _cleanup_process(self) -> None:
        """Mechanical cleanup of subprocess, background tasks, and pending futures."""
        initialized = self._is_initialized
        self._is_initialized = False

        # Reject any pending requests fail-closed so awaiting coroutines do not hang
        pending = list(self._pending_requests.items())
        self._pending_requests.clear()
        for req_id, fut in pending:
            if not fut.done():
                fut.set_exception(RuntimeError("ACP process disconnected or terminated"))

        # Cancel reader and stderr background tasks
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._reader_task = None
        self._stderr_task = None

        # Terminate / kill subprocess
        if self._process:
            if self._process.returncode is None:
                try:
                    # ACP owns stdin lifetime; EOF lets the Runtime dispose normally.
                    if initialized and self._process.stdin is not None:
                        self._process.stdin.close()
                    else:
                        self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
                except Exception:
                    if self._process.returncode is None:
                        try:
                            self._process.terminate()
                            await asyncio.wait_for(self._process.wait(), timeout=2.0)
                        except Exception:
                            if self._process.returncode is None:
                                self._process.kill()
                                await self._process.wait()
            logger.info(
                "KanaloaAdapter: ACP process stopped exit_code=%s",
                self._process.returncode if self._process.returncode is not None else "unknown",
            )
            self._process = None

    async def _ensure_process(self) -> None:
        """Ensure ACP subprocess is launched and initialized."""
        if not self.is_available():
            raise RuntimeError("Bundled Kanaloa runtime is unavailable; run npm install in this repository")
        async with self._lock:
            if self.is_alive() and self._is_initialized:
                return

            if self._process is not None:
                await self._cleanup_process()

            logger.info("KanaloaAdapter: launching ACP subprocess")
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._read_stdout_loop())
            self._stderr_task = asyncio.create_task(self._read_stderr_loop())

            try:
                # Perform ACP initialize handshake
                init_resp = await asyncio.wait_for(self._send_request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientInfo": {"name": "kane-harness", "version": "2.0.0"},
                        "capabilities": {},
                    },
                ), timeout=30)
                if "error" in init_resp:
                    raise RuntimeError("ACP initialization failed")
                self._is_initialized = True
                logger.info("KanaloaAdapter: initialized successfully")
            except BaseException:
                await self._cleanup_process()
                raise

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
                logger.warning("KanaloaAdapter: received non-JSON ACP stdout line")

        # Fail-closed cleanup when ACP subprocess stdout closes / process disconnects
        await self._handle_disconnect(reason="acp_process_disconnected")

    async def _read_stderr_loop(self) -> None:
        """Background loop logging stderr."""
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                break
            logger.debug("KanaloaAdapter: ACP stderr line received")

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
        update_type = update.get("sessionUpdate") or update.get("type")
        if not update_type:
            return

        if update_type in ("agent_message_chunk", "content"):
            if hasattr(self, "_active_thinking_turns"):
                self._active_thinking_turns.discard(turn_id)
            content_block = update.get("content", {})
            text = ""
            if isinstance(content_block, dict):
                if content_block.get("type") == "text" or "text" in content_block:
                    text = content_block.get("text", "")
            elif isinstance(content_block, str):
                text = content_block
            elif "text" in update:
                text = update.get("text", "")

            if text:
                await self.event_handler.emit_delta(turn_id, text)
                self.runtime.record_delta(turn_id, text)

        elif update_type in ("agent_thought_chunk", "thought", "thinking"):
            # Coarse live thinking status/event for UI; does NOT dump massive raw reasoning chains into turn_events or final text
            if not hasattr(self, "_active_thinking_turns"):
                self._active_thinking_turns = set()
            if turn_id not in self._active_thinking_turns:
                self._active_thinking_turns.add(turn_id)
                await self.event_handler.emit_event(turn_id, "thinking", {"status": "thinking"})

        elif update_type in ("tool_call", "tool_call_update", "tool_start", "tool_result", "tool_end"):
            if hasattr(self, "_active_thinking_turns"):
                self._active_thinking_turns.discard(turn_id)
            norm_event_type = "tool_start" if update_type in ("tool_call", "tool_start") else "tool_end"
            tool_name = (
                update.get("toolName")
                or update.get("name")
                or update.get("tool")
                or update.get("title")
                or "tool"
            )
            tool_name = str(tool_name) if len(str(tool_name)) <= 40 and str(tool_name).isascii() and str(tool_name).replace("_", "").replace("-", "").isalnum() else "tool"
            tool_payload: dict[str, Any] = {
                "type": update_type,
                "tool": str(tool_name),
            }
            tool_id = update.get("toolCallId") or update.get("id")
            if tool_id:
                tool_payload["tool_call_id"] = str(tool_id)
            if update.get("status") in ("pending", "in_progress", "completed", "failed", "cancelled"):
                tool_payload["status"] = update["status"]
            await self.event_handler.emit_event(turn_id, norm_event_type, tool_payload)

        elif update_type == "usage_update":
            # Usage updates (e.g. used/size token counters) are safely ignored without crashing
            logger.debug("KanaloaAdapter: usage update for session %s", session_id)

        else:
            logger.debug("KanaloaAdapter: unhandled ACP session update")

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
                record_message=False,
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
        self._pending_permissions.pop(perm_key, None)
        logger.info(
            "KanaloaAdapter: sent permission response %s for request %s (session %s)",
            decision,
            perm.request_id,
            perm.session_id,
        )

        # Resume Turn in Kane: transition waiting_user back to running via AgentEventHandler
        turn_id = perm.turn_id
        if turn_id:
            await self.event_handler.emit_resumed(
                turn_id,
                reason=f"permission_response:{request_id}:{decision}",
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

        for turn_id in turns_to_interrupt:
            if self.event_handler.is_turn_active(turn_id):
                await self.event_handler.emit_interrupted(
                    turn_id,
                    reason=f"permission_invalidated:{reason}",
                )

        if session_id is None:
            for turn_id in set(self._session_turns.values()) - turns_to_interrupt:
                if self.event_handler.is_turn_active(turn_id):
                    await self.event_handler.emit_interrupted(turn_id, reason=reason)
            self._active_sessions.clear()
        else:
            self._active_sessions.discard(session_id)

        # Reject pending RPC requests fail-closed so awaiting futures do not hang indefinitely (P1-01)
        if session_id is None:
            pending = list(self._pending_requests.items())
            self._pending_requests.clear()
            for req_id, fut in pending:
                if not fut.done():
                    fut.set_exception(RuntimeError(f"ACP process disconnected: {reason}"))

        if hasattr(self, "_active_thinking_turns"):
            if session_id:
                tid = self._session_turns.get(session_id)
                if tid:
                    self._active_thinking_turns.discard(tid)
            else:
                self._active_thinking_turns.clear()

        if not session_id:
            self.runtime.close_all()

    async def close(self) -> None:
        """Clean up active sessions and shutdown ACP process."""
        session_ids = list(self._active_sessions)
        # 1. Invalidate pending permissions, loops & runtimes fail-closed
        await self._handle_disconnect(reason="adapter_closed", purge=False)

        # 2. Close active sessions gracefully
        for session_id in session_ids:
            try:
                await self._send_request("session/close", {"sessionId": session_id})
            except Exception as exc:
                logger.debug("Failed closing session %s: %s", session_id, type(exc).__name__)
        self._active_sessions.clear()
        self._turn_sessions.clear()
        self._session_turns.clear()

        # 3. Clean up subprocess, background tasks, and pending requests
        await self._cleanup_process()

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

        try:
            await self._ensure_process()
        except Exception as exc:
            await self.event_handler.emit_interrupted(turn.turn_id, reason=f"acp_transport:{type(exc).__name__}")
            raise RuntimeError("ACP transport unavailable") from None

        session_id = turn.native_session_ref or self.get_native_session(turn.turn_id)

        # State B: Session New / Rebuild
        if not session_id or session_id not in self._active_sessions:
            # 1. Create native ACP session
            try:
                new_resp = await self._send_request("session/new", {
                    "cwd": self.cwd,
                    "mcpServers": [],
                })
            except Exception as exc:
                await self.event_handler.emit_interrupted(turn.turn_id, reason=f"acp_session:{type(exc).__name__}")
                raise RuntimeError("ACP session unavailable") from None
            if "error" in new_resp:
                await self.event_handler.emit_interrupted(turn.turn_id, reason="acp_session_new_failed")
                raise RuntimeError("ACP session/new failed")

            session_id = new_resp["result"]["sessionId"]
            turn.native_session_ref = session_id
            self.bind_session(turn.turn_id, session_id)
            self._active_sessions.add(session_id)
            await self.event_handler.emit_event(
                turn.turn_id,
                "status_change",
                {"native_session_ref": session_id},
            )

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

            logger.debug(
                "KanaloaAdapter: bootstrapped ACP session %s with %d context blocks",
                session_id,
                len(prompt_blocks) - 1,
            )
        else:
            turn.native_session_ref = session_id
            # State A: Active native session -> incremental message block only
            prompt_blocks = [{"type": "text", "text": message.content}]
            logger.debug(
                "KanaloaAdapter: sending incremental prompt to active ACP session %s",
                session_id,
            )

        # Dispatch prompt in background so send() returns while prompt streams
        if max_iterations is not _UNSET:
            asyncio.create_task(
                self.runtime.execute_loop(
                    turn=turn,
                    session_id=session_id,
                    initial_prompt_blocks=prompt_blocks,
                    max_iterations=max_iterations,
                    send_prompt_fn=self._send_prompt_for_loop,
                    event_handler=self.event_handler,
                )
            )
        else:
            asyncio.create_task(self._execute_prompt(turn.turn_id, session_id, prompt_blocks))

    async def _send_prompt_for_loop(
        self,
        session_id: str,
        prompt_blocks: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str | None]:
        """Low-level protocol transport executing one work-cycle prompt over ACP stdio."""
        resp = await self._send_request("session/prompt", {
            "sessionId": session_id,
            "prompt": prompt_blocks,
        })
        result = resp.get("result", {})
        stop_reason = result.get("stopReason")
        self.last_stop_reason = stop_reason
        return resp, stop_reason

    async def _execute_prompt(self, turn_id: str, session_id: str, prompt_blocks: list[dict[str, Any]]) -> None:
        """Execute session/prompt and normalize completion/cancellation outcome in Normal Mode."""
        try:
            resp = await self._send_request("session/prompt", {
                "sessionId": session_id,
                "prompt": prompt_blocks,
            })
            if "error" in resp:
                error_code = resp["error"].get("code", "unknown")
                error_code = error_code if isinstance(error_code, int) else "unknown"
                await self.event_handler.emit_interrupted(turn_id, reason=f"acp_protocol:{error_code}")
                return

            result = resp.get("result", {})
            stop_reason = result.get("stopReason")
            native_meta = result.get("_meta", {})
            native_kind = native_meta.get("kaneNativeEndKind")
            native_error_code = native_meta.get("kaneNativeErrorCode")
            if not isinstance(native_error_code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", native_error_code):
                native_error_code = None
            native_error_status = native_meta.get("kaneNativeErrorStatus")
            if not isinstance(native_error_status, int) or isinstance(native_error_status, bool) or not 100 <= native_error_status <= 599:
                native_error_status = None
            native_error_source = native_meta.get("kaneNativeErrorSource")
            if not isinstance(native_error_source, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", native_error_source):
                native_error_source = None
            stop_reason, native_kind, outcome = normalize_acp_outcome(stop_reason, native_kind)
            self.last_stop_reason = stop_reason
            logger.info(
                "KanaloaAdapter: prompt outcome turn=%s session=%s stop=%s native=%s error_code=%s error_status=%s error_source=%s",
                turn_id, session_id, stop_reason, native_kind,
                native_error_code or "unknown", native_error_status or "unknown", native_error_source or "unknown",
            )

            if not self.event_handler.is_turn_active(turn_id):
                return
            if outcome == "cancelled":
                await self.event_handler.emit_interrupted(turn_id, reason="cancelled_by_acp")
            elif outcome == "failed":
                await self.event_handler.emit_failed(turn_id, reason="agent_reported_failure")
            elif outcome == "completed":
                await self.event_handler.emit_message_complete(
                    turn_id=turn_id,
                    sender_id="kanaloa",
                )
            else:
                await self.event_handler.emit_interrupted(
                    turn_id, reason=f"acp_stop:{stop_reason or 'missing'}:native_{native_kind or 'missing'}"
                )
        except Exception as e:
            logger.error("KanaloaAdapter prompt execution exception: %s", type(e).__name__)
            if self.event_handler.is_turn_active(turn_id):
                await self.event_handler.emit_interrupted(turn_id, reason=f"acp_transport:{type(e).__name__}")
        finally:
            if hasattr(self, "_active_thinking_turns"):
                self._active_thinking_turns.discard(turn_id)

    def stop_loop(self, turn: Turn) -> None:
        """Signal runtime to gracefully stop continuing loop after current iteration."""
        self.runtime.stop_loop(turn.turn_id)

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

        self.runtime.record_steer(turn.turn_id, message.content)

        try:
            resp = await self._send_request("session/steer", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": message.content}],
            })
        except Exception as exc:
            if self.event_handler.is_turn_active(turn.turn_id):
                await self.event_handler.emit_interrupted(turn.turn_id, reason=f"acp_transport:{type(exc).__name__}")
            raise RuntimeError("ACP steer transport unavailable") from None
        if "error" in resp:
            raise RuntimeError("DSH steer failed")
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

        # Mark active loop as cancelled in runtime
        self.runtime.cancel_loop(turn.turn_id)

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
        ACP session/resume restores context, not unfinished execution.
        """
        session_id = turn.native_session_ref or self.get_native_session(turn.turn_id)
        if not session_id:
            raise ValueError(f"Cannot resume turn '{turn.turn_id}' without native_session_ref")

        try:
            await self._ensure_process()
            resp = await self._send_request("session/resume", {
                "sessionId": session_id,
                "cwd": self.cwd,
            })
        except Exception as exc:
            await self.event_handler.emit_interrupted(
                turn.turn_id, reason=f"acp_resume_transport:{type(exc).__name__}"
            )
            raise RuntimeError("ACP session restore unavailable") from exc
        if "error" in resp:
            raise RuntimeError("ACP session/resume failed")

        self._active_sessions.add(session_id)
        self.bind_session(turn.turn_id, session_id)
        # A subsequent explicit Send can continue this context without blind rerun.
        await self.event_handler.emit_interrupted(
            turn.turn_id, reason="session_rebound:unfinished_work_not_resumed"
        )
        logger.info("KanaloaAdapter: rebound session %s without resuming work", session_id)

    async def probe_session(self, native_session_ref: str | None) -> bool:
        """
        A resumable session is not proof that unfinished work survived restart.
        Only a session active in this live ACP process can keep a turn running.
        """
        if not native_session_ref:
            return False
        if not self.is_alive() or not self._is_initialized:
            return False
        active = native_session_ref in self._active_sessions
        logger.info("KanaloaAdapter: recovery probe session=%s live_work=%s", native_session_ref, active)
        return active
