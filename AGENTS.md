# AGENTS.md — agentcat-connectors

Single source of truth for agents entering this repo. Read this first. The global
Trappist spine (`~/.claude/CLAUDE.md`) applies on top; this local file wins on conflict.

## What this is
Agent Cat **Connectors** — the source-available, local-first bridge that lets the Agent Cat
menu bar app see local CLI-agent activity (Codex, Claude Code, Gemini CLI, Antigravity). It
installs a local collector CLI + daemon (`agentcatd` on `127.0.0.1:8765`), keeps all data
under `~/.agentcat`, sanitizes hook payloads, and serves normalized usage/activity snapshots.
Nothing is uploaded by this repo — server sync is a later product layer. Licensed under
PolyForm Shield 1.0.0 (not OSI open source).
Stack: **Python 3** (3.9–3.14), **standard library only** — no third-party deps, no
`pyproject.toml`/`requirements.txt`. The whole CLI is one large file: `bin/agentcat` (~9k lines,
argparse subcommands + a `ThreadingHTTPServer` daemon). Cross-platform (macOS / Linux / Windows).

## Run / build / verify
- Verify (**the one command that proves "done"**):
  **`python3 -m py_compile bin/agentcat scripts/install.py scripts/pro_channel_install.py && python3 -m unittest discover -s tests -p '*test*.py'`**
  (this is exactly what `.github/workflows/tests.yml` runs; 178 tests, all offline/mocked)
- Run the CLI: `bin/agentcat snapshot --json` · `bin/agentcat doctor` · `bin/agentcat daemon`
- Install locally (dev checkout): `./install.sh` (macOS/Linux) · `.\install.ps1` (Windows)
- Provider E2E smoke (offline, no network): `python3 scripts/verify_providers_e2e.py`

## Directory map
- `bin/agentcat` — the entire CLI + daemon (single ~9k-line stdlib script). Subcommands: `daemon`, `snapshot`, `usage`, `event`, `claude-statusline`, `claude-hook`, `gemini-hook`, `codex-notify`, `setup-prompt`, `doctor`, `version`, `update-check`.
- `scripts/` — `install.py` (backs up + merges Agent Cat-managed settings), `pro_channel_install.py` (Pro connector safe-swap with rollback), `verify_providers_e2e.py`, Windows PowerShell helpers.
- `tests/` — `unittest` suites: `test_agentcat.py` (large; loads `bin/agentcat` via SourceFileLoader), `test_install*.py`, `test_pro_channel_install.py`.
- `schemas/` — `agentcat-event-v1.json` (event contract). `docs/` — `architecture.md`, `provider-absorption-masterplan.md`. `skills/agentcat-usage/SKILL.md` — teaches Codex-style agents to call the snapshot instead of guessing.
- `install.sh` / `install.ps1` / `uninstall.sh` — installer entrypoints (archive SHA256 gating). `dist/` — released tarballs + `.sha256`.

## House rules (in addition to the global spine)
- Read this file + `git status` before editing. Stage only task files. Never revert the user's uncommitted work.
- New entrypoints default to **Python 3, standard library only**. Do NOT add third-party dependencies — the connector must run on a bare Python 3.9+ with no pip install. There is no ruff/black gate; match the existing style in `bin/agentcat`.
- Don't claim done without the verify command passing; show failing output when it fails. Tests must stay offline — mock all provider/OAuth network calls (see existing `unittest.mock.patch` usage).
- Privacy is the product guarantee: no prompt text is stored; hook payloads are recursively sanitized before persistence; runtime-mode signals persist only a short flag, never prompt/transcript/path bodies.

## Forbidden surfaces
- **Never bind the daemon to a non-loopback host.** `resolve_bind_host` forces `127.0.0.1`; do not weaken it or expose local usage data to the network.
- **Never persist prompt text, transcripts, file paths, or conversation bodies.** Keep the sanitization + `prompt_text_discarded` guarantees intact.
- **Do not make this repo upload data or add account/cloud/team features.** Those belong to a second layer that reads the local daemon with explicit consent (see `docs/architecture.md` Boundaries).
- Respect the PolyForm Shield license: no competing monitoring/quota/analytics/reporting product. Never commit secrets, tokens, OAuth credentials, or customer data.

## Verify loop
Closed loop (see `loops/`): bounded change → run the verify command
(`py_compile` + `unittest discover`) → evidence to `loops/_runs/` → stop only when it passes →
open a draft PR. Sensitive steps (release/dist tarball + SHA256, installer changes, deploy,
merge, anything touching the privacy/loopback guarantees) require explicit human approval.
Run the core loop with `/goal <done> — verify command passes, stop after N tries`.
