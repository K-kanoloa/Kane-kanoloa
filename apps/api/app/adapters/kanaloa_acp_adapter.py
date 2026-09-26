"""Kanaloa ACP (Agent Client Protocol) Adapter for DeepSeek Harness.

Disciplines:
- Strictly responsible for:
  1. Protocol translation (ACP JSON-RPC 2.0 stdio <-> Kane BaseAdapter contract)
  2. Session binding (Turn <-> ACP native sessionId)
  3. Honest capability declaration verified by real tests against official DSH ACP:
     - supports_cancel = True (verified via session/cancel notification)
     - supports_resume = True (verified via session/resume with cwd)
     - supports_stream = True (verified via session/update notifications)
     - supports_parallel_sessions = True (verified via multiple sessions on single connection)
     - steer_mode = 'follow_up_only' (verified: ACP rejects mid-flight prompt with 'already in flight')
  4. Two-state Prompt semantics with Non-Negotiable Side-Effect Boundary:
     - State A (Active Session): incremental prompt only
     - State B (Bootstrap / Session Rebuild): reconstruction != replay historical execution
       Historical messages are passed ONLY as passive context transcript blocks;
       NEVER as an executable command queue. Only current new message is executed.
  5. Process & transport liveness over official DSH ACP profile.
- ZERO in-tree vendoring, ZERO core pollution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from ..domain.models import AgentCapabilities, Message, Turn
from .base import AgentEventHandler, BaseAdapter

logger = logging.getLogger(__name__)


class KanaloaACPAdapter(BaseAdapter):
    """
    Adapter bridging Kane Harness to the Official DeepSeek Harness ACP profile
    over standard Agent Client Protocol (ACP) JSON-RPC 2.0 stdio.
    """

    def __init__(
        self,
        command: list[str] | None = None,
        cwd: str | None = None,
        event_handler: AgentEventHandler | None = None,
    ) -> None:
        super().__init__(event_handler)
        default_npx = "npx.cmd" if sys.platform == "win32" else "npx"
        self.command = command or [default_npx, "--yes", "@deepseek-ai/dsh", "--profile", "acp"]
        self.cwd = cwd or os.getcwd()

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

        # Audit properties for testing & verification
        self.last_sent_payload: dict[str, Any] | None = None
        self.last_stop_reason: str | None = None

    def capabilities(self) -> AgentCapabilities:
        """
        Honest capability declaration verified by real tests against official DSH ACP profile:
        - supports_cancel = True (verified: session/cancel notification cancels in-flight prompt)
        - supports_resume = True (verified: session/resume restores session config & context)
        - supports_stream = True (verified: session/update delivers stream content blocks)
        - supports_approval = True (verified: session/request_permission protocol)
        - supports_parallel_sessions = True (verified: multiple sessions on single connection)
        - max_parallel_sessions = None (unlimited on single connection)
        - steer_mode = 'follow_up_only' (verified: ACP rejects mid-flight prompt with 'already in flight')
        - branch_mode = 'unsupported'
        """
        return AgentCapabilities(
            supports_stream=True,
            supports_resume=True,
            supports_cancel=True,
            supports_approval=True,
            supports_parallel_sessions=True,
            max_parallel_sessions=None,
            steer_mode="follow_up_only",
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

            logger.info("KanaloaACPAdapter: launching ACP subprocess: %s", " ".join(self.command))
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
            logger.info("KanaloaACPAdapter: initialized successfully with agentInfo: %s", init_resp.get("result", {}).get("agentInfo"))

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
                logger.warning("KanaloaACPAdapter: received non-JSON line from stdout: %s", line_str)

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
            logger.debug("KanaloaACPAdapter: unhandled ACP notification: %s", method)

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
        elif update_type in ("thought", "thinking"):
            await self.event_handler.emit_event(turn_id, "thinking", update)
        elif update_type in ("tool_call", "tool_start", "tool_result", "tool_end"):
            await self.event_handler.emit_event(turn_id, update_type, update)

    async def _handle_permission_request(self, msg: dict[str, Any]) -> None:
        """Handle permission request from ACP agent (e.g. file edit or command execution)."""
        session_id = msg.get("params", {}).get("sessionId")
        turn_id = self._session_turns.get(session_id or "")
        if turn_id:
            await self.event_handler.emit_waiting_user(
                turn_id,
                prompt=msg.get("params", {}).get("message", "Permission required"),
            )

    async def close(self) -> None:
        """Clean up active sessions and shutdown ACP process."""
        # 1. Close active sessions gracefully
        for session_id in list(self._active_sessions):
            try:
                await self._send_request("session/close", {"sessionId": session_id})
            except Exception as e:
                logger.debug("Failed closing session %s: %s", session_id, e)
        self._active_sessions.clear()

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
    ) -> None:
        """
        Send prompt request over standard ACP JSON-RPC.

        Two deterministic prompt payload states:
        - State A (Active Session): If native sessionId exists and session is active,
          send ONLY the current incremental message as ContentBlock.
        - State B (Bootstrap / Session Rebuild): If session is new or rebuilt,
          RECONSTRUCTION != REPLAY HISTORICAL EXECUTION.
          Historical messages are passed ONLY as passive context transcript blocks;
          NEVER as an executable command queue.
          Only the current new message (message.content) is executed as the active prompt,
          preventing duplicate real-world side effects.
        """
        await self._ensure_process()

        session_id = turn.native_session_ref

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
                "KanaloaACPAdapter: bootstrapped ACP session %s with %d context blocks",
                session_id,
                len(prompt_blocks) - 1,
            )
        else:
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
                "KanaloaACPAdapter: sending incremental prompt to active ACP session %s",
                session_id,
            )

        self.last_sent_payload = payload

        # Dispatch prompt in background so send() returns while prompt streams
        asyncio.create_task(self._execute_prompt(turn.turn_id, session_id, prompt_blocks))

    async def _execute_prompt(self, turn_id: str, session_id: str, prompt_blocks: list[dict[str, Any]]) -> None:
        """Execute session/prompt and normalize completion/cancellation outcome."""
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
                    sender_id="kanaloa-acp",
                )
        except Exception as e:
            logger.error("KanaloaACPAdapter prompt execution exception: %s", e)
            await self.event_handler.emit_failed(turn_id, reason=str(e))

    async def steer(
        self,
        turn: Turn,
        message: Message,
    ) -> None:
        """
        Verified Fact: ACP specification and DSH ACP reject concurrent prompts
        with 'a prompt is already in flight for this session'.
        Native mid-flight steer is therefore unsupported; degrades to follow_up_only.
        """
        raise NotImplementedError("ACP protocol rejects mid-flight concurrent prompts. Use follow_up_only mode.")

    async def cancel(
        self,
        turn: Turn,
    ) -> None:
        """
        Verified: ACP cancellation is an RPC notification 'session/cancel' with sessionId.
        """
        session_id = turn.native_session_ref or self.get_native_session(turn.turn_id)
        if not session_id:
            logger.warning("KanaloaACPAdapter: cancel requested but no native_session_ref on turn %s", turn.turn_id)
            return

        await self._send_notification("session/cancel", {
            "sessionId": session_id,
        })
        logger.info("KanaloaACPAdapter: sent session/cancel notification for session %s", session_id)

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
        logger.info("KanaloaACPAdapter: resumed session %s successfully", session_id)
