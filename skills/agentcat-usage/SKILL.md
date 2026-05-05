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
- Claude totals come from `~/.claude/stats-cache.json` and future hook/status-line payloads.
- Gemini totals appear after future Gemini CLI runs with local telemetry enabled.
- Do not infer exact provider quota/capacity unless the snapshot includes it.
- Do not report prompt content. Agent Cat snapshots are intended to contain metadata only.

