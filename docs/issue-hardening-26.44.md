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

## Follow-up unit: remaining audited checklist tails

- #121: successful installers retain three recent backup folders and compact
  known closed backups older than 30 days into private recovery archives. Every
  regular file is SHA256-verified before its source is removed. Dirty-checkout
  recovery and unknown folders are excluded; compressed archives never expire
  automatically. Windows hidden launchers wait and forward the daemon exit,
  with Task Scheduler retries at one-minute intervals (three attempts), no
  execution time limit and no duplicate task instances. Both uninstall
  entrypoints use local installed source without downloads or replacement,
  retaining usage/backups. Both bootstrap platforms require Python >=3.9.
- #122: Copilot per-file byte cursors keep projected usage metadata only in an
  8 MiB / 20k-event / 128-file memory cache. Valid incomplete tails are rendered
  once and replaced on completion; hash-only boundary anchors invalidate
  rewritten headers/tails. Windows/models/transcript estimates are recomputed
  from metadata, preserving midnight rebucketing. OpenCode uses narrow per-row
  ID/update metadata and changed usage rows for supported schemas, recognizing
  backfill, deletion, session metadata and migration changes. Old/unsupported
  schemas and histories over 10k rows retain the complete reader. The desktop
  root-mtime cache has a mandatory five-minute TTL and preserves actual
  `checkedAt`, because nested file changes need not update the root mtime.
  Claude hot dedup metadata older than one year or above 50k records is moved
  transactionally into a private generation-keyed SQLite archive. Exact
  duplicate/correction metadata remains available after cursor restart; it is
  not discarded. Missing stale offsets are pruned without changing rollups.
- #123: Gemini/Codex format-drift counters persist with their usage cursors;
  bad payloads are not retained. Pricing alias collisions resolve lexically
  and explicit native IDs win, independent of feed order. Copilot CLI processes
  are already explicitly recognized. A VSCodium editor process by itself is
  not evidence of active Copilot and deliberately remains unclassified; its
  actual usage metadata roots are supported.
- #41: CLI-only bounded diagnostics cover snapshot version, macOS service
  registration/port ownership, Windows task evidence, metadata permissions and
  event recency. `doctor --fix` explicitly backs up and reapplies local
  hook/telemetry settings and restarts an existing registration; it performs no
  account action or credential refresh. Child output is not echoed into the
  report. The English reason-code guide is `docs/troubleshooting.md`, consistent
  with the repository's English-documentation rule. The additive source health
  contract follows the app's current decoder rather than the old speculative
  issue schema.

## Release limitations

Native Mac/Windows update-path attestations remain mandatory for the exact
release archive checksum, even when unit/CI checks pass. App health rendering
and real menu-bar gestures belong to the parent release workflow. PowerShell
entrypoint tests run only where PowerShell exists and report an explicit skip
on this Mac. The metadata fast paths retain complete-reader fallbacks; they do
not promise incremental work for every old provider schema. No issue has been
closed and no production release/deployment has been performed by this leaf.
