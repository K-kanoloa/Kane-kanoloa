"""Unit tests for Kane vNext domain models and SQLiteStore."""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.domain.models import (
    AgentBinding,
    AgentCapabilities,
    Conversation,
    Message,
    Turn,
    TurnEvent,
)
from app.store.sqlite_store import SQLiteStore


def test_conversation_lifecycle():
    store = SQLiteStore(":memory:")
    conv = Conversation(
        conversation_id="conv_1",
        title="Architecture Discussion",
        bound_agent_id="kanaloa",
    )
    store.save_conversation(conv)

    loaded = store.get_conversation("conv_1")
    assert loaded is not None
    assert loaded.title == "Architecture Discussion"
    assert loaded.bound_agent_id == "kanaloa"
    assert loaded.focus_turn_id is None

    # Update focus_turn
    conv.focus_turn_id = "turn_123"
    conv.updated_at = "2026-09-26T12:00:00Z"
    store.save_conversation(conv)

    updated = store.get_conversation("conv_1")
    assert updated is not None
    assert updated.focus_turn_id == "turn_123"


def test_message_append_only_and_edit_semantics():
    store = SQLiteStore(":memory:")
    conv_id = "conv_msg_test"

    # M1: original user message
    m1 = Message(
        message_id="msg_1",
        conversation_id=conv_id,
        sender="user",
        content="今天去悉尼",
    )
    store.append_message(m1)

    # M2: Edit operation - does NOT overwrite M1, appends as event log entry
    m2 = Message(
        message_id="msg_2",
        conversation_id=conv_id,
        sender="user",
        parent_id="msg_1",
        kind="edit",
        target_message_id="msg_1",
        content="明天去悉尼",
    )
    store.append_message(m2)

    messages = store.get_messages(conv_id)
    assert len(messages) == 2
    # Original message record is preserved untouched
    assert messages[0].message_id == "msg_1"
    assert messages[0].content == "今天去悉尼"
    assert messages[0].kind == "normal"
    # Edit message is appended
    assert messages[1].message_id == "msg_2"
    assert messages[1].content == "明天去悉尼"
    assert messages[1].kind == "edit"
    assert messages[1].target_message_id == "msg_1"


def test_branch_history_boundary():
    """
    Lineage: M1 -> M2 -> M3 -> M4 -> M5
    Branching at M3 should see only [M1, M2, M3] and exclude [M4, M5].
    """
    store = SQLiteStore(":memory:")
    conv_id = "conv_branch_test"

    m1 = Message(message_id="m1", conversation_id=conv_id, sender="user", parent_id=None, content="M1")
    m2 = Message(message_id="m2", conversation_id=conv_id, sender="agent", parent_id="m1", content="M2")
    m3 = Message(message_id="m3", conversation_id=conv_id, sender="user", parent_id="m2", content="M3")
    m4 = Message(message_id="m4", conversation_id=conv_id, sender="agent", parent_id="m3", content="M4")
    m5 = Message(message_id="m5", conversation_id=conv_id, sender="user", parent_id="m4", content="M5")

    for m in [m1, m2, m3, m4, m5]:
        store.append_message(m)

    # Full main history
    full_history = store.get_messages(conv_id)
    assert [m.message_id for m in full_history] == ["m1", "m2", "m3", "m4", "m5"]

    # Branch visible history cut at M3
    branch_history = store.get_messages(conv_id, up_to_message_id="m3")
    assert [m.message_id for m in branch_history] == ["m1", "m2", "m3"]


def test_turn_lifecycle_and_facts():
    store = SQLiteStore(":memory:")
    turn = Turn(
        turn_id="turn_test_1",
        conversation_id="conv_1",
        bound_agent_id="kanaloa",
        title="Research Kane Architecture",
        status="running",
        native_session_ref="dsh_sess_999",
    )
    store.save_turn(turn)

    t = store.get_turn("turn_test_1")
    assert t is not None
    assert t.status == "running"
    assert t.native_session_ref == "dsh_sess_999"

    # Agent requires user confirmation
    t.status = "waiting_user"
    store.save_turn(t)
    assert store.get_turn("turn_test_1").status == "waiting_user"

    # Stream buffer arrives and persists across restart in turn runtime buffer
    t.partial_output = "Analysis part 1: Muse harness model..."
    store.save_turn(t)
    assert store.get_turn("turn_test_1").partial_output == "Analysis part 1: Muse harness model..."

    # Session lost / process exited
    t.status = "interrupted"
    t.interrupt_reason = "process_exited"
    store.save_turn(t)

    interrupted = store.get_turn("turn_test_1")
    assert interrupted.status == "interrupted"
    assert interrupted.interrupt_reason == "process_exited"
    assert interrupted.partial_output == "Analysis part 1: Muse harness model..."

    # Explicit failure
    t.status = "failed"
    store.save_turn(t)
    assert store.get_turn("turn_test_1").status == "failed"

    # User follow-up can resume the same turn back to running (§8)
    t.status = "running"
    store.save_turn(t)
    assert store.get_turn("turn_test_1").status == "running"


