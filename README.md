# Agent Cat Connectors

Agent Cat Connectors lets the Agent Cat menu bar app see local CLI-agent activity from Codex, Claude Code, and Gemini CLI.

It installs a small local collector, keeps data under `~/.agentcat`, and patches supported CLI settings so future sessions can report activity without sending prompts to a remote server.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.sh | bash
```

For a cloned checkout:

```bash
./install.sh
```

Then verify:

```bash
agentcat snapshot
```

## What It Installs

- `~/.local/bin/agentcat`: local collector CLI
- `~/Library/LaunchAgents/com.trappist.agentcatd.plist`: local daemon on `127.0.0.1:8765`
- `~/.agentcat/events.sqlite`: local event store
- `~/.agentcat/latest-snapshot.json`: latest normalized usage snapshot
- timestamped backups under `~/.agentcat/backups/`

## Provider Support

| Provider | MVP signal | Notes |
| --- | --- | --- |
| Codex | local SQLite token totals + optional notify hook | Exact weekly/monthly quota is not exposed by Codex CLI locally. |
| Claude Code | `stats-cache.json`, status line input, hooks | Uses local stats when present and captures future status-line/hook payloads. |
| Gemini CLI | local telemetry file | Token data appears after Gemini runs with local telemetry enabled. |

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

Agent Cat can read `~/.agentcat/latest-snapshot.json` or call the local API. The snapshot includes usage plus `activity.processes`, `activity.countsByProvider`, `activity.totalCPUPercent`, and `activity.motionStage` so sandboxed Mac builds can use the connector instead of direct process scanning.

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
