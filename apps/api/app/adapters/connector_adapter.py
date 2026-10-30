"""Kane-side adapter for an Agent-initiated Kane Connector Protocol session."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import time
import uuid
from typing import Any

from starlette.websockets import WebSocket

from ..domain.models import AgentCapabilities, Message, Turn
from .base import AgentEventHandler, BaseAdapter


@dataclass
class PendingConnectorPermission:
    request_id: str
    session_id: str | None
    turn_id: str
    title: str
    created_at: float


class ConnectorAdapter(BaseAdapter):
    """Translate the generic wire protocol to the existing BaseAdapter contract."""

    def __init__(
        self,
        agent_id: str,
        capabilities: AgentCapabilities | None = None,
        event_handler: AgentEventHandler | None = None,
        configured: bool = True,
    ) -> None:
        super().__init__(event_handler)
        self.agent_id = agent_id
        self._capabilities = capabilities or AgentCapabilities()
        self._configured = configured
        self._websocket: WebSocket | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sessions: set[str] = set()
        self._seen_events: OrderedDict[str, None] = OrderedDict()
        self._permissions: dict[str, PendingConnectorPermission] = {}

    def capabilities(self) -> AgentCapabilities:
        return self._capabilities

    def is_available(self) -> bool:
        return self._configured and self.is_alive()

    def is_alive(self) -> bool:
        return self._websocket is not None

    async def attach(
        self,
        websocket: WebSocket,
        capabilities: AgentCapabilities,
        sessions: list[str],
    ) -> None:
        if self.is_alive():
            raise RuntimeError("An Agent Connector is already connected")
        self._websocket = websocket
        self._configured = True
        self._capabilities = capabilities
        self._sessions = set(sessions)

    async def detach(self) -> None:
        self._websocket = None
        self._sessions.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Agent Connector disconnected"))
        self._pending.clear()

    async def close(self) -> None:
        websocket = self._websocket
        await self.detach()
        if websocket is not None:
            await websocket.close(code=1000, reason="connector_disconnected_by_user")

    async def _command(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        websocket = self._websocket
        if websocket is None:
            raise RuntimeError(f"Agent '{self.agent_id}' is offline")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._send_lock:
                await websocket.send_json({
                    "protocol": "kane-connector",
                    "version": "0.1",
                    "type": command,
                    "id": uuid.uuid4().hex,
                    "request_id": request_id,
                    "payload": payload,
                })
            result = await asyncio.wait_for(future, timeout=30)
        finally:
            self._pending.pop(request_id, None)
        if result.get("status") != "accepted":
            raise RuntimeError(str(result.get("reason") or "Connector rejected command"))
        return result

    async def send(self, turn: Turn, message: Message, history: list[Message]) -> None:
        await self._command("turn.send", {
            "conversation_id": turn.conversation_id,
            "turn_id": turn.turn_id,
            "message_id": message.message_id,
            "content": message.content,
            "native_session_ref": turn.native_session_ref,
            "context_messages": [
                {"message_id": item.message_id, "sender": item.sender,
                 "sender_id": item.sender_id, "content": item.content}
                for item in history
            ],
        })

    async def steer(self, turn: Turn, message: Message) -> None:
        await self._command("turn.steer", {
            "conversation_id": turn.conversation_id,
            "turn_id": turn.turn_id,
            "message_id": message.message_id,
            "content": message.content,
            "native_session_ref": turn.native_session_ref,
        })

    async def cancel(self, turn: Turn) -> None:
        await self._command("turn.cancel", {
            "conversation_id": turn.conversation_id,
            "turn_id": turn.turn_id,
            "native_session_ref": turn.native_session_ref,
        })

    async def resume(self, turn: Turn, history: list[Message]) -> None:
        await self._command("turn.resume", {
            "conversation_id": turn.conversation_id,
            "turn_id": turn.turn_id,
            "native_session_ref": turn.native_session_ref,
            "context_messages": [
                {"message_id": item.message_id, "sender": item.sender,
                 "sender_id": item.sender_id, "content": item.content}
                for item in history
            ],
        })

    async def respond_permission(
        self,
        request_id: str | int,
        decision: str,
        session_id: str | None = None,
    ) -> None:
        permission = self._permissions.get(str(request_id))
        if not permission or permission.session_id != session_id:
            raise ValueError("unknown_connector_permission")
        if decision not in ("allow-once", "reject-once", "cancelled"):
            raise ValueError("unsupported_permission_decision")
        await self._command("permission.respond", {
            "request_id": str(request_id),
            "decision": decision,
            "native_session_ref": session_id,
            "turn_id": permission.turn_id,
        })
        self._permissions.pop(str(request_id), None)
        if decision != "cancelled":
            await self.event_handler.emit_resumed(permission.turn_id, "connector_permission_resolved")

    def list_pending_permissions(self, session_id: str | None = None) -> list[PendingConnectorPermission]:
        return [item for item in self._permissions.values() if session_id is None or item.session_id == session_id]

    async def probe_session(self, native_session_ref: str | None) -> bool:
        return bool(self.is_alive() and native_session_ref and native_session_ref in self._sessions)

    async def handle_frame(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Connector frame payload must be an object")

        event_id = frame.get("event_id")
        if frame_type != "command.result":
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("Connector event requires event_id")
            if event_id in self._seen_events:
                return

        if frame_type == "command.result":
            request_id = frame.get("request_id")
            future = self._pending.get(request_id) if isinstance(request_id, str) else None
            if future and not future.done():
                future.set_result(payload)
            return

        if frame_type == "agent.availability":
            return

        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str):
            raise ValueError("Connector event requires turn_id")

        if frame_type == "session.bound":
            session_ref = payload.get("native_session_ref")
            if not isinstance(session_ref, str) or not session_ref:
                raise ValueError("session.bound requires native_session_ref")
            self._sessions.add(session_ref)
            await self.event_handler.emit_event(turn_id, "status_change", {"native_session_ref": session_ref})
        elif frame_type == "reply.delta":
            reply_id, text = payload.get("reply_id"), payload.get("text")
            if not isinstance(reply_id, str) or not isinstance(text, str):
                raise ValueError("reply.delta requires reply_id and text")
            await self.event_handler.emit_delta(turn_id, text)
        elif frame_type == "reply.completed":
            reply_id = payload.get("reply_id")
            if not isinstance(reply_id, str):
                raise ValueError("reply.completed requires reply_id")
            stable_message_id = "msg_connector_" + hashlib.sha256(
                f"{self.agent_id}\0{turn_id}\0{reply_id}".encode("utf-8")
            ).hexdigest()
            await self.event_handler.emit_message_complete(
                turn_id,
                content=payload.get("content"),
                sender_id=self.agent_id,
                turn_finished=payload.get("turn_finished", True) is True,
                message_id=stable_message_id,
            )
        elif frame_type == "turn.waiting_user":
            await self.event_handler.emit_waiting_user(turn_id, prompt=payload.get("prompt"))
        elif frame_type == "permission.requested":
            request_id = payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("permission.requested requires request_id")
            permission = PendingConnectorPermission(
                request_id=request_id,
                session_id=payload.get("native_session_ref"),
                turn_id=turn_id,
                title=str(payload.get("title") or "Agent permission request"),
                created_at=time.time(),
            )
            self._permissions[request_id] = permission
            await self.event_handler.emit_waiting_user(turn_id, prompt=permission.title, record_message=False)
        elif frame_type == "turn.failed":
            await self.event_handler.emit_failed(turn_id, str(payload.get("reason") or "agent_failed"))
        elif frame_type == "turn.interrupted":
            await self.event_handler.emit_interrupted(turn_id, str(payload.get("reason") or "connector_interrupted"))
        else:
            raise ValueError(f"Unsupported Connector event: {frame_type}")

        # Only acknowledge facts that reached their handler successfully.
        self._seen_events[event_id] = None
        if len(self._seen_events) > 4096:
            self._seen_events.popitem(last=False)
