<!--
STATUS: spec-only (no connector code committed). The per-provider work orders in §4 are
the executable unit — one WO = one PR. Base = agentcat-connectors `main` @ 26.28.0.
Generated 2026-07-06/07 from source analysis of ccusage, agentsview, CodexBar + the
connector seam map. Every WO's exact token keys are confirmed from competitor source;
`capture-fixture` is a per-provider verification step, not a research prerequisite.
-->

# Provider Absorption Master Plan

**Goal.** Absorb the full *local-readable* provider coverage of the leading
open-source AI-usage trackers (ccusage, CodexBar, agentsview, Claude-Code-Usage-Monitor,
sniffly, VibeMeter) into `agentcat-connectors` so Agent Cat detects every AI CLI/agent
a user actually runs — with our local-first, privacy-preserving guarantees intact.

**This document is a build spec.** Each provider section below is a self-contained work
order: another model (Codex/Claude) should be able to open one section, implement it as
one PR, and pass the acceptance tests **without re-researching**. Read the "How to use"
and "Ground rules" sections once, then execute sections in the PR-sequence order.

---

## 0. Ground rules (read once, apply to every work order)

### 0.1 Base branch & scope
- **Base branch: `main`** (connector `26.28.0`, HEAD `7776b22`). NOT `feat/wave2-daemon`
  (which is strictly behind `main` and lacks all local-provider work) and NOT the
  stalled `feat/local-llm-pro-tracking` worktree merge.
