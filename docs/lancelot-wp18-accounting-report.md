# WP18 connector token-accounting report

## Audit basis

This report uses `docs/snapshot-2026-09-04.json`, captured by connector 26.36.0
with schema 4 / contract 1 at `2026-09-04T13:56:15.344177Z`. In the capture
timezone (Asia/Seoul), the local calendar date was 2026-09-04. The corrected
values below are calculations from that audit snapshot, not a regenerated
post-fix snapshot.

The common window definition is now:

- `today`: the current local calendar day;
- `week`: seven calendar days including today (`today - 6` through today);
- `month`: thirty calendar days including today (`today - 29` through today).

README and `usageCoverage.periodWindow` use those exact semantics.

## Snapshot calculations

| Metric | Before | Corrected calculation | Corrected |
| --- | ---: | --- | ---: |
| Grok all | 983,087,448 | `inputTokens + outputTokens` = 508,070,770 + 2,766,345 | 510,837,115 |
| Claude week | 628,388,488 | sum of `dailyTokens` for 2026-08-29 through 2026-09-04 | 573,735,876 |
| Codex week | 1,754,790,112 | sum of `dailyTokens` for 2026-08-29 through 2026-09-04 | 1,538,423,226 |

For Grok, the old result is exactly 510,837,115 + 470,056,192 cached-read
tokens + 2,194,141 reasoning tokens = 983,087,448. xAI reports cached reads
inside input and reasoning inside output, so those two fields remain useful
breakdowns but are not added to the total. Raw history is rebuilt after the
Grok accounting-state revision changes; the already-inflated snapshot
`dailyTokens` must not be used as corrected Grok period totals.

The corrected Claude week is 91,131,813 + 52,040,726 + 188,354,290 +
2,866,233 + 0 (no 2026-09-02 bucket) + 373,590 + 238,969,224 = 573,735,876.
The old eight-date window additionally included 2026-08-28 (54,652,612).

The corrected Codex week is 1,021,829,849 + 77,932,997 + 48,041,802 +
243,808,344 + 88,938,748 + 29,687,670 + 28,183,816 = 1,538,423,226.
The old eight-date window additionally included 2026-08-28 (216,366,886).
Codex's SQLite lifetime floor is intentionally undated: 25,586,793,905
`tokens.all` - 25,265,340,511 summed `dailyTokens` = an `undatedFloor` of
321,453,394.

Reproduction:

```bash
jq '
  .providers as $p |
  {grok:{before:$p.grok.tokens.all,
         after:($p.grok.tokens.inputTokens+$p.grok.tokens.outputTokens)},
   claude_week:{before:$p.claude.tokens.week,
     after:([$p.claude.dailyTokens|to_entries[]|
       select(.key>="2026-08-29" and .key<="2026-09-04")|.value]|add)},
   codex_week:{before:$p.codex.tokens.week,
     after:([$p.codex.dailyTokens|to_entries[]|
       select(.key>="2026-08-29" and .key<="2026-09-04")|.value]|add)},
   codex_undated_floor:($p.codex.tokens.all-([$p.codex.dailyTokens[]]|add))}
' docs/snapshot-2026-09-04.json
```

## Defect disposition and fixtures

| Defect | Resolution | Regression fixture class | Commit |
| --- | --- | --- | --- |
| D1 | Trust Grok `totalTokens`; otherwise use input + output, retain nested classes as information, and revision/rebuild Grok history. | `GrokAccountingTests` | `9e9be0d [WP18] D1 fix Grok token accounting` |
| D3 | Route provider windows through `periods_from_daily_tokens` with exact local 1/7/30-day boundaries. | `PeriodWindowTests` | `ed38633 [WP18] D3 centralize calendar usage windows` |
| D4 | Persist Claude all-time classes plus dated/model/hourly history, omit rolling cursor fields, and recompute snapshot windows from daily buckets. | `ClaudeWindowTests` | `a79af37 [WP18] D4 recompute Claude snapshot windows` |
| D2 | Build period summary token classes from period buckets and make reported estimated cost equal the reported class-cost sum. | `PeriodInsightTests` | `1939fa7 [WP18] D2 reconcile period insight classes` |
| D5 | Floor Claude lifetime total by the sum of merged journal/stats-cache daily buckets. | `ClaudeDailyFloorTests` | `41d20ee [WP18] D5 floor Claude lifetime by daily usage` |
| D6 | Clamp each provider's all-time insight contribution to `min(sum(model totals), tokens.all)` without changing model attribution. | `AllInsightClampTests` | `174eb55 [WP18] D6 clamp all-time insight providers` |
| D7 | Seed Gemini periods with zeroes and always publish `today`, `week`, and `month`. | `GeminiZeroWindowTests` | `d0f82ce [WP18] D7 preserve Gemini zero windows` |
| D8 | Publish nonnegative Codex `tokens.undatedFloor = all - sum(dailyTokens)`. | `CodexUndatedFloorTests` | `257405f [WP18] D8 expose Codex undated token floor` |
| D9 | Price provider period classes with the highest-token representative model while retaining the per-model provider-cost sum. | `ProviderCostTests` | `c6643a6 [WP18] D9 price provider classes with dominant model` |
| D10 | Use an Antigravity conversation database's mtime when a generation lacks an embedded timestamp; an embedded timestamp still wins. | `AntigravityMtimeTests` | `04a8f04 [WP18] D10 bucket Antigravity rows by database mtime` |
| D11 | Publish Copilot `tokens.estimated: true` and token-weighted `estimatedShare` when character-estimated records contribute; omit them for exact-only usage. | `CopilotEstimateTests` | `1c30c52 [WP18] D11 expose Copilot estimate provenance` |

`ConnectorContractTests` covers the additive contract/fixture changes. The new
`usage.tokenAccounting.v1` capability owns
`providers.codex.tokens.undatedFloor`, `providers.copilot.tokens.estimated`,
and `providers.copilot.tokens.estimatedShare`. The healthy fixture exercises
all three fields. Contract version 1, snapshot schema 4, and connector version
26.36.0 are unchanged.

## Verification status

- Final root verification passed 26 WP18 fixture tests and 7 contract tests.
  Provider tests redirect connector paths through `tests/sandbox.py`.
- The complete stdin-closed suite passed 419 tests with
  `python3 -m unittest discover -s tests < /dev/null`.
- `python3 -m py_compile bin/agentcat scripts/install.py`, `git diff --check`,
  the eleven-commit order/trailer audit, and the snapshot calculation assertion
  all passed. No connector, schema, or contract version was bumped.
