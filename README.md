# Agent Cat Connectors

[English](README.md) | [한국어](README.ko.md)

Agent Cat Connectors lets the Agent Cat menu bar app see local CLI-agent activity from Codex, Claude Code, and Gemini CLI.

It installs a small local collector, keeps data under `~/.agentcat`, and patches supported CLI settings so future sessions can report activity without sending prompts to a remote server.

## Install

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.ps1 | iex
```

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.sh | bash
```

For a cloned checkout:

```bash
./install.sh
```

On Windows from a cloned checkout:

```powershell
.\install.ps1
```

Then verify:

```powershell
agentcat snapshot
```

To copy the prompt you can paste into Codex, Claude Code, or Gemini CLI after installation:

```bash
agentcat setup-prompt
```

## What It Installs

- `~/.local/bin/agentcat` or `%USERPROFILE%\.local\bin\agentcat.cmd`: local collector CLI
- `~/Library/LaunchAgents/com.trappist.agentcatd.plist` or Windows startup task `AgentCatD`: local daemon on `127.0.0.1:8765`
- `~/.agentcat/events.sqlite`: local event store
- `~/.agentcat/latest-snapshot.json`: latest normalized usage snapshot
- timestamped backups under `~/.agentcat/backups/`

## Provider Support

| Provider | Signal | Notes |
| --- | --- | --- |
| Codex | local SQLite token totals + Codex OAuth usage API | Shows remaining 5-hour, 7-day, and exposed model/review quota percentages when `~/.codex/auth.json` is present. |
| Claude Code | local stats/hooks + Claude Code OAuth usage API | Shows remaining 5-hour, 7-day, model quota, and extra monthly credit data when Claude Code OAuth credentials are present. |
| Gemini CLI | local telemetry + Gemini Code Assist quota API | Shows remaining Code Assist request quota per model family for Google-login Gemini CLI sessions. |
| GitHub Copilot | local session-state + VS Code transcripts | Legacy events carry real token counts; the VS Code transcript path is char-estimated, so those snapshots set `estimated: true`. |
| Cursor | local `state.vscdb` bubble token counts | Real `tokenCount` when present; falls back to a char estimate for bubbles with no count, setting `estimated: true`. |
| Goose | local `sessions.db` accumulated tokens | Real per-session input/output token totals. |
| Kiro | local `.chat` files | No real token counts on disk — fully char-estimated, so snapshots always set `estimated: true`. |
| Roo Code | local cline-family task logs | Real `tokensIn`/`tokensOut` (plus cache reads/writes) per request. |
| Kilo Code | local cline-family task logs | Real `tokensIn`/`tokensOut` (plus cache reads/writes) per request. |
| Cline | local cline-family task logs | Real `tokensIn`/`tokensOut` (plus cache reads/writes) per request. |
| Qwen Code | local chat `usageMetadata` | Real prompt/candidate/thought/cached token counts. |
| Crush | local `crush.db` session store | Real prompt/completion token totals per root session. |
| Continue | local `dev_data/tokensGenerated.jsonl` | Real prompt/generated token counts per generation. |
| PearAI | local `dev_data/tokensGenerated.jsonl` (Continue fork) | Real prompt/generated token counts per generation. |
| llm (simonw) | local `logs.db` responses table | Real input/output token counts per response. |
| gptme | local `conversation.jsonl` usage | Real token fields when present; never estimated — reports `no_token_events_yet` instead of guessing. |

Local-usage providers read durable on-disk artifacts only (never IDE/process presence). Where a provider has no real token counts on disk, the connector char-estimates and flags the provider snapshot with `provider.estimated = true` (omitted/false otherwise) so the app can label those numbers as approximate. Estimate-based today: Cursor (fallback bubbles), Kiro (always), and Copilot's VS Code transcript path.

## Privacy

The connector is local-first.

- No prompt text is intentionally stored.
- Claude/Gemini hook payloads are recursively sanitized before persistence.
- The local daemon listens only on `127.0.0.1`.
- Nothing is uploaded by this repo. Server sync is a later product layer.

