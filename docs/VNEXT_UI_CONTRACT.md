# vNext Conversation Workspace Contract

This UI consumes the existing `/api/v1` HTTP/SSE contract. The server is the
source of truth for Conversation, Turn, Branch, permission and Loop state.
It is not the v2 Task/Run cockpit and has no client execution state machine.

## Workspace

- Left: conversation list, local title search, current selection, create dialog.
- Center: server-visible history, one streaming reply, approval, composer.
- Right: Turns, focus versus inspection, native session facts, coarse activity,
  registered agent capabilities, contextual controls.
- Desktop: collapsible columns; below 1000px: keyboard-contained drawers.
- Draft text is scoped to the viewed Conversation/Turn in browser memory.
  Refresh does not persist drafts. Messages remain server-owned.
- English and Chinese copy reuse the existing locale preference.

The retained visual foundation is Kane's walnut header, warm paper surfaces,
amber actions, moss status accents, rounded shell and system font stack.
Spacing, typography and control sizes use the shared stylesheet. The former
health-only page and unreferenced v2 presentation wrappers are removed.

## HTTP Mapping

Browser requests use the same-origin `/api/proxy` prefix. The proxy streams SSE
without buffering, forwards existing auth headers, and may use the server-side
`OCTOPUS_API_TOKEN`. It never puts that token into browser storage. This is not
a new authentication system; deployment access controls still apply.

| UI action | API | Request / response |
|---|---|---|
| Agent selector | `GET /api/v1/agents` | Actual registrations and capability facts only |
| List conversations | `GET /api/v1/conversations` | Conversation list; search filters titles locally |
| Create conversation | `POST /api/v1/conversations` | `title`, `bound_agent_id` |
| Open / refresh conversation | `GET /api/v1/conversations/{id}` | Includes authoritative `focus_turn_id` |
| List work | `GET /api/v1/conversations/{id}/turns` | Existing Turns |
| New task | `POST /api/v1/conversations/{id}/turns` | `title`, current `branch_id` when present |
| Focus task | `POST /api/v1/conversations/{id}/focus` | `turn_id` |
| Inspect task | `GET /api/v1/turns/{id}` | Read only; no focus mutation |
| Read history | `GET /api/v1/conversations/{id}/messages?turn_id={id}` | Server-resolved branch-visible history |
| Send / adjust / follow-up | `POST /api/v1/conversations/{id}/messages` | `content`, target `turn_id`, optional `reply_to_message_id`, `loop_mode`, `max_iterations` |
| Branch from message | `POST /api/v1/conversations/{id}/branches` | `message_id`, `name`; use returned `initial_turn_id`, then explicitly focus it |
| Cancel execution | `POST /api/v1/turns/{id}/cancel` | Native cancel; confirmation explains that side effects are not undone |
| Resume | `POST /api/v1/turns/{id}/resume` | Native resume only; does not resend the prompt |
| Stop Loop | `POST /api/v1/turns/{id}/stop-loop` | Graceful stop after current iteration |
| Approval | `POST /api/v1/turns/{id}/permissions/{request_id}/respond` | Exact `allow-once`, `reject-once`, `cancelled` decision |
| Live work | `GET /api/v1/turns/{id}/stream` | SSE subscription for the viewed Turn |

New Task is explicit. Ordinary Send never decides that text constitutes a new
task. Input to a running Turn uses the same message endpoint; the Backend owns
native steer, safe-boundary delivery and follow-up-only degradation. Inspecting
or cancelling a background Turn does not change focus. Sending while inspecting
targets the displayed Turn explicitly. Only focus controls mutate focus.

Branch creation does not modify original history. The UI uses the returned Turn
and the server's history query, not client-side history slicing. Only the actual
agent registry populates the selector; there are no placeholder agents.

## Minimal Read Projection

`GET /api/v1/turns/{id}` retains every existing Turn field and adds:

- `pending_permissions`: exact request IDs, safe tool title and creation time,
  filtered by native session and Turn. Enables refresh-safe approval cards.
- `loop`: current iteration, maximum iteration count and stop request flag,
  read from the existing active Kanaloa runtime. Null when no loop is active.
- `events`: latest 40 coarse stored events, excluding raw/delta content.
  Thinking exposes status only; other event payloads allow only string
  `status`, `tool` and `boundary` facts. No thought text or tool arguments.

This is a read DTO, not a persisted model, new endpoint or execution layer.
Core/Harness, routing, steer, recovery, Mailbox and Loop semantics are unchanged.
The Loop projection currently reads Kanaloa's existing runtime map; changes to
that map require updating the DTO and its contract test, not adding UI state.

## SSE and Message Rules

1. Fetch current Turn facts and visible history before subscribing.
2. Apply snapshot, then accumulate deltas into one transient reply bubble.
3. On completion, re-read the persisted Message/history. Do not create a Message
   per chunk or infer completion from silence.
4. Retain partial output for interruption and show the server reason. `failed`
   and `interrupted` remain distinct; neither automatically retries.
5. Display only coarse activity. Raw thought/argument text is neither rendered
   nor retained in UI activity state.
6. Switching target, refresh and disconnect abort the subscriber, never Cancel.
7. Reconnect uses bounded backoff and fetches facts/history before resubscribing.
   There is no frontend event replay engine.
8. Periodic reads refresh the permission/Loop projections and background Turn
   list because these facts do not all have dedicated SSE events.

Normal mode is the default. Kanaloa Loop controls send existing parameters:
Default = 5, Unlimited = JSON `null`, Custom = a positive safe integer.
Stop Loop and Cancel are separate actions. Unsupported capabilities are not
presented as available. Failed or stale permission actions show the actual error;
the UI does not retry side-effecting commands automatically.

## Verification

Run from the repository root:

```text
npm run typecheck:web
npm run test:api
npm run test:bridge
npm --workspace @kane/web run lint
npm run test:e2e:ui
```

`test:e2e:ui` starts an isolated API on 8100 and Web on 3100 (overridable through
`UI_TEST_API_PORT` / `UI_TEST_WEB_PORT`). It requires the existing Python API
dependencies and Playwright Chromium. It refuses occupied test ports.

The test uses real FastAPI routes, auth, SQLite, Coordinator, Dispatcher, Kanaloa
Adapter and Loop runtime with a deterministic ACP transport peer. The fixture
is loaded only by its explicit test factory, requires `KANE_UI_TEST=1`, and
rejects databases outside the OS temporary directory. It is not a selectable
production agent and does not call a model, DSH, tools or a system shell.

Coverage includes creation, streaming, one logical Message (including a >30k
reply), running input, parallel Turns, inspection/focus/targeted send, retained
drafts, Branch history/session isolation, all approval decisions, duplicate
approval rejection, Cancel, Resume, failure/interruption, all Loop limits,
SSE reconnect, auth failure, API outage, and desktop/tablet/mobile layouts.

Screenshots and the temporary SQLite database remain under the printed OS temp
directory, never `apps/data`. The runner stops only processes it started, waits
for exit and verifies both test ports are free. Cleanup failure fails the test.

The existing `test:e2e:smoke` is a read-only check of an already-running stack;
its page assertion now targets the workspace instead of the removed skeleton.
Final real DSH/IPython/Shell acceptance remains a separate later phase. A green
deterministic browser suite is not evidence of real provider execution.
