# Kane Connector Protocol v0.1

This is the wire contract for an Agent-side Connector that opens and maintains
an outbound connection to Kane. It does not change Conversation, Message,
Turn, Mailbox, Branch, Store, or the existing `BaseAdapter` contract.

Kane owns conversation persistence and routing. The Connector translates
between this protocol and the Agent's native interface. The Agent owns
reasoning, tools, execution, credentials, and native context/session behavior.
Kanaloa remains the bundled first-party Agent and keeps its existing DSH/ACP
path; this protocol does not replace it.

## Implementations

Implemented in the API:

- `POST /api/v1/agents/pairings`: create a short-lived pairing code and stable
  Agent identity. Loopback requests are allowed for local development. Remote
  pairing requires Kane API authentication.
- WebSocket `/api/v1/connectors/ws`: authenticate with a one-time pairing code or a
  reconnect token, exchange handshake frames, and keep an outbound WebSocket
  session alive.
- Kane-side `ConnectorAdapter`: deliver send, steer, cancel, resume, and
  permission-response commands; accept reply deltas, completion, waiting,
  failure, interruption, and session-binding events.
- Persistent Agent identity and hashed reconnect-token storage. The raw
  reconnect token is returned only once in `connector.ready`.
- Stable completion Message IDs make repeated `reply.completed` delivery
  idempotent across API process restarts.

Available connector assets:

- `connectors/codex/`: the Golden Reference Agent-side Connector for Codex
  app-server. Its tested scope is documented separately; it does not imply
  compatibility with other Agents.
- `skills/kane-connect/`: Agent-neutral connection and implementation guide.
- `connectors/kane-mcp/`: optional stdio Bootstrap, discovery, and diagnostics.

Not provided by this protocol implementation:
- Token revocation UI/API, rate limiting, or multi-instance pairing/session
  coordination.
- Durable replay of delta events. A Connector must not replay acknowledged
  deltas; after an uncertain disconnect it must report interruption and use
  the Agent's actual resume facts rather than guessing.

Pairing a name creates an unavailable identity; it does not mean an Agent is
online or that a native Agent has been integrated. A working Agent still needs
a Connector process implementing this contract. A deterministic test peer
proves only the Kane-side protocol path, not a real Codex, Cursor, or other
Agent integration.

The MCP server is optional Bootstrap and diagnostics only. It is not a message
transport and does not proxy conversation traffic. Its `get_connector_spec`
tool returns this document; this file remains the only wire-contract source.

## Connection And Authentication

The Connector initiates an outbound WebSocket to:

```text
ws(s)://<kane-host>/api/v1/connectors/ws
```

Use `wss://` for remote deployments, terminating TLS at Kane or a trusted
reverse proxy. Loopback `ws://` is suitable only for local development. The
remote Agent host does not need to accept inbound connections.

Pairing is initiated by an authorized Kane client:

```http
POST /api/v1/agents/pairings
Content-Type: application/json

{"agent_id":"my-agent","display_name":"My Agent"}
```

The response contains a high-entropy `pairing_code` for one successful handshake,
without a time limit. A replacement code, explicit disconnect/removal, or server
restart invalidates an unused code. Do not put it in a Skill, source file, command
history, or logs. Send it directly to the Connector using the WebSocket
`Authorization: Pairing <pairing_code>` header. After pairing, securely store
the one-time `connector_token` returned in `connector.ready`; use
`Authorization: Bearer <connector_token>` on reconnect. Kane persists only its
SHA-256 digest. Re-pairing rotates the credential for that Agent identity.

When `OCTOPUS_API_TOKEN` (or `OCTOPUS_API_KEY`) is configured, HTTP API calls require the normal Kane
API authentication. The pairing-code WebSocket authentication is scoped to one
Agent identity and is separate from the Kane API token.

## Frame Envelope

Every WebSocket frame is a JSON object:

```json
{
  "protocol": "kane-connector",
  "version": "0.1",
  "type": "connector.hello",
  "id": "opaque-frame-id",
  "payload": {}
}
```

Command frames from Kane include a `request_id`. Connector event frames must
include a stable `event_id`; Kane acknowledges them with `event.ack`. Repeated
event IDs are ignored while the current API process retains its bounded event
cache. Completed logical replies are independently deduplicated durably by
Agent identity, Turn, and `reply_id`.

## Handshake

After the WebSocket opens, send `connector.hello`:

```json
{
  "protocol": "kane-connector",
  "version": "0.1",
  "type": "connector.hello",
  "id": "hello-1",
  "payload": {
    "connector_id": "stable-installation-id",
    "agent_id": "my-agent",
    "display_name": "My Agent",
    "capabilities": {
      "supports_stream": true,
      "supports_resume": false,
      "supports_cancel": true,
      "supports_approval": false,
      "supports_parallel_sessions": false,
      "max_parallel_sessions": null,
      "steer_mode": "follow_up_only",
      "branch_mode": "unsupported"
    },
    "sessions": []
  }
}
```

Kane responds with `connector.ready`, including `agent_id` and a heartbeat
interval. On first pairing it also includes the one-time `connector_token`.
`connector_id` must remain stable for this installation. A live authenticated
socket determines online status; persisted identity alone is unavailable.
Capabilities are declarations, not proof of a working operation.

### Capability schema

This is the canonical schema returned by MCP `get_capabilities_schema` and
used by shared conformance validation. Keep it aligned with
`AgentCapabilities` in `apps/api/app/domain/models.py`.

