"""Mock Bidirectional Adapter for Kane vNext testing.

Allows full verification of:
- Streaming deltas & logical message completion (1 logical reply = 1 Message)
- Thinking and ephemeral tool events
- Mid-flight steer in 'native', 'safe_boundary', and 'follow_up_only' modes
- Safe boundary mailbox draining
- Waiting_user, interruption, and failure lifecycles
- Follow-up turn resumption
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..domain.models import AgentCapabilities, Message, Turn
from .base import BaseAdapter


class MockAdapter(BaseAdapter):
    def __init__(
        self,
        capabilities: AgentCapabilities | None = None,
    ) -> None:
        super().__init__()
        self._capabilities = capabilities or AgentCapabilities(
            supports_stream=True,
            supports_resume=True,
            supports_cancel=True,
            steer_mode="native",
        )
        self.sent_calls: list[dict[str, Any]] = []
        self.steer_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []

        # Hook for custom execution simulation
        self.on_send_behavior: Any = None

    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def set_capabilities(self, capabilities: AgentCapabilities) -> None:
        self._capabilities = capabilities

    async def send(
        self,
        turn: Turn,
        message: Message,
        history: list[Message],
    ) -> None:
        self.sent_calls.append(
            {"turn": turn, "message": message, "history": history}
        )
        if self.on_send_behavior:
            await self.on_send_behavior(self, turn, message, history)

    async def steer(
        self,
        turn: Turn,
        message: Message,
    ) -> None:
        self.steer_calls.append({"turn": turn, "message": message})

    async def cancel(
        self,
        turn: Turn,
    ) -> None:
        self.cancel_calls.append({"turn": turn})

    async def resume(
        self,
        turn: Turn,
        history: list[Message],
    ) -> None:
        self.resume_calls.append({"turn": turn, "history": history})

    # --- Helper simulators for test scenarios ---

    async def simulate_stream_and_complete(
        self,
        turn_id: str,
        deltas: list[str],
        final_content: str | None = None,
    ) -> Message:
        """Simulate streaming several chunks, followed by logical completion."""
        for d in deltas:
            await self.event_handler.emit_delta(turn_id, d)
        return await self.event_handler.emit_message_complete(
            turn_id, content=final_content
        )

    async def simulate_thinking_and_tool(
        self,
        turn_id: str,
        thinking_text: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> None:
        """Simulate ephemeral events."""
        await self.event_handler.emit_event(
            turn_id, "thinking", {"text": thinking_text}
        )
        await self.event_handler.emit_event(
            turn_id, "tool_start", {"tool": tool_name, "args": tool_args}
        )
        await self.event_handler.emit_event(
            turn_id, "tool_end", {"tool": tool_name, "status": "ok"}
        )

    async def simulate_ask_user(self, turn_id: str, prompt: str) -> None:
        """Simulate agent pausing to wait for user input."""
        await self.event_handler.emit_waiting_user(turn_id, prompt)

    async def simulate_fail(self, turn_id: str, error_message: str) -> None:
        """Simulate agent failing."""
        await self.event_handler.emit_failed(turn_id, error_message)

    async def simulate_interrupt(self, turn_id: str, reason: str) -> None:
        """Simulate agent being interrupted."""
        await self.event_handler.emit_interrupted(turn_id, reason)