- The 12 local providers prototyped in PR #11 / `feat/local-llm-pro-tracking`
  (cursor, kiro, roo-code, kilo-code, goose, qwen, crush, cline, continue, pearai, llm,
  gptme) + ollama/lmstudio + GLM/z.ai attribution **do not exist on `main`**. They are
  **ported here as fresh modules** (§Port set), not merged — the stalled merge is
  abandoned (8 conflict hunks; see the architecture plan's donor-branch policy).
- One provider = one PR. No PR may touch install scripts, `.github/workflows/*`, `dist/*`,
  the auto-update code, or the telemetry-emission boundary.

### 0.2 License hygiene — clean-room reimplementation ONLY
- `agentcat-connectors` is licensed **PolyForm Shield 1.0.0** (source-available, NOT OSI).
  This repo currently vendors **zero** third-party code (no `THIRD_PARTY_NOTICES.md`
  exists; `NOTICE` carries only the PolyForm required-notice lines).
- All six analyzed competitors are **MIT** (ccusage © ryoppippi; CodexBar/VibeMeter ©
  Peter Steinberger; agentsview © Kenn Software; Claude-Code-Usage-Monitor © Maciej;
  sniffly © Chip Huyen). **Facts are not copyrightable** — file paths, env-var names,
  on-disk schemas, and detection techniques may be freely reimplemented.
- **RULE: reimplement from facts, never copy source.** Do not paste or line-by-line
  translate any competitor code into `bin/agentcat`. If a future provider requires
  literally adapting MIT source, STOP — that requires introducing a
  `THIRD_PARTY_NOTICES.md` + attribution mechanism that does not exist yet; raise it
  first. PR #11 followed this clean-room pattern; every work order here mandates it.
- **Never** reference GPL/AGPL-licensed trackers — copyleft is incompatible with
  PolyForm Shield. (All six current sources are MIT, so this is a forward guardrail.)

### 0.3 Privacy / local-first (non-negotiable)
- Read only **local metadata**: token counts, model ids, timestamps, quota numbers.
  Never read, store, or transmit prompt/response content, file paths, repo names, or
  project names into telemetry. (Snapshot may hold local paths for the app; telemetry
  must not — the existing boundary applies unchanged.)
- Every new reader opens SQLite **read-only** (`file:...?mode=ro`, immutable where
  possible) — never mutate an agent's DB.
- Respect the size/count caps (`LOCAL_PROVIDER_PARSE_BYTES` = 16 MB,
  `LOCAL_PROVIDER_MAX_FILES` = 10k). Env-var path override first, then platform default.

### 0.4 Every provider ships with tests
- A provider PR without a fixture test **fails review**. Two test layers:
  - Unit test in `tests/test_agentcat.py` (temp-`HOME` pattern, §Testing).
  - A `write_<provider>(home)` fixture writer in the ported e2e harness
    (`scripts/verify_providers_e2e.py`, §Testing) + a registry tuple.

---

## 1. Insertion-seam reference (all line numbers are `main` @ 26.28.0)

Every work order cites these by name. This is the map of *where* provider code plugs in.

| Seam | Symbol | Line | Notes |
|---|---|---|---|
| Provider assembly | `build_snapshot()` | L8195 | providers dict literal **L8211-8218**; each entry `disabled_provider_snapshot(id) if disabled else <id>_snapshot()` |
| Google split | `split_google_cli_snapshots()` / `antigravity_provider_snapshot()` | L7226 / L7193 | gemini+antigravity pair, built L8206-8210 |
| Local-usage coverage | `attach_local_usage_coverage()` | L5225 | per-provider `descriptors` dict **L5229-5255** — new local providers extend this |
| Desktop app roots | `DESKTOP_APP_SPECS` / `desktop_app_data_roots()` | L991-1017 / L1123 | `DESKTOP_APP_PROVIDER_IDS=("claude","codex")` L989 |
| Journal/dir resolvers | `claude_journal_project_dirs()` / `codex_session_roots()` / `opencode_data_dir()` | L5784 / L4317 / L3848 | env-first path pattern |
| Token normalization | `TOKEN_KEYS` / `TOKEN_CLASS_KEYS` / `extract_tokens()` | L156-174 / L175 / L1441 | lowercased source key → canonical camelCase |
| Local-usage helper | `empty_provider_usage()` / `add_usage_metrics()` | L3550-3566 / L3738-3767 | the shape + the metrics accumulator |
| JSONL/file utils | `iter_jsonl_file()` / `recent_files()` / `read_text_limited()` | L3832 / L4016 / L3815 | |
| Pricing | `MODEL_PRICING` / `_lookup_pricing()` / `merged_pricing_table()` | L195-216 / L219-250 / L472 | LiteLLM feed already shipped (`pricing.feed`) |
| Enable/disable | `configured_provider_entries()` / `disabled_provider_snapshot()` / `CONFIG_PROVIDER_IDS` | L1043-1061 / L1318-1327 / L988 | **settings-based** on main (NOT providers.json) |
| Live limits | `runtime_limits()` / `prefer_live_limits()` | L3507-3516 / L3491 | add `<id>_live_limits()` only for quota-API providers |

**Must-port helpers** (exist only on `feat/local-llm-pro-tracking`, port to `main` in the
first infra PR, §Port set PR-0): `finish_local_usage_snapshot()` (L3253-3259),
`read_json_bounded()`, `safe_file_mtime()`, `recent_candidate_files()`,
`vscode_global_storage_dirs(app_names, extension_id)`, `parse_cline_task()`,
`cline_family_snapshot()` factory.

**The minimal new local-file provider** (port the `qwen_snapshot` shape, ~34 lines):
```
def <id>_snapshot():
    root = <env-first path resolver>()
    result = empty_provider_usage(source=str(root))
    files = recent_candidate_files(root, "<glob>")
    events = 0
    for path in files:
        for entry in iter_jsonl_file(path):          # or read_json_bounded / sqlite ro
            if not <is a usage record>: continue
            metrics = {"inputTokens": nonnegative_int(entry[...]),
                       "outputTokens": ..., "cacheReadTokens": ...}   # canonical keys
            model = normalized_model_name(entry.get("model"))
            when = parse_timestamp(entry.get("ts")) or safe_file_mtime(path)
            if add_usage_metrics(result, model, metrics, when): events += 1
    return finish_local_usage_snapshot(result, events, sources_seen=bool(files))
```
Then register it in the `build_snapshot()` providers dict and (if enable/disable-aware)
add its id to `CONFIG_PROVIDER_IDS`.

---

## 2. Coverage & gap matrix

Legend — **we (main)**: ✅ ship · 🟡 ship but inaccurate (accuracy fix) · ⛏ port from donor
· ➕ new absorb · 🕳 structure-only (no local tokens) · ☁ out-of-scope (cloud). Sources:
cc=ccusage, av=agentsview, cb=CodexBar.

| provider | we (main) | competitors | local read method (token keys) | tier | WO |
|---|---|---|---|---|---|
| claude | ✅ | cc,cb,av,+2 | `~/.claude/projects/**/*.jsonl` `message.usage.*` | T1 | — |
| codex | 🟡→✅ | cc,cb,av | **fix**: `~/.codex/sessions/**/rollout-*.jsonl` `last_token_usage.*` per-class over sqlite floor | T1 | WO-I |
| gemini | 🟡→✅ | cc,cb,av | **fix**: `~/.gemini/tmp/*/chats/session-*.json[l]` `usageMetadata.*` zero-setup + OTEL | T1 | WO-J |
| antigravity | ✅(26.28) | cb,av | OAuth Code Assist quota (shipped); +local-IDE fallback (§4bis) | T1 | — |
| copilot | 🟡→✅ | cc,cb,av | **fix**: `~/.copilot/otel/**/*.jsonl` real tokens over char-estimate | T2 | WO-K |
| opencode | ✅ | cc,cb,av | `~/.local/share/opencode/opencode.db` `tokens.*`; +channel-DB/JSON fallback | T2 | — |
| cursor | ⛏ | cb,av,vibe | `state.vscdb` cursorDiskKV + `~/.cursor/projects/**/agent-transcripts/*.jsonl` | T2 | WO-T |
| roo-code/kilo-code/cline | ⛏ | cc(kilo) | VS Code ext `ui_messages.json` (`cline_family_snapshot`) | T2 | WO-T |
| kiro (IDE) | ⛏ | cb,av | globalStorage `.chat` (char-est) | T2 | WO-T |
| goose | ⛏ | cc,av | `sessions.db` `accumulated_*_tokens` | T2 | WO-T |
| qwen | ⛏ | cc,av | `~/.qwen/projects/*/chats/*.jsonl` `usageMetadata.*` | T2 | WO-T |
| crush | ⛏ | — | per-project `crush.db` via XDG registry | T2 | WO-T |
| continue/pearai | ⛏ | — | `dev_data/…tokensGenerated.jsonl` | T2 | WO-T |
| llm (simonw) | ⛏ | — | `io.datasette.llm/logs.db` responses | T2 | WO-T |
| gptme | ⛏ | — | `gptme/logs/*/conversation.jsonl` metadata.usage | T2 | WO-T |
| ollama | ⛏ | — | opt-in proxy → `~/.agentcat/ollama/usage.jsonl` | T3 | WO-T |
| lmstudio | ⛏ | — | `~/.lmstudio/conversations/*.json` `promptTokensCount`/`predictedTokensCount` | T3 | WO-T |
| GLM / z.ai (attribution) | ⛏ | cb(cloud) | `is_glm_model()` label on `glm-*` local usage | T2 | WO-T |
| **hermes** | ➕ | cc,av | `~/.hermes[/sessions]/state.db` `sessions`(input/out/cache/**reasoning**/**actual_cost**) | T2 | WO-A |
| **pi** | ➕ | cc,av | `~/.pi/agent/sessions/**/*.jsonl` `usage.{input,output,cache*}` | T2 | WO-B |
| **kimi** (local) | ➕ | cc,av | `~/.kimi/sessions/**/wire.jsonl` `token_usage.*` + config model | T2 | WO-C |
| **openclaw** | ➕ | cc,av | `~/.openclaw/…/*.jsonl` (+archives) `usage.{input,output,cacheRead,cacheWrite}` | T2 | WO-D |
| **qclaw** | ➕ | av | `~/.qclaw/agents/**/sessions/*.jsonl` (openclaw twin) | T2 | WO-L |
| **kilo** (db) | ➕ | cc | `~/.local/share/kilo/kilo.db` `message.data.tokens.*` (≠ kilo-code) | T2 | WO-E |
| **workbuddy** | ➕ | av | `~/.workbuddy/projects/**/*.jsonl` `providerData.usage.*` (OpenAI aliases) | T2 | WO-M |
| **forge** | ➕ | av | `~/.forge/.forge.db` `context.messages[].usage.*` + `metrics` | T2 | WO-N |
| **piebald** | ➕ | av | `piebald/app.db` `messages.{input,output,reasoning,cache_*}_tokens` | T2 | WO-O |
| **warp** | ➕ | av,cb(cloud) | `warp.sqlite` `token_usage[].{warp,byok}_tokens` (session-total only) | T2 | WO-Q |
| **amp** | ➕ | cc,av,cb | `~/.local/share/amp/threads/*.json` `usage.{inputTokens,outputTokens,cache*}` | T2 | WO-P |
| **droid/factory** | ➕ | cc,cb(cloud) | `~/.factory/sessions/**/*.settings.json` `tokenUsage.*` | T2 | WO-R |
| **codebuff** | ➕ | cc,cb | `~/.config/manicode*/projects/**/chat-messages.json` `usage.*` | T2 | WO-S |
| **jetbrains-ai** | ➕ | cb | `…/JetBrains/<IDE>/options/AIAssistantQuotaManager2.xml` (quota %, no tokens) | quota | WO-F |
| **windsurf** | ➕ | cb | `Windsurf/User/globalStorage/state.vscdb` cachedPlanInfo (quota) | quota | WO-G |
| **opencode-go** | ➕ | cb | `opencode.db` providerID='opencode-go' cost→% (quota) | quota | WO-H |
| openhands/zencoder/cortex/kiro-cli/positron/iflow | 🕳 | av | conversation structure only — **no local tokens** | defer | §4 note |
| openai/azure/alibaba×2/manus/minimax/moonshot/kimik2/zai/t3chat/augment/grok | ☁ | cb | API-key/cookie/RPC only | OOS | §5 |

**Counts**: ship 6 · accuracy-fix 3 (WO-I/J/K) · port 14+attribution (WO-T) · new
token-bearing 13 (A/B/C/D/E/L/M/N/O/P/Q/R/S) · new quota 3 (F/G/H) · structure-only 7
(defer) · cloud OOS ~15. **Net-new local providers ≈ 30** (14 port + 13 token + 3 quota) +
3 accuracy fixes + 2 capability derivations (WO-U).

Prior art built upon (do NOT redo): `agent-cat/docs/competitor-coverage-analysis.md`
(2026-06-05) — per-repo counts (CodexBar 45, agentsview 29, ccusage 15), 10 gaps, 11-step
plan, 17 techniques. Corrections: its LiteLLM step **already shipped** on main 26.28.0; its
cited line numbers are worktree-stale — use §1.

---

## 3. Priority & PR sequence

Ordered high-impact / low-effort first. Three accuracy fixes on providers we already
ship come before breadth (they fix wrong numbers, not just add rows).

Ordered high-impact/low-effort first. The 3 accuracy fixes come before breadth (they fix
*wrong numbers* on the top-3 surfaces, not just add rows). Each row = 1 PR unless noted.

| PR | WO | Work | Impact | Effort |
|---|---|---|---|---|
| PR-0 | — | Port infra helpers to main (`finish_local_usage_snapshot`, `read_json_bounded`, `safe_file_mtime`, `recent_candidate_files`, `vscode_global_storage_dirs`, `parse_cline_task`, `cline_family_snapshot`) + **e2e harness in CI** + widen `TOKEN_KEYS` for `prompt_tokens`/`completion_tokens`/`…_details.*` | infra (enables all) | S/M |
| PR-1 | WO-I | **Codex sessions-JSONL** per-class reader (prefer over sqlite floor; cursor-incremental) | **high** (fixes wrong Codex numbers) | M |
| PR-2 | WO-J | **Zero-setup Gemini chats** reader (removes telemetry-hook precondition) | **high** (activation) | M |
| PR-3 | WO-K | **Copilot OTEL** reader (real tokens; drop char-estimate) — investigate keys first | med | S |
| PR-4 | WO-A | **hermes** (sqlite; reasoning + native cost — highest-fidelity add) | med-high | S |
| PR-5..7 | WO-N/O/Q | **forge, piebald, warp** (agentsview sqlite, token-bearing) | med | M each |
| PR-9..12 | WO-B/D/L/M | **pi, openclaw, qclaw, workbuddy** (JSONL, token-bearing) | med | S–M |
| PR-13..14 | WO-C/E | **kimi (local), kilo (db)** | med | S |
| PR-15..17 | WO-P/R/S | **amp, droid, codebuff** (keys confirmed; JSON/settings readers) | med | S–M |
| PR-18..N | WO-T | **Port the 14 donor providers** + GLM/z.ai attribution (1 PR each; batch the VS Code family) — close PR #11/#12 as superseded | breadth | S each |
| PR-N+1..3 | WO-F/G/H | **jetbrains, windsurf, opencode-go** local quota | med (local quota) | S–M |
| PR-last | WO-U | Derived **burn-rate/ETA/reset + P90 auto-quota** in `derive_insights` | high (capability) | M |

Dependency: PR-0 gates all breadth (helpers + harness). Accuracy fixes (PR-1..3) are
independent of PR-0 and can start immediately. Structure-only providers (openhands,
zencoder, cortex, kiro-cli/ide, positron, iflow) are **deferred** — no local tokens.

---

## 4. Provider work orders

Each is one PR. Fields: **id+name / mechanism / source+format+sample / extracted data /
insertion point / port-not-copy+license / tests / difficulty+risk / atomic steps.** WO
ids map to the PR sequence in §3. Every WO's token keys are confirmed from competitor
source; run `agentcat capture-fixture` once per provider as a sanity check before merging.

> **Canonical token keys** (`add_usage_metrics` metrics dict): `inputTokens`,
> `outputTokens`, `cacheReadTokens`, `cacheCreationTokens` (= cache-write), plus reasoning
> folded into output for pricing. Map each source's raw keys → these.

> **Cost policy for absorbed providers**: mirror ccusage's per-provider divergence, but
> our pricing is `merged_pricing_table()` (LiteLLM feed already shipped). Rule of thumb:
> if the source records a real cost, trust it when > 0; otherwise price from our table.
> Never invent a cost for a model the table lacks — leave `pricing_missing` (existing
> behavior). Reasoning tokens are added to output for pricing, like every competitor.

### WO-A · hermes-agent (NEW)  ⟶ id `hermes`
- **Mechanism**: read-only SQLite, session-granularity. Env-first path.
- **Source + format**: env `HERMES_HOME` (comma-sep home dirs) → `<home>/state.db`;
  default `~/.hermes/state.db`. Table `sessions`:
  `SELECT id, model, billing_provider, started_at, message_count, input_tokens,
  output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
  estimated_cost_usd, actual_cost_usd FROM sessions WHERE model IS NOT NULL AND
  TRIM(model) != ''`. `started_at` = REAL epoch **seconds** (>1e12 ⇒ already ms).
  Sample row: `model="claude-sonnet-4-…"`, `billing_provider="anthropic"`,
  `input_tokens=1200, output_tokens=300, cache_read_tokens=50, cache_write_tokens=20,
  reasoning_tokens=10, actual_cost_usd=0.34, estimated_cost_usd=0.12`.
- **Extracted data**: input/output/cacheRead/cacheCreation(=cache_write)/reasoning direct.
  Model ← `model`; session ← `id`; ts ← `started_at`. **Cost: prefer `actual_cost_usd`,
  else `estimated_cost_usd`, both clamped ≥0; a recorded 0 or missing → re-price from our
  table (subscription-included sessions record 0).** Skip rows where all 5 token cols AND
  cost are 0. No quota.
- **Insertion**: new `hermes_snapshot()` (SQLite ro reader; port `read_json_bounded` not
  needed — use `sqlite3` `mode=ro`); register in `build_snapshot()` providers dict
  (§1 L8211-8218); add `"hermes"` to `CONFIG_PROVIDER_IDS` (L988); add descriptor to
  `attach_local_usage_coverage` (L5229-5255). Cost path: reuse the pricing helper used by
  codex/claude cost estimation; only price when recorded cost ≤ 0.
- **License**: clean-room from schema facts (MIT source). No copy.
- **Tests**: unit — write a temp `state.db` with 3 rows incl. one all-zero (must-skip) and
  one subscription-zero-cost (asserts re-pricing); assert tokens + `actual_cost` trusted.
  e2e — `write_hermes(home)`.
- **Difficulty/risk**: S. Risk: collides in name with the user's *unrelated* internal
  "trappist-hermes" — this is a different public "hermes-agent" CLI; keep id `hermes`,
  displayName "Hermes".
- **Steps**: (1) path resolver + ro-sqlite reader → metrics; (2) cost trust/​reprice;
  (3) register + config id + coverage descriptor; (4) unit + e2e fixtures.

### WO-B · pi-agent (NEW)  ⟶ id `pi`
- **Mechanism**: JSONL glob, message-granularity. Env-first + real project grouping.
- **Source + format**: env `PI_AGENT_DIR` (comma-sep) → recursive `*.jsonl`; default
  `~/.pi/agent/sessions`. Record (line pre-filter: contains `"usage"` and `"message"`;
  accept when top-level `type` absent/`"message"`, `message.role=="assistant"`,
  `message.usage` present):
  ```json
  {"type":"message","timestamp":"2026-01-02T00:00:00.000Z",
   "message":{"role":"assistant","model":"gpt-5",
     "usage":{"input":100,"output":50,"cacheRead":10,"cacheWrite":20,"totalTokens":185,"cost":{"total":0.01}}}}
  ```
  Token keys: `usage.input/output/cacheRead/cacheWrite/totalTokens`; cost `usage.cost.total`.
  Top-level `timestamp` (RFC3339) is **required** (skip line without it).
- **Extracted data**: tokens direct; `totalTokens` surplus over the parts → reasoning/
  output. **Cost ← `usage.cost.total` directly (recorded-only; no lookup).** Model ←
  `message.model`. Session ← filename stem after first `_`. Project ← path segment after a
  `sessions` component (real project). Skip zero-token records.
- **Insertion**: new `pi_snapshot()` using `recent_candidate_files` + `iter_jsonl_file` +
  `add_usage_metrics`; register + `CONFIG_PROVIDER_IDS` + coverage descriptor.
- **License**: clean-room. **Tests**: unit fixture with a no-timestamp must-skip line;
  e2e `write_pi(home)`. **Difficulty**: S.

### WO-C · kimi (local wire logs) (NEW)  ⟶ id `kimi`
- **Mechanism**: depth-exact JSONL "wire" logs + config.json for model. Env-first.
- **Source + format**: env `KIMI_DATA_DIR` (comma-sep) default `~/.kimi`;
  files `<root>/sessions/<group>/<session>/wire.jsonl` (basename exactly `wire.jsonl`,
  **exactly 3 components below `sessions/`**). Model ← `<root>/config.json` key `"model"`
  (default `"kimi-for-coding"`). Record (line must contain `"StatusUpdate"` +
  `"token_usage"`; skip `"type":"metadata"`):
  ```json
  {"timestamp":1770983427.123,
   "message":{"type":"StatusUpdate","payload":{
     "token_usage":{"input_other":100,"output":50,"input_cache_read":10,"input_cache_creation":20,"total":180},
     "message_id":"msg-1"}}}
  ```
  Keys: `input_other`(input), `output`, `input_cache_read`(cacheRead),
  `input_cache_creation`(cacheCreation), `total`. `timestamp` = epoch **seconds**.
- **Extracted data**: as mapped; total surplus → reasoning. Model from config (not
  per-record — historical sessions get today's model; document this). **Cost: priced from
  our table** (moonshot/kimi family). Session ← wire file's parent dir.
- **Insertion**: new `kimi_snapshot()`; register + config id + descriptor.
- **License**: clean-room. **Tests**: fixture with the 3-deep path + config.json; a
  `metadata`-type must-skip line; e2e `write_kimi(home)`. **Difficulty**: S/M (path-depth
  filter + config lookup). **Risk**: don't confuse with cloud "Kimi/Moonshot API" (out of
  scope §5).

### WO-D · openclaw / qclaw (NEW)  ⟶ id `openclaw`
- **Mechanism**: JSONL with **stateful model tracking**, multiple dot-dir roots.
- **Source + format**: env `OPENCLAW_DIR` (comma-sep) then merge existing defaults
  `~/.openclaw`, `~/.clawdbot`, `~/.moltbot`, `~/.moldbot`; files recursive where basename
  suffix is exactly `.jsonl` or `.jsonl.deleted.<ts>` / `.jsonl.reset.<ts>` (archived
  count too). Line pre-filter: contains `model_change` | `model-snapshot` | `usage`. Two
  kinds: model events `{"type":"model_change","provider":"…","modelId":"…"}` update the
  running model; usage events:
  ```json
  {"type":"message","message":{"role":"assistant",
    "usage":{"input":1660,"output":55,"cacheRead":108928,"cacheWrite":0,"totalTokens":110643,"cost":{"total":0.02}},
    "timestamp":1769753935279}}
  ```
  Keys: `usage.input/output/cacheRead/cacheWrite/totalTokens`; cost `usage.cost.total`.
- **Extracted data**: tokens direct; totalTokens surplus → reasoning. **Cost ← recorded
  `usage.cost.total` only (no lookup).** Model ← last `model_change` (carry state across
  lines). ts ← `message.timestamp` (ms or RFC3339) or file mtime. Session ← filename
  before `.jsonl`.
- **Insertion**: new `openclaw_snapshot()` with a running-model accumulator across the
  file's lines; register + config id + descriptor.
- **License**: clean-room. **Tests**: fixture exercising model_change-then-usage ordering
  + one `.jsonl.reset.` archived file; e2e `write_openclaw(home)`. **Difficulty**: M
  (stateful parse).

### WO-E · kilo (ccusage `kilo.db`) (NEW — distinct from `kilo-code`)  ⟶ id `kilo`
- **Note**: this is a **different tool** from the VS Code `kilocode.kilo-code` extension we
  port as `kilo-code`. ccusage's `kilo` reads its own SQLite. Ship both; different ids.
- **Source + format**: env `KILO_DATA_DIR` (comma-sep) default `~/.local/share/kilo`; DB
  `<dir>/kilo.db`, `SELECT id, session_id, data FROM message`; `data` JSON:
  ```json
  {"role":"assistant","providerID":"anthropic","modelID":"claude-…","time":{"created":1767312000000},
   "tokens":{"input":100,"output":50,"reasoning":5,"total":234,"cache":{"read":10,"write":20}},"cost":0.02}
  ```
  Keys: `tokens.input/output/reasoning/total`, `tokens.cache.read/write`, `modelID`
  (required), `time.created` (seconds **or** ms — `<1e12` ⇒ ×1000), `cost`.
  Row **dropped if `time.created` missing**. `role` must be `"assistant"`.
- **Extracted data**: direct; total surplus → reasoning/output. **Cost: recorded `cost`
  when present (a stored 0.0 is kept), else priced.** 
- **Insertion**: new `kilo_snapshot()` (ro-sqlite); register + config id + descriptor.
- **License**: clean-room. **Tests**: fixture incl. a no-`time.created` must-skip row;
  e2e `write_kilo(home)`. **Difficulty**: S.

### Port-set cross-references (ccusage variants of providers we already have on the donor branch)
- **goose** (port-set): donor reads `sessions.db` `accumulated_input/output_tokens`.
  ccusage adds a fuller path — env `GOOSE_PATH_ROOT` → `<root>/data/sessions/sessions.db`
  + macOS `~/Library/Application Support/goose/…` + `~/.local/share/Block/goose/…`, and
  reads `accumulated_*` with `total/input/output` fallback + `model_config_json.model_name`.
  **When porting goose, widen its path list and column fallbacks to match.**
- **qwen** (port-set): donor + ccusage agree on `~/.qwen/projects/<p>/chats/*.jsonl`,
  `usageMetadata.{promptTokenCount,candidatesTokenCount,thoughtsTokenCount,cachedContentTokenCount,totalTokenCount}`,
  `type:"assistant"` filter. Port as-is; env `QWEN_DATA_DIR`.
- **opencode** (we ship it): ccusage adds a channel-DB fallback (`opencode-<channel>.db`,
  first sorted) + legacy `storage/message/**/*.json` tree, and model-alias normalization
  (`gemini-3-pro-high→…-preview`, `claude-sonnet-4.5→…-4-5`). **Consider hardening our
  `opencode_snapshot` with the channel-DB + JSON fallback (dual-mode storage).**

### Cross-source enrichments for WO-A..E (two trackers read the same tools — merge the richer)
- **hermes (WO-A)**: agentsview ALSO reads a hermes `state.db` `sessions` table, and it is
  richer than ccusage's — it carries `reasoning_tokens` and a guarded cost policy
  (`actual_cost_usd` > `estimated_cost_usd`, but `cost_status=="included"` + a real
  `cost_source` ⇒ trust a stored 0; else re-price). agentsview's default root is
  `~/.hermes/sessions/state.db` (env `HERMES_SESSIONS_DIR`), ccusage's is `~/.hermes/state.db`
  (env `HERMES_HOME`) — **probe both roots**. Columns to read (superset):
  `id, model, billing_provider/source, started_at(REAL epoch s), message_count,
  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
  estimated_cost_usd, actual_cost_usd, cost_status, cost_source`. This is the **only**
  absorbed provider that ships real reasoning tokens AND native cost — highest-fidelity add.
- **pi (WO-B)**: agentsview confirms the usage object accepts BOTH nested
  `usage.cache.{read,write}` (OpenCode-style) and flat `usage.cacheRead/cacheCreation`
  (Anthropic-style) — handle both. Coverage rule: an empty `{}` usage → leave tokens unset
  (row skipped); an explicit all-zero `{input:0,output:0}` → preserve as known-zero.
- **kimi (WO-C)**: two different "kimi" locals exist — ccusage's wire-log
  (`~/.kimi/sessions/<g>/<s>/wire.jsonl`, model from `config.json`) and agentsview's
  (`~/.kimi/sessions/**/wire.jsonl`, StatusUpdate `token_usage.output` **session-total only,
  no model**). Ship the ccusage variant (per-class + model) as `kimi`; it supersedes.
- **openclaw (WO-D)**: agentsview also ships **qclaw** (a rename-fork, see WO-L). openclaw
  uses short usage keys `usage.{input,output,cacheRead,cacheWrite}`; discard `usage.cost`
  and re-price from model (agentsview's documented choice — the model name is load-bearing).

### WO-I · Codex sessions-JSONL reader (ACCURACY FIX on shipped `codex`)  ⟶ high impact
- **Problem**: we sum `~/.codex/state_*.sqlite` `threads.tokens_used` (one lifetime total
  per thread) → cannot split input/output/cacheRead/cacheCreation, cannot price per-class,
  and the sqlite "floor" inflates today/week (audit #44). ccusage/CodexBar/agentsview all
  read the session JSONL per-turn instead.
- **Source + format**: `~/.codex/sessions/**/rollout-*.jsonl` (env `CODEX_HOME` →
  `<home>/sessions`; also scan `archived_sessions`). Per-turn token events: lines of
  `type=="turn_context"` and `type=="token_count"`, sometimes wrapped as
  `{"type":"event_msg","payload":{...}}`; the counts live at
  `payload.info.last_token_usage.{input_tokens, cached_input_tokens, output_tokens,
  reasoning_output_tokens}` (also handle headless `codex exec` lines and flat token
  fields — audit #44 lists 3 variants; count unrecognized token-like lines rather than
  dropping them, and DO NOT advance the cursor past them).
- **Extracted**: input←`input_tokens`, cacheRead←`cached_input_tokens`, output←`output_tokens`,
  reasoning←`reasoning_output_tokens` (fold into output for pricing); model from the turn
  context. **Prefer this per-class reader over the sqlite total; keep sqlite as a fallback
  only when JSONL is absent.** Mirror `scan_claude_journal`'s cursor (a `jsonl-cursor.json`
  entry) for incremental offset parsing.
- **Insertion**: extend `codex_snapshot()` / `codex_session_roots()` (L4317); add a
  `parse_codex_usage_line()` alongside `parse_claude_usage_line` (L5705). Reuse
  `iter_jsonl_file` + `add_usage_metrics`.
- **License**: clean-room (format facts; reference ccusage `adapter/…/codex`, agentsview
  `codex.go`). **Tests**: fixture with `turn_context`+`token_count`+one headless variant +
  one unparseable line (asserts counter, cursor holds); assert per-class totals and
  non-inflated today/week vs the sqlite floor. **Difficulty**: M. **Risk**: format variants
  — implement under the reader-strategy pattern (unparsed-line counter, cursor safety).

### WO-J · Zero-setup Gemini chats reader (ACCURACY/ACTIVATION FIX on shipped `gemini`)  ⟶ high
- **Problem**: we only read the OTEL telemetry log, which requires the user to have wired
  `settings.telemetry.outfile` (install.py does this, but only counts sessions AFTER install
  → most Gemini usage shows `no_telemetry_yet`/undercounted, verified live 2026-07-06).
  Competitors read the chat files directly with zero setup.
- **Source + format**: `~/.gemini/tmp/*/chats/session-*.json[l]` (env `GEMINI_DATA_DIR`).
  Records carry `usageMetadata.{promptTokenCount, candidatesTokenCount, thoughtsTokenCount,
  cachedContentTokenCount, totalTokenCount}` (SAME shape as our shipped `qwen` reader —
  qwen-code is a Gemini-CLI fork). Roll `thoughtsTokenCount` into output like agentsview.
- **Extracted**: input←`promptTokenCount`, output←`candidatesTokenCount`,
  cacheRead←`cachedContentTokenCount`, reasoning←`thoughtsTokenCount`.
- **Insertion**: add a `gemini_chats_snapshot()` source and MERGE it into
  `gemini_snapshot()` via the existing gemini merge plumbing (dedupe with OTEL by
  session/message id so we don't double-count when both exist). This also feeds
  `antigravity` if its chats live under the antigravity-cli tmp tree — check.
- **License**: clean-room. **Tests**: fixture `~/.gemini/tmp/x/chats/session-1.jsonl`;
  assert tokens with NO telemetry.log present. **Difficulty**: M. **Big activation win** —
  removes the telemetry-hook precondition that leaves most Gemini users at zero.

### WO-K · Copilot OTEL reader (ACCURACY FIX on shipped `copilot`)  ⟶ medium
- **Problem**: we char-estimate Copilot output (sets `estimated:true`). ccusage reads real
  counts incl. reasoning from the OTEL export.
- **Source + keys** (confirmed from ccusage `adapter/copilot/parser.rs`): `~/.copilot/otel/**/*.jsonl`
  (env `COPILOT_OTEL_FILE_EXPORTER_PATH`). OTEL LogRecord attributes:
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
  `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens`
  (also `cache_write.input_tokens`), `gen_ai.usage.reasoning_tokens` (also
  `gen_ai.usage.reasoning.output_tokens`), `gen_ai.usage.total_tokens`; model from
  `gen_ai.request.model` / `gen_ai.response.model`. Prefer OTEL over the transcript
  char-estimate; **clear `estimated` when OTEL counts are used**.
- **Insertion**: add an OTEL branch to `copilot_snapshot()` (L4012) ahead of the transcript
  parse. **License**: clean-room. **Tests**: fixture OTEL jsonl; assert real (non-estimated)
  tokens. **Difficulty**: S once keys confirmed.

### WO-L · qclaw (NEW, agentsview)  ⟶ id `qclaw`
- **Note**: rename-fork of openclaw (WO-D); ship as its own id. Same machinery, `~/.qclaw`.
- **Source + format**: env `QCLAW_DIR` → `~/.qclaw/agents/<agentId>/sessions/<sessionId>.jsonl`
  (+ archive suffixes `.jsonl.deleted.<ts>` / `.jsonl.reset.<ts>` / `.jsonl.full.bak`;
  active `.jsonl` wins, else newest embedded-timestamp archive). pi-style envelope; assistant
  `message.model` + `message.usage{input, output, cacheRead, cacheWrite}` (+ `totalTokens`,
  `cost` — both ignored, re-price from model). Session id `qclaw:<agentId>:<sessionId>`.
- **Extracted**: input/output/cacheRead/cacheCreation direct; context = input+cacheRead+cacheWrite.
- **Insertion**: `qclaw_snapshot()` (share an `_openclaw_family_snapshot(root, id)` helper
  with WO-D — the two parsers are byte-identical except id/branding); register + config id +
  descriptor. **License**: clean-room. **Tests**: fixture agent-dir with one active + one
  `.reset.` archive; e2e `write_qclaw(home)`. **Difficulty**: S (factor with openclaw).

### WO-M · workbuddy (NEW, agentsview)  ⟶ id `workbuddy`
- **Source + format**: env `WORKBUDDY_PROJECTS_DIR` →
  `~/.workbuddy/projects/<project>/<uuid>.jsonl` (+ subagents at
  `<project>/<uuid>/subagents/<agentId>.jsonl`). JSONL events `type` ∈
  message|function_call|function_call_result; `timestamp` epoch **ms** number.
  `providerData.model` + `providerData.usage{…}` with OpenAI-style **alias chains**:
  input=`inputTokens|input_tokens|prompt_tokens`, output=`outputTokens|output_tokens|completion_tokens`,
  cacheRead=`cacheReadInputTokens|cache_read_input_tokens|prompt_tokens_details.cached_tokens`,
  cacheCreation=`cacheCreationInputTokens|cache_creation_input_tokens`,
  reasoning=`reasoningTokens|reasoning_tokens|completion_tokens_details.reasoning_tokens`.
  **OpenAI double-count guard**: when `prompt_tokens` key is used, `input = max(input − cacheRead, 0)`.
- **Insertion**: `workbuddy_snapshot()`; the alias chains motivate widening `TOKEN_KEYS`
  (L156) to cover `prompt_tokens`/`completion_tokens`/`…_details.*` for reuse. register +
  config id + descriptor. **License**: clean-room. **Tests**: fixture with a `prompt_tokens`
  row (asserts the cached-subtraction) + a subagent file; e2e `write_workbuddy(home)`.
  **Difficulty**: M (alias handling + subagent tree).

### WO-N · forge (NEW, agentsview, sqlite)  ⟶ id `forge`
- **Source + format**: env `FORGE_DIR` → `~/.forge/.forge.db` (SQLite, ro, DSN
  `?mode=ro&_journal_mode=WAL&_busy_timeout=3000`). Table `conversations(conversation_id,
  title, context TEXT json, metrics TEXT json, created_at, updated_at)`. Per-message tokens
  in `context.messages[].usage.{prompt_tokens.actual, completion_tokens.actual,
  cached_tokens.actual}` + `messages[].message.text.model`; session totals in the `metrics`
  column `{input_tokens, output_tokens, cached_input_tokens}` (prefer metrics; fall back to
  summing per-message). No cache-creation concept.
- **Insertion**: `forge_snapshot()` (ro-sqlite + gjson-equiv via json_extract or py json);
  register + config id + descriptor. **License**: clean-room. **Tests**: fixture DB with
  `context`+`metrics`; assert totals + per-message. **Difficulty**: M.

### WO-O · piebald (NEW, agentsview, sqlite)  ⟶ id `piebald`
- **Source + format**: env `PIEBALD_DIR` → `app.db` under
  `~/Library/Application Support/piebald/` (macOS), `~/.local/share/piebald/` (Linux),
  `%APPDATA%/piebald/` (Windows). SQLite, ro, DSN `file:...?mode=ro&_busy_timeout=3000`
  (**no WAL pragma**). Normalized: `messages(input_tokens, output_tokens, reasoning_tokens,
  cache_read_tokens, cache_write_tokens, model, finish_reason, parent_chat_id, …)` joined to
  `chats`/`projects`. **reasoning folded into output**. Filter `is_deleted=0 AND message_count>0`.
- **Insertion**: `piebald_snapshot()`; register + config id + descriptor. **License**:
  clean-room. **Tests**: fixture DB (CREATE TABLE from agentsview `piebald_test.go` schema)
  with NULL vs zero token cols (NULL = absent, not zero). **Difficulty**: M.

### WO-P · amp (NEW)  ⟶ id `amp`
- **Source + keys** (confirmed from ccusage `adapter/amp/parser.rs`): env `AMP_DATA_DIR` →
  `~/.local/share/amp/threads/*.json` (one JSON doc per thread). Token usage lives under a
  `usage` (a.k.a. `usageLedger`) object: `inputTokens`, `outputTokens`,
  `cacheCreationInputTokens`, `cacheReadInputTokens` (+ `totalInputTokens`/`totalTokens`;
  short `input`/`output` variants also seen). Model at `model`. **Tokens DO exist** —
  agentsview's amp parser simply ignores them; read them like ccusage.
- **Insertion**: `amp_snapshot()`; register + config id + descriptor. **License**:
  clean-room. **Tests**: fixture thread JSON with `usage.*`; e2e `write_amp(home)`.
  **Difficulty**: S–M. **Value**: popular tool.

### WO-Q · warp (NEW, agentsview, sqlite — session-total tokens only)  ⟶ id `warp`
- **Source + format**: env `WARP_DIR` → `warp.sqlite` under macOS
  `~/Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/dev.warp.Warp-Stable/`,
  Linux `~/.local/state/warp-terminal/`, Windows `%LOCALAPPDATA%/warp/Warp/data/`. Tables
  `agent_conversations(conversation_data JSON)` + `ai_queries(model_id, working_directory,
  input)`. Tokens: `conversation_data.conversation_usage_metadata.token_usage[].{warp_tokens,
  byok_tokens}` — **session-total only** (Warp's own token unit; sum into a session total,
  no per-class). Model from `ai_queries.model_id` (identity strings like `auto-genius`).
- **Insertion**: `warp_snapshot()` (ro-sqlite WAL); status `ok` with a session-total; mark
  `usageCoverage` as total-only. register + config id + descriptor. **License**: clean-room.
  **Tests**: fixture DB. **Difficulty**: M. **Note**: no per-class → totals-only card.

### WO-R · droid / factory (NEW)  ⟶ id `droid`
- **Source + keys** (confirmed from ccusage `adapter/droid/`): env `DROID_SESSIONS_DIR` →
  `~/.factory/sessions/**/*.settings.json` (files ending `.settings.json`). Token object
  `tokenUsage.{inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens,
  thinkingTokens}` (+ `totalTokens`); model at `model`. (CodexBar's factory path is cloud
  WorkOS — ignore.) **Insertion**: `droid_snapshot()` (`.settings.json` glob + json read);
  register + config id + descriptor. **License**: clean-room. **Tests**: fixture with a
  zero-token must-skip settings file. **Difficulty**: S–M.

### WO-S · codebuff / manicode (NEW)  ⟶ id `codebuff`
- **Source + keys** (confirmed from ccusage `adapter/codebuff/`): channels `manicode`,
  `manicode-dev`, `manicode-staging` → `~/.config/<channel>/projects/**/chat-messages.json`
  (file named exactly `chat-messages.json`, a JSON array of messages). `usage` object with
  snake+camel aliases: input=`input_tokens|inputTokens|prompt_tokens|promptTokens`,
  output=`output_tokens|outputTokens|completion_tokens|completionTokens`,
  cacheRead=`cache_read_input_tokens|cacheReadInputTokens`,
  cacheCreation=`cache_creation_input_tokens|cacheCreationInputTokens|cache_creation_tokens|cached_tokens_created`;
  model at `model`. (Same alias-widening as WO-M workbuddy — reuse.) **Insertion**:
  `codebuff_snapshot()`; register + config id + descriptor. **License**: clean-room.
  **Tests**: fixture `chat-messages.json` array. **Difficulty**: S–M.

### WO-T · Port set — the 12 PR#11 local providers + ollama/lmstudio + GLM (from the donor branch)
Port each as a fresh `<id>_snapshot()` on `main` (NOT a merge). All exist on
`feat/local-llm-pro-tracking` with parsers + `write_<id>` fixtures — cherry-pick the LOGIC.
One PR per provider (or batch the trivial VS Code-family ones). Donor references from the
seam map:
- **cursor** `state.vscdb` cursorDiskKV bubbles (+ harden with agentsview's documented
  `~/.cursor/projects/**/agent-transcripts/*.jsonl` primary path). **kiro** IDE globalStorage
  `.chat` (char-estimate). **roo-code** / **kilo-code** / **cline** — VS Code ext
  `ui_messages.json` via the `cline_family_snapshot(provider, extension_id, …)` factory
  (donor L3327). **goose** `sessions.db` (widen path list + `accumulated_*` fallbacks per
  ccusage, see WO cross-ref). **qwen** `~/.qwen/projects/*/chats/*.jsonl` `usageMetadata.*`.
  **crush** per-project `crush.db` via XDG registry. **continue**/**pearai**
  `dev_data/…tokensGenerated.jsonl`. **llm** (simonw) `io.datasette.llm/logs.db`. **gptme**
  `gptme/logs/*/conversation.jsonl`.
- **ollama** (T3): opt-in `agentcat ollama-proxy` in front of `127.0.0.1:11434` →
  `~/.agentcat/ollama/usage.jsonl` (Ollama exposes counts only in the HTTP body; the proxy
  is the only local token source — NOT the ollama.com cloud CodexBar tracks). **lmstudio**
  (T3): `~/.lmstudio/conversations/*.json` `promptTokensCount`/`predictedTokensCount`.
- **GLM / z.ai attribution**: port `is_glm_model()` + GLM pricing rows; attribute `glm-*`
  usage seen in ANY local file to a `zai` pseudo-provider (donor L617/L325). Graft into
  main's `merged_pricing_table()` pipeline (donor has no LiteLLM code).
- **Must-port helpers first** (PR-0): `finish_local_usage_snapshot`, `read_json_bounded`,
  `safe_file_mtime`, `recent_candidate_files`, `vscode_global_storage_dirs`, `parse_cline_task`,
  `cline_family_snapshot`, and the e2e harness. Then close connectors PR #11 / #12 as superseded.

### WO-U · Derived insights: burn-rate / ETA / reset + P90 auto-quota (capability, `derive_insights`)
Not a provider — a derivation over data we already have. (1) burn-rate: tokens/min &
cost/hr from `hourlyTokens`/`dailyTokens`, projected depletion vs known quota, "best
provider to use now" (lowest quota pressure / nearest-but-not-imminent reset). (2) P90
auto-quota: for the local-only agents with no cloud quota, infer a soft-limit from the
90th-percentile session size (Claude-Code-Usage-Monitor `p90_calculator`, quantiles n=10)
→ a remaining-capacity estimate replacing the bare `provider_no_limits` finding. Both live
in `derive_insights` (L598); no new sources. **Difficulty**: M each.

### Structure-only providers (parse cleanly but expose NO local tokens — defer or activity-only)
From agentsview, these read conversations but carry no token/quota data locally:
**openhands** (`~/.openhands/conversations/**` event dirs), **zencoder**
(`~/.zencoder/sessions/*.jsonl`), **cortex-code** (`~/.snowflake/cortex/conversations/*.json`),
**kiro-cli**/**kiro-ide** (`~/.kiro/…` + `data.sqlite3`), **positron**
(`Positron/User/workspaceStorage/**`), **iflow** (`~/.iflow/projects/**`). Ship as
activity-only (`status:"no_token_events_yet"`, appears in the strip but no numbers) only if
process/activity detection wants them; otherwise **defer** — they add rows without the #1
value (token counts). Do NOT spend a token-reader PR on these.

---

## 4bis · Local nuggets extracted from CodexBar (quota-only, no token cost)

**Strategic finding**: CodexBar advertises 45 providers but is **cookie/web/API-first** —
of 26 sampled, 25 are `supportsTokenCost:false` and pull quota via browser-cookie
scraping, WorkOS/OAuth, or vendor RPCs (only `openai` Admin API yields token cost; `vertexai`
gets cost from *local Claude logs*, not its own API). **CodexBar's breadth is ~95%
out-of-scope for local-first; ccusage + agentsview (local-file readers) are our real
coverage template.** From CodexBar we absorb only the fully-local **quota** readers below.

### WO-F · jetbrains-ai (local quota)  ⟶ id `jetbrains` (quota-only)
- **Mechanism**: parse a local IDE XML file. No network, no tokens — a real monthly
  quota meter with refill date (rare local quota source).
- **Source + format**: `<ideDir>/options/AIAssistantQuotaManager2.xml` under IDE config
  roots: macOS `~/Library/Application Support/JetBrains/<IDE><ver>/` and `.../Google/`;
  Linux `~/.config/JetBrains/<IDE><ver>/`. Pick the IDE whose quota file has the newest
  mtime; override `AGENTCAT_JETBRAINS_BASE`. IDE prefixes: IntelliJIdea, PyCharm,
  WebStorm, GoLand, CLion, DataGrip, RubyMine, Rider, PhpStorm, RustRover, AndroidStudio,
  Fleet, etc. XPath `//component[@name='AIAssistantQuotaManager2']/option[@name='quotaInfo']/@value`
  and `.../option[@name='nextRefill']/@value` — values are **HTML-entity-encoded JSON
  strings** (decode `&#10; &quot; &amp; &lt; &gt; &apos;`). quotaInfo JSON:
  `{type, current(String used), maximum(String), until(ISO8601), tariffQuota:{available}}`;
  nextRefill: `{type, next(ISO8601), amount, duration}`.
- **Extracted data**: `usedPercent = current/maximum*100`, `resetsAt = nextRefill.next`,
  plan = quotaInfo `type`, org = "<IDE> <ver>" → `quotas[]` entry. **No tokens.**
- **Insertion**: new `jetbrains_snapshot()` (XML read; Python `xml.etree` in stdlib);
  register + config id + descriptor; quota via `configured_limits`/`runtime_limits`.
- **License**: clean-room. **Tests**: fixture XML with entity-encoded JSON; assert
  quota % + refill. e2e `write_jetbrains(home)`. **Difficulty**: M (double-decode: JSON
  inside HTML-escaped XML attr). **Value**: only local-first *real quota* meter besides
  the big-3 OAuth APIs — good local-first story.

### WO-G · windsurf (local quota)  ⟶ id `windsurf` (quota-only)
- **Source + format**: `~/Library/Application Support/Windsurf/User/globalStorage/state.vscdb`
  (SQLite, `mode=ro`), `SELECT value FROM ItemTable WHERE key='windsurf.settings.cachedPlanInfo'`.
  `value` = JSON `{planName, usage{messages,usedMessages,remainingMessages,flowActions,…,
  flexCredits,…}, quotaUsage{dailyRemainingPercent,weeklyRemainingPercent,
  dailyResetAtUnix,weeklyResetAtUnix}}`.
- **Extracted**: daily/weekly `usedPercent = 100 - *RemainingPercent`, reset unix →
  `quotas[]`. Fallback from `usedMessages`/`remainingMessages`. **No tokens.**
- **Insertion**: new `windsurf_snapshot()`; register + config id + descriptor. **License**:
  clean-room. **Tests**: fixture `state.vscdb`. **Difficulty**: S.

### WO-H · opencode-go (local quota)  ⟶ id `opencodego` (quota-only, distinct from `opencode`)
- **Source + format**: `~/.local/share/opencode/opencode.db` (SQLite, ro),
  `SELECT json_extract(data,'$.time.created'), json_extract(data,'$.cost') FROM message
  WHERE json_extract(data,'$.providerID')='opencode-go' AND json_extract(data,'$.role')='assistant'`.
  Fixed USD limits: session 12, weekly 30, monthly 60 → `usedPercent = costSum/limit*100`;
  monthly window anchored to earliest row's day-of-month. **No token counts** (cost-derived).
- **Insertion**: new `opencodego_snapshot()`; register + config id + descriptor. **License**:
  clean-room. **Tests**: fixture DB with opencode-go rows. **Difficulty**: S.

### Antigravity local-IDE fallback (enhancement note, not a new provider)
CodexBar reaches Antigravity quota via a **local IDE bridge** as an alternative to OAuth:
find the running Antigravity process (bundle `com.google.antigravity*`), `lsof` its
listening TCP port, scrape `--csrf_token`/`--extension_server_port` from its cmdline, then
Connect-RPC `POST http://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/GetUserStatus`
with header `X-Codeium-Csrf-Token`. **Consider adding this as a fallback** to our shipped
`antigravity_live_limits()` (OAuth path, 26.28.0) for users whose OAuth token can't refresh
but who have the IDE running. Low priority; note in the antigravity provider section.

---

## 5. Out of scope (cloud-only — no local source, do NOT spec)

Expose usage **only** via API key / browser cookies / vendor RPC / GraphQL → violate
local-first. Listed so nobody re-investigates. (Where a tool also has a *local* reader, we
cover the local one and skip its cloud path.)

- **Pure cloud (no local variant)**: OpenAI org cost/usage (Admin API), Azure OpenAI
  (deployment ping), OpenRouter, DeepSeek, Mistral, Perplexity, Doubao, Abacus AI, Venice,
  StepFun, GroqCloud, ElevenLabs, Deepgram, AWS Bedrock, Vertex AI, Manus (credits RPC),
  Command Code, Crof, Xiaomi MiMo, T3 Chat (tRPC cookies), and any hosted-usage aggregator.
- **Alibaba Coding Plan / Token Plan** (Qwen coding-plan quota): Bailian/Aliyun console
  RPC via cookies + `sec_token`, intl/cn regions — cloud-only. (Distinct from the *local*
  `qwen` chat-file reader in the port set, which we DO cover.)
- **z.ai / MiniMax / Moonshot / Kimi(official) / KimiK2 / Ollama(codexbar) / Cursor /
  Copilot(quota) / Gemini(quota) / Windsurf(web) / OpenCode(Go web) / Factory(web) /
  Augment / Kiro(cli scrape) / Kilo(api) / Amp(ampcode.com)** — CodexBar reaches these via
  web/cookie/OAuth/API-key, but each is EITHER a cloud endpoint (skip) OR has a local
  source we cover instead: cursor `state.vscdb` (tokens), copilot OTEL/transcripts
  (tokens), gemini chats/OTEL + Code Assist quota (shipped), windsurf `state.vscdb`
  cachedPlanInfo (WO-G quota), opencode-go `opencode.db` (WO-H quota), jetbrains XML
  (WO-F quota), amp `~/.local/share/amp/threads` (WO-P tokens, local), kilo `~/.local/share/kilo/kilo.db`
  (WO-E tokens, local), kiro `~/.kiro/sessions/cli` (structure — see §6 note). **Cover the
  local path; never add the cookie/API-key path** (privacy + fragility). Note: CodexBar's
  "ollama" tracks the ollama.com CLOUD service, NOT the local `:11434` daemon — our
  `ollama` (port set) is the real local one.
- **Grok/xAI, z.ai/GLM cloud, Moonshot/Kimi *API***: cloud usage endpoints → a separate
  future **"T4 API-key" track**, NOT part of this local-first absorption. (z.ai/GLM local
  *attribution* — labeling `glm-*` model usage seen in local files — IS in the port set.)

---

## 6. Testing & CI (applies to every PR)

- **Unit** (`tests/test_agentcat.py`): `bin/agentcat` is loaded via `SourceFileLoader`
  at module scope; `setUp` makes a `TemporaryDirectory` and rebinds `agentcat.HOME` /
  `AGENTCAT_HOME` / path globals. A provider test writes a realistic fixture at the exact
  read path (incl. ≥1 must-skip row), patches env for env-first providers, calls
  `agentcat.<id>_snapshot()`, and asserts `status` / `events` / `tokens.totalTokens` /
  per-class + per-model buckets.
- **E2E** (`scripts/verify_providers_e2e.py`): PORT from `feat/local-llm-pro-tracking` to
  `main` in PR-0 and wire into CI (`.github/workflows/tests.yml`, new job). Adding a
  provider = one `write_<id>(home) -> (expected_total, expected_model)` writer + one
  registry tuple. Harness builds a fake `$HOME`, runs `bin/agentcat` in a subprocess
  (HOME captured at import), asserts `status=="ok"` + exact `tokens.all` + model present.
- **Live probe** (per work order): a one-line command the implementer runs on a real
  machine that has the tool, e.g. `agentcat snapshot | jq '.providers.<id>'`, to confirm
  detection against real data before merging.
- CI matrix stays 3 OS × Python 3.9–3.14; PRs must be green on all cells.
