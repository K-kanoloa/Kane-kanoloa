# Codex Connector: Golden Reference

This Agent-side Connector is the reference implementation of
[`docs/KANE_CONNECTOR_PROTOCOL.md`](../../docs/KANE_CONNECTOR_PROTOCOL.md). It
keeps Codex app-server knowledge at the Agent edge; Kane Core sees only the
generic Connector protocol and `ConnectorAdapter`.

The Connector owns Codex executable discovery, app-server startup, JSON-RPC,
thread/turn mapping, native event parsing, subprocess lifecycle, and translating
Codex output into one Kane logical reply. It does not move Codex reasoning,
tools, credentials, or execution into Kane.

## Requirements

- Python 3.11 or newer
- Codex CLI installed and authenticated on the Connector host
- Kane API reachable from that host

Install the connector's isolated dependencies and run it from a dedicated
working directory:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py --url ws://127.0.0.1:8000/api/v1/connectors/ws --agent-id codex --display-name "Codex" --cwd "$env:TEMP\kane-codex-work"
```

For remote Kane, use `wss://` and a trusted TLS endpoint. Create the Agent
identity through the Kane client/API and enter the one-time pairing code at
the hidden prompt. For supervised automation only, `KANE_PAIRING_CODE` may be
provided through a protected process environment; never place it in a command
argument, source file, or log. The Kane API token is not used by this process. On pairing,
the Connector stores the scoped reconnect token and stable installation ID in
the operating-system credential store when available. If credential storage
is unavailable, it keeps the token only in process memory and requires pairing
again after restart. No credential belongs in this repository or in logs.

## Verified and Declared Behavior

The real Golden Test must be run against the current Codex CLI and current Kane
commit before release. The current protocol declaration is:

| Capability | Connector declaration |
|---|---|
| Streaming and explicit completion | Supported |
| Parallel native sessions | Supported; no fixed maximum declared |
| Native steer | Supported by Codex app-server; behavioral effect must be tested separately |
| Cancel | Supported by Codex app-server |
| Approval forwarding | Supported for command and file-change approval requests |
| Native resume | Not supported by this Connector |
| Branch | Unsupported |

These declarations are not substitutes for operation-level tests. The standard
baseline conformance excludes steer, cancel, approval, and branch. Native Codex
behavior remains at the Connector edge and can change with Codex CLI releases.

## Tests

Run the shared protocol validator tests from the repository root:

```powershell
python -m unittest discover -s connectors/conformance -p "test_*.py"
```

Run Connector unit tests in this directory's environment:

```powershell
.venv\Scripts\python -m unittest discover -s connectors/codex -p "test_*.py"
```

The shared validator checks redacted wire traces only. It does not claim native
Codex execution. The real Golden Test is separately reported with the tested
Codex version and the persisted Kane Message/Turn result.
