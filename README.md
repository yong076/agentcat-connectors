# Agent Cat Connectors

[English](README.md) | [한국어](README.ko.md)

Agent Cat Connectors lets the Agent Cat menu bar app see local CLI-agent
activity from Codex, Claude Code, Gemini CLI, and other supported agent tools.

It installs a small local collector, keeps data under `~/.agentcat`, and patches
supported CLI settings so future sessions can report activity without sending
prompts to a remote server. This public connector powers the free product:
local monitoring, provider breadth, quota state, basic costs, budget caps, and
weekly report inputs.

## License

Agent Cat Connectors is source-available under the
[PolyForm Shield License 1.0.0](LICENSE). It is not OSI open source.

**Commercial competitive use is prohibited.** Do not use this connector to
build, operate, sell, or distribute a competing product, hosted service,
analytics tool, quota monitor, reporting product, or team/admin product without
a separate commercial license from Trappist.

You may inspect, install, modify, and distribute the connector for permitted
purposes, including use with Agent Cat. You may not use it to provide, package,
host, sell, or distribute a product or service that competes with Agent Cat or
Trappist's Agent Cat-related connector, monitoring, quota, analytics, reporting,
account, or team tooling without a separate commercial license.

See [`NOTICE`](NOTICE) for the required copyright and line-of-business notices.
For commercial licensing, contact `meow@agentcat.app`.

## Install

The normal user path is app-led:

1. Open Agent Cat.
2. Go to Home -> Agents / Connector.
3. Click **Install connector**.
4. Wait for the app to show live provider data.

The app-led path is preferred because it explains what will change, keeps a
rollback backup, and verifies the local daemon after install. Use the commands
below only for development, CI, remote support, or when the app cannot open the
installer.

Advanced Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.ps1 | iex
```

Advanced macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.sh | bash
```

Development from a cloned checkout:

```bash
./install.sh
```

### Connector archive integrity

When the connector is fetched as a tarball (the archive download and the clone
fallback in `install.sh`), `install.sh` verifies the archive against a
known-good SHA256 when one is available, gating the swap exactly like the Pro
channel (`scripts/pro_channel_install.py`). Note: an existing install created
via `git clone` updates through `git pull --ff-only` (HTTPS git object
integrity is the trust boundary there) and does not pass through this archive
gate yet — routing that path through verification is part of the release-infra
follow-up below. Supply a digest via either:

- `AGENTCAT_CONNECTORS_SHA256` — a 64 lowercase hex digest passed by the caller
  (for an app-led update, or local QA), or
- `AGENTCAT_CONNECTORS_SHA256_URL` — a sidecar digest published next to the
  tarball.
- `AGENTCAT_CONNECTORS_ARCHIVE_URL` — a pinned archive URL supplied by release
  automation or local QA. When omitted, the installer keeps using the public
  `main` branch archive.

When no digest is supplied (the current default), the install proceeds as
before so existing installs keep updating. Publishing a per-release digest and
wiring it into the auto-update path is the remaining step to make verification
mandatory on the public channel.

Pro connector safe-swap QA (local/private builds only):

```bash
python3 scripts/pro_channel_install.py \
  --archive /path/to/agentcat-connectors-pro.tgz \
  --manifest /path/to/pro-manifest.json \
  --install-dir "$TMPDIR/agentcat-pro-connectors" \
  --event-log "$TMPDIR/agentcat-pro-connector-events.jsonl"
```

The optional event log records `pro_connector_swap_started`,
`pro_connector_swap_succeeded`, and `pro_connector_swap_rolled_back` without
contacting the Pro API. App-led installs can additionally pass
`--event-api-url`, `--event-bearer`, and `--device-id` after entitlement checks.
Those flags are observability only; install success never depends on them.

Development on Windows from a cloned checkout:

```powershell
.\install.ps1
```

Then verify:

```powershell
agentcat snapshot
```

If an agent runtime needs manual setup text after installation, copy the
fallback prompt:

```bash
agentcat setup-prompt
```

## What It Installs

- `~/.local/bin/agentcat` or `%USERPROFILE%\.local\bin\agentcat.cmd`: local collector CLI
- `~/Library/LaunchAgents/com.trappist.agentcatd.plist` or Windows startup task `AgentCatD`: local daemon on `127.0.0.1:8765`. If task registration is unavailable, Windows uses the current user's `HKCU Run` entry instead; the installer no longer creates a VBS startup script.
- `~/.agentcat/events.sqlite`: local event store
- `~/.agentcat/latest-snapshot.json`: latest normalized usage snapshot
- timestamped backups under `~/.agentcat/backups/`

## Provider Support

| Provider | Signal | Notes |
| --- | --- | --- |
| Codex | local SQLite token totals + Codex OAuth usage API | Shows remaining 5-hour, 7-day, exposed model/review quota percentages, available reset credits, and Codex credit/spend-cap state when `~/.codex/auth.json` is present. |
| Claude Code | local stats/hooks + Claude Code OAuth usage API | Shows remaining 5-hour, 7-day, model quota, and extra monthly credit data when Claude Code OAuth credentials are present. |
| Gemini CLI | local telemetry + Gemini Code Assist quota API | Shows remaining Code Assist request quota per model family for Google-login Gemini CLI sessions. |

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

- Codex: reads `~/.codex/auth.json`, then calls the ChatGPT Codex usage endpoints for rolling 5-hour/7-day utilization, reset times, available reset credits, and Codex credit/spend-cap state. Reset credits are reported only as availability/metadata; this connector never redeems them.
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
