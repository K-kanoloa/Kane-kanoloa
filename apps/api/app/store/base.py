"""Store interface for Kane vNext.

Isolates persistence from Harness and Adapter logic.
Default implementation for v0.1 is SQLiteStore.
Future persistence implementations can replace the Store interface
without touching Harness logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal, Sequence

from ..domain.models import (
    AgentBinding,
    BranchBoundary,
    Conversation,
    Message,
    Turn,
    TurnEvent,
)


class BaseStore(ABC):
    """Abstract store interface defining all persistence contracts."""

    @abstractmethod
    def delete_agent_binding(self, agent_id: str) -> None:
        raise NotImplementedError

    # --- Branch Boundary Operations (§16, §17) ---
    @abstractmethod
    def save_branch(self, branch: BranchBoundary) -> None:
        """Create or update a branch boundary."""
        raise NotImplementedError

    @abstractmethod
    def get_branch(self, branch_id: str) -> BranchBoundary | None:
        """Fetch branch boundary by branch_id."""
        raise NotImplementedError

    @abstractmethod
    def list_branches(self, conversation_id: str) -> list[BranchBoundary]:
        """List all branches belonging to a conversation."""
        raise NotImplementedError

    @abstractmethod
    def get_or_create_main_branch(self, conversation_id: str) -> BranchBoundary:
        """Get or create the root main branch for a conversation."""
        raise NotImplementedError

    @abstractmethod
    def update_conversation_title(self, conversation_id: str, title: str) -> Conversation | None:
        raise NotImplementedError

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete local records atomically; reject running/waiting_user Turns."""
        raise NotImplementedError

    # --- Conversation Operations ---
    @abstractmethod
    def save_conversation(self, conversation: Conversation) -> None:
        """Create or update a conversation."""
        raise NotImplementedError

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Fetch a conversation by id."""
        raise NotImplementedError

    @abstractmethod
    def list_conversations(self) -> list[Conversation]:
        """List all conversations ordered by updated_at desc."""
        raise NotImplementedError

    # --- Message Operations (Append-Only) ---
    @abstractmethod
    def append_message(
        self,
        message: Message,
        delivery_kind: Literal["message", "steer"] | None = None,
    ) -> None:
        """Append a message and, when queued, its delivery intent atomically."""
        raise NotImplementedError

    @abstractmethod
    def list_unsettled_inbound(self) -> list[tuple[str, str, str, str]]:
        """Return (message_id, turn_id, kind, state) for pending/uncertain input."""
        raise NotImplementedError

    @abstractmethod
    def claim_inbound(self, message_id: str) -> bool:
        """Claim pending input before sending it to an adapter."""
        raise NotImplementedError

    @abstractmethod
    def complete_inbound(self, message_id: str) -> None:
        """Clear an input after the adapter accepts it."""
        raise NotImplementedError

    @abstractmethod
    def get_messages(
        self,
        conversation_id: str,
        up_to_message_id: str | None = None,
    ) -> list[Message]:
        """
        Get messages for a conversation.
        If up_to_message_id is provided, returns the lineage of visible messages
        up to that message (branch history boundary).
        """
        raise NotImplementedError

    @abstractmethod
    def get_message(self, message_id: str) -> Message | None:
        """Fetch a single message by id."""
        raise NotImplementedError

    @abstractmethod
    def get_turn_id_by_message_id(self, message_id: str) -> str | None:
        """Deterministically resolve the turn_id that a message belongs to."""
        raise NotImplementedError

    # --- Turn Operations ---
    @abstractmethod
    def save_turn(self, turn: Turn) -> None:
        """Create or update a Turn's runtime facts."""
        raise NotImplementedError

    @abstractmethod
    def get_turn(self, turn_id: str) -> Turn | None:
        """Fetch a Turn by id."""
        raise NotImplementedError

    @abstractmethod
    def list_turns(self, conversation_id: str) -> list[Turn]:
        """List all turns belonging to a conversation."""
        raise NotImplementedError

    @abstractmethod
    def list_active_turns(self) -> list[Turn]:
        """List running and waiting_user turns for startup liveness reconciliation."""
        raise NotImplementedError

    @abstractmethod
    def finalize_turn_completion(
        self,
        turn: Turn,
        message: Message,
        status_event: TurnEvent | None = None,
    ) -> None:
        """
        Atomically persist a logical Message and its Turn status/event in one transaction.
        """
        raise NotImplementedError

    # --- Turn Event Operations ---
    @abstractmethod
    def append_event(self, event: TurnEvent) -> None:
        """Record an ephemeral turn event (thinking, delta, progress)."""
        raise NotImplementedError

    @abstractmethod
    def list_events(self, turn_id: str) -> list[TurnEvent]:
        """Get all events for a given turn."""
        raise NotImplementedError

    # --- Agent Binding Operations ---
    @abstractmethod
    def save_agent_binding(self, binding: AgentBinding) -> None:
        """Register or update an agent binding configuration."""
        raise NotImplementedError

    @abstractmethod
    def get_agent_binding(self, agent_id: str) -> AgentBinding | None:
        """Fetch agent binding by agent_id."""
        raise NotImplementedError

    @abstractmethod
    def list_agent_bindings(self) -> list[AgentBinding]:
        """List all registered agent bindings."""
        raise NotImplementedError
