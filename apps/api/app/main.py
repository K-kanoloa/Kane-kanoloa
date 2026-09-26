from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .security.auth import ApiAuthMiddleware
from .routes.health import router as health_router
from .settings_env import get_cors_allow_origins
from .startup_timing import mark
from .version import PLATFORM_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    mark("lifespan.begin")
    mark("lifespan.ready")
    yield


def create_app() -> FastAPI:
    mark("create_app.begin")
    app = FastAPI(
        title="Kane Agent Platform API",
        version=PLATFORM_VERSION,
        description="Kane Agent Platform control-plane API (vNext Phase 1 minimal skeleton).",
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
    mark("routers.registered")

    return app


mark("modules.imported")

app = create_app()
