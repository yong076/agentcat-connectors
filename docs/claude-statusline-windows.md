# Claude statusline window reconciliation — 26.46.0 preparation

This is the first implementation unit from the Coucou/magpie comparison.
Ticket: TRA-3970. Release remains held for the owner's separate decision.

## Problem and behavior

Previously the last statusline event replaced both account quota windows. A
late session reporting 20% could overwrite another session's 70%, and partial
reports could remove the weekly quota. The global latest event could also come
from a different CLI home.

The collector now retains the highest observed percentage for the same reset
time. A later reset starts a new window and may legitimately lower the value.
Missing, null, or malformed windows preserve the previous observation. Expired
windows remain visibly stale; they are not converted into a fabricated 0%.

The independent five-hour and weekly windows are persisted in SQLite in the
same transaction as the event. Concurrent sessions cannot overwrite a higher
observation with an older copy. Persistence survives daemon restarts and event
retention. Each window keeps its own collector timestamp. The aggregate uses
the oldest retained timestamp, conservatively marking the row stale when any
displayed observation is stale. Equal or increasing observations can refresh
their own window; lower or missing observations cannot.

## Identity and privacy

The scope is a device-local HMAC of the CLI home, account UUID and organization
from that home's `oauthAccount` metadata. No credentials or Keychain entries
are read. Raw identity values and paths are not stored in the new table or
added to snapshot fields. Payload-supplied scope values are overwritten.

The default runtime overlay reads only the scope for its selected CLI home.
Switching accounts or organizations cannot inherit the previous scope's
high-water mark. Unknown/legacy identities retain last-report behavior and
are never merged. A known account needs a newly collected scoped event; old
unscoped events are not retroactively assigned to it.

Scope comes from metadata at collection time. This does not authenticate the
account used by a long-running CLI session after an external login change.
Per-instance quota probes retain their existing independent behavior.

## Reference and implementation

Reviewed Coucou's `ClaudePlanGauge.swift` merge behavior from the local reference
checkout of [Louis-CFM/coucou](https://github.com/Louis-CFM/coucou). This is a new
Python implementation; no upstream source or assets were copied. Unlike a
single global high-water mark, this implementation isolates identities and
preserves observation freshness.

## Acceptance evidence

- Baseline regression: a late 20% report replaced 70%, and changing account
  metadata left the old account's quota available. Both tests fail before the fix.
- Sandbox regression cases cover same-window reordering, resets, partial/null
  windows, timestamps and expiry, accounts, organizations, homes, unknown
  identity, invalid numbers, context-only reports, forged payload scope,
  persistence and concurrent writers.
- Tests run from a temporary copy with a fake HOME and closed stdin.
- Final local full suite: 1,118 tests passed (4 skipped). The focused suite
  passed all 12 cases, and concurrent first-run writers passed 30 repetitions.
- Built-in diff review checked transaction ordering, account selection,
  timestamp propagation, legacy behavior and snapshot identity exposure.
- The first CI run exposed a concurrent first-run WAL initialization lock on
  Linux, macOS and Windows with Python 3.11. Initialization now retries only SQLite's transient lock,
  bounded by the existing five-second database wait budget.
- No live provider, installed daemon, native UI, Windows runtime, release, or
  update-promotion verification is claimed by these fixture tests.

The remaining comparison ideas (gateway attribution, waiting-state observation,
demo tour, reset reminders and review prompts) are separate implementation units.
