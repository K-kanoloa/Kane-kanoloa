"""Store access helpers and factory."""

from __future__ import annotations

import os
from pathlib import Path

from .base import BaseStore
from .sqlite_store import SQLiteStore

_STORE_INSTANCE: BaseStore | None = None


def get_default_db_path() -> Path:
    raw = os.getenv("KANE_SQLITE_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "kane.db"


def get_store() -> BaseStore:
    """Return the global default Store instance (v0.1 SQLiteStore)."""
    global _STORE_INSTANCE
    if _STORE_INSTANCE is None:
        _STORE_INSTANCE = SQLiteStore(get_default_db_path())
    return _STORE_INSTANCE


def reset_store_instance(instance: BaseStore | None = None) -> None:
    """Helper to reset or inject a mock store for tests."""
    global _STORE_INSTANCE
    _STORE_INSTANCE = instance