```json capabilities-schema
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "supports_stream": {"type": "boolean"},
    "supports_resume": {"type": "boolean"},
    "supports_cancel": {"type": "boolean"},
    "supports_approval": {"type": "boolean"},
    "supports_parallel_sessions": {"type": "boolean"},
    "max_parallel_sessions": {"type": ["integer", "null"], "minimum": 1},
    "steer_mode": {"enum": ["native", "safe_boundary", "follow_up_only"]},
    "branch_mode": {"enum": ["native", "replay", "unsupported"]}
  },
  "required": [
    "supports_stream", "supports_resume", "supports_cancel",
    "supports_approval", "supports_parallel_sessions",
    "max_parallel_sessions", "steer_mode", "branch_mode"
  ]
}
```

Send `connector.heartbeat` at the provided interval. Kane replies with
`connector.heartbeat.ack`. Losing the socket is a transport interruption; it
does not prove remote work stopped or undo external side effects.

## Commands From Kane

Commands include `request_id` and `payload`. The Connector replies with
`command.result` carrying the same `request_id` and a payload such as
`{"status":"accepted"}` or `{"status":"rejected","reason":"..."}`.
Acceptance means only that the Connector accepted delivery, not that the Agent
completed the work.

- `turn.send`: includes `conversation_id`, `turn_id`, `message_id`, `content`,
  `native_session_ref`, and bounded `context_messages` for the current visible
  history.
- `turn.steer`: includes the same conversation/Turn/message identity and
  content. It must target the same Turn; honor the declared `steer_mode`.
- `turn.cancel`: requests cancellation. It does not promise rollback of
  world-side effects.
- `turn.resume`: requests continuation of the same Turn and includes current
  visible history. Accept only if the Agent can actually resume; otherwise
  reject and report the actual interruption state.
- `permission.respond`: includes a request ID, decision, session reference,
  and Turn ID. Use only when `supports_approval` is true.

## Events From Connector

Every event has `protocol`, `version`, `type`, `id`, `event_id`, and an object
`payload`. Events tied to a Turn must include its `turn_id`; include
`conversation_id` when known.

- `session.bound`: `native_session_ref`; binds the current Turn to an Agent
  native session.
- `reply.delta`: `turn_id`, `reply_id`, and `text`. This is transient output,
  not a chat Message. Deliver each delta once; do not retransmit uncertain
  deltas after reconnect.
- `reply.completed`: `turn_id`, `reply_id`, optional final `content`, and
  optional `turn_finished` (defaults to true). Send only on the Agent's
  explicit logical completion signal. Kane persists at most one Message for
  the same Agent/Turn/`reply_id`, including after API restart.
- `turn.waiting_user`: `turn_id` and optional `prompt`.
- `permission.requested`: `turn_id`, `request_id`, optional
  `native_session_ref` and `title`.
- `turn.failed`: only when the Agent explicitly ends its work unsuccessfully.
- `turn.interrupted`: for process, transport, native-session, cancellation,
  or other incomplete execution facts.
- `agent.availability`: currently accepted as informational; connected status
  still comes from the authenticated live socket.

An unsupported or malformed frame closes the current socket. Kane does not
infer completion from silence or a timeout. On disconnect, active Turns for
that Agent become `interrupted`; their partial output remains in Kane and is
not promoted to a completed Message.

## Optional Capabilities

Identity, authenticated connection, session binding, send/receive, and an
explicit logical completion signal form the base contract. `steer`, `cancel`,
approval, resume, parallel sessions, and branch behavior are optional. Declare
only behavior the Agent-side Connector actually implements. Kane does not
emulate unsupported Agent behavior. Branch history boundaries remain owned by
Kane; native fork or bounded replay belongs at the Connector/Agent edge.

## Security And Conformance

- Use TLS for remote WebSockets. Do not expose an unauthenticated public
  listener.
- Keep pairing and reconnect credentials out of Skill text, source, logs, and
  ordinary Agent discovery responses.
- Treat Agent-provided text and metadata as untrusted. Never execute arbitrary
  wire-provided shell commands in Kane.
- Test stable identity, live availability, command routing, explicit
  completion, partial-output interruption, reply idempotency, and reconnect
  facts before claiming an Agent integration works.
- A deterministic peer test proves Kane-side wire handling only. Each Agent
  still needs its own real end-to-end native integration test.
- Shared trace-conformance input is a JSON array of records:
  `{"direction":"connector_to_kane"|"kane_to_connector"|"transport"|"kane_observation","frame":{...}}`.
  Transport records use `{"type":"disconnect","agent_status":"unavailable"}`
  and `{"type":"reconnect","agent_status":"ready"}`. A single Kane
  observation uses `{"type":"turn_state","turn_id":"...","status":"finished","agent_message_count":1}`.
  A trace must contain an authenticated ready
  handshake, a delivered `turn.send` with an accepted command result, one
  explicit logical completion (with zero or more deltas), and a reconnect
  handshake preserving Agent/Connector identity and native session reference.
  `run_conformance_check` validates a supplied trace; it does not claim to
  execute or verify the Agent's native runtime.

## Explicitly Out Of Scope

This protocol does not define contacts, groups, `@Agent`, pairing product UI,
Agent planning/tools/models/memory, Kanaloa/DSH runtime changes, or a universal
claim that every Agent can connect without an Agent-side Connector.
