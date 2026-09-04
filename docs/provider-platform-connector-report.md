# Provider platform connector report

Date: 2026-09-04  
Branch: `yong076/provider-platform-connector`

## Outcome

Slices 2, 3, and 5 are implemented. The connector contract is v2, all emitted
quota rows have explicit `scope` and `aggregate` semantics, all providers carry
path-free presentation metadata, and provider construction now runs through a
registry with isolated hooks. JetBrains AI and Hermes Agent are included as the
first providers added through that seam. The connector version remains
`26.34.7`.

No `AGENTS.md` existed in the worktree or its parent directories up to
`/Users/danielmacbook`; the task brief, README, and TED were used as the
governing instructions.

## Slice 2 — quota schema v2

- Bumped `CONTRACT_VERSION` from 1 to 2 without changing snapshot schema v4.
- Added `quota.scoped.v2` and `providers.metadata.v1`.
- Added `scope` (`account`, `model`, or `surface`) and `aggregate` to the shared
  quota constructor and to legacy cached rows before they re-enter a snapshot.
- Added live-limit cache `schemaVersion: 2`; the generic migration also retains
  the existing Claude and Codex cache repair rules.
- Added metadata for every provider: display name, brand color, icon hint,
  advertised windows, and localization source-hint key. Metadata contains no
  filesystem paths.
- Extended `contracts/connector-v1.json`, all existing golden fixtures, and a
  comprehensive provider quota/metadata fixture.
- Updated public release manifests and pinned-install defaults to contract v2.

Verification after the slice: requested `py_compile` passed; 396 unit tests
passed.

## Slice 3 — provider plugins

- Added `ProviderSpec`, `HomeSpec`, and `ProviderContext`, plus one module for
  each original provider. Every module exports `SPEC` and
  `discover/usage/quota/cost/health`.
- The registry derives provider IDs, capabilities, metadata, home specs, the
  standard-coverage tuple, and the ordered provider payload map.
- The registry applies `sanitize_payload` at the usage boundary and to quota
  and cost payloads. Provider exceptions remain isolated.
- `PROVIDER_HOME_SPECS` now covers every registered provider.
- Added a sandboxed full-snapshot compatibility test. It constructs the same
  fixture-backed snapshot through the former literal-map algorithm and the new
  registry, removes only delivery timestamps, serializes both, and compares the
  bytes.

Verification after the slice: requested `py_compile` passed; 398 unit tests
passed.

### Packaging decision

`scripts/build_public_release.py` uses `git archive`; it has no concatenation
stage. Following the TED fallback, provider modules live under
`bin/agentcat_providers/` and `bin/agentcat` imports them. `bin/agentcat` remains
the sole executable entrypoint, while the provider package is an additional
runtime artifact in the installed source tree. The release builder, public
candidate validator, and local installer now reject a source tree without the
registry, and public validation compiles every provider module. The contract
records this source-tree plugin layout.

## Slice 5 — seam proof

### JetBrains AI

- Scans declared local JetBrains config roots, including the requested
  `~/Library/Preferences/JetBrains*/` layout, and also current macOS,
  Linux, and Windows JetBrains config roots.
- Picks the newest `options/AIAssistantQuotaManager2.xml` and decodes the
  JSON-valued `quotaInfo` and `nextRefill` options.
- Emits one account-aggregate monthly credit quota. It performs no network
  request and exposes no token/cost claims.

The cached option shape follows the local-only implementation documented by
[Krisvid](https://github.com/zsoltjanes/Krisvid-AI-Usage-Monitor/blob/master/src/providers/jetbrains/quota.js);
JetBrains documents the quota as monthly AI Credits renewed every 30 days.

### Hermes Agent

- Opens `~/.hermes/state.db` read-only and prefers the current
  `session_model_usage` accounting table, falling back to compatible `sessions`
  columns.
- Aggregates local period/model token metadata only; it never reads message or
  prompt bodies.
- Emits `actualCostUSD` and a cost slice marked `estimated: false`, sourced only
  from Hermes' stored `actual_cost_usd` values.

The schema choice follows the upstream
[Hermes state schema](https://github.com/NousResearch/hermes-agent/blob/main/hermes_state_schema.py),
which defines token classes plus estimated and actual USD cost columns.

### Marginal file count

Each new provider took **6 files** when shared files are counted for each:

1. its provider module;
2. the registry;
3. the connector contract;
4. the comprehensive contract fixture;
5. the sandboxed provider test file; and
6. its dedicated local-data fixture.

The generic home/cost callbacks and one capability-count assertion were shared
seam updates, not provider-specific branches in `bin/agentcat`.

Verification after the slice: requested `py_compile` passed; 400 unit tests
passed. Both provider fixtures execute inside `tests/sandbox.py` redirection,
and their full snapshot tests replace `urlopen` with a failure sentinel.

## Final checks

- `python3 -m py_compile bin/agentcat scripts/install.py`
- `python3 -m unittest discover -s tests`
- `git diff --check`
- Connector version unchanged
- No push performed
