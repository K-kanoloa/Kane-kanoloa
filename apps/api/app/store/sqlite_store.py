"""SQLiteStore: v0.1 default implementation of BaseStore for Kane vNext."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from .base import BaseStore
from ..domain.models import (
    AgentBinding,
    AgentCapabilities,
    BranchBoundary,
    Conversation,
    Message,
    Turn,
    TurnEvent,
)


class SQLiteStore(BaseStore):
    """
    SQLite-backed store implementing the BaseStore interface.
    Thread-safe connection handling with WAL mode enabled.
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        self._local = threading.local()
        self._memory_conn: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._memory_conn = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
                timeout=30.0,
            )
            self._memory_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            self._local.conn = conn
        return self._local.conn

    def close(self) -> None:
        """Close database connection."""
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    def _init_db(self) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    bound_agent_id TEXT NOT NULL,
                    focus_turn_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS branches (
                    branch_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    branch_point_message_id TEXT,
                    name TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_branches_conversation_id
                ON branches (conversation_id, created_at);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT,
                    sender TEXT NOT NULL,
                    sender_id TEXT,
                    reply_to TEXT,
                    parent_id TEXT,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target_message_id TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
                ON messages (conversation_id, created_at);
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_turn_id
                ON messages (turn_id);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbound_delivery (
                    message_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('message', 'steer')),
                    state TEXT NOT NULL CHECK (state IN ('pending', 'dispatching'))
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    bound_agent_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL DEFAULT 'main',
                    title TEXT,
                    status TEXT NOT NULL,
                    native_session_ref TEXT,
                    last_event_at TEXT NOT NULL,
                    interrupt_reason TEXT,
                    partial_output TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_turns_conversation_id
                ON turns (conversation_id, created_at);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turn_events (
                    event_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_turn_events_turn_id
                ON turn_events (turn_id, created_at);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_bindings (
                    agent_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    config TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                """
            )

            # Lightweight schema migration for existing databases
            cursor = conn.execute("PRAGMA table_info(turns);")
            existing_columns = {row["name"] for row in cursor.fetchall()}
            if "partial_output" not in existing_columns:
                conn.execute("ALTER TABLE turns ADD COLUMN partial_output TEXT;")
            if "branch_id" not in existing_columns:
                conn.execute("ALTER TABLE turns ADD COLUMN branch_id TEXT NOT NULL DEFAULT 'main';")

            cursor_msg = conn.execute("PRAGMA table_info(messages);")
            existing_msg_cols = {row["name"] for row in cursor_msg.fetchall()}
            if "turn_id" not in existing_msg_cols:
                conn.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT;")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_turn_id ON messages (turn_id);")

    # --- Branch Boundary Operations (§16, §17) ---
    def save_branch(self, branch: BranchBoundary) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO branches (
                    branch_id, conversation_id, branch_point_message_id, name, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(branch_id) DO UPDATE SET
                    branch_point_message_id = excluded.branch_point_message_id,
                    name = excluded.name;
                """,
                (
                    branch.branch_id,
                    branch.conversation_id,
                    branch.branch_point_message_id,
                    branch.name,
                    branch.created_at,
                ),
            )

    def get_branch(self, branch_id: str) -> BranchBoundary | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM branches WHERE branch_id = ?;",
            (branch_id,),
        ).fetchone()
        if not row:
            return None
        return BranchBoundary(**dict(row))

    def list_branches(self, conversation_id: str) -> list[BranchBoundary]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM branches WHERE conversation_id = ? ORDER BY created_at ASC;",
            (conversation_id,),
        ).fetchall()
        return [BranchBoundary(**dict(r)) for r in rows]

    def get_or_create_main_branch(self, conversation_id: str) -> BranchBoundary:
        branches = self.list_branches(conversation_id)
        for b in branches:
            if b.branch_point_message_id is None:
                return b
        main_branch = BranchBoundary(
            branch_id=f"main_{conversation_id}",
            conversation_id=conversation_id,
            branch_point_message_id=None,
            name="Main",
        )
        self.save_branch(main_branch)
        return main_branch

    # --- Conversation Operations ---
    def save_conversation(self, conversation: Conversation) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO conversations (
                    conversation_id, title, bound_agent_id, focus_turn_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title = excluded.title,
                    bound_agent_id = excluded.bound_agent_id,
                    focus_turn_id = excluded.focus_turn_id,
                    updated_at = excluded.updated_at;
                """,
                (
                    conversation.conversation_id,
                    conversation.title,
                    conversation.bound_agent_id,
                    conversation.focus_turn_id,
                    conversation.created_at,
                    conversation.updated_at,
                ),
            )

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?;",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        row_dict = dict(row)
        return Conversation(**row_dict)

    def list_conversations(self) -> list[Conversation]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM conversations ORDER BY updated_at DESC;"
        ).fetchall()
        return [Conversation(**dict(r)) for r in rows]

    # --- Message Operations (Append-Only) ---
    def append_message(
        self,
        message: Message,
        delivery_kind: Literal["message", "steer"] | None = None,
    ) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, conversation_id, turn_id, sender, sender_id, reply_to,
                    parent_id, content, kind, target_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    message.message_id,
                    message.conversation_id,
                    message.turn_id,
                    message.sender,
                    message.sender_id,
                    message.reply_to,
                    message.parent_id,
                    message.content,
                    message.kind,
                    message.target_message_id,
                    message.created_at,
                ),
            )
            if delivery_kind is not None:
                if message.sender != "user" or not message.turn_id:
                    raise ValueError("Queued delivery requires a user message bound to a Turn")
                conn.execute(
                    "INSERT INTO inbound_delivery (message_id, turn_id, kind, state) VALUES (?, ?, ?, 'pending')",
                    (message.message_id, message.turn_id, delivery_kind),
                )

    def list_unsettled_inbound(self) -> list[tuple[str, str, str, str]]:
        rows = self._get_connection().execute(
            "SELECT message_id, turn_id, kind, state FROM inbound_delivery ORDER BY rowid"
        ).fetchall()
        return [(r["message_id"], r["turn_id"], r["kind"], r["state"]) for r in rows]

    def claim_inbound(self, message_id: str) -> bool:
        conn = self._get_connection()
        with conn:
            result = conn.execute(
                "UPDATE inbound_delivery SET state = 'dispatching' "
                "WHERE message_id = ? AND state = 'pending'",
                (message_id,),
            )
        return result.rowcount == 1

    def complete_inbound(self, message_id: str) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                "DELETE FROM inbound_delivery WHERE message_id = ? AND state = 'dispatching'",
                (message_id,),
            )

    def get_messages(
        self,
        conversation_id: str,
        up_to_message_id: str | None = None,
    ) -> list[Message]:
        conn = self._get_connection()
        if up_to_message_id:
            # Query target message
            target = conn.execute(
                "SELECT * FROM messages WHERE message_id = ? AND conversation_id = ?;",
                (up_to_message_id, conversation_id),
            ).fetchone()
            if not target:
                return []

            # Pure tree lineage walk backwards via parent_id (§16, §17)
            # Zero reliance on rowid or insertion order
            lineage: list[Message] = []
            curr_id: str | None = up_to_message_id
            visited: set[str] = set()

            while curr_id and curr_id not in visited:
                visited.add(curr_id)
                row = conn.execute(
                    "SELECT * FROM messages WHERE message_id = ?;",
                    (curr_id,),
                ).fetchone()
                if not row:
                    break
                row_dict = dict(row)
                row_dict.pop("rowid", None)
                msg = Message(**row_dict)
                lineage.append(msg)
                curr_id = msg.parent_id

            lineage.reverse()
            return lineage

        # Normal full history for conversation
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC;
            """,
            (conversation_id,),
        ).fetchall()
        return [Message(**dict(r)) for r in rows]

    def get_message(self, message_id: str) -> Message | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM messages WHERE message_id = ?;",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        return Message(**dict(row))

    def get_turn_id_by_message_id(self, message_id: str) -> str | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT turn_id FROM messages WHERE message_id = ?;",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        return row["turn_id"]

    # --- Turn Operations ---
    def save_turn(self, turn: Turn) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO turns (
                    turn_id, conversation_id, bound_agent_id, branch_id, title, status,
                    native_session_ref, last_event_at, interrupt_reason,
                    partial_output, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    branch_id = excluded.branch_id,
                    title = excluded.title,
                    status = excluded.status,
                    native_session_ref = excluded.native_session_ref,
                    last_event_at = excluded.last_event_at,
                    interrupt_reason = excluded.interrupt_reason,
                    partial_output = excluded.partial_output,
                    finished_at = excluded.finished_at;
                """,
                (
                    turn.turn_id,
                    turn.conversation_id,
                    turn.bound_agent_id,
                    turn.branch_id,
                    turn.title,
                    turn.status,
                    turn.native_session_ref,
                    turn.last_event_at,
                    turn.interrupt_reason,
                    turn.partial_output,
                    turn.created_at,
                    turn.finished_at,
                ),
            )

    @staticmethod
    def _normalize_turn_data(data: dict) -> dict:
        data.pop("branch_point_message_id", None)
        if not data.get("branch_id"):
            data["branch_id"] = "main"
        valid_statuses = {"running", "waiting_user", "finished", "failed", "interrupted"}
        status = data.get("status")
        if status not in valid_statuses:
            legacy_map = {
                "completed": "finished",
                "done": "finished",
                "waiting": "waiting_user",
                "error": "failed",
            }
            mapped = legacy_map.get(str(status).lower() if status is not None else "")
            if mapped:
                data["status"] = mapped
            else:
                raise ValueError(f"Invalid or unrecognized turn status: {status}")
        return data

    def get_turn(self, turn_id: str) -> Turn | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM turns WHERE turn_id = ?;",
            (turn_id,),
        ).fetchone()
        if not row:
            return None
        return Turn(**self._normalize_turn_data(dict(row)))

    def list_turns(self, conversation_id: str) -> list[Turn]:
        conn = self._get_connection()
        rows = conn.execute(
            """
            SELECT * FROM turns
            WHERE conversation_id = ?
            ORDER BY created_at ASC;
            """,
            (conversation_id,),
        ).fetchall()
        return [Turn(**self._normalize_turn_data(dict(r))) for r in rows]

    def list_running_turns(self) -> list[Turn]:
        """List all turns currently in 'running' status across all conversations (for startup reconciliation)."""
        conn = self._get_connection()
        rows = conn.execute(
            """
            SELECT * FROM turns
            WHERE status = 'running'
            ORDER BY created_at ASC;
            """
        ).fetchall()
        return [Turn(**self._normalize_turn_data(dict(r))) for r in rows]

    def finalize_turn_completion(
        self,
        turn: Turn,
        message: Message,
        status_event: TurnEvent | None = None,
    ) -> None:
        """
        Atomically persist final Message, clear Turn partial_output, set Turn status to finished,
        and optionally append completion TurnEvent in a single ACID transaction.
        """
        conn = self._get_connection()
        turn_id = message.turn_id or turn.turn_id
        with conn:
            # 1. Insert message
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, conversation_id, turn_id, sender, sender_id, reply_to,
                    parent_id, content, kind, target_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    message.message_id,
                    message.conversation_id,
                    turn_id,
                    message.sender,
                    message.sender_id,
                    message.reply_to,
                    message.parent_id,
                    message.content,
                    message.kind,
                    message.target_message_id,
                    message.created_at,
                ),
            )
            # 2. Update turn atomically: clear partial_output, set finished status and finished_at
            conn.execute(
                """
                INSERT INTO turns (
                    turn_id, conversation_id, bound_agent_id, branch_id, title, status,
                    native_session_ref, last_event_at, interrupt_reason,
                    partial_output, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    status = excluded.status,
                    partial_output = NULL,
                    last_event_at = excluded.last_event_at,
                    finished_at = excluded.finished_at;
                """,
                (
                    turn.turn_id,
                    turn.conversation_id,
                    turn.bound_agent_id,
                    turn.branch_id,
                    turn.title,
                    turn.status,
                    turn.native_session_ref,
                    turn.last_event_at,
                    turn.interrupt_reason,
                    None,
                    turn.created_at,
                    turn.finished_at,
                ),
            )
            # 3. Insert status event if provided
            if status_event:
                conn.execute(
                    """
                    INSERT INTO turn_events (
                        event_id, turn_id, conversation_id, event_type, payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        status_event.event_id,
                        status_event.turn_id,
                        status_event.conversation_id,
                        status_event.event_type,
                        json.dumps(status_event.payload, ensure_ascii=False),
                        status_event.created_at,
                    ),
                )

    # --- Turn Event Operations ---
    def append_event(self, event: TurnEvent) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO turn_events (
                    event_id, turn_id, conversation_id, event_type, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    event.event_id,
                    event.turn_id,
                    event.conversation_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at,
                ),
            )

    def list_events(self, turn_id: str) -> list[TurnEvent]:
        conn = self._get_connection()
        rows = conn.execute(
            """
            SELECT * FROM turn_events
            WHERE turn_id = ?
            ORDER BY created_at ASC;
            """,
            (turn_id,),
        ).fetchall()
        events: list[TurnEvent] = []
        for r in rows:
            data = dict(r)
            data["payload"] = json.loads(data["payload"])
            events.append(TurnEvent(**data))
        return events

    # --- Agent Binding Operations ---
    def save_agent_binding(self, binding: AgentBinding) -> None:
        conn = self._get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO agent_bindings (
                    agent_id, display_name, adapter_name, capabilities,
                    config, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    adapter_name = excluded.adapter_name,
                    capabilities = excluded.capabilities,
                    config = excluded.config,
                    is_active = excluded.is_active;
                """,
                (
                    binding.agent_id,
                    binding.display_name,
                    binding.adapter_name,
                    binding.capabilities.model_dump_json(),
                    json.dumps(binding.config, ensure_ascii=False),
                    1 if binding.is_active else 0,
                    binding.created_at,
                ),
            )

    def get_agent_binding(self, agent_id: str) -> AgentBinding | None:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM agent_bindings WHERE agent_id = ?;",
            (agent_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["capabilities"] = AgentCapabilities.model_validate_json(data["capabilities"])
        data["config"] = json.loads(data["config"])
        data["is_active"] = bool(data["is_active"])
        return AgentBinding(**data)

    def list_agent_bindings(self) -> list[AgentBinding]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM agent_bindings ORDER BY agent_id ASC;"
        ).fetchall()
        bindings: list[AgentBinding] = []
        for r in rows:
            data = dict(r)
            data["capabilities"] = AgentCapabilities.model_validate_json(data["capabilities"])
            data["config"] = json.loads(data["config"])
            data["is_active"] = bool(data["is_active"])
            bindings.append(AgentBinding(**data))
        return bindings
