# Agent connection requirements

Choose the documented interface supported by the installed Agent version.
Authenticate the Agent on its own host before starting its Connector.

- Keep credentials in the Agent host’s protected credential store.
- Use a stable Agent and Connector identity across reconnects.
- Preserve the native session reference for each Kane Turn.
- Acknowledge delivery separately from completion.
- Emit one explicit completion for each logical reply.
- Report an unavailable or lost native session as an interruption.
- Resume only when the native Agent supports it; do not repeat side effects.
- Declare cancellation, steering, branching, and approvals only when supported.

The bundled Codex Connector uses Codex app-server. Other Agents require a
compatible Connector for their own documented interface. Consult
`docs/KANE_CONNECTOR_PROTOCOL.md` for message formats and
`connectors/codex/README.md` for Codex setup.
