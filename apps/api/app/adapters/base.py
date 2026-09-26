"""Kane vNext Generic Bidirectional Adapter Contract.

Disciplines:
- Kane Core is model-free (0 LLM, 0 Planner, 0 Tool Loop).
- Adapter contract is strictly bidirectional and symmetric:
  - Kane -> Agent (Inbound control): send, steer, cancel, resume, capabilities.
  - Agent -> Kane (Outbound facts): emit_event, emit_delta, emit_message_complete,
    emit_waiting_user, emit_interrupted, emit_failed.
- Causal Isolation:
  - Mailbox is strictly for inbound items (user messages, steer, cancel).
  - Event Stream is strictly for outbound runtime facts (deltas, thinking, status).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..domain.models import AgentCapabilities, EventType, Message, Turn


class AgentEventHandler(ABC):
    """
    Inbound event receiver from Agent/Adapter to Kane.
    Handles streaming deltas, lifecycle state transitions, and logical message finalization.
    """

    @abstractmethod
    async def emit_event(
        self,
        turn_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Emit ephemeral runtime fact (thinking, tool_start, tool_end, progress, etc.)."""
        ...

    @abstractmethod
    async def emit_delta(self, turn_id: str, text: str) -> None:
        """Stream output delta. Aggregates into Turn partial_output buffer."""
        ...

    @abstractmethod
    async def emit_message_complete(
        self,
        turn_id: str,
        content: str | None = None,
        sender_id: str | None = None,
    ) -> Message:
        """
        Signal completion of one logical reply.
        Persists ONE permanent Message to store, clears partial_output, transitions Turn to 'finished'.
        """
        ...

    @abstractmethod
    async def emit_waiting_user(
        self,
        turn_id: str,
        prompt: str | None = None,
    ) -> None:
        """Agent pauses execution awaiting user input or approval. Transitions Turn to 'waiting_user'."""
        ...

    @abstractmethod
    async def emit_interrupted(
        self,
        turn_id: str,
        reason: str,
    ) -> None:
        """Execution interrupted (network, cancel, process exit). Preserves partial_output, Turn to 'interrupted'."""
        ...

    @abstractmethod
    async def emit_failed(
        self,
        turn_id: str,
        reason: str,
    ) -> None:
        """Execution encountered an unrecoverable failure. Transitions Turn to 'failed'."""
        ...

    @abstractmethod
    async def emit_boundary_signal(
        self,
        turn_id: str,
    ) -> None:
        """
        Signal from Adapter that the underlying agent has reached a native safe execution boundary.
        Enables Kane to dequeue and feed any pending safe-boundary steer inputs without exposing Mailbox to the Agent.
        """
        ...


class BaseAdapter(ABC):
    """
    Generic Bidirectional Agent Adapter interface.
    Every external Agent (Kanaloa, Codex CLI, Claude Code, etc.) implements this contract.
    """

    def __init__(self, event_handler: AgentEventHandler | None = None) -> None:
        self._event_handler = event_handler

    def bind_event_handler(self, handler: AgentEventHandler) -> None:
        self._event_handler = handler

    @property
    def event_handler(self) -> AgentEventHandler:
        if self._event_handler is None:
            raise RuntimeError("Adapter does not have a bound AgentEventHandler.")
        return self._event_handler

    @abstractmethod
    def capabilities(self) -> AgentCapabilities:
        """Declare agent capabilities (steer_mode, branch_mode, stream, cancel, resume, etc.)."""
        ...

    @abstractmethod
    async def send(
        self,
        turn: Turn,
        message: Message,
        history: list[Message],
    ) -> None:
        """Send a user message/prompt to begin or continue Turn execution."""
        ...

    @abstractmethod
    async def steer(
        self,
        turn: Turn,
        message: Message,
    ) -> None:
        """Mid-flight steer input while Turn is running."""
        ...

    @abstractmethod
    async def cancel(
        self,
        turn: Turn,
    ) -> None:
        """Abort/stop ongoing Turn execution."""
        ...

    @abstractmethod
    async def resume(
        self,
        turn: Turn,
        history: list[Message],
    ) -> None:
        """Resume an interrupted or waiting Turn."""
        ...

    async def respond_permission(
        self,
        request_id: str | int,
        decision: str,
        session_id: str | None = None,
    ) -> None:
        """Respond to an inbound permission request. Subclasses supporting approval should implement this."""
        raise NotImplementedError("This adapter does not support approval responses.")

    async def probe_session(self, native_session_ref: str | None) -> bool:
        """
        Thinnest physical liveness/recovery probe (§30).
        Probes whether the native session / process / transport is alive and recoverable.
        Returns True if alive and recoverable, False if lost or dead.
        Must NEVER rely on timeout or guessing.
        """
        return False
