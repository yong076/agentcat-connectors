# Agent Cat Connectors Architecture

Agent Cat Connectors is a local-first bridge between terminal AI tools and the Agent Cat menu bar app.

## Components

- `agentcat` CLI: captures hook events, reads local usage stores, and prints normalized snapshots.
- `agentcat daemon`: serves the same snapshot over `127.0.0.1:8765`.
- Provider adapters: small readers for Codex, Claude Code, and Gemini CLI local files.
- Installer: backs up existing settings and merges Agent Cat-managed entries.

## Data Flow

1. CLI tools run normally in the user's terminal.
2. Provider hooks or local telemetry files emit metadata.
3. `agentcat` sanitizes payloads and stores event metadata in `~/.agentcat/events.sqlite`.
4. Snapshots merge persisted events with provider-local usage files.
5. Agent Cat reads `~/.agentcat/latest-snapshot.json` or calls `GET /v1/snapshot`.

## Capability Matrix

| Provider | Local running state | Token usage | Quota/capacity | Install hook |
| --- | --- | --- | --- | --- |
| Codex | Process scan remains in Mac app | `~/.codex/state_*.sqlite` | Not exposed locally | `notify` when no existing notify is configured |
| Claude Code | Process scan remains in Mac app | `~/.claude/stats-cache.json` + hook payloads | Status-line input when Claude provides it | `statusLine` + hooks |
| Gemini CLI | Process scan remains in Mac app | Local telemetry after future runs | `/stats` is interactive; exact cap not stored by this collector yet | telemetry settings |

## Boundaries

This repository does not upload data. Cloud sync, account login, mobile widgets, and team dashboards should be built as a second layer that reads from the local daemon with explicit user consent.

