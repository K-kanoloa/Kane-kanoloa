"""Pairing and persistent WebSocket transport for Agent-side Connectors."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.connector_adapter import ConnectorAdapter
from ..domain.models import AgentBinding, AgentCapabilities
from ..harness.coordinator import HarnessCoordinator
from ..store.base import BaseStore
from ..settings_env import get_api_token
from .deps import get_coordinator, get_store

router = APIRouter(tags=["connectors"])


class PairAgentRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)


class PairAgentResponse(BaseModel):
    agent_id: str
    pairing_code: str
    expires_in_seconds: int | None = None


class AgentNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str = Field(min_length=1, max_length=120)


def external_binding(agent_id: str, store: BaseStore) -> AgentBinding:
    if agent_id == "kanaloa":
        raise HTTPException(status_code=409, detail="Built-in Agent configuration is managed separately")
    binding = store.get_agent_binding(agent_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Agent not found")
    if binding.adapter_name != "kane_connector":
        raise HTTPException(status_code=409, detail="Built-in Agent configuration is managed separately")
    return binding


@router.patch("/agents/{agent_id}")
async def rename_external_agent(agent_id: str, body: AgentNameRequest, store: BaseStore = Depends(get_store)) -> dict[str, str]:
    binding = external_binding(agent_id, store)
    binding.display_name = body.display_name
    store.save_agent_binding(binding)
    return {"agent_id": agent_id, "display_name": binding.display_name}


@router.post("/agents/{agent_id}/disconnect", status_code=204)
async def disconnect_external_agent(agent_id: str, request: Request, store: BaseStore = Depends(get_store), coordinator: HarnessCoordinator = Depends(get_coordinator)) -> Response:
    binding = external_binding(agent_id, store)
    if coordinator.has_adapter(agent_id) and not isinstance(coordinator.get_adapter(agent_id), ConnectorAdapter):
        raise HTTPException(status_code=409, detail="Not an external Connector")
    binding.is_active = False
    binding.config.pop("connector_token_sha256", None)
    store.save_agent_binding(binding)
    pairings = getattr(request.app.state, "connector_pairings", {})
    for key, item in list(pairings.items()):
        if item["agent_id"] == agent_id:
            pairings.pop(key, None)
    if coordinator.has_adapter(agent_id):
        adapter = coordinator.get_adapter(agent_id)
        if not isinstance(adapter, ConnectorAdapter):
            raise HTTPException(status_code=409, detail="Not an external Connector")
        await adapter.close()
    for turn in store.list_active_turns():
        if turn.bound_agent_id == agent_id:
            await coordinator.emit_interrupted(turn.turn_id, "connector_disconnected_by_user")
    return Response(status_code=204)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_external_agent(agent_id: str, request: Request, store: BaseStore = Depends(get_store), coordinator: HarnessCoordinator = Depends(get_coordinator)) -> Response:
    external_binding(agent_id, store)
    if any(turn.bound_agent_id == agent_id for turn in store.list_active_turns()):
        raise HTTPException(status_code=409, detail="Stop active Turns before removing this Agent")
    await disconnect_external_agent(agent_id, request, store, coordinator)
    store.delete_agent_binding(agent_id)
    coordinator._adapters.pop(agent_id, None)
    return Response(status_code=204)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "testclient"


@router.post("/agents/pairings", response_model=PairAgentResponse)
async def create_pairing(
    body: PairAgentRequest,
    request: Request,
    store: BaseStore = Depends(get_store),
    coordinator: HarnessCoordinator = Depends(get_coordinator),
) -> PairAgentResponse:
    """Create a local/admin-authorized one-time code for one stable Agent identity."""
    if not get_api_token() and not _is_loopback(request):
        raise HTTPException(status_code=403, detail="remote_pairing_requires_api_auth")
    if coordinator.has_adapter(body.agent_id) and not isinstance(coordinator.get_adapter(body.agent_id), ConnectorAdapter):
        raise HTTPException(status_code=409, detail="agent_id_already_owned_by_local_adapter")
    if coordinator.has_adapter(body.agent_id) and coordinator.get_adapter(body.agent_id).is_alive():
        raise HTTPException(status_code=409, detail="agent_connector_already_connected")

    binding = store.get_agent_binding(body.agent_id)
    if binding and binding.adapter_name != "kane_connector":
        raise HTTPException(status_code=409, detail="agent_id_already_bound_to_another_adapter")
    if binding is None:
        binding = AgentBinding(
            agent_id=body.agent_id,
            display_name=body.display_name,
            adapter_name="kane_connector",
        )
    else:
        binding.display_name = body.display_name
        binding.is_active = True
        binding.config.pop("connector_token_sha256", None)
    store.save_agent_binding(binding)

    if not coordinator.has_adapter(body.agent_id):
        coordinator.register_adapter(
            body.agent_id,
            ConnectorAdapter(body.agent_id, binding.capabilities, coordinator, configured=False),
        )
    code = secrets.token_urlsafe(24)
    pairings = getattr(request.app.state, "connector_pairings", None)
    if pairings is None:
        pairings = {}
        request.app.state.connector_pairings = pairings
    for key, item in list(pairings.items()):
        if item["agent_id"] == body.agent_id:
            pairings.pop(key, None)
    pairings[_digest(code)] = {
        "agent_id": body.agent_id,
        "reserved": False,
    }
    return PairAgentResponse(
        agent_id=body.agent_id,
        pairing_code=code,
    )


@router.websocket("/connectors/ws")
async def connector_socket(websocket: WebSocket) -> None:
    store: BaseStore = get_store(websocket)  # type: ignore[arg-type]
    coordinator: HarnessCoordinator = get_coordinator(websocket)  # type: ignore[arg-type]
    auth = websocket.headers.get("authorization", "")
    scheme, _, credential = auth.partition(" ")
    pairings: dict[str, dict[str, Any]] = getattr(websocket.app.state, "connector_pairings", {})
    pairing_key: str | None = None
    pairing: dict[str, Any] | None = None
    binding: AgentBinding | None = None

    if scheme.lower() == "pairing" and credential:
        pairing_key = _digest(credential.strip())
        candidate = pairings.get(pairing_key)
        if candidate and not candidate["reserved"]:
            candidate["reserved"] = True
            pairing = candidate
    elif scheme.lower() == "bearer" and credential:
        for item in store.list_agent_bindings():
            expected = item.config.get("connector_token_sha256")
            if item.is_active and item.adapter_name == "kane_connector" and isinstance(expected, str) and hmac.compare_digest(expected, _digest(credential.strip())):
                binding = item
                break

    if pairing is None and binding is None:
        await websocket.close(code=4401, reason="connector_auth_required")
        return
    await websocket.accept()
    adapter: ConnectorAdapter | None = None
    attached = False
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if not isinstance(hello, dict) or hello.get("protocol") != "kane-connector" or hello.get("version") != "0.1" or hello.get("type") != "connector.hello":
            raise ValueError("unsupported_connector_handshake")
        payload = hello.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid_connector_handshake_payload")
        agent_id = payload.get("agent_id")
        connector_id = payload.get("connector_id")
        if not isinstance(agent_id, str) or not isinstance(connector_id, str) or not connector_id:
            raise ValueError("connector_identity_required")

        if pairing is not None:
            if pairings.get(pairing_key) is not pairing:
                raise ValueError("pairing_credential_revoked")
            if agent_id != pairing["agent_id"]:
                raise ValueError("pairing_identity_mismatch")
            binding = store.get_agent_binding(agent_id)
            if not binding or not binding.is_active or binding.adapter_name != "kane_connector":
                raise ValueError("pairing_identity_not_registered")
            token = secrets.token_urlsafe(32)
            binding.config["connector_id"] = connector_id
            binding.config["connector_token_sha256"] = _digest(token)
            binding.capabilities = AgentCapabilities.model_validate(payload.get("capabilities") or {})
            binding.display_name = str(payload.get("display_name") or binding.display_name)
            store.save_agent_binding(binding)
        else:
            token = ""
            fresh = store.get_agent_binding(agent_id) if isinstance(agent_id, str) else None
            if not fresh or not fresh.is_active or fresh.config.get("connector_token_sha256") != binding.config.get("connector_token_sha256"):
                raise ValueError("connector_credential_revoked")
            binding = fresh
            if not binding or agent_id != binding.agent_id or connector_id != binding.config.get("connector_id"):
                raise ValueError("connector_identity_mismatch")
            binding.capabilities = AgentCapabilities.model_validate(payload.get("capabilities") or {})
            store.save_agent_binding(binding)

        if pairing_key:
            pairings.pop(pairing_key, None)
        if not coordinator.has_adapter(agent_id):
            adapter = ConnectorAdapter(agent_id, binding.capabilities, coordinator)
            coordinator.register_adapter(agent_id, adapter)
        else:
            current = coordinator.get_adapter(agent_id)
            if not isinstance(current, ConnectorAdapter):
                raise ValueError("agent_id_adapter_conflict")
            adapter = current

        sessions = payload.get("sessions") or []
        if not isinstance(sessions, list) or any(not isinstance(item, str) for item in sessions):
            raise ValueError("sessions_must_be_string_list")
        await adapter.attach(websocket, binding.capabilities, sessions)
        attached = True
        await websocket.send_json({
            "protocol": "kane-connector",
            "version": "0.1",
            "type": "connector.ready",
            "id": secrets.token_hex(12),
            "payload": {
                "agent_id": agent_id,
                "heartbeat_interval_seconds": 20,
                **({"connector_token": token} if token else {}),
            },
        })

        while True:
            frame = await websocket.receive_json()
            if not isinstance(frame, dict) or frame.get("protocol") != "kane-connector" or frame.get("version") != "0.1":
                raise ValueError("invalid_connector_frame")
            frame_type = frame.get("type")
            if frame_type == "connector.heartbeat":
                await websocket.send_json({"protocol": "kane-connector", "version": "0.1", "type": "connector.heartbeat.ack", "id": secrets.token_hex(12), "payload": {}})
            else:
                if frame_type == "agent.availability":
                    event_payload = frame.get("payload")
                    if not isinstance(event_payload, dict) or event_payload.get("agent_id") != adapter.agent_id:
                        raise ValueError("event_agent_mismatch")
                elif frame_type != "command.result":
                    event_payload = frame.get("payload")
                    turn_id = event_payload.get("turn_id") if isinstance(event_payload, dict) else None
                    turn = store.get_turn(turn_id) if isinstance(turn_id, str) else None
                    if not turn or turn.bound_agent_id != adapter.agent_id:
                        raise ValueError("event_turn_not_bound_to_connector")
                    conversation_id = event_payload.get("conversation_id")
                    if conversation_id is not None and conversation_id != turn.conversation_id:
                        raise ValueError("event_conversation_mismatch")
                await adapter.handle_frame(frame)
                if isinstance(frame.get("event_id"), str):
                    await websocket.send_json({
                        "protocol": "kane-connector", "version": "0.1",
                        "type": "event.ack", "id": secrets.token_hex(12),
                        "payload": {"event_id": frame["event_id"]},
                    })
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close(code=4400, reason=str(exc)[:120])
    finally:
        if pairing_key and pairing_key in pairings:
            pairings.pop(pairing_key, None)
        if attached and adapter:
            await adapter.detach()
            for turn in store.list_active_turns():
                if turn.bound_agent_id == adapter.agent_id and coordinator.is_turn_active(turn.turn_id):
                    await coordinator.emit_interrupted(turn.turn_id, "connector_disconnected")
