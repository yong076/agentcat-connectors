# Reflect R1 report — connector `reflect` module

Slice R1 of *Agent Cat Reflect (돌아보기)* (TED: `agent-cat/docs/ted/agentcat-reflect-ted.md`, §5 R1).
Branch `yong076/reflect-r1` on connector 26.36.3; nine `[R1]` commits followed by five `[R1b]` commits, one per requested step; `CONNECTOR_VERSION` not bumped.

Everything lives in `bin/agentcat` (single-file daemon, section `# Reflect (돌아보기)` placed just before the
loopback host guard) plus `tests/test_reflect.py` (75 tests), the copied app fixtures under `tests/fixtures/reflect/`, and ten Reflect sandbox paths in `tests/sandbox.py`.

## 1. Files and functions

| Step | Commit | Functions / symbols |
| --- | --- | --- |
| Config + indexer | `66c6036` | `REFLECT_DB`, `REFLECT_CONFIG_FILE`, `REFLECT_SCRATCH_DIR`, `REFLECT_DEFAULT_CONFIG`, `reflect_config`, `reflect_write_default_config`, `reflect_session_files` (reuses `claude_journal_files_by_root` + `codex_session_files`), `reflect_session_id` (reuses `_session_identity`), `reflect_project_name`, `reflect_is_system_text`, `_reflect_iter_jsonl` (streaming, byte-needle prefilter), `reflect_parse_claude_session` (reuses `parse_claude_usage_line` + dedupe key), `reflect_parse_codex_session` (reuses `codex_session_token_class_amounts`, `normalized_model_name`), `_reflect_finish_digest` (cost via `estimate_cost`), `reflect_session_row`, `reflect_length_bucket` |
| Scrub + sampling | `85486f6` | `SECRET_VALUE_PATTERNS` (split out of `SENSITIVE_VALUE_PATTERNS`, which now composes it — hook sanitizing unchanged), `reflect_scrub_secrets`, `reflect_elide_paths`, `reflect_scrub_text`, `reflect_scrub_digest`, `reflect_sample_turns`, `reflect_fit_budget`, `reflect_prompt_payload` |
| Analyzer | `c24870e` | `REFLECT_FRICTION_CATEGORIES` (9), `REFLECT_ATTRIBUTIONS` (3), `REFLECT_PATTERN_CATEGORIES` (8), `REFLECT_PROMPT_DIMENSIONS` (5), `reflect_analysis_schema`, `reflect_build_prompt`, `reflect_extract_json`, `reflect_validate_analysis`, `ReflectRunner`, `ClaudeNativeRunner`, `reflect_parse_claude_cli_output`, `reflect_find_claude_binary`, `reflect_runner`, `reflect_analyze_payload` (retry once with nudge) |
| Store | `449fd89` | `reflect_init_db` (tables `sessions`, `analyses`, `weekly_synthesis`, `rules`, `state`; WAL; 0600), `reflect_upsert_session`, `reflect_sync`, `reflect_get_session`, `reflect_get_analysis`, `reflect_store_analysis` (upsert on `(session_id, length_bucket)`), `reflect_analyses_created_on`, `reflect_list_sessions`, `reflect_all_analyses`, `reflect_normalize_rule`, `reflect_store_synthesis`, `reflect_get_synthesis`, `reflect_state_get/set`, `reflect_analyze_session` |
| Weekly synthesis | `7877fbb` | `reflect_iso_week_key`, `reflect_week_of` (local-time ISO week), `reflect_fluency_score`, `reflect_synthesize_week` (pure), `reflect_week_items`, `reflect_week`, `REFLECT_TELEMETRY_FIELDS`, `reflect_telemetry_summary` |
| HTTP + CLI | `967b42c` | `reflect_http_get`, `reflect_http_post`, `_reflect_error_payload`; routes in `AgentCatHandler.do_GET/do_POST` (after the existing host-header guard, same `send_json` envelope); `command_reflect` + `reflect` subparser |
| Scheduler | `37f2ee8` | `reflect_daemon_idle` (`terminal_activity_snapshot().motionStage == "sleeping"`), `reflect_scheduler_due` (pure), `reflect_pending_sessions`, `reflect_nightly`, `reflect_scheduler_tick`, `reflect_scheduler_loop`; `run_daemon` writes the default `reflect.json` once and starts the loop thread |
| Privacy test | `184fe6c` | tests only |
| Per-tool synthesis + checks | `952ac0f` | `REFLECT_NEXT_CHECKS_BY_CATEGORY`, `REFLECT_REWORK_CATEGORIES`; `reflect_synthesize_week` emits `byTool`, localized `toolComparison`, and 3–5 evidence-backed `nextChecks` while retaining `rules` |
| HTML report | `c310d26` | `REFLECT_REPORT_COPY`, `reflect_report_html`, `AgentCatHandler.send_html`; `GET /reflect/report.html?week=&lang=` |
| Multi-tool readers | `5c26e06` | shared `Turn{role,text,tool,ts}` via `_reflect_shared_turn`; readers/parsers for OpenCode, Copilot CLI, Grok, Gemini CLI, Cursor, and counts-only Kimi; defensive counts-only fallback; `counts_only` DB migration |
| Automated sessions | `371be3d` | `reflect_claude_automated_marker`, `reflect_is_orca_workspace`; `automated` DB migration; prompt-quality averages exclude automated sessions by default |
| R1b report | documentation commit | this updated implementation report |

