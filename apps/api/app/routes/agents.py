"""Agent Discovery and Capability Query HTTP Routes."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..domain.models import BranchMode, SteerMode
from ..harness.coordinator import HarnessCoordinator
from .deps import get_coordinator

router = APIRouter(tags=["agents"])


class AgentSummaryResponse(BaseModel):
    agent_id: str
    status: str
    supports_stream: bool
    supports_resume: bool
    supports_cancel: bool
    supports_approval: bool
    supports_parallel_sessions: bool
    max_parallel_sessions: int | None
    branch_mode: BranchMode
    steer_mode: SteerMode


@router.get("/agents", response_model=list[AgentSummaryResponse])
async def list_agents(
    coordinator: HarnessCoordinator = Depends(get_coordinator),
) -> list[AgentSummaryResponse]:
    """
    List registered agent bindings and their truthful capability declarations (§13).
    Sourced directly from adapter.capabilities() truth.
    """
    agents: list[AgentSummaryResponse] = []
    for agent_id, adapter in coordinator._adapters.items():
        caps = adapter.capabilities()
        is_live = adapter.is_alive() if hasattr(adapter, "is_alive") else True
        agents.append(
            AgentSummaryResponse(
                agent_id=agent_id,
                status="ready" if is_live else "idle",
                supports_stream=caps.supports_stream,
                supports_resume=caps.supports_resume,
                supports_cancel=caps.supports_cancel,
                supports_approval=caps.supports_approval,
                supports_parallel_sessions=caps.supports_parallel_sessions,
                max_parallel_sessions=caps.max_parallel_sessions,
                branch_mode=caps.branch_mode,
                steer_mode=caps.steer_mode,
            )
        )
    return agents
