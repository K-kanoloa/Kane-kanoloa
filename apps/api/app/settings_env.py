"""Environment configuration (no secrets committed; read from process env)."""

from __future__ import annotations

import os


def get_local_bridge_url() -> str:
    return os.getenv("OCTOPUS_LOCAL_BRIDGE_URL", "http://127.0.0.1:8010").rstrip("/")


def get_bridge_shared_secret() -> str | None:
    return os.getenv("OCTOPUS_BRIDGE_SHARED_SECRET")


def get_api_token() -> str | None:
    """When set, mutating API routes require X-Api-Key or Authorization: Bearer."""
    raw = os.getenv("OCTOPUS_API_TOKEN") or os.getenv("OCTOPUS_API_KEY")
    return raw.strip() if raw and raw.strip() else None


def get_cors_allow_origins() -> list[str]:
    raw = (os.getenv("OCTOPUS_CORS_ORIGINS") or "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