def test_turn_events_isolation():
    store = SQLiteStore(":memory:")
    turn_id = "turn_evt_test"
    conv_id = "conv_evt_test"

    event1 = TurnEvent(
        turn_id=turn_id,
        conversation_id=conv_id,
        event_type="thinking",
        payload={"text": "Analyzing the blueprint..."},
    )
    event2 = TurnEvent(
        turn_id=turn_id,
        conversation_id=conv_id,
        event_type="tool_start",
        payload={"tool": "file_reader", "args": {"file": "SKILL.md"}},
    )
    store.append_event(event1)
    store.append_event(event2)

    events = store.list_events(turn_id)
    assert len(events) == 2
    assert events[0].event_type == "thinking"
    assert events[1].payload["tool"] == "file_reader"

    # Events must NOT appear in chat message list
    messages = store.get_messages(conv_id)
    assert len(messages) == 0


def test_agent_binding_and_capabilities():
    store = SQLiteStore(":memory:")
    kanaloa = AgentBinding(
        agent_id="kanaloa",
        display_name="Kanaloa",
        adapter_name="kanaloa_dsh",
        capabilities=AgentCapabilities(
            supports_stream=True,
            supports_resume=True,
            supports_cancel=True,
            steer_mode="safe_boundary",
            branch_mode="replay",
        ),
        config={"mode": "normal", "runtime": "dsh"},
    )
    store.save_agent_binding(kanaloa)

    loaded = store.get_agent_binding("kanaloa")
    assert loaded is not None
    assert loaded.display_name == "Kanaloa"
    assert loaded.capabilities.steer_mode == "safe_boundary"
    assert loaded.capabilities.branch_mode == "replay"
    assert loaded.config["runtime"] == "dsh"


def test_sqlite_persistence_across_reconnect():
    with tempfile.TemporaryDirectory(prefix="kane_sqlite_test_") as tmpdir:
        db_file = Path(tmpdir) / "kane_test.db"

        # Connection 1: write entities
        store1 = SQLiteStore(db_file)
        try:
            conv = Conversation(conversation_id="c_persisted", title="Persisted Conv")
            turn = Turn(turn_id="t_persisted", conversation_id="c_persisted", bound_agent_id="kanaloa")
            msg = Message(message_id="m_persisted", conversation_id="c_persisted", sender="user", content="Hello!")

            store1.save_conversation(conv)
            store1.save_turn(turn)
            store1.append_message(msg)
        finally:
            store1.close()

        # Connection 2: open new store instance on same file
        store2 = SQLiteStore(db_file)
        try:
            assert store2.get_conversation("c_persisted") is not None
            assert store2.get_turn("t_persisted") is not None
            messages = store2.get_messages("c_persisted")
            assert len(messages) == 1
            assert messages[0].content == "Hello!"
        finally:
            store2.close()


def test_sqlite_schema_upgrade_and_legacy_status_compatibility():
    """Verify an older SQLite database without partial_output and with legacy status strings upgrades seamlessly."""
    import sqlite3

    with tempfile.TemporaryDirectory(prefix="kane_sqlite_upgrade_test_") as tmpdir:
        db_file = Path(tmpdir) / "legacy_kane.db"

        # Simulate older database created before partial_output column was introduced
        conn = sqlite3.connect(str(db_file))
        with conn:
            conn.execute(
                """
                CREATE TABLE turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    bound_agent_id TEXT NOT NULL,
                    title TEXT,
                    status TEXT NOT NULL,
                    native_session_ref TEXT,
                    last_event_at TEXT NOT NULL,
                    interrupt_reason TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                """
            )
            # Insert legacy turn with legacy status 'completed' and 'error'
            conn.execute(
                """
                INSERT INTO turns VALUES (
                    'turn_legacy_1', 'conv_leg', 'kanaloa', 'Old Task', 'completed',
                    'sess_old', '2026-01-01T00:00:00Z', NULL, '2026-01-01T00:00:00Z', NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO turns VALUES (
                    'turn_legacy_2', 'conv_leg', 'kanaloa', 'Failed Task', 'error',
                    'sess_old2', '2026-01-01T00:00:00Z', 'unknown', '2026-01-01T00:00:00Z', NULL
                );
                """
            )
        conn.close()

        # Open with new SQLiteStore
        store = SQLiteStore(db_file)
        try:
            # 1. Verify schema upgrade occurred: partial_output column exists
            raw_conn = store._get_connection()
            cols = {row["name"] for row in raw_conn.execute("PRAGMA table_info(turns);").fetchall()}
            assert "partial_output" in cols

            # 2. Verify legacy status 'completed' normalized to 'finished'
            turn1 = store.get_turn("turn_legacy_1")
            assert turn1 is not None
            assert turn1.status == "finished"
            assert turn1.partial_output is None

            # 3. Verify legacy status 'error' normalized to 'failed'
            turn2 = store.get_turn("turn_legacy_2")
            assert turn2 is not None
            assert turn2.status == "failed"

            # 4. Verify we can now save partial_output to existing upgraded turn
            turn1.partial_output = "Fresh partial output after migration"
            store.save_turn(turn1)

            reloaded = store.get_turn("turn_legacy_1")
            assert reloaded.partial_output == "Fresh partial output after migration"
        finally:
            store.close()
