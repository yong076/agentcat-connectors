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

Measured from stable HEAD `a4fcd4f` on 2026-09-04 (Asia/Seoul):

- `python3 -m unittest tests.test_wp10_engine` — exit 0; `Ran 20 tests in 0.538s`; `OK`.
- `python3 -m unittest tests.test_public_channel_install tests.test_install tests.test_install_integrity` — exit 0; `Ran 19 tests in 0.397s`; `OK`.
- `python3 -m unittest discover -s tests` — exit 0; `Ran 369 tests in 4.968s`; `OK`.
- `python3 -m py_compile bin/agentcat scripts/install.py` — exit 0; no output.

The full-suite run also emitted fixture diagnostics, including two Python
`ResourceWarning` messages for cleaned-up mocked HTTP errors; they did not
change the `OK` result.
