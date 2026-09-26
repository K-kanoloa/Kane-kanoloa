"""Kane vNext core domain models.

Disciplines:
- Kane Core is model-free (0 LLM, 0 Planner, 0 Tool Loop).
- Messages are strictly append-only (Event Log semantics).
- Transport chunks != Messages.
- Turn represents a long-running work lifecycle, NOT a single request-response.
"""

from __future__ import annotations

import time
from typing import Any, Literal
from pydantic import BaseModel, Field

from ..id_utils import new_id


def current_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- Message ---
MessageSender = Literal["user", "agent", "system"]
MessageKind = Literal["normal", "edit", "retract"]


class Message(BaseModel):
    """
    Append-only chat message primitive.
    Editing or retracting a message creates a new append-only entry with
    kind='edit' | 'retract' and points to target_message_id.
    """
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    conversation_id: str
    sender: MessageSender
    sender_id: str | None = None
    reply_to: str | None = None
    parent_id: str | None = None  # History lineage for branching
    content: str
    kind: MessageKind = "normal"
    target_message_id: str | None = None  # If kind is edit or retract
    created_at: str = Field(default_factory=current_iso)


# --- Turn ---
TurnStatus = Literal["running", "waiting_user", "finished", "failed", "interrupted"]


class Turn(BaseModel):
    """
    Turn represents an Agent's continuous lifecycle around a specific objective.
    A Turn can span minutes, hours, or days across multiple messages and steers.
    No heavy state machine: only minimal runtime facts (§8 & §39).
    """
    turn_id: str = Field(default_factory=lambda: new_id("turn"))
    conversation_id: str
    bound_agent_id: str
    title: str | None = None
    status: TurnStatus = "running"
    native_session_ref: str | None = None
    last_event_at: str = Field(default_factory=current_iso)
    interrupt_reason: str | None = None
    partial_output: str | None = None  # Survives across restart in Turn buffer (§31)
    created_at: str = Field(default_factory=current_iso)
    finished_at: str | None = None


# --- Event ---
EventType = Literal[
    "thinking",
    "reading",
    "tool_start",
    "tool_end",
    "progress",
    "delta",
    "status_change",
    "raw"
]


class TurnEvent(BaseModel):
    """
    Ephemeral runtime events (thinking, tools, streaming deltas).
    Kept for live UI status; does NOT pollute permanent chat message history.
    """
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    turn_id: str
    conversation_id: str
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=current_iso)


# --- Conversation ---
class Conversation(BaseModel):
    """
    Long-lived chat container.
    """
    conversation_id: str = Field(default_factory=lambda: new_id("conv"))
    title: str = "New Conversation"
    bound_agent_id: str = "kanaloa"
    focus_turn_id: str | None = None
    branch_point_message_id: str | None = None  # None = main lineage
    created_at: str = Field(default_factory=current_iso)
    updated_at: str = Field(default_factory=current_iso)


# --- Agent Binding & Capability ---
SteerMode = Literal["native", "safe_boundary", "follow_up_only"]
BranchMode = Literal["native", "replay", "unsupported"]


class AgentCapabilities(BaseModel):
    supports_stream: bool = True
    supports_resume: bool = False
    supports_cancel: bool = True
    supports_approval: bool = False
    supports_parallel_sessions: bool = False
    max_parallel_sessions: int | None = None
    steer_mode: SteerMode = "follow_up_only"
    branch_mode: BranchMode = "unsupported"


class AgentBinding(BaseModel):
    agent_id: str
    display_name: str
    adapter_name: str
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    config: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: str = Field(default_factory=current_iso)
