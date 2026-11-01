# Connector troubleshooting

Run `agentcat doctor --json` for local source evidence. It checks loopback
identity, snapshot age/version, service registration, source metadata access,
credential reason codes and local event recency. It does not make provider
requests unless `--network` is supplied. Public vendor status and local source
health are separate signals.

Run `agentcat doctor --fix` only when you want local configuration repaired.
It backs up and reapplies Agent Cat's Claude/Gemini/Codex hook/telemetry settings,
then restarts an existing service registration. It does not update binaries,
refresh a CLI credential, open a login, claim a quota reset or change a plan.
On a device without a service registration, rerun the documented installer.

| Reason | Meaning | Next step |
| --- | --- | --- |
| `daemon_unreachable` | The configured loopback endpoint did not return Agent Cat's identity. | Check registration and port ownership; run explicit local repair if appropriate. |
| `daemon_version_mismatch` | CLI and last snapshot come from different versions. | Restart the daemon, then rerun doctor. |
| `daemon_not_registered` | The macOS LaunchAgent was not found. | Reinstall the connector using the official installer. |
| `port_in_use` | Another listener owns the configured port. | Inspect the listener locally; do not terminate an unknown process. Both daemon and doctor accept `AGENTCAT_PORT`; `--port` overrides the daemon default. |
| `snapshot_stale` | The last observed numbers are old. | Inspect daemon health and logs before trusting quota values. |
| `keychain_denied` | The existing CLI credential could not be read. | Grant access in macOS Keychain or reopen the provider CLI. Agent Cat never refreshes the CLI's token. |
| `token_missing` / `cli_login_expired` | The provider CLI needs its own login. | Sign in through that official CLI, then wait for the next read. |
| `permission_denied` | Desktop usage metadata is unreadable. | Review Full Disk Access for the installed connector's interpreter. |
| `hook_not_installed` / `telemetry_not_configured` | Local collection wiring is absent. | Run explicit `doctor --fix`, which preserves unrelated hooks and creates backups. |
| `no_events_7d` | No recent local hook event was observed. | This can mean inactivity; run the provider once before treating it as a failure. |
| `health_unknown` | Available metadata cannot establish a cause. | Keep the state unknown; collect a support bundle. |
| `repair_failed` | Local config repair or registered-service restart did not finish. | Existing backups remain; use the official installer to restore registration. |
| `gemini_telemetry_record_too_large` | A malformed or unusually large OTEL object exceeded the bounded parser. | Preserve the telemetry file for local inspection; do not label old numbers as live. |

`agentcat doctor --bundle <destination>` writes a redacted local bundle. Review
it before sharing. No upload happens automatically. Logs rotate locally.

Windows users can run the installed `uninstall.ps1`; it uses local source,
does not download a replacement and keeps usage/backups. Hidden logon tasks
monitor the daemon's exit and retry a failure up to three times at one-minute
intervals. If task registration is unavailable the existing Run-entry fallback
is retained; that fallback cannot promise Task Scheduler restart behavior.

Successful installers keep three recent backup directories. Known backups
older than 30 days are compressed into private `.tar.gz` recovery archives;
every regular file is checksum-verified before removing its source directory.
Dirty-checkout recovery material and unknown directories are excluded. Recovery
archives are retained and never uploaded or automatically expired.

OpenCode uses narrow changed-row reads only when the database supplies stable
message IDs and update timestamps. Backfilled rows, deletions and session
metadata changes invalidate the relevant cache. Older schemas keep the complete
reader. Copilot byte cursors retain usage metadata and hash-only boundaries in
bounded memory; incomplete tails are replaced when completed. Desktop metadata
tree scans have a five-minute maximum cache age and preserve `checkedAt`.
