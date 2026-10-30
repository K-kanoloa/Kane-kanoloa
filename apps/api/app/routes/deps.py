"""Dependencies for FastAPI routes (thin dependency getters from app.state)."""

from __future__ import annotations

from fastapi import Request

from ..harness.coordinator import HarnessCoordinator
from ..harness.dispatcher import Dispatcher
from ..harness.mailbox import MailboxManager
from ..store.base import BaseStore
from ..store.deps import get_store as get_store_instance


def get_store(request: Request) -> BaseStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        store = get_store_instance()
        request.app.state.store = store
    return store


def get_coordinator(request: Request) -> HarnessCoordinator:
    coord = getattr(request.app.state, "coordinator", None)
    if coord is None:
        store = get_store(request)
        mbx_mgr = getattr(request.app.state, "mailbox_manager", None) or MailboxManager()
        request.app.state.mailbox_manager = mbx_mgr
        coord = HarnessCoordinator(store, mbx_mgr)
        request.app.state.coordinator = coord
    return coord


def get_dispatcher(request: Request) -> Dispatcher:
    disp = getattr(request.app.state, "dispatcher", None)
    if disp is None:
        store = get_store(request)
        coord = get_coordinator(request)
        mbx_mgr = getattr(request.app.state, "mailbox_manager", None) or MailboxManager()
        disp = Dispatcher(store, coord, mbx_mgr)
        request.app.state.dispatcher = disp
    return disp
