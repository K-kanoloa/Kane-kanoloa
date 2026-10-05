from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from connectors.conformance import ConformanceError, check_trace


REPO_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = REPO_ROOT / "docs" / "KANE_CONNECTOR_PROTOCOL.md"
SCHEMA_MARKER = re.compile(r"```json capabilities-schema\s*(.*?)\s*```", re.DOTALL)


def _base_url() -> str:
    return (os.getenv("KANE_API_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")


def _agent_id() -> str:
    return (os.getenv("KANE_AGENT_ID") or "").strip()


def _auth_headers() -> dict[str, str]:
    token = os.getenv("KANE_API_TOKEN") or os.getenv("OCTOPUS_API_TOKEN") or os.getenv("OCTOPUS_API_KEY")
    return {"X-Api-Key": token} if token else {}


def _get_json(path: str) -> dict[str, Any] | list[Any]:
    request = Request(f"{_base_url()}{path}", headers=_auth_headers())
    try:
        with urlopen(request, timeout=5) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Kane API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise RuntimeError("Kane API is unreachable or returned invalid JSON") from None
    if not isinstance(value, (dict, list)):
        raise RuntimeError("Kane API returned an unexpected response")
    return value


def get_connector_spec() -> dict[str, str]:
    return {"path": "docs/KANE_CONNECTOR_PROTOCOL.md", "spec": PROTOCOL_PATH.read_text(encoding="utf-8")}


def get_connection_guide() -> dict[str, Any]:
    paths = (
        "skills/kane-connect/SKILL.md",
        "skills/kane-connect/references/agent-interface-notes.md",
    )
    return {
        "protocol_source": "docs/KANE_CONNECTOR_PROTOCOL.md",
        "guides": [{"path": path, "content": (REPO_ROOT / path).read_text(encoding="utf-8")} for path in paths],
    }


def get_connection_info() -> dict[str, Any]:
    base = _base_url()
    websocket = os.getenv("KANE_CONNECTOR_WS_URL")
    if not websocket:
        websocket = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        websocket += "/api/v1/connectors/ws"
    return {
        "api_base_url": base,
        "connector_websocket_url": websocket,
        "agent_id": _agent_id() or None,
        "api_token_configured": bool(_auth_headers()),
        "credential_policy": "MCP never returns API or reconnect tokens; transfer a one-time pairing code directly to the Connector.",
    }


def get_agent_identity() -> dict[str, Any]:
    agent_id = _agent_id()
    if not agent_id:
        return {"configured": False, "required_environment": "KANE_AGENT_ID"}
    agents = _get_json("/api/v1/agents")
    if not isinstance(agents, list):
        raise RuntimeError("Kane agent registry response is invalid")
    match = next((item for item in agents if isinstance(item, dict) and item.get("agent_id") == agent_id), None)
    return {"configured": True, "found": match is not None, "agent": match}


def get_capabilities_schema() -> dict[str, Any]:
    source = PROTOCOL_PATH.read_text(encoding="utf-8")
    match = SCHEMA_MARKER.search(source)
    if not match:
        raise RuntimeError("Canonical capability schema is missing from the protocol document")
    return {"source": "docs/KANE_CONNECTOR_PROTOCOL.md", "schema": json.loads(match.group(1))}


def test_connection() -> dict[str, Any]:
    health = _get_json("/health")
    if not isinstance(health, dict) or health.get("status") != "ok":
        return {"status": "failed", "api_reachable": False, "agent": None}
    agent_result = get_agent_identity() if _agent_id() else None
    agent = (agent_result or {}).get("agent") if agent_result else None
    return {
        "status": "pass",
        "api_reachable": True,
        "api_version": health.get("version"),
        "agent": agent,
        "note": "API reachability is not proof that an Agent Connector WebSocket is currently connected.",
    }


def run_conformance_check(trace_json: str) -> dict[str, Any]:
    try:
        trace = json.loads(trace_json)
        return check_trace(trace)
    except (json.JSONDecodeError, ConformanceError) as exc:
        return {"status": "fail", "reason": str(exc)}
