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
from ..id_utils import new_id
from ..store.base import BaseStore
from .mailbox import MailboxItem, MailboxManager


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

        if payload and "native_session_ref" in payload:
            turn.native_session_ref = payload["native_session_ref"]

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
        turn_finished: bool = True,
        message_id: str | None = None,
    ) -> Message:
        """
        Signal completion of one logical reply.
        Persists ONE permanent Message to store, clears partial_output,
        and transitions Turn to 'finished' only when the Agent finished the work.
        """
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        if message_id:
            existing = self.store.get_message(message_id)
            if existing:
                if existing.turn_id != turn_id or existing.conversation_id != turn.conversation_id:
                    raise ValueError("Completion message belongs to another Turn")
                return existing

        if turn.status not in ("running", "waiting_user"):
            raise ValueError("Completion received for an inactive Turn")

        # Resolve final content: explicit content, or accumulated partial_output buffer
        final_content = content if content is not None else (turn.partial_output or "")

        # Resolve parent_id: latest message in this turn/conversation for true tree lineage
        turn_msgs = [
            m for m in self.store.get_messages(turn.conversation_id)
            if m.turn_id == turn.turn_id
        ]
        parent_id = turn_msgs[-1].message_id if turn_msgs else None

        message = Message(
            message_id=message_id or new_id("msg"),
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            sender="agent",
            sender_id=sender_id or turn.bound_agent_id,
            parent_id=parent_id,
            content=final_content,
        )
        # Update turn state and prepare completion status event
        turn.partial_output = None
        turn.status = "finished" if turn_finished else "running"
        turn.finished_at = current_iso() if turn_finished else None
        turn.last_event_at = current_iso()

        status_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="status_change",
            payload={
                "status": turn.status,
                "message_id": message.message_id,
            },
        )

        # Single atomic ACID transaction: Message + Turn completion + Status event
        self.store.finalize_turn_completion(turn, message, status_event)
        self._broadcast_event(status_event)

        # Check mailbox for pending follow-up items (e.g. from follow_up_only steer mode)
        mailbox = self.mailbox_manager.get_mailbox(turn_id)
        pending = mailbox.peek_pending()
        if turn_finished and pending and pending[0].item_type == "message":
            next_item = mailbox.get_nowait()
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
        record_message: bool = True,
    ) -> None:
        """Agent pauses execution awaiting user input or approval."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        turn.status = "waiting_user"
        turn.last_event_at = current_iso()

        status_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="status_change",
            payload={"status": "waiting_user", "prompt": prompt},
        )
        if record_message and (prompt or turn.partial_output):
            final_content = turn.partial_output or ""
            if prompt and prompt not in final_content:
                final_content = f"{final_content}\n\n{prompt}" if final_content else prompt
            turn_msgs = [
                message for message in self.store.get_messages(turn.conversation_id)
                if message.turn_id == turn.turn_id
            ]
            message = Message(
                conversation_id=turn.conversation_id,
                turn_id=turn.turn_id,
                sender="agent",
                sender_id=turn.bound_agent_id,
                parent_id=turn_msgs[-1].message_id if turn_msgs else None,
                content=final_content,
            )
            turn.partial_output = None
            status_event.payload["message_id"] = message.message_id
            self.store.finalize_turn_completion(turn, message, status_event)
        else:
            self.store.save_turn(turn)
            self.store.append_event(status_event)
        self._broadcast_event(status_event)

    async def emit_resumed(
        self,
        turn_id: str,
        reason: str | None = None,
    ) -> None:
        """Agent resumes execution (e.g. after approval response). Transitions Turn to 'running'."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        turn.status = "running"
        turn.last_event_at = current_iso()
        self.store.save_turn(turn)

        status_event = TurnEvent(
            turn_id=turn_id,
            conversation_id=turn.conversation_id,
            event_type="status_change",
            payload={"status": "running", "resumed_from": reason or "adapter_resume"},
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
            if not steer_msg or not self.store.claim_inbound(steer_msg.message_id):
                return

            adapter = self.get_adapter(turn.bound_agent_id)
            try:
                await adapter.steer(turn, steer_msg)
            except Exception as exc:
                if self.is_turn_active(turn_id):
                    await self.emit_interrupted(turn_id, reason=f"delivery_outcome_unknown:{exc}")
                return
            self.store.complete_inbound(steer_msg.message_id)

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

    def is_turn_active(self, turn_id: str) -> bool:
        """Check whether turn is still in active lifecycle (running or waiting_user)."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            return False
        return turn.status in ("running", "waiting_user")

    async def reconcile_startup_turns(self) -> dict[str, str]:
        """
        Startup Reconciliation Pass (§30):
        Scans all turns where persisted status is running or waiting_user.
        Queries corresponding adapter's probe_session truth:
        - If recoverable: remain 'running'.
        - If unrecoverable (process exited, lost session, resume failed, no adapter):
          transition to 'interrupted' with reason='unrecoverable:session_lost_on_startup'.
          Strictly preserves partial_output, DOES NOT rerun prompt, DOES NOT create replacement session.
        - waiting_user remains waiting only if its native session is live.
        - Terminal turns (finished, failed, interrupted) remain unchanged.
        Returns a dict of turn_id -> new_or_confirmed_status.
        """
        active_turns = self.store.list_active_turns()
        results: dict[str, str] = {}
        for turn in active_turns:
            has_input = any(
                message.turn_id == turn.turn_id and message.sender == "user"
                for message in self.store.get_messages(turn.conversation_id)
            )
            if turn.status == "running" and not turn.native_session_ref and not has_input:
                # An explicit New Task may be focused before its first message.
                results[turn.turn_id] = turn.status
                continue
            agent_id = turn.bound_agent_id
            if not self.has_adapter(agent_id):
                await self.emit_interrupted(turn.turn_id, reason="unrecoverable:no_adapter_available")
                results[turn.turn_id] = "interrupted"
                continue

            adapter = self.get_adapter(agent_id)
            is_live = await adapter.probe_session(turn.native_session_ref)
            if is_live:
                bind_session = getattr(adapter, "bind_session", None)
                if callable(bind_session) and turn.native_session_ref:
                    bind_session(turn.turn_id, turn.native_session_ref)
                results[turn.turn_id] = turn.status
            else:
                await self.emit_interrupted(turn.turn_id, reason="unrecoverable:session_lost_on_startup")
                results[turn.turn_id] = "interrupted"

        for message_id, turn_id, kind, state in self.store.list_unsettled_inbound():
            turn = self.store.get_turn(turn_id)
            message = self.store.get_message(message_id)
            if not turn or not message:
                continue
            if state == "dispatching":
                if turn.status in ("running", "waiting_user"):
                    await self.emit_interrupted(turn_id, reason="delivery_outcome_unknown_on_restart")
                    results[turn_id] = "interrupted"
                continue
            if not self.has_adapter(turn.bound_agent_id):
                continue
            if kind == "message" and turn.status == "finished" and self._followup_handler:
                await self._followup_handler(turn, message)
                results[turn_id] = self.store.get_turn(turn_id).status
                continue
            if kind == "message" and self._followup_handler:
                user_inputs = [
                    item for item in self.store.get_messages(turn.conversation_id)
                    if item.turn_id == turn_id and item.sender == "user"
                ]
                if len(user_inputs) == 1 and user_inputs[0].message_id == message_id:
                    await self._followup_handler(turn, message)
                    results[turn_id] = self.store.get_turn(turn_id).status
                    continue
            mailbox = self.mailbox_manager.get_mailbox(turn_id)
            if not any(item.payload.get("message_id") == message_id for item in mailbox.peek_pending()):
                mailbox.put_nowait(MailboxItem(
                    turn_id=turn_id,
                    item_type=kind,
                    payload={"message_id": message_id, "content": message.content},
                ))
        return results
