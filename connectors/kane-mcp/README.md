# Kane MCP Bootstrap

This optional stdio MCP server helps an Agent discover Kane configuration,
read the canonical Connector Protocol, inspect the registered Agent identity,
probe Kane health, and validate a redacted Connector trace. It is not a chat
transport, does not hold a persistent Agent session, and does not proxy
conversation messages.

Install separately from Kane API dependencies:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Configure the MCP host to start `python connectors/kane-mcp/server.py` from the
repository root. Optional environment values:

| Variable | Purpose | Default |
|---|---|---|
| `KANE_API_BASE_URL` | Kane API base URL | `http://127.0.0.1:8000` |
| `KANE_CONNECTOR_WS_URL` | Explicit Connector WebSocket URL | derived from API base URL |
| `KANE_AGENT_ID` | Stable identity to inspect | unset |
| `KANE_API_TOKEN` | API token for protected registry calls | unset |

The API token is sent only as `X-Api-Key` to Kane and is never returned by an
MCP tool. The one-time pairing code and reconnect token are not created or
exposed by this MCP server; pairing stays in the authorized Kane client, and
the code is transferred directly to the Connector process.

## Tools

- `get_connection_guide`: reads the existing generic Kane Connect Skill and
  its reported Agent-interface reference notes. Not a whitelist, credential
  issuer, or substitute for real Agent conformance.

- `get_connector_spec`: returns `docs/KANE_CONNECTOR_PROTOCOL.md` itself.
- `get_connection_info`: configured API/WebSocket URLs and Agent ID, with
  secrets redacted.
- `get_agent_identity`: fetches the configured Agent's current registry entry.
- `get_capabilities_schema`: extracts the canonical schema from the protocol
  document rather than maintaining a duplicate.
- `test_connection`: probes `/health` and, when `KANE_AGENT_ID` is set, reads
  the registry. API reachability is not proof of a live Connector socket.
- `run_conformance_check`: validates a JSON trace using the shared
  `connectors/conformance` implementation. It does not execute the Agent.

## Local Checks

```powershell
python -m unittest discover -s connectors/kane-mcp -p "test_*.py"
python -m unittest discover -s connectors/conformance -p "test_*.py"
```
