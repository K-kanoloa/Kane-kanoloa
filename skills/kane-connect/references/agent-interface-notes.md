# Agent Interface Notes

Source: user-supplied Muse report dated 2026-10-01 Australia/Sydney.
These are reported results, not locally reproduced certifications. The source
Connector code and redacted traces must be obtained separately before adopting
any implementation. No new Agent is registered by this document.

## Reported native interfaces

| Agent | Reported interface | Important reported integration detail |
|---|---|---|
| Codex | app-server JSON-RPC | Keep native thread mapping at the Connector edge. Distinguish native resume from a newly created session with context replay. |
| Claude Code | noninteractive JSON output, native resume | Accept delivery before inference; retain the actual native session ID. |
| Kimi Code | official native CLI | Verify executable identity; a Wine wrapper is not a native Linux binary. |
| OpenClaw | local Agent JSON interface | Linux passed in the report; Wine was blocked. Do not generalize to native Windows. |
| Hermes | native Python CLI with resume | Use the user's actual interpreter and native session, not a Windows-runtime claim. |
| Pi | JSONL native events | Report found stdin must close for the tested one-shot invocation; preserve explicit message completion. |
| WorkBuddy | tested codebuddy CLI stream-json | Report tested CodeBuddy CLI, not proof of every WorkBuddy desktop integration. Plain JSON reportedly hung. |
| User-owned DSH | headless NDJSON events | Separate user-owned DSH from built-in Kanaloa; keep provider configuration Agent-side. |
| Cursor | native CLI | Deferred at account authentication in this report. Do not treat as currently verified. |

Versions and command flags can change. Confirm the installed native interface
before use. Do not copy provider-specific settings as mandatory defaults.
Report Wine and Linux evidence separately; neither certifies native Windows.

## Reusable lessons

- Stable Agent identity is independent of availability. Keep the same
  Connector identity and known native session references across reconnect.
- `command.result` accepted acknowledges delivery, not model completion.
- Bind a nonempty native session reference and preserve isolation per Turn.
- A single logical reply ends with one explicit completion and one stored
  Agent Message, regardless of stream chunks.
- Native session loss is not successful recovery. If context replay is used,
  identify it as replay; do not claim unfinished work resumed or rerun side
  effects automatically.
- Credentials belong to the Agent host. Use its protected credential facility
  or authorized environment injection; never include real credentials in
  guides, traces, source, or chat history.
- Shared conformance proves wire behavior. Keep real Agent/model evidence
  distinct from mock peers and API reachability.

## Kanaloa boundary

The report's new Python agent directly calling a model is a test reference,
not this repository's built-in Kanaloa. Its PASS results do not validate our
modified DSH-based Kanaloa Runtime, tools, IPython, recovery, or private Loop.
Do not replace the built-in implementation or its identity with that sample.

Only `docs/KANE_CONNECTOR_PROTOCOL.md` defines the wire contract. These notes
do not add message types, pairing behavior, capabilities, or acceptance rules.
