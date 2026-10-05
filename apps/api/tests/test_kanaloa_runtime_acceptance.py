"""Opt-in real HTTP/ACP acceptance using the saved model route, without mocks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os

import httpx
import pytest

from app.adapters.kanaloa_adapter import KanaloaAdapter
from app.main import create_app
from app.settings_env import get_api_token
from app.store.sqlite_store import SQLiteStore

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("KANE_REAL_DSH_TEST") != "1", reason="opt-in real saved-provider Runtime acceptance"),
]


@asynccontextmanager
async def real_http_runtime(tmp_path):
    app = create_app()
    app.state.store = SQLiteStore(tmp_path / "acceptance.db")
    adapter = KanaloaAdapter(cwd=str(tmp_path))
    app.state.kanaloa_adapter = adapter
    frames = []
    original = adapter._send_request

    async def capture(method, params=None):
        response = await original(method, params)
        result = response.get("result", {})
        frames.append({
            "method": method,
            "session": (params or {}).get("sessionId") or result.get("sessionId"),
            "stop": result.get("stopReason"),
            "native": result.get("_meta", {}).get("kaneNativeEndKind"),
        })
        return response

    adapter._send_request = capture
    async with app.router.lifespan_context(app):
        headers = {"X-Api-Key": get_api_token()} if get_api_token() else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kane", headers=headers) as client:
            yield app, client, adapter, frames


async def create_and_send(client, content, **options):
    response = await client.post("/api/v1/conversations", json={"title": "Real Runtime Acceptance", "bound_agent_id": "kanaloa"})
    assert response.status_code == 200
    cid = response.json()["conversation_id"]
    response = await client.post(f"/api/v1/conversations/{cid}/messages", json={"content": content, **options})
    assert response.status_code == 200
    return cid, response.json()["turn"]["turn_id"]


async def wait_terminal(client, tid, approvals):
    async with asyncio.timeout(180):
        while True:
            response = await client.get(f"/api/v1/turns/{tid}")
            assert response.status_code == 200
            turn = response.json()
            for permission in turn["pending_permissions"]:
                approvals.append({"request_id": permission["request_id"], "status": turn["status"]})
                reply = await client.post(f"/api/v1/turns/{tid}/permissions/{permission['request_id']}/respond", json={"decision": "allow-once"})
                assert reply.status_code == 200
            if turn["status"] not in {"running", "waiting_user"}:
                return turn
            await asyncio.sleep(0.1)


async def agent_messages(client, cid):
    response = await client.get(f"/api/v1/conversations/{cid}/messages")
    assert response.status_code == 200
    return [message for message in response.json() if message["sender"] == "agent"]


async def test_real_http_normal_continuity_and_whole_loop(tmp_path):
    async with real_http_runtime(tmp_path) as (app, client, adapter, frames):
        approvals = []
        cid, tid = await create_and_send(client, "Remember the marker KANE_CONTINUITY_4827 for this conversation. Reply with PONG only. Do not use tools.")
        turn = await wait_terminal(client, tid, approvals)
        assert turn["status"] == "finished"
        session = turn["native_session_ref"]
        messages = await agent_messages(client, cid)
        assert len(messages) == 1 and messages[0]["content"].strip() == "PONG"
        assert frames[-1]["native"] == "completed" and adapter.is_alive()
        print(f"ACCEPTANCE HTTP_PONG session={session} turn={tid} status=finished native=completed agent_messages=1")

        response = await client.post(f"/api/v1/conversations/{cid}/messages", json={"content": "Repeat only the marker I asked you to remember in the previous message. Do not use tools."})
        assert response.status_code == 200 and response.json()["turn"]["turn_id"] == tid
        turn = await wait_terminal(client, tid, approvals)
        assert turn["status"] == "finished" and turn["native_session_ref"] == session
        messages = await agent_messages(client, cid)
        assert len(messages) == 2 and "KANE_CONTINUITY_4827" in messages[-1]["content"]
        assert sum(frame["method"] == "session/new" for frame in frames) == 1
        assert all(frame["session"] == session for frame in frames if frame["method"] == "session/prompt")
        print(f"ACCEPTANCE continuity session={session} turn={tid} status=finished native_session_count=1 agent_messages=2")

        start = len(frames)
        completions = []
        original_complete = app.state.coordinator.emit_message_complete

        async def record_complete(*args, **kwargs):
            completions.append(kwargs.get("turn_id"))
            return await original_complete(*args, **kwargs)

        app.state.coordinator.emit_message_complete = record_complete
        cid, tid = await create_and_send(client, "For the first work cycle reply LOOP_FIRST only, with no completion marker. When asked to continue in the second cycle, reply [COMPLETE]. Do not use tools.", loop_mode=True, max_iterations=2)
        turn = await wait_terminal(client, tid, approvals)
        prompts = [frame for frame in frames[start:] if frame["method"] == "session/prompt"]
        assert turn["status"] == "finished"
        assert len(prompts) == 2 and all(frame["native"] == "completed" for frame in prompts)
        assert all(frame["session"] == turn["native_session_ref"] for frame in prompts)
        assert completions == [tid]
        assert len(await agent_messages(client, cid)) == 1 and turn["partial_output"] is None
        assert adapter.is_alive()
        print(f"ACCEPTANCE whole_loop session={turn['native_session_ref']} turn={tid} status=finished iterations=2 completion_count=1 agent_messages=1")


async def test_real_http_tool_success_and_natural_approval(tmp_path):
    async with real_http_runtime(tmp_path) as (_, client, adapter, frames):
        marker = tmp_path / "tool-success.txt"
        command = f"Set-Content -LiteralPath '{marker}' -Value 'TOOL_OK'; Get-Content -LiteralPath '{marker}'"
        cid, tid = await create_and_send(client, f"Use pwsh exactly once with run_in_background=false and command: {command}. Wait for its actual result, then reply TOOL_OK only. No other tools.")
        approvals = []
        turn = await wait_terminal(client, tid, approvals)
        assert turn["status"] == "finished"
        assert marker.read_text(encoding="utf-8-sig").strip() == "TOOL_OK"
        assert any(event["event_type"] == "tool_start" for event in turn["events"])
        assert any(event["event_type"] == "tool_end" for event in turn["events"])
        assert len(await agent_messages(client, cid)) == 1
        assert frames[-1]["native"] == "completed" and adapter.is_alive()
        print(f"ACCEPTANCE tool_success session={turn['native_session_ref']} turn={tid} status=finished native=completed tool_start_end=true agent_messages=1 approval={'PASS' if approvals else 'NOT_TRIGGERED'} approval_count={len(approvals)}")
        if approvals:
            assert any(item["status"] == "waiting_user" for item in approvals)


async def test_real_native_completion_arrives_after_http_cancel(tmp_path):
    async with real_http_runtime(tmp_path) as (_, client, adapter, frames):
        completion_received = asyncio.Event()
        deliver_completion = asyncio.Event()
        handled = asyncio.Event()
        original_request = adapter._send_request
        original_execute = adapter._execute_prompt

        async def delayed_delivery(method, params=None):
            response = await original_request(method, params)
            if method == "session/prompt":
                assert response["result"]["_meta"]["kaneNativeEndKind"] == "completed"
                completion_received.set()
                await deliver_completion.wait()
            return response

        async def record_handled(*args, **kwargs):
            try:
                await original_execute(*args, **kwargs)
            finally:
                handled.set()

        adapter._send_request = delayed_delivery
        adapter._execute_prompt = record_handled
        try:
            cid, tid = await create_and_send(client, "Reply with PONG only. Do not use tools.")
            await asyncio.wait_for(completion_received.wait(), 180)
            before = (await client.get(f"/api/v1/turns/{tid}")).json()
            assert before["status"] == "running" and "PONG" in before["partial_output"]
            response = await client.post(f"/api/v1/turns/{tid}/cancel")
            assert response.status_code == 200 and response.json()["status"] == "interrupted"
            deliver_completion.set()
            await asyncio.wait_for(handled.wait(), 10)
            after = (await client.get(f"/api/v1/turns/{tid}")).json()
            assert after["status"] == "interrupted" and after["partial_output"] == before["partial_output"]
            assert not await agent_messages(client, cid)
            assert frames[-1]["native"] == "completed"
            print(f"ACCEPTANCE cancel_late_completion session={after['native_session_ref']} turn={tid} status=interrupted native=completed late_delivery_ignored=true partial_chars={len(after['partial_output'])} agent_messages=0")
        finally:
            deliver_completion.set()
