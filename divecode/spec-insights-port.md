# Connector spec — Insights derivation port (Q1 + Q3)

> Tight spec for moving Insights derivation from Swift (agent-cat) to Python (this repo). Resolves agent-cat/divecode/audit-insights-v1.md Q1 (split-bucket pricing) and Q3 (move to connector before Windows ships).

## Goal

Add an `insights` object to the connector snapshot so:
- Cost is computed with per-bucket pricing (no more 3-5× over-estimate from lumping cache tokens)
- Mac, Windows, and any future client decode rather than re-derive
- `schemaVersion` bumps to `3` to signal the addition (additive — old clients ignoring `insights` still work)

## Decisions (locked from audit + this spec)

**Directives**:
- D1: Pricing is split-bucket — `{input, output, cache_read, cache_write}` per model
- D2: Unknown models keep `cost = null` (don't guess). Emit `pricing_missing` finding.
- D3: Pricing source is bundled Python dict for v1 (Q3 from audit). LiteLLM remote refresh deferred to a later bolt.
- D4: `insights.status: ok | unavailable | error` so client knows whether to render the section
- D5: `schemaVersion: 3` is additive only — `insights` is the only new top-level key
- D6: Derivation is pure (no I/O, no side effects). Easy to test, easy to call from `build_snapshot()`.

**Rejected**:
- R1: Reusing Swift table verbatim — its blended single-rate per model is the bug we're fixing
- R2: LiteLLM remote fetch in v1 — too much new failure surface for one bolt
- R3: Embedding individual prompt/response in payload — privacy violation

**Constraints**:
- C1: Mac, Windows, CLI all read the same JSON — schema must be stable
- C2: Snapshot writes must not block on pricing errors (degrade to `unavailable`)
- C3: No PII / prompts / paths in the insights payload
- C4: Backward compat — `schemaVersion: 2` clients see no `insights` field but everything else works

## Slice plan (3 slices)

### Slice A — Pricing module (~120 lines + tests)
**Goal**: Add `MODEL_PRICING` dict and `estimate_cost(model, tokens)` returning split-bucket cost.

**Layer**: domain (pricing table) + usecase (cost calc helper)

**Test cases (drive RED)**:
- `test_pricing_known_model_returns_split_cost`
- `test_pricing_cache_read_cheaper_than_input`
- `test_pricing_unknown_model_returns_none`
- `test_pricing_model_alias_normalization`
- `test_pricing_zero_tokens_returns_zero_not_none`

**Files**: `bin/agentcat` (added), `tests/test_agentcat.py` (added test class)
**Verification**: `python -m unittest tests.test_agentcat -k Pricing`

### Slice B — derive_insights() (~180 lines + tests)
**Goal**: Pure function `derive_insights(snapshot) -> dict` that:
- Iterates `snapshot["providers"]`
- Per provider/model: sums `inputTokens / outputTokens / cacheReadInputTokens / cacheCreationInputTokens`
- Calls `estimate_cost()` per bucket → per-model + per-provider cost
- Builds summary (today/week/month totals, top provider, top model)
- Emits findings: `pricing_missing`, `provider_no_limits`, `high_weekly_usage`

**Test cases**:
- `test_derive_handles_empty_snapshot`
- `test_derive_splits_cache_tokens_correctly` (the Q1 fix verification — same input gives lower cost than Swift's blended approach)
- `test_derive_emits_pricing_missing_for_unknown_model`
- `test_derive_sums_across_providers`
- `test_derive_top_model_and_provider_picked`

**Files**: `bin/agentcat`, `tests/test_agentcat.py`
**Verification**: `python -m unittest tests.test_agentcat -k Insights`

### Slice C — Schema v3 + integration (~40 lines + tests)
**Goal**: Wire `derive_insights()` into `build_snapshot()`, bump schemaVersion.

**Test cases**:
- `test_build_snapshot_includes_insights_object`
- `test_build_snapshot_schema_version_is_3`
- `test_insights_status_unavailable_when_no_providers`
- `test_insights_status_error_caught_not_raised` (snapshot write must not crash if derive fails)

**Files**: `bin/agentcat`, `tests/test_agentcat.py`
**Verification**: full `python -m unittest tests.test_agentcat`

## After this bolt

Next bolts (separate sessions):
- **agent-cat**: delete `AgentInsights.derive()` Swift code, replace with decode of `snapshot["insights"]`. Tests updated. (Half day per status doc.)
- **agentcat-telemetry**: admin metrics can now read `insights.summary` directly without re-derivation
- **agent-cat-windows**: insights section becomes free — just decode + render
