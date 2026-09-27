"""FastAPI Application Entrypoint with Lifespan Startup Reconciliation."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .security.auth import ApiAuthMiddleware
from .settings_env import get_cors_allow_origins
from .startup_timing import mark
from .version import PLATFORM_VERSION

from .store.deps import get_store
from .harness.mailbox import MailboxManager
from .harness.coordinator import HarnessCoordinator
from .harness.dispatcher import Dispatcher
from .adapters.kanaloa_adapter import KanaloaAdapter
from .adapters.kanaloa_runtime import KanaloaRuntime

from .routes.health import router as health_router
from .routes.conversations import router as conversations_router
from .routes.turns import router as turns_router
from .routes.agents import router as agents_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    mark("lifespan.begin")

    # 1. Initialize existing Store
    store = getattr(app.state, "store", None)
    if store is None:
        store = get_store()
        app.state.store = store

    # 2. Mailbox Manager & HarnessCoordinator
    mailbox_manager = getattr(app.state, "mailbox_manager", None)
    if mailbox_manager is None:
        mailbox_manager = MailboxManager()
        app.state.mailbox_manager = mailbox_manager

    coordinator = getattr(app.state, "coordinator", None)
    if coordinator is None:
        coordinator = HarnessCoordinator(store, mailbox_manager)
        app.state.coordinator = coordinator

    # 3. Dispatcher
    dispatcher = getattr(app.state, "dispatcher", None)
    if dispatcher is None:
        dispatcher = Dispatcher(store, coordinator, mailbox_manager)
        app.state.dispatcher = dispatcher

    # 4. Runtime & KanaloaAdapter (if not already registered)
    if not coordinator.has_adapter("kanaloa"):
        runtime = getattr(app.state, "kanaloa_runtime", None) or KanaloaRuntime()
        adapter = getattr(app.state, "kanaloa_adapter", None) or KanaloaAdapter(
            event_handler=coordinator,
            runtime=runtime,
        )
        coordinator.register_adapter("kanaloa", adapter)
        app.state.kanaloa_runtime = runtime
        app.state.kanaloa_adapter = adapter

    # 5. Startup Reconciliation Pass: MUST complete before serving requests (§1, §30)
    mark("lifespan.reconciliation.begin")
    reconciled = await coordinator.reconcile_startup_turns()
    mark("lifespan.reconciliation.done")
    logger.info("Startup reconciliation complete: %d turns processed", len(reconciled))

    mark("lifespan.ready")
    yield

    # Shutdown
    mark("lifespan.shutdown.begin")
    for agent_id, adapter in list(coordinator._adapters.items()):
        if hasattr(adapter, "close"):
            try:
                await adapter.close()
            except Exception as e:
                logger.warning("Error closing adapter '%s': %s", agent_id, e)
    if hasattr(store, "close"):
        store.close()
    mark("lifespan.shutdown.done")


def create_app() -> FastAPI:
    mark("create_app.begin")
    app = FastAPI(
        title="Kane Agent Platform API",
        version=PLATFORM_VERSION,
        description="Kane Agent Platform control-plane API (vNext).",
        lifespan=lifespan,
    )
    mark("fastapi.init")

    app.add_middleware(ApiAuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_cors_allow_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(conversations_router, prefix="/api/v1")
    app.include_router(turns_router, prefix="/api/v1")
    app.include_router(agents_router, prefix="/api/v1")
    mark("routers.registered")

    return app


mark("modules.imported")

app = create_app()
