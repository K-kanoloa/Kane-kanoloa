from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server import MCPServer

from service import (
    get_agent_identity as service_get_agent_identity,
    get_capabilities_schema as service_get_capabilities_schema,
    get_connection_info as service_get_connection_info,
    get_connection_guide as service_get_connection_guide,
    get_connector_spec as service_get_connector_spec,
    run_conformance_check as service_run_conformance_check,
    test_connection as service_test_connection,
)


mcp = MCPServer("Kane Connector Bootstrap")


@mcp.tool()
def get_connector_spec() -> dict[str, str]:
    """Return the single canonical Kane Connector Protocol document."""
    return service_get_connector_spec()


@mcp.tool()
def get_connection_guide() -> dict:
    """Read the generic Connect Skill and reported interface notes; no credentials."""
    return service_get_connection_guide()


@mcp.tool()
def get_connection_info() -> dict:
    """Return configured Kane URLs and Agent ID without exposing credentials."""
    return service_get_connection_info()


@mcp.tool()
def get_agent_identity() -> dict:
    """Look up the configured Agent identity and truthful registry status."""
    return service_get_agent_identity()


@mcp.tool()
def get_capabilities_schema() -> dict:
    """Return the capability schema embedded in the canonical protocol spec."""
    return service_get_capabilities_schema()


@mcp.tool()
def test_connection() -> dict:
    """Probe Kane health and, when configured, the registered Agent identity."""
    return service_test_connection()


@mcp.tool()
def run_conformance_check(trace_json: str) -> dict:
    """Validate a redacted wire trace; this does not execute the Agent runtime."""
    return service_run_conformance_check(trace_json)


if __name__ == "__main__":
    mcp.run(transport="stdio")
