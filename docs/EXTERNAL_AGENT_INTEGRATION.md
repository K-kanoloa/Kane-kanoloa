# External Agent Connector Integration

Kane vNext accepts external Agents through the generic Agent-side Kane
Connector Protocol. The Agent Connector initiates and maintains an outbound
WebSocket to Kane; Kane Core does not discover or launch Codex, Claude, Pi, or
other Agent executables.

## Responsibility Boundaries

| Piece | Responsibility |
|---|---|
| Kane Connector Protocol | The single versioned wire contract in `KANE_CONNECTOR_PROTOCOL.md`. |
| Kane `ConnectorAdapter` | Generic translation between protocol frames and Kane's existing adapter contract. |
| Agent Connector | Native Agent protocol, session binding, event parsing, and subprocess/transport lifecycle. |
| Kane Connect Skill | Agent-neutral instructions for implementing or operating a Connector. |
| Kane MCP | Optional bootstrap, discovery, health probe, and shared trace validation; not chat transport. |
| Kanaloa | Bundled first-party Agent/runtime; its DSH/ACP path remains separate and unchanged. |

Codex is the Golden Reference Connector under `connectors/codex/`. Codex
app-server JSON-RPC and native session details stay in that directory. The old
v2 Local Bridge task execution route is not the vNext Codex integration path.

## Connection Flow

1. An authorized Kane client creates an Agent identity and one-time pairing
   code through `POST /api/v1/agents/pairings`.
2. The Agent-side Connector opens
   `ws(s)://<kane-host>/api/v1/connectors/ws` with the pairing credential.
3. The Connector sends `connector.hello`; Kane returns `connector.ready` and a
   one-time scoped reconnect token.
4. Kane sends `turn.send`; the Connector acknowledges delivery and translates
   it to the selected Agent's native session.
5. The Connector returns optional `reply.delta` frames and one explicit
   `reply.completed` for a logical reply. Kane persists one Message for that
   reply ID.

The Connector identity remains registered while offline. Online state comes
from the authenticated live socket, not from installation or pairing alone.
Advanced operations are optional capabilities and must not be emulated when
the native Agent does not support them.

## Validation

Use the shared conformance validator in `connectors/conformance/` on a
redacted protocol trace, then run a real end-to-end test against the native
Agent. The shared trace validator does not prove native execution. Report the
tested Agent version, native interface, declared capabilities, and any
unverified lifecycle behavior.

See `KANE_CONNECTOR_PROTOCOL.md`, `connectors/codex/README.md`,
`connectors/kane-mcp/README.md`, and `skills/kane-connect/SKILL.md`.