## HTTP API

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/v1/snapshot
```

Agent Cat can read `~/.agentcat/latest-snapshot.json` or call the local API. The snapshot includes usage plus `activity.processes`, `activity.countsByProvider`, `activity.totalCPUPercent`, `activity.totalMemoryBytes`, `activity.memoryBytesByProvider`, `activity.runnableProcessCount`, `activity.activityScore`, and `activity.motionStage` so sandboxed Mac builds can use the connector instead of direct process scanning.

`activity.motionStage` is based on current activity, not raw agent count. The connector uses `totalCPUPercent + runnableProcessCount * 4` as the activity score and emits the same four stages as the app: `sleeping` when no agent process is present, `walking` while processes are present but mostly waiting, `running` from 7 points, and `sprinting` from 22 points.

Memory usage is local RSS memory from `/bin/ps`, exposed per process as `memoryBytes` and grouped by provider as `memoryBytesByProvider`. It does not inspect prompts, transcripts, or model responses.

`activity.runtimeModes` is an optional local-only signal for high-effort agent sessions. Claude Code `UserPromptSubmit` hooks detect `ultrathink` / `ultracode` in memory, discard the prompt text, and persist only a short-lived flag such as `mode=ultrathink`, `confidence=exact`, and `privacy=prompt_text_discarded`. Metadata-only effort signals such as `effort.level=xhigh` and Codex `model_reasoning_effort=xhigh` are normalized as `mode=effort_xhigh` without reading prompts or transcripts. Claude `Stop` hooks clear the flag. No prompt text, file paths, transcripts, or conversation bodies are persisted.

On Windows, Agent Cat prefers PowerShell 7 (`pwsh`) when available, tries a fast `Get-Process` scan first, and only falls back to richer command-line scanning or `tasklist` when needed. Slow corporate environments can raise the scan timeout in `~/.agentcat/settings.json`:

```json
{
  "windowsProcessScanTimeoutSeconds": 8
}
```

## Limits

Agent Cat reports remaining quota when a provider exposes it through the same local auth state used by its CLI:

- Codex: reads `~/.codex/auth.json`, then calls the ChatGPT Codex usage endpoint for rolling 5-hour/7-day utilization and reset times.
- Claude Code: reads Claude Code OAuth credentials from Keychain or `~/.claude`, then calls the Claude Code OAuth usage endpoint for 5-hour/7-day/model utilization plus monthly extra-usage credits.
- Gemini CLI: reads `~/.gemini/oauth_creds.json` and `~/.gemini/settings.json`, then calls Gemini Code Assist `loadCodeAssist` and `retrieveUserQuota` for model request quota fractions and reset times.
- Fallback: if live quota lookup fails, Codex/Claude still use the latest local status-line or session `token_count` event when available.

The normalized snapshot includes `providers.<name>.limits.quotas[]`. Each quota entry prefers `remaining` or `remainingPercent`, with `usedPercent` and `resetAt` for progress meters. Some providers expose percentages only, not absolute token or request counts; Agent Cat marks unavailable values as unavailable instead of guessing.

For missing or manually managed caps, use `~/.agentcat/limits.json`; configured values override compatible auto-detected token caps while live quota cards remain visible.

Example:

```json
{
  "providers": {
    "codex": {
      "week": 1000000000,
      "month": 4000000000,
      "session": 200000
    },
    "claude": {
      "week": 500000000,
      "month": 2000000000,
      "session": 200000
    },
    "gemini": {
      "week": 500000000,
      "month": 2000000000,
      "session": 1000000
    }
  }
}
```

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/uninstall.sh | bash
```

For a cloned checkout:

```bash
./uninstall.sh
```

Uninstall removes the LaunchAgent, binary link, and Agent Cat-managed config entries. Local usage data under `~/.agentcat` is retained unless you remove it manually.

## Development

```bash
python3 -m py_compile bin/agentcat scripts/install.py
bin/agentcat snapshot --json
```

## Codex Skill

This repo also includes `skills/agentcat-usage/SKILL.md` so Codex-style agents can be taught to call `agentcat snapshot --json` instead of guessing local usage.
