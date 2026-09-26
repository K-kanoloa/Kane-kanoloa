"""Kane vNext Deterministic Dispatcher.

Disciplines:
- 100% Deterministic target Turn resolution:
  Priority: explicit_turn_id -> reply_to_message_id -> conversation.focus_turn_id -> create new Turn.
  ZERO AI guessing, ZERO heuristics classifier.
- Steer degradation based on AgentCapabilities.steer_mode:
  - native: immediate adapter.steer() call
  - safe_boundary: queue in TurnMailbox for agent step boundary polling
  - follow_up_only: queue in TurnMailbox for auto-dispatch when current turn finishes
- Turn continuity: sending follow-up to finished/failed/interrupted turn resumes same turn back to running.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import Message, Turn, current_iso
from ..store.base import BaseStore
from .coordinator import HarnessCoordinator
from .mailbox import MailboxItem, MailboxManager


class Dispatcher:
    """
    Deterministic Inbound Message and Control Dispatcher.
    """

    def __init__(
        self,
        store: BaseStore,
        coordinator: HarnessCoordinator,
        mailbox_manager: MailboxManager,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.mailbox_manager = mailbox_manager
        # Wire follow-up handler into coordinator
        self.coordinator.set_followup_handler(self._handle_queued_followup)

    def resolve_target_turn(
        self,
        conversation_id: str,
        explicit_turn_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> Turn:
        """
        Purely deterministic resolution of target Turn:
        1. Explicit turn_id
        2. Reply-to message lineage
        3. Conversation focus_turn_id
        4. Fresh Turn creation
        """
        # 1. Explicit turn requested
        if explicit_turn_id:
            turn = self.store.get_turn(explicit_turn_id)
            if not turn:
                raise ValueError(
                    f"Target turn '{explicit_turn_id}' does not exist"
                )
            if turn.conversation_id != conversation_id:
                raise ValueError(
                    f"Turn '{explicit_turn_id}' belongs to conversation '{turn.conversation_id}', not '{conversation_id}'"
                )
            return turn

        # Ensure conversation exists
        conv = self.store.get_conversation(conversation_id)
        if not conv:
            from ..domain.models import Conversation
            conv = Conversation(conversation_id=conversation_id)
            self.store.save_conversation(conv)

        # 2. Reply-to message
        if reply_to_message_id:
            msg_turn_id = self.store.get_turn_id_by_message_id(reply_to_message_id)
            if msg_turn_id:
                target_turn = self.store.get_turn(msg_turn_id)
                if target_turn and target_turn.conversation_id == conversation_id:
                    return target_turn
            msg = self.store.get_message(reply_to_message_id)
            if msg and msg.turn_id:
                target_turn = self.store.get_turn(msg.turn_id)
                if target_turn and target_turn.conversation_id == conversation_id:
                    return target_turn
            if msg and conv.focus_turn_id:
                focus = self.store.get_turn(conv.focus_turn_id)
                if focus:
                    return focus

        # 3. Focus turn on conversation
        if conv.focus_turn_id:
            focus = self.store.get_turn(conv.focus_turn_id)
            if focus:
                return focus

        # 4. Check existing turns in this conversation
        existing_turns = self.store.list_turns(conversation_id)
        if len(existing_turns) == 0:
            # If conversation never had any Turn yet -> automatically create initial Turn
            new_turn = Turn(
                conversation_id=conversation_id,
                bound_agent_id=conv.bound_agent_id,
                status="running",
                title="Initial Turn",
            )
            self.store.save_turn(new_turn)

            conv.focus_turn_id = new_turn.turn_id
            conv.updated_at = current_iso()
            self.store.save_conversation(conv)
            return new_turn

        # Conversation already has turns, but none could be resolved -> DO NOT silently spawn a new Turn!
        raise ValueError(
            f"target_turn_required: conversation '{conversation_id}' already has existing turns, "
            f"but no target or focus turn could be resolved. "
            f"Please specify an explicit target_turn_id or request a new turn."
        )

    def create_new_turn(self, conversation_id: str, title: str | None = None) -> Turn:
        """
        Explicitly create an additional independent Turn for a conversation (New Turn).
        """
        conv = self.store.get_conversation(conversation_id)
        if not conv:
            from ..domain.models import Conversation
            conv = Conversation(conversation_id=conversation_id)
            self.store.save_conversation(conv)

        new_turn = Turn(
            conversation_id=conversation_id,
            bound_agent_id=conv.bound_agent_id,
            status="running",
            title=title or "New Turn",
        )
        self.store.save_turn(new_turn)

        conv.focus_turn_id = new_turn.turn_id
        conv.updated_at = current_iso()
        self.store.save_conversation(conv)
        return new_turn

    async def dispatch_user_message(
        self,
        conversation_id: str,
        content: str,
        target_turn_id: str | None = None,
        reply_to_message_id: str | None = None,
        parent_id: str | None = None,
        is_new_task: bool = False,
    ) -> tuple[Message, Turn]:
        """
        Dispatch a user message to the conversation and target Turn.
        Resolves turn deterministically, appends user message with bound turn_id to store,
        and routes according to adapter capabilities.
        """
        # 1. Deterministic Turn Resolution first
        if is_new_task:
            turn = self.create_new_turn(conversation_id)
        else:
            turn = self.resolve_target_turn(
                conversation_id=conversation_id,
                explicit_turn_id=target_turn_id,
                reply_to_message_id=reply_to_message_id,
            )

        # 2. Append User Message with explicit turn_id
        user_msg = Message(
            conversation_id=conversation_id,
            turn_id=turn.turn_id,
            sender="user",
            reply_to=reply_to_message_id,
            parent_id=parent_id,
            content=content,
        )
        self.store.append_message(user_msg)

        # 3. Resolve Adapter and Capabilities
        adapter = self.coordinator.get_adapter(turn.bound_agent_id)
        caps = adapter.capabilities()

        # 4. Turn Status Lifecycle Handling
        if turn.status in ("finished", "failed", "interrupted"):
            # §8 & §39: User follow-up resumes the same turn back to running
            turn.status = "running"
            turn.finished_at = None
            turn.interrupt_reason = None
            turn.last_event_at = current_iso()
            self.store.save_turn(turn)

            history = self.store.get_messages(conversation_id)
            await adapter.send(turn, user_msg, history)

        elif turn.status == "waiting_user":
            # Resume waiting turn
            turn.status = "running"
            turn.last_event_at = current_iso()
            self.store.save_turn(turn)

            history = self.store.get_messages(conversation_id)
            await adapter.send(turn, user_msg, history)

        elif turn.status == "running":
            # Turn is actively running -> Steer Degradation
            steer_mode = caps.steer_mode
            if steer_mode == "native":
                await adapter.steer(turn, user_msg)
            elif steer_mode == "safe_boundary":
                mailbox = self.mailbox_manager.get_mailbox(turn.turn_id)
                await mailbox.put(
                    MailboxItem(
                        turn_id=turn.turn_id,
                        item_type="steer",
                        payload={
                            "message_id": user_msg.message_id,
                            "content": user_msg.content,
                        },
                    )
                )
            elif steer_mode == "follow_up_only":
                mailbox = self.mailbox_manager.get_mailbox(turn.turn_id)
                await mailbox.put(
                    MailboxItem(
                        turn_id=turn.turn_id,
                        item_type="message",
                        payload={
                            "message_id": user_msg.message_id,
                            "content": user_msg.content,
                        },
                    )
                )

        return user_msg, turn

    async def _handle_queued_followup(self, turn: Turn, followup_msg: Message) -> None:
        """Triggered by Coordinator when a turn finishes with pending follow-up in mailbox."""
        adapter = self.coordinator.get_adapter(turn.bound_agent_id)
        turn.status = "running"
        turn.finished_at = None
        turn.interrupt_reason = None
        turn.last_event_at = current_iso()
        self.store.save_turn(turn)

        history = self.store.get_messages(turn.conversation_id)
        await adapter.send(turn, followup_msg, history)

    async def cancel_turn(self, turn_id: str, reason: str = "cancelled_by_user") -> Turn:
        """Abort/cancel an active Turn. Enforces supports_cancel capability gate."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        adapter = self.coordinator.get_adapter(turn.bound_agent_id)
        caps = adapter.capabilities()
        if not caps.supports_cancel:
            raise RuntimeError(
                f"Agent adapter '{turn.bound_agent_id}' does not support cancellation"
            )

        await adapter.cancel(turn)
        await self.coordinator.emit_interrupted(turn_id, reason=reason)
        return self.store.get_turn(turn_id)  # Refresh from store

    async def resume_turn(self, turn_id: str) -> Turn:
        """Resume an interrupted or waiting Turn natively. Enforces supports_resume capability gate."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        adapter = self.coordinator.get_adapter(turn.bound_agent_id)
        caps = adapter.capabilities()
        if not caps.supports_resume:
            raise RuntimeError(
                f"Agent adapter '{turn.bound_agent_id}' does not support native resume"
            )

        turn.status = "running"
        turn.interrupt_reason = None
        turn.last_event_at = current_iso()
        self.store.save_turn(turn)

        history = self.store.get_messages(turn.conversation_id)
        await adapter.resume(turn, history)
        return self.store.get_turn(turn_id)
