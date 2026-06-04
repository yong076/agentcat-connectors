# Changelog

All notable changes to the Agent Cat connector are documented here.

## 26.23.1

### Added

- **12 new local-usage providers.** The connector now reads durable on-disk
  agent artifacts (never IDE/process presence) for: cursor, goose, kiro,
  roo-code, kilo-code, cline, qwen, crush, continue, pearai, llm, and gptme.
  Each parser extracts real token counts where the provider persists them and
  is exercised end-to-end by `scripts/verify_providers_e2e.py`.
- **Provider management config.** A `~/.agentcat/providers.json` file plus a
  `GET`/`POST /v1/config` API lets the app enable/disable providers and set
  manual per-provider token limits. Disabled providers report a `disabled`
  status with zero tokens so "turned off" is distinct from "not_found".
- **Estimated-token flag.** Providers that char-estimate tokens when real
  counts are absent now set `provider.estimated = true` on the snapshot
  (omitted/false otherwise). This is the connector half of an app contract:
  the macOS app reads `provider.estimated` and labels those numbers as
  approximate. Estimate-based providers today: cursor (fallback bubbles), kiro
  (always char-estimated), and copilot's VS Code transcript path. gptme never
  estimates — it reports `no_token_events_yet` instead of guessing.

### Security

- `POST /v1/config` and `POST /v1/events` now reject non-local requests (403):
  any request carrying an `Origin` or `Referer` header, or a non-loopback
  `Host` header, is refused. This blocks browser-driven CSRF and DNS-rebind
  attacks against the loopback daemon. Read-only `GET` routes are unguarded.
- `providers.json` writes allowlist and coerce the `limits` block (only the
  known numeric token caps survive, each coerced to a positive int), so the
  config file can't grow unbounded client-controlled keys.

### Changed

- Snapshot builds serialize behind a lock so concurrent builders (the refresh
  loop and per-request `/v1/config` threads) never clobber the shared snapshot
  temp file.
- Local-usage JSON parsers (cline-family `ui_messages.json` /
  `api_conversation_history.json`, kiro `.chat` files) skip oversized files
  instead of slurping them into memory; continue/pearai use bounded explicit
  globs instead of a recursive directory walk.
- Cross-platform path resolution: gptme honors `GPTME_LOGS_HOME` / XDG /
  Windows `%LOCALAPPDATA%` / macOS Application Support, and `llm`'s Windows
  data dir now resolves under `%APPDATA%` (Roaming), matching Click's
  `get_app_dir` default.
