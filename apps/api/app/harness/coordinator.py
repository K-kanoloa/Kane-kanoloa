"""Kane vNext Harness Coordinator.

Implements AgentEventHandler to process outbound facts from external Agents:
- Ephemeral events (thinking, tool calls, status changes)
- Streaming deltas (accumulated in Turn partial_output buffer)
- Logical message finalization (ONE logical reply = ONE Message in store)
- Lifecycle transitions (waiting_user, interrupted, failed)
- Event subscriptions for UI / SSE streaming
- Automatic dispatching of pending follow-up mailbox items
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..adapters.base import AgentEventHandler, BaseAdapter
from ..domain.models import EventType, Message, Turn, TurnEvent, current_iso
from ..store.base import BaseStore
from .mailbox import MailboxManager


class HarnessCoordinator(AgentEventHandler):
    """
    Coordinates Agent outbound events, Turn lifecycle, and Store persistence.
    """

    def __init__(self, store: BaseStore, mailbox_manager: MailboxManager) -> None:
        self.store = store
        self.mailbox_manager = mailbox_manager
        self._adapters: dict[str, BaseAdapter] = {}
        self._listeners: dict[str, set[asyncio.Queue[TurnEvent]]] = {}
        # Callback to trigger follow-up execution when turn finishes
        self._followup_handler: Callable[[Turn, Message], Any] | None = None

    def register_adapter(self, agent_id: str, adapter: BaseAdapter) -> None:
        """Register an adapter and bind this coordinator as its event handler."""
        self._adapters[agent_id] = adapter
        adapter.bind_event_handler(self)

    def get_adapter(self, agent_id: str) -> BaseAdapter:
        if agent_id not in self._adapters:
            raise KeyError(f"No adapter registered for agent '{agent_id}'")
        return self._adapters[agent_id]

    def has_adapter(self, agent_id: str) -> bool:
        return agent_id in self._adapters

    def set_followup_handler(self, handler: Callable[[Turn, Message], Any]) -> None:
        """Set dispatcher callback to handle queued follow-up messages upon turn finish."""
        self._followup_handler = handler

    # --- Live Event Subscription (SSE / WebSockets) ---
    def subscribe(self, turn_id: str) -> asyncio.Queue[TurnEvent]:
        """Subscribe to live TurnEvents for a given turn."""
        if turn_id not in self._listeners:
            self._listeners[turn_id] = set()
        queue: asyncio.Queue[TurnEvent] = asyncio.Queue()
        self._listeners[turn_id].add(queue)
        return queue

    def unsubscribe(self, turn_id: str, queue: asyncio.Queue[TurnEvent]) -> None:
        """Unsubscribe a queue from a turn's events."""
        if turn_id in self._listeners:
            self._listeners[turn_id].discard(queue)
            if not self._listeners[turn_id]:
                del self._listeners[turn_id]

    def _broadcast_event(self, event: TurnEvent) -> None:
        queues = self._listeners.get(event.turn_id, set())
        for q in list(queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # --- AgentEventHandler Implementation ---
    async def emit_event(
        self,
        turn_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Emit ephemeral runtime fact (thinking, tool progress, etc.)."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type=event_type,
            payload=payload or {},
        )
        self.store.append_event(event)

        turn.last_event_at = event.created_at
        self.store.save_turn(turn)

        self._broadcast_event(event)

    async def emit_delta(self, turn_id: str, text: str) -> None:
        """
        Stream output delta. Aggregates into Turn partial_output buffer.
        Deltas are ephemeral and NEVER become separate chat messages.
        """
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        turn.partial_output = (turn.partial_output or "") + text
        turn.last_event_at = current_iso()
        self.store.save_turn(turn)

        # Broadcast as ephemeral delta event
        delta_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="delta",
            payload={"delta": text},
        )
        self._broadcast_event(delta_event)

    async def emit_message_complete(
        self,
        turn_id: str,
        content: str | None = None,
        sender_id: str | None = None,
    ) -> Message:
        """
        Signal completion of one logical reply.
        Persists ONE permanent Message to store, clears partial_output,
        and transitions Turn to 'finished'.
        """
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        # Resolve final content: explicit content, or accumulated partial_output buffer
        final_content = content if content is not None else (turn.partial_output or "")

        # Resolve parent_id: latest message in this turn/conversation for true tree lineage
        turn_msgs = [
            m for m in self.store.get_messages(turn.conversation_id)
            if m.turn_id == turn.turn_id
        ]
        parent_id = turn_msgs[-1].message_id if turn_msgs else None

        message = Message(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            sender="agent",
            sender_id=sender_id or turn.bound_agent_id,
            parent_id=parent_id,
            content=final_content,
        )
        # Update turn state and prepare completion status event
        turn.partial_output = None
        turn.status = "finished"
        turn.finished_at = current_iso()
        turn.last_event_at = current_iso()

        status_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="status_change",
            payload={
                "status": "finished",
                "message_id": message.message_id,
            },
        )

        # Single atomic ACID transaction: Message + Turn completion + Status event
        self.store.finalize_turn_completion(turn, message, status_event)
        self._broadcast_event(status_event)

        # Check mailbox for pending follow-up items (e.g. from follow_up_only steer mode)
        mailbox = self.mailbox_manager.get_mailbox(turn_id)
        next_item = mailbox.get_nowait()
        if next_item and next_item.item_type == "message":
            msg_payload = next_item.payload
            followup_msg_id = msg_payload.get("message_id")
            if followup_msg_id:
                # Retrieve the saved follow-up message
                followup_msg = self.store.get_message(followup_msg_id)
                if followup_msg and self._followup_handler:
                    asyncio.create_task(self._followup_handler(turn, followup_msg))

        return message

    async def emit_waiting_user(
        self,
        turn_id: str,
        prompt: str | None = None,
    ) -> None:
        """Agent pauses execution awaiting user input or approval."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        turn.status = "waiting_user"
        turn.last_event_at = current_iso()
        self.store.save_turn(turn)

        status_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="status_change",
            payload={"status": "waiting_user", "prompt": prompt},
        )
        self.store.append_event(status_event)
        self._broadcast_event(status_event)

    async def emit_interrupted(
        self,
        turn_id: str,
        reason: str,
    ) -> None:
        """Execution interrupted (network drop, cancel, process exit). Preserves partial_output."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        turn.status = "interrupted"
        turn.interrupt_reason = reason
        turn.last_event_at = current_iso()
        # Note: turn.partial_output is preserved as-is (§31)
        self.store.save_turn(turn)

        status_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="status_change",
            payload={"status": "interrupted", "reason": reason},
        )
        self.store.append_event(status_event)
        self._broadcast_event(status_event)

    async def emit_failed(
        self,
        turn_id: str,
        reason: str,
    ) -> None:
        """Execution encountered an unrecoverable failure."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        turn.status = "failed"
        turn.interrupt_reason = reason
        turn.last_event_at = current_iso()
        turn.finished_at = current_iso()
        self.store.save_turn(turn)

        status_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="status_change",
            payload={"status": "failed", "reason": reason},
        )
        self.store.append_event(status_event)
        self._broadcast_event(status_event)

    async def emit_boundary_signal(self, turn_id: str) -> None:
        """
        Adapter signals that the underlying agent has reached a native safe execution boundary.
        Kane checks the Turn Mailbox for any pending 'steer' item,
        and feeds it directly to adapter.steer().
        The Agent does not touch or know about Kane's Mailbox.
        """
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        mailbox = self.mailbox_manager.get_mailbox(turn_id)
        pending_items = mailbox.peek_pending()
        steer_item = None
        for item in pending_items:
            if item.item_type == "steer":
                steer_item = item
                break

        if steer_item:
            # Remove this steer item from mailbox queue
            all_items = mailbox.drain_all()
            for it in all_items:
                if it.item_id != steer_item.item_id:
                    mailbox.put_nowait(it)

            msg_id = steer_item.payload.get("message_id")
            steer_msg = self.store.get_message(msg_id) if msg_id else None
            if not steer_msg:
                steer_msg = Message(
                    conversation_id=turn.conversation_id,
                    sender="user",
                    content=steer_item.payload.get("content", ""),
                )

            adapter = self.get_adapter(turn.bound_agent_id)
            await adapter.steer(turn, steer_msg)

            boundary_event = TurnEvent(
                turn_id=turn_id,
                conversation_id=turn.conversation_id,
                event_type="status_change",
                payload={
                    "boundary": "safe_steer_injected",
                    "message_id": steer_msg.message_id,
                },
            )
            self.store.append_event(boundary_event)
            self._broadcast_event(boundary_event)

    async def reconcile_startup_turns(self) -> dict[str, str]:
        """
        Startup Reconciliation Pass (§30):
        Scans all turns where persisted status == 'running'.
        Queries corresponding adapter's probe_session truth:
        - If recoverable: remain 'running'.
        - If unrecoverable (process exited, lost session, resume failed, no adapter):
          transition to 'interrupted' with reason='unrecoverable:session_lost_on_startup'.
          Strictly preserves partial_output, DOES NOT rerun prompt, DOES NOT create replacement session.
        - waiting_user turns remain waiting_user; stale permissions fail-closed (§34).
        - Terminal turns (finished, failed, interrupted) remain unchanged.
        Returns a dict of turn_id -> new_or_confirmed_status.
        """
        running_turns = self.store.list_running_turns()
        results: dict[str, str] = {}
        for turn in running_turns:
            agent_id = turn.bound_agent_id
            if not self.has_adapter(agent_id):
                await self.emit_interrupted(turn.turn_id, reason="unrecoverable:no_adapter_available")
                results[turn.turn_id] = "interrupted"
                continue

            adapter = self.get_adapter(agent_id)
            is_live = await adapter.probe_session(turn.native_session_ref)
            if is_live:
                results[turn.turn_id] = "running"
            else:
                await self.emit_interrupted(turn.turn_id, reason="unrecoverable:session_lost_on_startup")
                results[turn.turn_id] = "interrupted"
        return results
