"""Deterministic ACP transport for browser contract tests; never loaded by app.main."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.adapters.kanaloa_adapter import KanaloaAdapter
from app.harness.coordinator import HarnessCoordinator
from app.harness.dispatcher import Dispatcher
from app.harness.mailbox import MailboxManager
from app.main import create_app
from app.store.sqlite_store import SQLiteStore


class PermissionSink:
    def __init__(self, adapter):
        self.adapter = adapter

    def write(self, data):
        response = json.loads(data)
        self.adapter.calls.append({"method": "permission/response", "response": response})
        self.adapter.permission_gates[str(response["id"])].set()

    async def drain(self):
        pass


class BrowserTestAdapter(KanaloaAdapter):
    """Exercise the real Kanaloa adapter/runtime with a controlled ACP peer."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []
        self.scenarios = {}
        self.permission_gates = {}
        self.hold_gates = {}
        self.counter = 0

    async def _ensure_process(self):
        self._process = SimpleNamespace(returncode=None, stdin=PermissionSink(self))
        self._is_initialized = True

    async def _send_request(self, method, params=None):
        params = params or {}
        self.calls.append({"method": method, "params": params})
        if method == "session/new":
            self.counter += 1
            return {"result": {"sessionId": f"browser_session_{self.counter}"}}
        session = params.get("sessionId")
        turn_id = self._session_turns.get(session)
        if method == "session/steer":
            await self.event_handler.emit_event(turn_id, "progress", {"status": "steer_received"})
            return {"result": {}}
        if method == "session/resume":
            asyncio.create_task(self.finish_resume(turn_id))
            return {"result": {}}
        if method != "session/prompt":
            return {"result": {}}
        prompt = params["prompt"][-1]["text"]
        if not self.runtime.is_loop_active(turn_id) or session not in self.scenarios:
            self.scenarios[session] = prompt
        scenario = self.scenarios[session]
        await self.event_handler.emit_event(turn_id, "thinking", {"status": "thinking", "summary": "PRIVATE_CHAIN_OF_THOUGHT_MUST_NOT_RENDER"})
        await asyncio.sleep(0.35)
        await self._handle_session_update({"sessionId": session, "update": {"type": "tool_start", "toolName": "read_project"}})
        await self._handle_session_update({"sessionId": session, "update": {"type": "content", "content": {"type": "text", "text": "I have reviewed the project. "}}})
        await asyncio.sleep(0.35)
        if "[approval]" in scenario:
            request_id = f"permission_{session}"
            self.permission_gates[request_id] = asyncio.Event()
            await self._handle_permission_request({"id": request_id, "method": "session/request_permission", "params": {"sessionId": session, "message": "Please confirm access to the project files.", "toolCall": {"title": "Read the project files", "arguments": "PRIVATE_TOOL_ARGUMENTS"}, "options": [{"optionId": "allow-once"}, {"optionId": "reject-once"}]}})
            await self.permission_gates[request_id].wait()
        if "[interrupt]" in scenario:
            raise ConnectionError("Test transport disconnected")
        if "[failed]" in scenario:
            return {"result": {"stopReason": "failed", "failureReason": "The agent could not complete this work"}}
        if "[hold]" in scenario:
            gate = self.hold_gates.setdefault(session, asyncio.Event())
            await gate.wait()
            return {"result": {"stopReason": "cancelled"}}
        text = "The boundaries are clear: Kane preserves conversation continuity; the agent owns execution.\n\nNext, I will check the adapter contract and the recovery path."
        if self.runtime.is_loop_active(turn_id):
            text = f"Iteration {self.runtime.get_loop_iteration(turn_id)} checked.\n"
            await asyncio.sleep(0.25)
        if "[long]" in scenario:
            text = "A long reply stays in one message.\n" * 1000
        await self._handle_session_update({"sessionId": session, "update": {"type": "content", "content": {"type": "text", "text": text}}})
        await self._handle_session_update({"sessionId": session, "update": {"type": "tool_end", "toolName": "read_project"}})
        return {"result": {"stopReason": "endTurn"}}

    async def _send_notification(self, method, params=None):
        self.calls.append({"method": method, "params": params})
        if method == "session/cancel":
            gate = self.hold_gates.get(params["sessionId"])
            if gate:
                gate.set()

    async def finish_resume(self, turn_id):
        await asyncio.sleep(0.3)
        await self.event_handler.emit_delta(turn_id, "The native session has resumed.")
        await self.event_handler.emit_message_complete(turn_id)

    async def close(self):
        self.runtime.cancel_all_loops()
        for gate in [*self.hold_gates.values(), *self.permission_gates.values()]:
            gate.set()


def create_ui_app():
    if os.environ.get("KANE_UI_TEST") != "1":
        raise RuntimeError("This fixture is only for isolated browser tests")
    path = Path(os.environ["KANE_SQLITE_PATH"]).resolve()
    if not path.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise RuntimeError("Browser tests must use a temporary database")
    app = create_app()
    store = SQLiteStore(path)
    mailbox = MailboxManager()
    coordinator = HarnessCoordinator(store, mailbox)
    adapter = BrowserTestAdapter(event_handler=coordinator)
    coordinator.register_adapter("kanaloa", adapter)
    app.state.store = store
    app.state.mailbox_manager = mailbox
    app.state.coordinator = coordinator
    app.state.dispatcher = Dispatcher(store, coordinator, mailbox)
    app.state.kanaloa_adapter = adapter

    @app.get("/__ui__/calls")
    async def calls():
        return adapter.calls

    return app
