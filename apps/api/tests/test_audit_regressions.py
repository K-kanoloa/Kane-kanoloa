"""Regressions for confirmed Daybreak audit paths; no new public contracts."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.adapters.connector_adapter import ConnectorAdapter
from app.adapters.base import BaseAdapter
from app.adapters.mock_adapter import MockAdapter
from app.domain.models import AgentCapabilities, BranchBoundary, Conversation, Message, Turn
from app.harness.coordinator import HarnessCoordinator
from app.harness.dispatcher import Dispatcher
from app.harness.mailbox import MailboxManager
from app.main import create_app
from app.routes.turns import stream_turn_events
from app.store.sqlite_store import SQLiteStore


@pytest.fixture
def harness():
    store = SQLiteStore(":memory:")
    mailbox = MailboxManager()
    coordinator = HarnessCoordinator(store, mailbox)
    dispatcher = Dispatcher(store, coordinator, mailbox)
    coordinator.register_adapter("kanaloa", MockAdapter())
    conversation = Conversation()
    store.save_conversation(conversation)
    turn = dispatcher.create_new_turn(conversation.conversation_id)
    yield store, coordinator, dispatcher, conversation, turn
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["interrupted", "failed", "finished"])
async def test_late_completion_preserves_terminal_fact_and_partial(harness, status):
    store, coordinator, _, conversation, turn = harness
    turn.status = status
    turn.partial_output = "partial"
    store.save_turn(turn)
    with pytest.raises(ValueError, match="inactive Turn"):
        await coordinator.emit_message_complete(turn.turn_id)
    assert store.get_turn(turn.turn_id) == turn
    assert store.get_messages(conversation.conversation_id) == []


@pytest.mark.asyncio
async def test_completion_id_cannot_borrow_another_turn_message(harness):
    store, coordinator, _, conversation, turn = harness
    foreign = Message(conversation_id="other", turn_id="other", sender="agent", content="wrong")
    store.append_message(foreign)
    with pytest.raises(ValueError, match="another Turn"):
        await coordinator.emit_message_complete(turn.turn_id, message_id=foreign.message_id)
    assert store.get_messages(conversation.conversation_id) == []


@pytest.mark.asyncio
async def test_connector_retries_event_after_failed_persistence(harness, monkeypatch):
    store, coordinator, _, _, turn = harness
    adapter = ConnectorAdapter("kanaloa", AgentCapabilities(), coordinator)
    frame = {"type": "reply.delta", "event_id": "retry", "payload": {
        "turn_id": turn.turn_id, "reply_id": "reply", "text": "once",
    }}
    with monkeypatch.context() as patch:
        patch.setattr(store, "save_turn", lambda _: (_ for _ in ()).throw(OSError("write failed")))
        with pytest.raises(OSError):
            await adapter.handle_frame(frame)
    assert "retry" not in adapter._seen_events
    await adapter.handle_frame(frame)
    await adapter.handle_frame(frame)
    assert store.get_turn(turn.turn_id).partial_output == "once"


@pytest.mark.parametrize("foreign", [False, True])
def test_new_turn_rejects_unknown_or_foreign_branch_without_saving(harness, foreign):
    store, _, dispatcher, conversation, turn = harness
    branch = BranchBoundary(conversation_id="other")
    if foreign:
        store.save_branch(branch)
    before = store.list_turns(conversation.conversation_id)
    with pytest.raises(ValueError, match="Branch must belong"):
        dispatcher.create_new_turn(conversation.conversation_id, branch_id=branch.branch_id)
    assert store.list_turns(conversation.conversation_id) == before
    turn.branch_id = branch.branch_id
    store.save_turn(turn)
    with pytest.raises(ValueError, match="Branch must belong"):
        dispatcher.get_turn_history(turn)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["focus", "explicit", "new"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_reply_target_rejects_unknown_or_foreign_before_dispatch(harness, target, foreign):
    store, _, dispatcher, conversation, turn = harness
    message = Message(conversation_id="other", sender="user", content="private")
    if foreign:
        store.append_message(message)
    with pytest.raises(ValueError, match="Reply target must belong"):
        await dispatcher.dispatch_user_message(
            conversation.conversation_id, "reply",
            target_turn_id=turn.turn_id if target == "explicit" else None,
            reply_to_message_id=message.message_id, is_new_task=target == "new",
        )
    assert store.get_messages(conversation.conversation_id) == []
    assert len(store.list_turns(conversation.conversation_id)) == 1


@pytest.mark.asyncio
async def test_sse_reads_snapshot_after_subscription_not_route_creation(harness):
    store, coordinator, _, _, turn = harness
    request = AsyncMock()
    response = await stream_turn_events(turn.turn_id, request, store, coordinator)
    await coordinator.emit_message_complete(turn.turn_id, content="done")
    chunks = [chunk async for chunk in response.body_iterator]
    snapshot = json.loads(chunks[0].split("data: ")[1])
    assert snapshot["status"] == "finished"
    assert len(chunks) == 1
    assert coordinator._listeners == {}


@pytest.mark.parametrize("declared", [False, True])
def test_permission_route_respects_declared_capability(harness, declared):
    store, coordinator, dispatcher, _, turn = harness
    if declared:
        adapter = coordinator.get_adapter("kanaloa")
        adapter.set_capabilities(AgentCapabilities(supports_approval=True))
        adapter.respond_permission = BaseAdapter.respond_permission.__get__(adapter)
    app = create_app()
    app.state.store, app.state.coordinator, app.state.dispatcher = store, coordinator, dispatcher
    client = TestClient(app)
    response = client.post(f"/api/v1/turns/{turn.turn_id}/permissions/unknown/respond", json={"decision": "allow-once"})
    assert response.status_code == 400
    assert store.get_turn(turn.turn_id) == turn


@pytest.mark.asyncio
async def test_message_activity_updates_recency_and_equal_dates_keep_insert_order(harness):
    store, coordinator, _, conversation, turn = harness
    newer = Conversation(updated_at="2020-01-02T00:00:00Z")
    conversation.updated_at = "2020-01-01T00:00:00Z"
    store.save_conversation(conversation)
    store.save_conversation(newer)
    assert store.list_conversations()[0].conversation_id == newer.conversation_id
    messages = [Message(conversation_id=conversation.conversation_id, turn_id=turn.turn_id,
                        sender="user", content=str(index), created_at="2020-01-03T00:00:00Z") for index in range(3)]
    for message in messages:
        store.append_message(message)
    assert store.get_messages(conversation.conversation_id) == messages
    assert store.list_conversations()[0].conversation_id == conversation.conversation_id
    await coordinator.emit_message_complete(turn.turn_id, content="done")
    stored = store.get_messages(conversation.conversation_id)
    assert store.get_conversation(conversation.conversation_id).updated_at == stored[-1].created_at


def test_failed_settings_commit_does_not_overwrite_active_credential(tmp_path):
    script = Path(__file__).resolve().parents[3] / "scripts" / "configure-kanaloa-model.mjs"
    env = {**os.environ, "DSH_HOME": str(tmp_path)}
    env.pop("KANE_KANALOA_API_KEY", None)
    config = {"base_url": "https://example.test/v1", "model": "test-model",
              "api_format": "openai-completions", "api_key": "original-test-key"}
    def save(body):
        return subprocess.run(["node", str(script)], input=json.dumps(body), text=True,
                              capture_output=True, env=env, timeout=30)
    assert save(config).returncode == 0
    credentials = tmp_path / ".credentials.yaml"
    before = credentials.read_text(encoding="utf-8")
    (tmp_path / "settings.yaml").write_text("[]\n", encoding="utf-8")
    assert save({**config, "api_key": "replacement-test-key"}).returncode != 0
    assert "original-test-key" in credentials.read_text(encoding="utf-8")
    assert "original-test-key" in before
    assert credentials.read_text(encoding="utf-8") == before
