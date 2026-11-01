# Connector issue reconciliation for 26.44

This follow-up is local connector hardening. Provider account actions and
credential refresh/write behavior are unchanged. Tests use copied sources,
temporary homes and synthetic credentials. A public release still requires
the separate Mac and Windows update-path attestations in `release-safety.md`.

## Implemented here

- connectors #114: compare decoded nested hook commands, collapse exact
  duplicates, preserve unrelated hooks and group metadata. Windows quoted
  paths remain idempotent after repeated installation.
- CI: Python 3.9 on Ubuntu uses the compatible 24.04 runner; all other matrix
  entries retain their current runner. Python 3.9 remains supported.
- Mac #121: honor `AGENTCAT_PORT` as the daemon default, with explicit `--port`
  taking precedence. Check Python >=3.9 before installer mutation. Bound a
  malformed Gemini streaming record to 8 Mi characters and report a parse
  failure instead of accumulating an unbounded buffer.
- Mac #122: write the hot snapshot compactly. Other state files retain their
  readable format.
- Mac #123: after merging dated usage buckets, lifetime totals cover their
  sum; re-entry into a top-N project list seeds a new baseline; project ledger
  keys remain `provider|project-id`. Add VSCodium metadata roots. Reject
  unpriced `air` and nested `mini`/`lite`/`nano` pricing variants. Honor explicit
  delta telemetry temporality and keep the first chosen Gemini metric family
  per session/model/token stream, including across cached scan batches.
- Mac #41: emit `connector.health.v1` with bounded usage/limits source rows
  derived from the existing snapshot build. A healthy usage reader does not
  hide a quota credential failure. No extra credential read or network call is
  performed. Unknown evidence stays `health_unknown`; the block is not a live
  official provider status check. Health failure cannot block publication.
- Synthetic SQLite fixtures explicitly construct closed/no-sidecar databases
  after checkpointing; Apple's SQLite keeps empty WAL/SHM files after close.
  This preserves immutable-read race coverage without touching real databases.

## Existing behavior retained

- #121 log rotation, relative-path warnings, bind failures, SIGTERM/SIGHUP
  cleanup and uninstall paths already exist. Do not restore an explicit
  launchd ThrottleInterval: the newer lifecycle fix deliberately removed it.
- #134 Codex archive identity/cursor handling and #42 pre-encoded HTTP snapshot
  publication already have regression coverage and are not reimplemented.
- #123 Codex usage-breakdown failures already use token-scoped negative-cache
  backoff capped at one hour, with the documented 15-minute failure polling
  floor. CLI tokens are never refreshed.
- OpenCode's newer committed tests intentionally include preview/import DBs
  with cross-generation ID dedup and exclude archived/child sessions. The old
  request to narrow the DB glob/include children conflicts with that current
  contract. It is superseded here; no retrospective semantics change is made.

## Remaining work, explicitly not completed by this patch

- #121: retention/pruning for old source backups; Windows scheduled-task
  automatic restart policy and an `uninstall.ps1` entrypoint. Backups are
  recovery material and are not deleted by this release patch.
- #122: Copilot per-file byte cursors, OpenCode high-water incremental reads,
  historical Claude usage-record pruning and a safe desktop tree-mtime cache.
  These require preservation of corrections, removals, midnight rebucketing
  and changing in-place database rows; a timestamp-only cache is insufficient.
- #123: persistent Gemini/Codex format-drift counters; deterministic pricing
  alias priority across feed collisions; explicit VSCodium process attribution
  beyond usage roots (the generic editor process is not proof of Copilot).
- #41: non-mutating version/port/launchd ownership diagnostics, explicit
  `doctor --fix` repair orchestration, event-recency/FDA rows, and complete
  reason-code troubleshooting docs. Existing doctor/network/bundle/logging
  tools remain available. The new snapshot health block has a smaller bounded
  contract than the original speculative issue schema.

These are residual issue subitems, not a claim that every historical issue is
closed. Native update-path tests and app health rendering belong to the parent
release workflow.
