"""Store interface for Kane vNext.

Isolates persistence from Harness and Adapter logic.
Default implementation for v0.1 is SQLiteStore.
Future persistence implementations can replace the Store interface
without touching Harness logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ..domain.models import (
    AgentBinding,
    Conversation,
    Message,
    Turn,
    TurnEvent,
)


class BaseStore(ABC):
    """Abstract store interface defining all persistence contracts."""

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
    def append_message(self, message: Message) -> None:
        """Append a new message. Historical records cannot be overwritten."""
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
    def list_running_turns(self) -> list[Turn]:
        """List all turns currently in 'running' status across all conversations (for startup reconciliation)."""
        raise NotImplementedError

    @abstractmethod
    def finalize_turn_completion(
        self,
        turn: Turn,
        message: Message,
        status_event: TurnEvent | None = None,
    ) -> None:
        """
        Atomically persist final Message, clear Turn partial_output, set Turn status to finished,
        and optionally append completion TurnEvent in a single ACID transaction.
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
