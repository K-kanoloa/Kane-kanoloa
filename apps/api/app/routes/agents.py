"""Agent Discovery and Capability Query HTTP Routes."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, SecretStr, field_validator

from ..domain.models import AgentBinding, BranchMode, SteerMode
from ..harness.coordinator import HarnessCoordinator
from ..adapters.kanaloa_adapter import KanaloaAdapter
from ..store.base import BaseStore
from .deps import get_coordinator, get_store

router = APIRouter(tags=["agents"])
logger = logging.getLogger(__name__)


class AgentSummaryResponse(BaseModel):
    agent_id: str
    display_name: str | None = None
    status: str
    supports_stream: bool
    supports_resume: bool
    supports_cancel: bool
    supports_approval: bool
    supports_parallel_sessions: bool
    max_parallel_sessions: int | None
    branch_mode: BranchMode
    steer_mode: SteerMode
    auto_start: bool | None = None


class KanaloaPreferenceRequest(BaseModel):
    auto_start: bool


class KanaloaModelConfigRequest(BaseModel):
    base_url: str
    model: str
    api_format: Literal["openai-completions", "openai-responses", "anthropic-messages"] = "openai-completions"
    api_key: SecretStr

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if (
            len(value) > 2048
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Enter an HTTP(S) API base URL without credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            raise ValueError("Model must contain 1 to 256 printable characters")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.strip() or len(raw) > 4096:
            raise ValueError("API key must contain 1 to 4096 characters")
        return SecretStr(raw.strip())


@router.get("/agents", response_model=list[AgentSummaryResponse])
async def list_agents(
    coordinator: HarnessCoordinator = Depends(get_coordinator),
    store: BaseStore = Depends(get_store),
) -> list[AgentSummaryResponse]:
    """
    List registered agent bindings and their truthful capability declarations (§13).
    Sourced directly from adapter.capabilities() truth.
    """
    agents: list[AgentSummaryResponse] = []
    for agent_id, adapter in coordinator._adapters.items():
        caps = adapter.capabilities()
        is_available = adapter.is_available() if hasattr(adapter, "is_available") else True
        is_live = adapter.is_alive() if hasattr(adapter, "is_alive") else True
        binding = store.get_agent_binding(agent_id)
        auto_start = bool(binding.config.get("auto_start", False)) if binding else False
        agents.append(
            AgentSummaryResponse(
                agent_id=agent_id,
                display_name=binding.display_name if binding else None,
                status="unavailable" if not is_available else "ready" if is_live else "idle",
                supports_stream=caps.supports_stream,
                supports_resume=caps.supports_resume,
                supports_cancel=caps.supports_cancel,
                supports_approval=caps.supports_approval,
                supports_parallel_sessions=caps.supports_parallel_sessions,
                max_parallel_sessions=caps.max_parallel_sessions,
                branch_mode=caps.branch_mode,
                steer_mode=caps.steer_mode,
                auto_start=auto_start if agent_id == "kanaloa" else None,
            )
        )
    registered_ids = {item.agent_id for item in agents}
    for binding in store.list_agent_bindings():
        if binding.agent_id in registered_ids:
            continue
        agents.append(AgentSummaryResponse(
            agent_id=binding.agent_id,
            display_name=binding.display_name,
            status="unavailable",
            supports_stream=binding.capabilities.supports_stream,
            supports_resume=binding.capabilities.supports_resume,
            supports_cancel=binding.capabilities.supports_cancel,
            supports_approval=binding.capabilities.supports_approval,
            supports_parallel_sessions=binding.capabilities.supports_parallel_sessions,
            max_parallel_sessions=binding.capabilities.max_parallel_sessions,
            branch_mode=binding.capabilities.branch_mode,
            steer_mode=binding.capabilities.steer_mode,
            auto_start=None,
        ))
    return agents


@router.post("/agents/kanaloa/activate", response_model=AgentSummaryResponse)
async def activate_kanaloa(
    coordinator: HarnessCoordinator = Depends(get_coordinator),
) -> AgentSummaryResponse:
    """Explicitly activate the bundled first-party agent; this does not validate a model provider."""
    if not coordinator.has_adapter("kanaloa"):
        raise HTTPException(status_code=404, detail="Kanaloa is not registered")
    adapter = coordinator.get_adapter("kanaloa")
    if not isinstance(adapter, KanaloaAdapter):
        raise HTTPException(status_code=409, detail="Kanaloa activation is not available for this adapter")
    if not adapter.is_available():
        raise HTTPException(status_code=503, detail="Bundled Kanaloa runtime is unavailable")
    try:
        await adapter.activate()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Kanaloa activation failed: {exc}") from exc
    caps = adapter.capabilities()
    return AgentSummaryResponse(agent_id="kanaloa", status="ready", **caps.model_dump())


@router.post("/agents/kanaloa/deactivate", status_code=204)
async def deactivate_kanaloa(coordinator: HarnessCoordinator = Depends(get_coordinator), store: BaseStore = Depends(get_store)) -> Response:
    if not coordinator.has_adapter("kanaloa"):
        raise HTTPException(status_code=404, detail="Kanaloa is not registered")
    if any(turn.bound_agent_id == "kanaloa" for turn in store.list_active_turns()):
        raise HTTPException(status_code=409, detail="Stop active Turns before deactivating Kanaloa")
    adapter = coordinator.get_adapter("kanaloa")
    if not isinstance(adapter, KanaloaAdapter):
        raise HTTPException(status_code=409, detail="Not the built-in Kanaloa Runtime")
    await adapter.close()
    return Response(status_code=204)


@router.post("/agents/kanaloa/preferences", response_model=AgentSummaryResponse)
async def set_kanaloa_preferences(
    body: KanaloaPreferenceRequest,
    coordinator: HarnessCoordinator = Depends(get_coordinator),
    store: BaseStore = Depends(get_store),
) -> AgentSummaryResponse:
    if not coordinator.has_adapter("kanaloa"):
        raise HTTPException(status_code=404, detail="Kanaloa is not registered")
    adapter = coordinator.get_adapter("kanaloa")
    binding = store.get_agent_binding("kanaloa") or AgentBinding(
        agent_id="kanaloa", display_name="Kanaloa", adapter_name="kanaloa"
    )
    binding.config["auto_start"] = body.auto_start
    binding.capabilities = adapter.capabilities()
    store.save_agent_binding(binding)
    is_available = adapter.is_available() if hasattr(adapter, "is_available") else True
    is_live = adapter.is_alive() if hasattr(adapter, "is_alive") else False
    return AgentSummaryResponse(
        agent_id="kanaloa",
        status="unavailable" if not is_available else "ready" if is_live else "idle",
        auto_start=body.auto_start,
        **adapter.capabilities().model_dump(),
    )


@router.post("/agents/kanaloa/model-config")
async def set_kanaloa_model_config(
    body: KanaloaModelConfigRequest,
    coordinator: HarnessCoordinator = Depends(get_coordinator),
) -> dict[str, str]:
    """Configure the built-in Kanaloa Runtime's default OpenAI-compatible model route."""
    if not coordinator.has_adapter("kanaloa"):
        raise HTTPException(status_code=404, detail="Kanaloa is not registered")
    adapter = coordinator.get_adapter("kanaloa")
    if not isinstance(adapter, KanaloaAdapter) or not adapter.is_available():
        raise HTTPException(status_code=503, detail="Built-in Kanaloa runtime is unavailable")

    repo_root = Path(__file__).resolve().parents[4]
    helper = repo_root / "scripts" / "configure-kanaloa-model.mjs"
    try:
        process = await asyncio.create_subprocess_exec(
            "node", str(helper),
            cwd=repo_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate(
            json.dumps({
                "base_url": body.base_url,
                "model": body.model,
                "api_format": body.api_format,
                "api_key": body.api_key.get_secret_value(),
            }).encode("utf-8")
        )
    except OSError as exc:
        logger.warning("Kanaloa model configuration write failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Kanaloa model configuration could not be saved") from None
    if process.returncode == 3:
        raise HTTPException(status_code=409, detail="The API key is overridden by the API process environment")
    if process.returncode != 0 or b'"status":"saved"' not in stdout:
        logger.warning("Kanaloa model configuration write failed: helper_exit=%s", process.returncode)
        raise HTTPException(status_code=503, detail="Kanaloa model configuration could not be saved")
    return {"status": "saved", "provider": "kane-kanaloa", "model": body.model}
