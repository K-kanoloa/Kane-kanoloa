"""Conversation, Message, Branch, and Turn Creation HTTP Routes."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..domain.models import Conversation, Message, Turn
from ..harness.dispatcher import Dispatcher
from ..store.base import BaseStore
from .deps import get_dispatcher, get_store

router = APIRouter(tags=["conversations"])


# --- Request & Response DTOs ---

class CreateConversationRequest(BaseModel):
    title: str = "New Conversation"
    bound_agent_id: str = "kanaloa"


class SendMessageRequest(BaseModel):
    content: str
    turn_id: str | None = None
    reply_to_message_id: str | None = None
    loop_mode: bool = False
    max_iterations: int | None = Field(default=5, gt=0)


class SendMessageResponse(BaseModel):
    message: Message
    turn: Turn


class CreateTurnRequest(BaseModel):
    title: str | None = None
    branch_id: str | None = None


class SetFocusTurnRequest(BaseModel):
    turn_id: str


class CreateBranchRequest(BaseModel):
    message_id: str
    name: str | None = None


class BranchResponse(BaseModel):
    branch_id: str
    conversation_id: str
    branch_point_message_id: str
    initial_turn_id: str


# --- Endpoints ---

@router.post("/conversations", response_model=Conversation)
async def create_conversation(
    body: CreateConversationRequest,
    store: BaseStore = Depends(get_store),
) -> Conversation:
    """Create a new conversation container without auto-starting turns or agents (§4)."""
    conv = Conversation(
        title=body.title,
        bound_agent_id=body.bound_agent_id,
    )
    store.save_conversation(conv)
    return conv


@router.get("/conversations", response_model=list[Conversation])
async def list_conversations(
    store: BaseStore = Depends(get_store),
) -> list[Conversation]:
    """List all existing conversations (§4)."""
    return store.list_conversations()


@router.get("/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(
    conversation_id: str,
    store: BaseStore = Depends(get_store),
) -> Conversation:
    """Get single conversation by ID (§4)."""
    conv = store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(
            status_code=404, detail=f"Conversation '{conversation_id}' not found"
        )
    return conv


@router.get("/conversations/{conversation_id}/messages", response_model=list[Message])
async def get_conversation_messages(
    conversation_id: str,
    turn_id: str | None = Query(None, description="Optional filter by turn_id for branch-visible history"),
    store: BaseStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> list[Message]:
    """Retrieve chat message history for a conversation (§5)."""
    conv = store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(
            status_code=404, detail=f"Conversation '{conversation_id}' not found"
        )
    if turn_id:
        turn = store.get_turn(turn_id)
        if not turn:
            raise HTTPException(status_code=404, detail=f"Turn '{turn_id}' not found")
        if turn.conversation_id != conversation_id:
            raise HTTPException(
                status_code=400,
                detail=f"Turn '{turn_id}' does not belong to conversation '{conversation_id}'",
            )
        return dispatcher.get_turn_history(turn)
    return store.get_messages(conversation_id)


@router.post("/conversations/{conversation_id}/messages", response_model=SendMessageResponse)
async def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    store: BaseStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> SendMessageResponse:
    """
    Send a user message to a conversation (§5, §6).
    Delegates 100% to Dispatcher for deterministic Turn resolution and Steer degradation.
    """
    conv = store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(
            status_code=404, detail=f"Conversation '{conversation_id}' not found"
        )
    if not body.loop_mode and "max_iterations" in body.model_fields_set:
        raise HTTPException(status_code=400, detail="max_iterations requires loop_mode=true")

    try:
        user_msg, turn = await dispatcher.dispatch_user_message(
            conversation_id=conversation_id,
            content=body.content,
            target_turn_id=body.turn_id,
            reply_to_message_id=body.reply_to_message_id,
            loop_mode=body.loop_mode,
            max_iterations=body.max_iterations,
        )
        return SendMessageResponse(message=user_msg, turn=turn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/conversations/{conversation_id}/turns", response_model=Turn)
async def create_turn(
    conversation_id: str,
    body: CreateTurnRequest,
    store: BaseStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> Turn:
    """Explicitly create a new parallel work Turn for a conversation (§7)."""
    conv = store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(
            status_code=404, detail=f"Conversation '{conversation_id}' not found"
        )

    try:
        turn = dispatcher.create_new_turn(
            conversation_id=conversation_id,
            branch_id=body.branch_id,
            title=body.title,
        )
        return turn
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/conversations/{conversation_id}/turns", response_model=list[Turn])
async def list_conversation_turns(
    conversation_id: str,
    store: BaseStore = Depends(get_store),
) -> list[Turn]:
    """List all work Turns for a conversation (§8)."""
    conv = store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(
            status_code=404, detail=f"Conversation '{conversation_id}' not found"
        )
    return store.list_turns(conversation_id)


@router.post("/conversations/{conversation_id}/focus")
async def set_focus_turn(
    conversation_id: str,
    body: SetFocusTurnRequest,
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> dict[str, str]:
    """Explicitly switch conversation focus to a specific Turn (§9)."""
    try:
        turn = dispatcher.set_focus_turn(conversation_id, body.turn_id)
        return {"conversation_id": conversation_id, "focus_turn_id": turn.turn_id}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/conversations/{conversation_id}/branches", response_model=BranchResponse)
async def create_branch(
    conversation_id: str,
    body: CreateBranchRequest,
    store: BaseStore = Depends(get_store),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> BranchResponse:
    """
    Explicitly create a new persistent history boundary (Branch) from a historical message (§10).
    Spawns an isolated Turn operating on the new branch worldline.
    """
    try:
        branch_turn = dispatcher.branch_from_message(
            conversation_id=conversation_id,
            message_id=body.message_id,
            title=body.name,
        )
        branch = store.get_branch(branch_turn.branch_id)
        point_msg_id = branch.branch_point_message_id if branch else body.message_id
        return BranchResponse(
            branch_id=branch_turn.branch_id,
            conversation_id=conversation_id,
            branch_point_message_id=point_msg_id or body.message_id,
            initial_turn_id=branch_turn.turn_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
