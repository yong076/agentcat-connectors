---
name: agentcat-usage
description: Use when a user asks an AI agent to report local Agent Cat usage, inspect Codex/Claude/Gemini token activity, or verify whether Agent Cat connectors are installed and collecting data.
---

# Agent Cat Usage

Use the local `agentcat` command instead of guessing provider usage.

## Workflow

1. Check installation:

   ```bash
   command -v agentcat
   ```

2. Read a normalized snapshot:

   ```bash
   agentcat snapshot --json
   ```

3. If the command is missing, tell the user to install Agent Cat Connectors:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/yong076/agentcat-connectors/main/install.sh | bash
   ```

## Interpretation

- Codex totals come from local `~/.codex/state_*.sqlite`.
- Codex remaining quotas appear under `providers.codex.limits.quotas[]` when `~/.codex/auth.json` can call the Codex usage API.
- Claude totals come from `~/.claude/stats-cache.json`, hooks, and Claude Code OAuth usage when available.
- Claude remaining quotas appear under `providers.claude.limits.quotas[]`, including 5-hour, 7-day, model, and monthly extra credit entries when the provider exposes them.
- Gemini totals appear after Gemini CLI runs with local telemetry enabled.
- Gemini remaining request quotas appear under `providers.gemini.limits.quotas[]` when Google-login Gemini CLI OAuth credentials can call Code Assist quota APIs.
- Current CPU and memory usage come from `activity`. Memory is process RSS from `/bin/ps`, exposed as `activity.processes[].memoryBytes` and grouped as `activity.memoryBytesByProvider`.
- Report exact provider quota/capacity only when the snapshot includes `remaining`, `limit`, or `remainingPercent`. Otherwise say unavailable.
- Do not report prompt content. Agent Cat snapshots are intended to contain metadata only.
