---
name: kane-connect
description: Help an Agent implement or operate an Agent-side Connector for Kane using Kane's documented WebSocket protocol. Use only when the user explicitly asks to connect an Agent to Kane or develop its Connector.
---

# Kane Connect Skill

Kane is a model-free Conversation-to-Agent harness. Keep the Agent's reasoning,
native session, tools, credentials, and execution inside the Agent. A Connector
is an Agent-side long-running transport process. This Skill is guidance, not a
transport. Kane MCP is optional bootstrap/discovery and trace validation; MCP
is not the chat transport. The only wire contract is
`docs/KANE_CONNECTOR_PROTOCOL.md`.

## Before connecting

For a single bootstrap entry point, call MCP `get_connection_guide`. It returns
this Skill and the reported Agent interface notes, without credentials. The
notes are reference evidence, not a supported-Agent whitelist: an unlisted
Agent can connect using the same protocol. Read only the relevant Agent row.

1. Read the single canonical `KANE_CONNECTOR_PROTOCOL.md` returned by
   `get_connector_spec` or available at `docs/KANE_CONNECTOR_PROTOCOL.md`.
   Do not create a second protocol definition, invent message types, or claim
   unsupported capabilities.
2. Confirm the user explicitly wants this Agent connected and identify the
   Agent's stable native interface (official SDK, app-server, ACP, API, or
   documented CLI protocol). Do not inspect or export credentials.
3. Check whether a compatible Agent-side Connector already exists. Do not
   create a second one or modify Kane Core to accommodate an Agent-specific
   protocol.
4. If there is no reliable way to keep a bidirectional connection alive and
   detect logical completion, report that limitation. Do not register the
   Agent as online or substitute polling/silence heuristics for completion.

## Pairing and credentials

Pairing is initiated from an authorized Kane client with
`POST /api/v1/agents/pairings`, using an Agent ID and display name. The response
contains a one-time pairing code without a time limit. Reissuing it, explicit
disconnect/removal, or server restart invalidates an unused code. The user or
trusted Kane UI must transfer that code directly to the Connector process.
MCP `get_connection_info` returns endpoint configuration, never a pairing code,
API token, or reconnect token. Do not implement a pairing UI as part of this
Skill.

Never put pairing codes, reconnect tokens, API tokens, or provider credentials
in this Skill, source files, shell command arguments, screenshots, logs, or
chat history. Read secrets from a protected prompt or environment mechanism
chosen by the user. The Connector receives its scoped reconnect token once in
`connector.ready`; store it using the host's secret store or a user-protected
file with restrictive permissions. Do not print it.

Connect outbound to `wss://<kane-host>/api/v1/connectors/ws` for remote use.
Use `ws://127.0.0.1:8000/api/v1/connectors/ws` only for local development.
Authenticate first with `Authorization: Pairing <code>`, then use
`Authorization: Bearer <connector-token>` for reconnect. The API token used to
create a pairing is separate and must never be forwarded to the Agent-side
Connector.

## Wire lifecycle

1. Open the outbound WebSocket and send `connector.hello` with stable
   `agent_id`, `connector_id`, truthful capabilities, and known native session
   references.
2. Wait for Kane's `connector.ready`. Store the one-time reconnect token in
   the host secret store. Mark connected only while the authenticated socket is
   live; send heartbeats at the returned interval.
3. For a Kane `turn.send`, deliver the content to the selected native session
   (or create one for the first message in that Turn), bind it with
   `session.bound`, and return `command.result` for the matching `request_id`.
   An accepted result means accepted for delivery, not completed.
4. Emit zero or more `reply.delta` frames using one stable `reply_id`. On the
   native Agent's explicit logical completion signal, emit exactly one
   `reply.completed` for that logical reply. Do not treat stream silence or a
   timeout as completion. `event.ack` confirms receipt; do not replay an
   acknowledged delta.
5. Report an Agent-declared unsuccessful end as `turn.failed`. Report process,
   transport, session loss, cancellation, or uncertain completion as
   `turn.interrupted`. A disconnected socket makes the Agent unavailable; it
   does not prove side effects were rolled back.
6. On reconnect, preserve the same Agent and Connector identities and report
   known native session references. Resume only when the native Agent confirms
   it is resumable; never blindly re-run work.

`connector.hello` and `connector.ready` are the actual frame names. There is no
`agent.hello` frame in protocol v0.1.

## Connector behavior

Implement only the protocol in `docs/KANE_CONNECTOR_PROTOCOL.md`:

- Keep a stable `agent_id` and `connector_id`; report online only while the
  authenticated WebSocket is live.
- Translate `turn.send` to the intended native session and acknowledge only
  after the Connector accepts delivery.
- Preserve one Kane Turn/native-session binding. Do not create a new Turn for
  each message.
- Send `reply.delta` for transient output and exactly one `reply.completed`
  when the native Agent explicitly signals one logical reply is complete.
  Transport chunks must never become separate Kane Messages.
- Report infrastructure/session interruption as `turn.interrupted`; report
  `turn.failed` only when the Agent explicitly ends work unsuccessfully.
- Declare optional capabilities truthfully. Unsupported steer, cancel,
  approval, resume, or branch operations must be rejected, not simulated.
- On disconnect, retain the Agent's actual session facts. Do not blindly replay
  a prompt or repeat side effects.

## Validation

Before claiming success, use the shared trace format and
`run_conformance_check` to check identity, declared capabilities, accepted
delivery, one explicit completion, and reconnect/session continuity. The
shared validator checks protocol evidence only. A deterministic protocol peer
test is not a real Agent integration test. Report the exact Agent version and
native interface tested. Codex is the Golden Reference example; its native
app-server details belong only in `connectors/codex/` and are not generic Kane
instructions.

Do not install software, edit user Agent configuration, start a persistent
background process, or transmit a pairing credential without the user's
explicit authorization for that action.

## User-selected Loop

For external Agents, a Loop selection is an explicit instruction in the
ordinary message content (including the requested iteration count). Forward
it unchanged to the Agent. Execution belongs to that Agent; do not create a
Kane-side loop, invent a wire capability, or report iterations as completed
without native evidence. Kanaloa's private Loop is not an external Connector
dependency.