Error classes: `ReflectError` → `ReflectNotFound` (404), `ReflectRunnerError` (503), `ReflectAnalysisError` (502, carries `errors`).

## 2. Rule → test matrix

| Rule (from the brief / TED) | Test(s) in `tests/test_reflect.py` |
| --- | --- |
| Session list across Claude/Codex reuses the connector's session paths; subagent journals excluded | `IndexerTests.test_session_files_cover_claude_and_codex_and_skip_subagents` |
| Digest shape `{id, tool, project (basename), started_at, ended_at, turns, user_turns, tool_calls, tokens, cost_usd, path}`; assistant text capped at 400 chars; tool counts by name; cost from the connector's own pricing; streamed chunks with the same `requestId` counted once | `test_claude_digest_shape`, `test_codex_digest_shape`, `test_project_name_is_basename_only`, `test_session_row_has_no_transcript` |
| System/meta/sidechain/`/command` entries and the analyzer's own sessions are not user turns | `test_claude_digest_shape`, `test_claude_string_content_and_no_user_turns`, `test_claude_analyzer_own_sessions_are_skipped`, Codex `environment_context`/developer in `test_codex_digest_shape` |
| Only files modified within `indexDays` are indexed; prefilter skips non-digest lines | `test_session_files_skip_stale_files`, `test_prefilter_skips_lines_without_needles` |
| Length buckets short/medium/long | `test_length_bucket` |
| No seeded secret survives (sk-, sk-ant, ghp_, github_pat_, xoxb-, AKIA, Bearer, JWT, `.env` `KEY=value`, `key: value`) | `ScrubTests.test_no_seeded_secret_survives`, `test_scrub_keeps_ordinary_text` |
| Absolute paths → basenames (POSIX, `~`, Windows, dash-encoded) | `test_paths_become_basenames`, `test_scrub_digest_removes_path_and_scrubs_turns` |
| Sampling keeps first turn, last turn, ≤30 user turns around the largest tool bursts; gaps marked; short sessions untouched | `test_sampling_keeps_first_last_and_largest_bursts`, `test_short_sessions_are_not_sampled`, `test_fit_budget_shrinks_text` |
| Prompt to the runner carries no secret, no absolute path, no home dir | `AnalyzerTests.test_prompt_payload_is_clean`, `PrivacyTests.test_only_the_runner_leaves_the_process` |
| Rubric: 9 frictions × 3 attributions, 8 patterns, 5 prompt dimensions with before/after, 3-sentence style, 3 rules; strict schema | `test_prompt_carries_rubric_and_schema`, `test_validate_accepts_valid_and_computes_mean`, `test_validate_rejects_bad_shapes`, `test_validate_trims_rules_and_evidence` |
| claude-native runner: `claude -p --model sonnet --output-format json`, prompt on stdin, 120 s timeout, scratch cwd, nested-session env stripped; accepts result object **and** event array | `test_claude_native_runner_command_stdin_cwd_env_timeout`, `test_parse_claude_cli_output_object_and_array`, `test_claude_native_runner_timeout_and_missing_binary`, `test_runner_factory` |
| Validation + exactly one retry with a "return only JSON" nudge | `test_retry_once_with_nudge_then_success`, `test_fails_after_two_invalid_replies` |
| SQLite at `~/.agentcat/reflect.db`, owner-only; idempotent by `(session_id, length_bucket)` | `StoreTests.test_store_analysis_is_idempotent_per_bucket`, `test_analyze_session_stores_then_caches`, `PrivacyTests.test_reflect_db_is_owner_only` |
| Sync skips unchanged files, re-indexes appended ones | `test_sync_indexes_and_skips_unchanged` |
| Sessions window + analyzed flags | `test_list_sessions_window_and_flags` |
| Weekly synthesis: counts, cost summed once per session per category, attribution counts, prompt-quality trend, fluency formula, clamping | `SynthesisTests.test_counts_costs_rates_and_fluency`, `test_fluency_formula`, `test_empty_week`, `test_iso_week_helpers` |
| 3–5 rules deduplicated by normalized text | `test_rules_dedupe_by_normalized_text`, `WeekStoreTests.test_synthesis_and_rules_persist` |
| `GET /reflect/week?iso`, `/reflect/sessions?days`, `/reflect/session/{id}`, `/reflect/rules?week`, `POST /reflect/analyze/{id}`, host-header guard, JSON errors | `HttpTests.test_week_sessions_session_rules_endpoints`, `test_post_analyze_uses_configured_runner`, `test_post_sync`, `test_host_guard_applies_to_reflect_routes` |
| `agentcat reflect {sync\|analyze <id>\|week [iso]}` | `CliTests.test_reflect_subcommands` |
| Nightly 03:00 local once per day, only when idle; Sunday 18:00 weekly once per week | `SchedulerTests.test_due_logic_nightly_and_weekly`, `test_nightly_waits_for_idle_then_runs_once`, `test_weekly_synthesis_stored_on_sunday`, `test_daemon_idle_uses_motion_stage` |
| Cap 20/day: 50 new sessions → 20 analyzed, 30 queued; one call per session per bucket | `test_nightly_cap_analyzes_twenty_and_queues_the_rest`, `test_nightly_one_call_per_session_per_bucket`, `test_nightly_stops_when_runner_unavailable` |
| Off unless `reflect.json` has `enabled: true`; default file written on first daemon start, never overwritten | `test_default_config_written_once_and_disabled`, `test_tick_is_off_unless_enabled`, `test_run_daemon_writes_default_config_and_starts_scheduler` |
| Telemetry-shaped struct is exactly `{week, fluency_score, friction_top_category, pattern_top_category, sessions_analyzed}`; the runner is the only outbound shape | `PrivacyTests.test_telemetry_summary_has_exactly_five_fields`, `test_only_the_runner_leaves_the_process` (network patched to raise, `subprocess.run` patched to raise in every test's `setUp`) |
| Weekly per-tool rows contain sessions/tokens/cost/rework/prompt quality; comparison exists only for 2+ tools; checks are localized and evidence-backed; `rules` remains | `SynthesisTests.test_counts_costs_rates_and_fluency`, `test_next_checks_and_comparison_are_localized`, `test_empty_week`, `HttpTests.test_week_sessions_session_rules_endpoints` |
| Self-contained report HTML renders the four localized headings plus tool comparison, with inline CSS and no external URLs/assets | `HttpTests.test_self_contained_html_report_has_localized_four_parts` |
| TED §4.1 presentation keys and JSON types match all six app fixtures; detail is flat, unanalyzed rows carry `analysis: null`, history contains eight weeks, and tool ids use the app vocabulary | `ReflectContractTests` |
| OpenCode, Copilot, Grok, Gemini, and Cursor readers yield exactly `{role,text,tool,ts}`; Cursor is copied before SQLite open | `IndexerTests.test_opencode_reader_yields_shared_turns_and_digest`, `test_copilot_reader_yields_shared_turns_and_digest`, `test_grok_reader_yields_shared_turns_and_digest`, `test_gemini_reader_yields_shared_turns_and_digest`, `test_cursor_reader_copies_sqlite_and_yields_shared_turns` |
| Every new reader path is discovered; unknown schemas and Kimi wire-without-user-text become counts-only rows instead of aborting sync | `IndexerTests.test_session_files_cover_all_multi_tool_readers`, `test_kimi_wire_and_unknown_format_are_counts_only` |
| Claude SDK/system provenance, Codex `codex_exec`, and Orca-dispatched workspaces set `automated: true`; rows remain listed and prompt-quality averages exclude them by default | `IndexerTests.test_automated_markers_cover_claude_codex_and_orca_workers`, `SynthesisTests.test_automated_sessions_are_listed_but_excluded_from_quality_averages` |

Suite: `python3 -m unittest discover -s tests < /dev/null` → **Ran 546 tests, OK** (75 Reflect tests). The implementation stays within Python 3.9 syntax and was also checked with `py_compile`.

## 3. Live smoke on this Mac (2026-09-05)

```
$ python3 bin/agentcat reflect sync --json           # first run, real ~/.claude + ~/.codex
{"empty": 38, "indexed": 3118, "sessions": 3118, "skipped": 0}
real 25.17s   (3,156 files within indexDays=30; Codex slice is 5.5 GB on this machine)

$ python3 bin/agentcat reflect sync                  # second run
reflect: indexed 3, unchanged 3115, empty 39, total 3118
real 3.30s

$ python3 bin/agentcat reflect analyze claude:d9445c5a-9a59-4614-9dac-ba2d72438ecc
real 87.05s
cached: false  bucket: short
meta: {"runner": "claude-native", "model": "sonnet", "attempts": 1, "cost_usd": 0.212002, "duration_ms": 86759}
session: {"tool": "claude", "project": "trappist-chart-daily-ingest", "turns": 12, "user_turns": 6,
          "tool_calls": 180, "tokens": 33102439, "cost_usd": 45.167609, "model": "claude-fable-5",
          "started_at": "2026-08-30T03:57:36.916000Z"}
prompt_quality_mean: 2.0  {clarity 2, context 2, constraints 2, success_criteria 2, scope 2}
frictions: repeated_corrections×2 (environmental), scope_creep (environmental),
           missing_context (user_actionable), tool_or_environment_failure (environmental),
           wrong_or_buggy_output (ai_capability)
patterns:  context_provided, explicit_constraints, verification_requested, incremental_scoping, delegation_to_tools
candidate_rules[0]: "Before dispatching a worker task, compile the complete set of required changes
                     (copy text, exact diffs, validation rules) into a single message; …"
working_style (first sentence): "This session is not a live human conversation but a fully automated
                     Orca-dispatched worker run, where 'user' turns are coordinator relay messages …"

$ python3 bin/agentcat reflect analyze claude:d9445c5a-…   # again
cached: true   real 0.52s

$ python3 bin/agentcat reflect week 2026-W35
{"week": "2026-W35", "sessions_analyzed": 1, "user_turns": 6, "cost_usd_total": 45.1676,
 "fluency_score": 0.325, "friction_rate_per_10_turns": 10.0, "pattern_rate_per_10_turns": 8.333,
 "frictions_top": [{"category": "repeated_corrections", "count": 2, "sessions": 1, "cost_usd": 45.1676,
                    "attribution": {"user_actionable": 0, "ai_capability": 0, "environmental": 2}}, …],
 "patterns_top": [{"category": "context_provided", "count": 1, "sessions": 1, "cost_usd": 45.1676}, …],
 "prompt_quality": {"mean": 2.0, "by_dimension": {…}, "trend": [{"session_id": "…", "started_at": "…", "prompt_quality": 2.0}]},
 "rules": [{"text": "…", "count": 1, "sessions": ["claude:d9445c5a-…"]}, …3 total], "stored": false}

reflect.db after the smoke: mode 0600, sessions 3118 (claude 108 / codex 3010), analyses 1
(short, claude-native, sonnet, pq 2.0, 6 frictions, 5 patterns, created_day 2026-09-05)
```

Two observations from the original real-data smoke, now addressed by R1b:

- The sampled session was an Orca coordinator-dispatched worker run; it is now tagged `automated: true` from the Orca workspace path. Claude `entrypoint` / `promptSource` and Codex `originator` markers cover SDK/exec automation as well. Automated rows remain visible, but their prompt-quality scores do not enter default weekly or per-tool averages.
- Short sessions saturate the friction rate (6 frictions on 6 user turns = 10 per 10 turns → clamped to 1), which drags the fluency score to the prompt-quality term alone. The TED already says "shown as a trend, never as a single judgement"; the app should render the trend and the raw rates, not the score alone.

R1b inspected real local schemas without printing message bodies. Gemini (`~/.gemini/tmp/**/chats/*.jsonl`), Grok (`~/.grok/sessions/*/*/updates.jsonl`), Cursor (`~/.cursor/chats/*/*/store.db`, copied before opening), and Kimi `wire.jsonl` all passed a structural read-only smoke. This Mac's OpenCode has migrated to `~/.local/share/opencode/opencode.db`; its old `storage/session/**` files are absent, although the legacy file reader is fixture-covered. Copilot is configured but has no `~/.copilot/session-state` directory or `events.jsonl` on this Mac; its persisted event names/fields were checked against GitHub's official Copilot CLI/SDK documentation and covered synthetically.

The HTTP layer was smoked through the test suite's real `ThreadingHTTPServer` rather than by starting a second daemon on this Mac — two daemons would race on `~/.agentcat/latest-snapshot.json`. The production daemon (26.36.3) does not yet carry these routes.

## 4. Caps

| Cap | Value | Where |
| --- | --- | --- |
| Index window | files modified within 30 days (`reflect.json` `indexDays`; `0` = unlimited) | `reflect_session_files` |
| Codex file list | inherits `LOCAL_PROVIDER_MAX_FILES` (10,000, newest first) | `codex_session_files` |
| Text per turn | 400 chars (`REFLECT_TEXT_PER_TURN`) | `_reflect_finish_digest` |
| Sampling | first turn + last turn + ≤30 user turns by tool burst (`REFLECT_SAMPLE_USER_TURNS`); ≤6 assistant turns per exchange (first 3 + last 3) | `reflect_sample_turns` |
| Prompt budget | 60,000 transcript chars, shrinking per-turn text to 200/100/60 | `reflect_fit_budget` |
| Analyzer | 120 s timeout, 1 retry, `--model sonnet` | `ClaudeNativeRunner`, `reflect_analyze_payload` |
| Evidence / notes / rewrites | 300 / 600 / 600 chars; ≤20 frictions, ≤20 patterns, 3 rules per session | `reflect_validate_analysis` |
| Daily analyses | 20 (`dailyCap`), counted by local `created_day` so restarts cannot double-spend | `reflect_nightly` |
| Per session | one analyzer call per `(session_id, length_bucket)`; buckets short <10, medium <40, long ≥40 user turns | `reflect_analyze_session` |
| Nightly candidates | sessions started within `lookbackDays` (14), newest `ended_at` first | `reflect_pending_sessions` |
| Weekly rules | 3–5, deduplicated by normalized text | `reflect_synthesize_week` |
| `/reflect/sessions?days` | 1…365 | `reflect_http_get` |
| POST body | 256 KB (existing `read_json_body`) | `AgentCatHandler` |

Scheduler windows: nightly fires during the 03:xx local hour once per day when idle (busy ticks retry every minute until the hour ends); weekly fires during Sunday 18:xx local once per ISO week. State (`last_nightly_date`, `last_weekly_week`) lives in `reflect.db.state`.

## 5. Exclusions

- **Kimi remains counts-only**: real `wire.jsonl` records provide status/token protocol metadata but no user text. It is listed with tokens/cost where available, `counts_only: true`, and cannot be analyzed.
- OpenCode's implemented full reader is for the TED-specified legacy `storage/session` + `message` + `part` tree. The current local installation has migrated its sessions into `opencode.db`, which this slice intentionally does not index as multiple sessions through the one-file/one-session cursor contract.
- Unknown or changed OpenCode/Copilot/Grok/Gemini/Cursor formats produce a session row with `counts_only: true` and file-mtime timestamps; one bad source never aborts the rest of the index.
- Claude subagent journals (`…/<session>/subagents/*.jsonl`), `isSidechain` and `isMeta` entries, `/command` output, `<system-reminder>`, interrupted-request markers; Codex `developer` messages and `<environment_context>` / `<user_instructions>` blocks.
- The analyzer's own `claude -p` sessions (cwd `~/.agentcat/reflect-scratch`, prompt prefixed with `AGENTCAT_REFLECT_ANALYSIS_REQUEST`) are never indexed, so Reflect cannot analyze itself.
- Codex token accounting sums `last_token_usage` per `token_count` event (the competitors' delta); it does not replay the connector's divergent-totals correction, so a Codex session's `cost_usd` can differ slightly from the ledger on sessions where Codex rewrote its totals.
- Unknown models price at 0 (`estimate_cost` returns `None`); `tokens` still counts them.
- `agentcat reflect analyze` remains an explicit CLI action and runs regardless of `enabled`; app-facing HTTP reads, report HTML, and analyze return `reflect_disabled` when the config file explicitly disables Reflect. The scheduler remains gated as before.
- Weekly synthesis is computed on request (`GET /reflect/week`) and only persisted by the Sunday job or `reflect week --store`; `stored` in the payload says which.

### Reader capability matrix

| Tool | Reader status | Source and behavior |
| --- | --- | --- |
| Claude Code | Full | `~/.claude/projects/**/*.jsonl`; existing streamed-usage dedupe and system/meta filtering |
| Codex | Full | `~/.codex/sessions/**/*.jsonl` + archived sessions; existing per-turn token accounting |
| OpenCode | Full for legacy file store | `~/.local/share/opencode/storage/session/**` joined to `message/<session>` and `part/<message>`; unknown metadata is counts-only |
| Copilot CLI | Full | `~/.copilot/session-state/*/events.jsonl`; persisted `user.message`, `assistant.message`, `tool.execution_start`, and usage/shutdown events |
| Grok | Full | `~/.grok/sessions/*/*/updates.jsonl`; message chunks, tool names, existing usage parser; never reads tool `rawInput`/titles |
| Gemini CLI | Full | `~/.gemini/tmp/**/chats/session-*.json*`; user/gemini records, tool calls, token object |
| Cursor | Full | Cursor chat `store.db` JSON blobs and workspace `state.vscdb` `ItemTable`; always copied to a private temp directory and opened read-only |
| Kimi | Counts-only | `~/.kimi-code/sessions/**/wire.jsonl`; token/status records only because real wire files contain no user text |

## 6. Final JSON shapes for the app (WP35)

All timestamps are UTC ISO-8601 with `Z`. `null` is possible wherever marked. Responses use the daemon's existing envelope (`Content-Type: application/json`, keys sorted, 2-space indent).

The camelCase TED §4.1 fields below are now the authoritative presentation contract for both `/reflect/*` and `agentcat reflect ... --json`. Tool ids are exactly `claude-code`, `codex`, `gemini`, `kimi`, `cursor`, `opencode`, `copilot`, or `grok`; the storage id `claude` is presented as `claude-code`.

### Authoritative presentation contract

`GET /reflect/week?iso=2026-W36`:

```json
{
  "week": "2026-W36",
  "generatedAt": "2026-09-05T09:12:44.512Z",
  "sessionsAnalyzed": 12,
  "sessionsTotal": 19,
  "fluencyScore": 0.71,
  "promptQuality": {
    "clarity": 4.2, "context": 3.6, "constraints": 4.4,
    "successCriteria": 3.5, "scope": 3.9
  },
  "trend": [{"week": "2026-W29", "fluencyScore": 0.58}],
  "frictions": [{
    "category": "missing-context", "count": 4, "attribution": "user-actionable",
    "costTokens": 41000000, "costUSD": 12.4,
    "example": {"sessionId": "claude:<uuid>", "quote": "≤ 20 words", "fix": "…"}
  }],
  "patterns": [{
    "category": "explicit-constraints", "count": 6, "driver": "user-driven",
    "example": {"sessionId": "claude:<uuid>", "quote": "…"}
  }],
  "byTool": [{
    "tool": "claude-code", "sessions": 7, "tokens": 512000000,
    "costUSD": 41.2, "reworkRate": 0.09, "promptQuality": 4.2
  }],
  "toolComparison": "one sentence or null",
  "nextChecks": [{
    "id": "c1", "text": "…", "derivedFrom": "missing-context",
    "evidenceSessionIds": ["claude:<uuid>"]
  }],
  "workingStyle": "Three sentences from the latest analyzed session in the week.",
  "rules": [{
    "id": "r1", "text": "…", "target": "AGENTS.md",
    "evidenceSessionIds": ["claude:<uuid>"]
  }]
}
```

`trend` always contains the eight ISO weeks ending in the requested week, oldest first; a week without analyzable data has `fluencyScore: null`. `sessionsTotal` counts indexed sessions in the requested week. Category token and USD costs count a session once per category. Weekly examples use the first evidence-bearing occurrence, and quotes are capped at 20 words.

`GET /reflect/sessions?days=7` returns `{"sessions": [row, ...]}` (and the compatibility `days` field), where a row is:

```json
{
  "id": "claude:<uuid>", "tool": "claude-code", "project": "agent-cat",
  "startedAt": "…Z", "endedAt": "…Z", "turns": 41, "userTurns": 12,
  "toolCalls": 31, "tokens": 1832000, "costUSD": 3.1, "automated": false,
  "analysis": null
}
```

An analyzed row replaces `null` with `{"promptQuality": 4.1, "frictionCount": 2, "patternCount": 3, "headline": "one sentence"}`. `analysis` is always present and exactly `null` for an unanalyzed session.

`GET /reflect/session/{id}` is the row itself at the response root. Its `analysis` is `null` or the full shape:

```json
{
  "frictions": [{"category": "…", "count": 1, "attribution": "…", "costTokens": 100, "costUSD": 0.01,
                 "example": {"sessionId": "…", "quote": "…", "fix": "…"}}],
  "patterns": [{"category": "…", "count": 1, "driver": "user-driven",
                "example": {"sessionId": "…", "quote": "…"}}],
  "promptQuality": {
    "clarity": {"score": 4, "before": "…", "after": "…"},
    "context": {"score": 4, "before": "…", "after": "…"},
    "constraints": {"score": 4, "before": "…", "after": "…"},
    "successCriteria": {"score": 4, "before": "…", "after": "…"},
    "scope": {"score": 4, "before": "…", "after": "…"}
  },
  "workingStyle": "…", "rules": [{"id": "r1", "text": "…", "target": "AGENTS.md", "evidenceSessionIds": ["…"]}],
  "analyzedAt": "…Z", "runner": "claude-native", "lengthBucket": "long"
}
```

`POST /reflect/analyze/{id}` returns `{"queued": true}` when queued, otherwise the same flattened detail with compatibility execution metadata. `GET /reflect/rules?week=...` returns the presentation `rules` array above. An explicitly disabled `reflect.json` makes app-facing GETs, report HTML, and analyze return `403 {"error": "reflect_disabled"}`. Other app error ids are `session_not_found`, `runner_unavailable`, and `analysis_failed`.

### One-release compatibility fields (deprecated)

For connector 26.36.x compatibility, the current snake_case R1 fields remain alongside the presentation fields for one release. This includes `sessions_analyzed`, `fluency_score`, `prompt_quality`, `frictions_top`, `patterns_top`, `generated_at`, `started_at`, `user_turns`, `tool_calls`, `cost_usd`, the wrapped detail `session`, and `analysis_meta`. Enriched `rules`, `frictions`, and `patterns` retain their old member data (`count`/`sessions`, and `evidence`/`turn`/`note`) where the old and new contracts share an array key. Error payloads carry the former id in `legacy_error`. These compatibility fields are deprecated and may be removed in the release after WP35.

### Deprecated R1 session fields

```json
{
  "id": "claude:<uuid>",              // tool-prefixed; stable across re-syncs
  "tool": "claude" | "codex" | "opencode" | "copilot" | "grok" | "gemini" | "cursor" | "kimi",
  "project": "my-repo",               // basename only, never a path
  "path": "/abs/path/to/session.jsonl", // for the daemon's own use; do not display or forward
  "started_at": "2026-08-30T03:57:36.916000Z" | null,
  "ended_at":   "…Z" | null,
  "turns": 12, "user_turns": 6, "tool_calls": 180, "tokens": 33102439,
  "cost_usd": 45.167609,
  "tool_call_counts": {"Bash": 90, "Read": 60, "Edit": 30},
  "model": "claude-fable-5" | null,   // dominant model by tokens
  "counts_only": false,                // true when transcript text is unavailable/unsupported
  "automated": false,                  // true for SDK/exec/Orca-dispatched work
  "length_bucket": "short" | "medium" | "long",
  // only in /reflect/sessions:
  "analyzed": true, "analyzed_bucket": "short" | null,
  "prompt_quality": 2.0 | null, "frictions": 6 | null, "patterns": 5 | null
}
```

`GET /reflect/sessions?days=7` → `{"days": 7, "sessions": [row, …]}` newest first.

### Deprecated R1 analysis fields

```json
{
  "schema_version": 1,
  "frictions": [{"category": "<one of 9>", "attribution": "user_actionable|ai_capability|environmental",
                 "evidence": "≤300 chars, user's words", "turn": 3 | null, "note": "…"}],
  "patterns":  [{"category": "<one of 8>", "evidence": "…", "turn": 2 | null, "note": "…"}],
  "prompt_quality": {
    "clarity":          {"score": 1-5, "before": "…", "after": "…"},
    "context":          {…}, "constraints": {…}, "success_criteria": {…}, "scope": {…}
  },
  "prompt_quality_mean": 2.0,
  "working_style": "three sentences",
  "candidate_rules": ["…", "…", "…"]
}
```

Friction categories: `missing_context, unclear_request, scope_creep, ai_misunderstanding, wrong_or_buggy_output, tool_or_environment_failure, repeated_corrections, permission_or_approval_stalls, context_loss_or_compaction`.
Pattern categories: `clear_goal_upfront, context_provided, explicit_constraints, verification_requested, incremental_scoping, good_correction, reuse_of_prior_work, delegation_to_tools`.

Compatibility fields on `GET /reflect/session/{id}` also include `{"session": row, "analysis_meta": {"runner", "model", "created_at", "length_bucket"} | null}`; the authoritative session row and `analysis` are at the response root.

The synchronous analyze response retains `{"ok": true, "session_id", "length_bucket", "cached": bool, "session": row, "meta": {…}}` beside the authoritative flattened detail. When `cached` is true, `meta` is `{"runner", "model", "created_at"}`. This call blocks for up to ~2 × 120 s.

### Deprecated R1 weekly fields

`lang` is `ko` (default), `en`, `ja`, or `zh-Hans`. It localizes `toolComparison` and `nextChecks[].text`; unsupported values return 400.

```json
{
  "schema_version": 1, "week": "2026-W36",
  "sessions_analyzed": 1, "user_turns": 6, "cost_usd_total": 45.1676,
  "fluency_score": 0.325 | null,                  // 0…1; null when nothing analyzed
  "prompt_quality": {"mean": 2.0 | null,
                     "by_dimension": {"clarity": 2.0 | null, "context": …, "constraints": …, "success_criteria": …, "scope": …},
                     "trend": [{"session_id": "…", "started_at": "…Z", "prompt_quality": 2.0, "automated": false}]},  // sorted by started_at
  "friction_rate_per_10_turns": 10.0, "pattern_rate_per_10_turns": 8.333,   // raw, unclamped
  "frictions_total": 6, "patterns_total": 5,
  "frictions_top": [{"category": "…", "count": 2, "sessions": 1, "cost_usd": 45.1676,
                     "attribution": {"user_actionable": 0, "ai_capability": 0, "environmental": 2}}],  // sorted by count, then cost
  "patterns_top":  [{"category": "…", "count": 1, "sessions": 1, "cost_usd": 45.1676}],
  "byTool": [{"tool": "codex", "sessions": 7, "tokens": 512000000, "costUSD": 41.2,
              "reworkRate": 0.18, "promptQuality": 3.9 | null}],
  "toolComparison": "Codex의 재작업 세션 비율은 25%로 Claude의 10%보다 높아요." | null,
  "nextChecks": [{"id": "c1", "text": "첫 문장에 대상 파일·화면과 원하는 결과를 함께 적어요.",
                  "derivedFrom": "missing_context", "evidenceSessionIds": ["claude:…"]}],
  "rules": [{"text": "…", "count": 1, "sessions": ["claude:…"]}],   // 3–5, deduplicated
  "generated_at": "…Z", "stored": false
}
```

`cost_usd` on a legacy category is the summed `cost_usd` of the sessions where it occurred (each session once per category). `reworkRate` is the share of analyzed sessions for a tool containing `ai_misunderstanding`, `wrong_or_buggy_output`, or `repeated_corrections`. `toolComparison` is non-null only with at least two tool rows. `nextChecks` has 3–5 rows when friction exists (empty for an empty/friction-free week), ranked by friction and backed by the sessions where that category occurred. Presentation rule objects retain legacy `count` and `sessions` members.

Automated sessions still contribute sessions, tokens, cost, rework, friction, and pattern metrics. Their prompt scores remain in `prompt_quality.trend` with `automated: true`, but are excluded by default from the overall and `byTool[].promptQuality` averages. Fluency = `0.5 × (mean−1)/4 + 0.3 × (1 − clamp(friction_rate)) + 0.2 × clamp(pattern_rate)`.

The rules endpoint retains `week`, while every rule carries both presentation members (`id`, `target`, `evidenceSessionIds`) and deprecated members (`count`, `sessions`).

### HTML report

`GET /reflect/report.html?week=2026-W36&lang=ko` returns `text/html; charset=utf-8`. It is a self-contained document using system fonts and inline CSS only, with the four ordered sections **한눈에 / 잘한 점 / 개선점 / 앞으로의 체크 방식** (localized in all four languages) and the tool comparison when present. Every dynamic field is HTML-escaped; there are no external URLs or assets.

### Errors

App-facing errors use `reflect_disabled`, `session_not_found`, `runner_unavailable`, or `analysis_failed`. During the compatibility release, renamed errors include the former value in `legacy_error`; validation details and messages remain additive. Bad input and unexpected failures retain `reflect_bad_request` and `reflect_failed`; the host guard still answers `403 {"error": "forbidden", "message": "host_not_allowed"}`.

### Telemetry-shaped summary (not sent by this slice; the only shape that ever may be)

`reflect_telemetry_summary(week)` → `{"week": "2026-W36", "fluency_score": 0.325 | null, "friction_top_category": "…" | null, "pattern_top_category": "…" | null, "sessions_analyzed": 1}` — asserted to have exactly these five keys.

### Config `~/.agentcat/reflect.json`

```json
{"enabled": false, "runner": "claude-native", "dailyCap": 20, "nightlyHour": 3,
 "weeklyWeekday": 6, "weeklyHour": 18, "lookbackDays": 14, "indexDays": 30}
```

Written once by the daemon on first start; unknown keys ignored; malformed file → defaults (disabled).
