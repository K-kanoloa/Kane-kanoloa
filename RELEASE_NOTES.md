# Kane-Kanaloa v3.0.0-beta

Date: 2026-10-03 (Australia/Sydney). Early evaluation release on
`vnext/thin-harness`. **Known or unknown bugs may exist.** Not production,
security, universal Agent compatibility or full product certification.
Historical main/v2 tags are preserved; this release does not replace main.
The earlier v0.1.0-beta tag was a naming error; v3.0.0-beta is the corrected
release identity following v2. The old tag is retained for traceability.

## Scope

Thin model-free Kane Harness, bundled Kanaloa/modified DSH Runtime, current
Web control UI, unified external Connector endpoint, generic Skill/MCP bootstrap,
shared conformance and Codex reference. Agent execution remains outside Kane Core.
No feature development or architecture refactor was performed during release preparation.

## Release Checks

- Web typecheck: PASS.
- Existing read-only stack smoke: PASS; API health 200, Web 200, no observed
  browser console errors or bad HTTP responses.
- Secret scan: no real secrets found in publishable files or new branch history.
  Test placeholders were reviewed.
- Git integrity: PASS. Dangling objects are not repository corruption.
- Runtime databases, credentials, local .env, logs, caches and account data
  are not uploaded. The tracked .env.example contains placeholders.
- Upstream DSH MIT notice preserved in THIRD_PARTY_NOTICES.md. Installed DSH
  packages retain LICENSE files; node_modules and virtual environments are excluded.

No full API, Bridge, MCP, Connector, UI or real model/crash suite was rerun
as a release gate. No fresh-machine or all-Agent acceptance was performed.

## Earlier Evidence, Not A Fresh Release Retest

The 2026-10-01 acceptance recorded API 191 PASS / 0 SKIP, Bridge 4 PASS,
Web typecheck PASS, MCP 4 PASS, conformance 7 PASS and Codex reference regression
6 PASS. Real Kanaloa PONG, session continuity, two-cycle Loop, Tool success,
process-tree crash recovery and delayed completion after Cancel were exercised
using saved Provider configuration. These results do not guarantee another environment.

Approval was NOT_TRIGGERED, not fabricated as PASS. Subagent crash injection
terminated the shared Runtime process tree during child work; it was not an
isolated independent child-process crash test.

## Known Limitations / Untested Areas

- Session rebind is not unfinished work resume. Unknown outcomes stay interrupted;
  no blind rerun or side-effect rollback is promised.
- Continuable child recovery does not guarantee automatic resumed execution.
- Provider/network errors, permissions and Tool/IPython installation remain
  deployment-dependent.
- Pairing/registry presence is not a live compatible Agent-side Connector.
- UI model setup focuses on OpenAI-compatible Base URL/Model/API Key; not every
  Backend option has a UI control.
- All external Agents, fresh install, cross-machine reconnect, production
  deployment, load/security testing and full UI suite were not retested here.
- Historical docs and package/API version strings may still report v2.0.0.
  This release's identity is v3.0.0-beta.
- Never expose the default local unauthenticated service to the Internet.

## Data And Licensing

Breaking architecture baseline relative to v2. No automatic legacy data import
is promised. Back up databases and configuration before upgrading.
Historical releases are retained. Project license status remains UNLICENSED;
dependency attribution is in THIRD_PARTY_NOTICES.md.
