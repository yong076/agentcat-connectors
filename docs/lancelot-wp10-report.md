# Lancelot WP10 merge report

## Ported

The former insights engine now ships in the single public connector. It adds
today/week/month/all-time insights, provider burn rate and automatic quota
estimates, quota-aware recommendations, per-project daily cost, OpenAI
organization usage through a securely stored local API key, the local `set-key`
command and key-status API, Codex usage-source breakdowns, Antigravity history,
and reset-safe per-model baselines.

The merge keeps the public implementation as its base. Legacy `insights` and
`projectsDaily` output remain alongside the richer fields, and the public
contract/daemon surfaces, Grok and Kimi support, multi-home provider instances,
Antigravity local SQLite path, WP8 live-limit resilience, privacy controls, and
fail-soft optional data remain intact.

The final hardening pass rejects cross-origin local key mutations; makes the
Codex breakdown cache fail soft, accept only finite numeric totals, and preserve
stale data when refreshes fail; rebases model-class counter resets without
charging false usage; and uses Antigravity history conservatively only after
dedicated OpenTelemetry/SQLite sources take precedence. Focused fixture
coverage of the relevant former Pro behavior was expanded across these paths.

## Dropped

The private installer and its tests were removed, together with daemon-side
private-manifest validation, download-token handling, install scheduling, and
event handoff. Those paths belonged to an obsolete private delivery lane and
would duplicate or weaken the single public release path. A stored legacy
`channel: "pro"` value now resolves to the public lane without contacting a
private service, while new private-channel writes are rejected.

The public release manifest, versioned archive and SHA-256 verification, safe
extraction, dirty-checkout refusal, health validation, and rollback behavior
were preserved.

## Test output

Measured from stable implementation HEAD
`01279667d6353e24ccfe200569feb81f55bfc0ae` on 2026-09-04 (Asia/Seoul):

- `python3 -m unittest tests.test_wp10_engine` — exit 0; `Ran 44 tests in 1.699s`; `OK`.
- `python3 -m unittest tests.test_public_channel_install tests.test_install tests.test_install_integrity` — exit 0; `Ran 19 tests in 0.472s`; `OK`.
- `python3 -m unittest discover -s tests` — exit 0; `Ran 393 tests in 6.254s`; `OK`.
- `python3 -m py_compile bin/agentcat scripts/install.py` — exit 0; no output.

The full-suite run also emitted fixture diagnostics, including two Python
`ResourceWarning` messages for cleaned-up mocked HTTP errors; they did not
change the `OK` result.
