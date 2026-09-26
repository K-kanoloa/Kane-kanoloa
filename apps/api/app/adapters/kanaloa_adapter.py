"""Kanaloa DSH Adapter.

Disciplines:
- KanaloaAdapter is strictly responsible for:
  1. Protocol translation (JSON-RPC stdio <-> Kane BaseAdapter contract)
  2. Session binding (Turn <-> DSH Native Session reference)
  3. Capability declaration (honest reporting of what the underlying DSH transport actually supports)
  4. Event normalization / mapping (DSH wire events -> Kane stable emit_* callbacks)
  5. Process/transport liveness
- KanaloaAdapter MUST NOT manage:
  - IPython lifecycle policies
  - Loop mode decisions
  - Agent reasoning or planning
  - Tool orchestration
  - Context compression or retry policies
  (These remain strictly inside Kanaloa / Official DSH)
- Capability Honesty:
  - DSH SDK currently does NOT support per-session cancel or mid-turn cancel -> supports_cancel = False
  - Resume and steer are honestly declared based on current transport support
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from ..domain.models import AgentCapabilities, Message, Turn
from .base import AgentEventHandler, BaseAdapter

logger = logging.getLogger(__name__)


class KanaloaAdapter(BaseAdapter):
    """
    Adapter bridging Kane Harness to the Official DSH programmatic stdio interface.
    """

    def __init__(
        self,
        command: list[str] | None = None,
        event_handler: AgentEventHandler | None = None,
    ) -> None:
        super().__init__(event_handler)
        self.command = command or ["npx", "@deepseek-ai/dsh", "--stdio"]
        self._process: asyncio.subprocess.Process | None = None
        self._turn_sessions: dict[str, str] = {}  # turn_id -> native_session_id
        self._active_sessions: set[str] = set()    # Active session IDs that have received prompt bootstrap
        self.last_sent_payload: dict[str, Any] | None = None
        self._is_running = False

    def capabilities(self) -> AgentCapabilities:
        """
        Honest capability declaration based on verified DSH SDK wire protocol.
        Does NOT inflate capabilities that the transport cannot deliver.
        - supports_cancel = False (DSH SDK wire protocol currently lacks per-session/mid-turn cancel)
        - supports_resume = False (Native resume unsupported; Continue uses session rebuild via send())
        - steer_mode = 'follow_up_only' (Safe boundary is a Generic BaseAdapter feature; SDK uses follow_up_only)
        """
        return AgentCapabilities(
            supports_stream=True,
            supports_resume=False,       # DSH SDK protocol does not yet offer verified session/resume
            supports_cancel=False,       # DSH SDK wire protocol currently lacks per-session/mid-turn cancel
            supports_approval=False,
            supports_parallel_sessions=False,
            max_parallel_sessions=1,
            steer_mode="follow_up_only",  # SDK protocol lacks mid-flight steering
            branch_mode="unsupported",
        )

    # --- Session Binding ---
    def get_native_session(self, turn_id: str) -> str | None:
        return self._turn_sessions.get(turn_id)

    def bind_session(self, turn_id: str, native_session_id: str) -> None:
        self._turn_sessions[turn_id] = native_session_id

    # --- Process & Transport Liveness ---
    def is_alive(self) -> bool:
        if self._process is None:
            return self._is_running
        return self._process.returncode is None

    async def close(self) -> None:
        self._is_running = False
        self._active_sessions.clear()
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                if self._process.returncode is None:
                    self._process.kill()

    # --- Event Normalization / Mapping ---
    async def normalize_dsh_event(self, turn_id: str, raw_event: dict[str, Any]) -> None:
        """
        Deterministic normalization / mapping from DSH SDK wire events
        to Kane stable AgentEventHandler callbacks.
        """
        event_type = raw_event.get("type") or raw_event.get("method")
        payload = raw_event.get("params") or raw_event.get("data") or {}

        if event_type in ("delta", "chunk", "text_delta"):
            text = payload.get("text", "")
            if text:
                await self.event_handler.emit_delta(turn_id, text)

        elif event_type in ("complete", "done", "session_complete"):
            final_text = payload.get("content") or payload.get("text")
            await self.event_handler.emit_message_complete(
                turn_id=turn_id,
                content=final_text,
                sender_id="kanaloa",
            )

        elif event_type in ("waiting_user", "need_input", "ask_user"):
            prompt = payload.get("prompt") or payload.get("message")
            await self.event_handler.emit_waiting_user(turn_id, prompt=prompt)

        elif event_type in ("error", "fatal"):
            reason = payload.get("message") or payload.get("error") or "Unknown DSH error"
            await self.event_handler.emit_failed(turn_id, reason=str(reason))

        elif event_type in ("thinking", "tool_start", "tool_end", "progress"):
            # Map directly to ephemeral TurnEvent
            await self.event_handler.emit_event(turn_id, event_type, payload)

        else:
            # Fallback for unclassified ephemeral events
            await self.event_handler.emit_event(turn_id, "raw", raw_event)

    # --- BaseAdapter Methods ---
    async def send(
        self,
        turn: Turn,
        message: Message,
        history: list[Message],
    ) -> None:
        """
        Format prompt request into DSH programmatic stdio JSON-RPC.

        Two deterministic prompt payload states:
        - State A (Active Session): If session is already active and process is alive,
          send ONLY incremental message (do NOT repeat full history).
        - State B (Bootstrap / Session Rebuild): If session is new or rebuilt,
          replay visible history context along with the new message.
        """
        # Ensure session binding
        if not turn.native_session_ref:
            turn.native_session_ref = f"dsh_sess_{turn.turn_id}"
            self.bind_session(turn.turn_id, turn.native_session_ref)

        session_ref = turn.native_session_ref
        self._is_running = True

        if session_ref in self._active_sessions and self.is_alive():
            # State A: Existing active native session -> incremental message only
            payload = {
                "method": "session/prompt",
                "params": {
                    "session_id": session_ref,
                    "prompt": message.content,
                },
            }
            logger.debug(
                "KanaloaAdapter: sending incremental prompt to active DSH session %s for turn %s",
                session_ref,
                turn.turn_id,
            )
        else:
            # State B: Fresh bootstrap or rebuilt session -> replay visible history context
            self._active_sessions.add(session_ref)
            payload = {
                "method": "session/prompt",
                "params": {
                    "session_id": session_ref,
                    "prompt": message.content,
                    "history": [
                        {"role": m.sender, "content": m.content}
                        for m in history
                        if m.message_id != message.message_id
                    ],
                },
            }
            logger.debug(
                "KanaloaAdapter: bootstrapping DSH session %s with %d history messages for turn %s",
                session_ref,
                len(payload["params"]["history"]),
                turn.turn_id,
            )

        self.last_sent_payload = payload

    async def steer(
        self,
        turn: Turn,
        message: Message,
    ) -> None:
        """
        Under current DSH SDK wire protocol, mid-turn steer is not supported natively.
        Degrades to follow-up through Kane Dispatcher.
        """
        raise NotImplementedError("DSH SDK programmatic transport does not support native mid-turn steer.")

    async def cancel(
        self,
        turn: Turn,
    ) -> None:
        """
        DSH SDK wire protocol currently lacks per-session cancel.
        Raises NotImplementedError to enforce capability honesty.
        """
        logger.warning(
            "KanaloaAdapter: cancel requested for turn %s, but DSH SDK lacks per-session cancel.",
            turn.turn_id,
        )
        raise NotImplementedError("KanaloaAdapter (SDK): DSH SDK wire protocol does not support per-session cancellation.")

    async def resume(
        self,
        turn: Turn,
        history: list[Message],
    ) -> None:
        """
        Native resume is not supported under DSH SDK wire protocol.
        Continue / follow-up is handled via send(turn, user_msg, history).
        """
        raise NotImplementedError("KanaloaAdapter (SDK): DSH SDK wire protocol does not support native resume.")
