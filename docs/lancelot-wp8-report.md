# Lancelot WP8 rate-limit improvements

## Changes

1. Claude OAuth access tokens are refreshed in memory when expired or within five minutes of expiry. The refresh request is form-encoded, uses the Claude OAuth client ID, and has a 10-second timeout. A failed refresh reports `token_expired` without modifying Claude Code credentials.
2. Live-limit failures now expose stable reasons for rate limiting, expired tokens, missing Claude scope, server failures, JSON parsing failures, and network failures. HTTP 429 responses persist `retryAt` from `Retry-After` (300 seconds by default), and cached stale limits suppress probes until that time.
3. Provider cache entries track `failureStreak`. Consecutive failures back off for `min(30 * 2**(streak-1), 900)` seconds, successful probes reset the streak, and `not_applicable` retains its 300-second TTL.
4. Claude statusline events use a SQLite-backed, cross-process throttle. Each session has a 15-second minimum write interval, identical payloads are dropped for 30 seconds, and payload/session identifiers are stored only as SHA-256 hashes in throttle state.
5. Codex rate-limit windows accept one minute of drift around 300 and 10,080 minutes, then fall back to primary-as-session and secondary-as-weekly. Codex usage and reset-credit requests send `User-Agent: codex-cli` and `OpenAI-Beta: codex-1`.
6. On macOS, an explicit `CLAUDE_CONFIG_DIR` selects `Claude Code-credentials-<8-character SHA-256 prefix>` before the legacy keychain service. Claude limits report `credentialSource` as `scoped-keychain`, `legacy-keychain`, or `credentials-file`.

Regression coverage lives in `tests/test_lancelot_wp8.py`. Every WP8 test uses `redirect_module_paths` and `assert_sandboxed`, and blocks `urllib.request.urlopen` unless the individual test supplies a mock. The legacy `AgentCatConnectorTests` fixture now uses the same path sandbox, removes inherited provider-home overrides, and treats Keychain lookup as not found unless a test explicitly mocks it.

## Verification

Commands were run with an isolated temporary `HOME` and provider-home/authentication environment variables unset.

```text
$ python3 -m py_compile bin/agentcat scripts/install.py scripts/pro_channel_install.py scripts/public_channel_install.py
(exit 0; no output)

$ python3 -m unittest discover -s tests -p '*test*.py'
Ran 366 tests in 3.956s

OK
```

## Left unchanged

All six requested items, including the optional scoped-keychain item, were completed. Connector version and install scripts were not changed. Broader research-report ideas outside this work package—Claude OAuth/CLI window merging, Gemini bucket naming/deduplication, Codex reset-credit consumption, and cookie-based providers—remain out of scope.
