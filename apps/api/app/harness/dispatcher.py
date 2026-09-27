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

from ..domain.models import BranchBoundary, Message, Turn, current_iso
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

    def set_focus_turn(self, conversation_id: str, turn_id: str) -> Turn:
        """
        Explicitly set a turn as the focus turn of a conversation (§11, §14).
        Only updates conv.focus_turn_id when the user explicitly chooses it.
        """
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")
        if turn.conversation_id != conversation_id:
            raise ValueError(f"Turn '{turn_id}' does not belong to conversation '{conversation_id}'")
        conv = self.store.get_conversation(conversation_id)
        if not conv:
            raise ValueError(f"Conversation '{conversation_id}' not found")
        conv.focus_turn_id = turn_id
        conv.updated_at = current_iso()
        self.store.save_conversation(conv)
        return turn

    def create_branch(
        self,
        conversation_id: str,
        message_id: str,
        name: str | None = None,
    ) -> BranchBoundary:
        """
        Create a new persistent history boundary (Branch) inside a conversation (§16, §17).
        Branch is decoupled from Turn: a Branch represents a history worldline that can host
        multiple subsequent Turns.
        """
        conv = self.store.get_conversation(conversation_id)
        if not conv:
            raise ValueError(f"Conversation '{conversation_id}' not found")

        msg = self.store.get_message(message_id)
        if not msg:
            raise ValueError(f"Message '{message_id}' not found")
        if msg.conversation_id != conversation_id:
            raise ValueError(
                f"Message '{message_id}' belongs to conversation '{msg.conversation_id}', not '{conversation_id}'"
            )

        branch = BranchBoundary(
            conversation_id=conversation_id,
            branch_point_message_id=message_id,
            name=name,
        )
        self.store.save_branch(branch)
        return branch

    def branch_from_message(
        self,
        conversation_id: str,
        message_id: str,
        title: str | None = None,
    ) -> Turn:
        """
        Explicitly create a new Branch path inside the same Conversation (§16, §17, §40).
        1. Creates BranchBoundary metadata (history boundary).
        2. Spawns the initial isolated Turn operating on this new branch (branch_id = branch.branch_id).
        Does NOT conflate Branch with Turn: subsequent Turns can be created on this branch.
        """
        conv = self.store.get_conversation(conversation_id)
        if not conv:
            raise ValueError(f"Conversation '{conversation_id}' not found")

        branch = self.create_branch(conversation_id, message_id, name=title)

        branch_turn = Turn(
            conversation_id=conversation_id,
            bound_agent_id=conv.bound_agent_id,
            branch_id=branch.branch_id,
            status="running",
            title=title or f"Branch from {message_id}",
        )
        self.store.save_turn(branch_turn)
        return branch_turn

    def get_turn_history(self, turn: Turn) -> list[Message]:
        """
        Compute the visible historical messages for a Turn (§16, §17, §40):
        Branch = conversation history worldline.
        Turn = work lifecycle within that worldline.

        - If Turn is on a side branch (branch_point_message_id is not None):
          Visible history = [lineage up to branch_point_message_id via parent_id walk]
                          + [all messages from turns operating in this branch].
          Never sees main messages created after the branch point.
          Never sees messages from other side branches.
        - If Turn is on the Main branch (branch_id == 'main' or no branch_point):
          Visible history = all messages in conversation EXCLUDING messages from side branch turns.
          Never sees branch messages.
        """
        conversation_id = turn.conversation_id
        all_conv_messages = self.store.get_messages(conversation_id)
        all_turns = self.store.list_turns(conversation_id)

        branch = self.store.get_branch(turn.branch_id) if turn.branch_id and turn.branch_id != "main" else None
        branch_point = branch.branch_point_message_id if branch else None

        if branch_point:
            # 1. Base historical lineage up to branch point (pure parent_id walk, zero rowid fallback)
            base_history = self.store.get_messages(
                conversation_id,
                up_to_message_id=branch_point,
            )
            base_ids = {m.message_id for m in base_history}

            # 2. Add prior messages belonging to any Turn operating on this branch
            branch_turn_ids = {
                t.turn_id for t in all_turns if getattr(t, "branch_id", "main") == turn.branch_id
            }
            branch_turn_msgs = [
                m for m in all_conv_messages
                if m.turn_id in branch_turn_ids and m.message_id not in base_ids
            ]
            return base_history + branch_turn_msgs
        else:
            # Main lineage: exclude messages belonging to turns on any side branch
            side_branches = {
                b.branch_id for b in self.store.list_branches(conversation_id)
                if b.branch_point_message_id is not None
            }
            side_branch_turn_ids = {
                t.turn_id for t in all_turns if getattr(t, "branch_id", "main") in side_branches
            }
            return [m for m in all_conv_messages if m.turn_id not in side_branch_turn_ids]

    def create_new_turn(
        self,
        conversation_id: str,
        branch_id: str | None = None,
        title: str | None = None,
    ) -> Turn:
        """
        Explicitly create an additional independent Turn for a conversation (New Turn).
        Can be created on 'main' or on an existing side branch.
        """
        conv = self.store.get_conversation(conversation_id)
        if not conv:
            from ..domain.models import Conversation
            conv = Conversation(conversation_id=conversation_id)
            self.store.save_conversation(conv)

        # Resolve target branch: explicit branch_id, or branch of focus turn, or 'main'
        target_branch_id = branch_id
        if not target_branch_id:
            if conv.focus_turn_id:
                focus = self.store.get_turn(conv.focus_turn_id)
                if focus and getattr(focus, "branch_id", None):
                    target_branch_id = focus.branch_id
        if not target_branch_id:
            target_branch_id = "main"

        new_turn = Turn(
            conversation_id=conversation_id,
            bound_agent_id=conv.bound_agent_id,
            branch_id=target_branch_id,
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

        # Resolve effective parent_id for branch lineage
        effective_parent_id = parent_id
        if effective_parent_id is None:
            # Look up messages already belonging to this branch
            branch_turn_ids = {
                t.turn_id for t in self.store.list_turns(conversation_id)
                if getattr(t, "branch_id", "main") == turn.branch_id
            }
            branch_msgs = [
                m for m in self.store.get_messages(conversation_id)
                if m.turn_id in branch_turn_ids
            ]
            if branch_msgs:
                effective_parent_id = branch_msgs[-1].message_id
            else:
                # First message in this branch:
                branch = self.store.get_branch(turn.branch_id) if turn.branch_id and turn.branch_id != "main" else None
                if branch and branch.branch_point_message_id:
                    effective_parent_id = branch.branch_point_message_id
                else:
                    # Root message in main branch: link to last main message if any
                    main_turn_ids = {
                        t.turn_id for t in self.store.list_turns(conversation_id)
                        if getattr(t, "branch_id", "main") == "main"
                    }
                    main_msgs = [
                        m for m in self.store.get_messages(conversation_id)
                        if m.turn_id in main_turn_ids
                    ]
                    effective_parent_id = main_msgs[-1].message_id if main_msgs else None

        # 2. Append User Message with explicit turn_id
        user_msg = Message(
            conversation_id=conversation_id,
            turn_id=turn.turn_id,
            sender="user",
            reply_to=reply_to_message_id,
            parent_id=effective_parent_id,
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

            history = [m for m in self.get_turn_history(turn) if m.message_id != user_msg.message_id]
            await adapter.send(turn, user_msg, history)

        elif turn.status == "waiting_user":
            # Resume waiting turn
            turn.status = "running"
            turn.last_event_at = current_iso()
            self.store.save_turn(turn)

            history = [m for m in self.get_turn_history(turn) if m.message_id != user_msg.message_id]
            await adapter.send(turn, user_msg, history)

        elif turn.status == "running":
            # If the adapter manages native sessions and no native session has been established yet for this turn
            # (e.g. initial message for a brand new turn or a newly created branch turn),
            # this message is the turn's initial bootstrap send, not a mid-flight steer.
            needs_initial_send = False
            if hasattr(adapter, "get_native_session"):
                native_sess = turn.native_session_ref or adapter.get_native_session(turn.turn_id)
                if not native_sess:
                    needs_initial_send = True

            if needs_initial_send:
                history = [m for m in self.get_turn_history(turn) if m.message_id != user_msg.message_id]
                await adapter.send(turn, user_msg, history)
            else:
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

        history = [m for m in self.get_turn_history(turn) if m.message_id != followup_msg.message_id]
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

        history = self.get_turn_history(turn)
        await adapter.resume(turn, history)
        return self.store.get_turn(turn_id)

    def stop_loop(self, turn_id: str) -> Turn:
        """Gracefully stop loop execution for an active loop Turn (§11)."""
        turn = self.store.get_turn(turn_id)
        if not turn:
            raise ValueError(f"Turn '{turn_id}' not found")

        adapter = self.coordinator.get_adapter(turn.bound_agent_id)
        if hasattr(adapter, "stop_loop"):
            adapter.stop_loop(turn)
        return turn
