# AGENTS.md — agentcat-connectors (agentcatd)

This repo is the local connector daemon: the source of truth for provider
usage, quota, accounts and activity that the Agent Cat apps render.

## Boot

Read the shared memory first, then return here:
`/Users/danielmacbook/Trappist/agent-cat-md/AGENTS.md`
(GitHub: https://github.com/yong076/agent-cat-md).

## Binding rules for logins, tokens and quota

Before changing anything that reads a credential, calls a provider usage
endpoint, or reports quota / reset passes / billing, read
`agent-cat-md/01-technical/oauth-and-usage-probing.md`. In short:

- Never refresh or write a CLI-owned credential (file or Keychain). Expired CLI
  token → report `cli_login_expired`. Managed accounts use their own login.
- Read, never act: no claiming reset passes, buying credits or changing plans.
- Tokens never leave the device (no logs, telemetry, sync payloads).
- Every quota row carries `updatedAt` / `stale`; never present old data as live.
- Adding a provider = add its row to the provider table in that doc first.

## Tests

Run from a copy with a fake HOME and closed stdin; tests must never read the
real Keychain, real CLI homes or call a provider (`tests/sandbox.py`, injected
runners/token readers):

    T=$(mktemp -d); rsync -a --exclude .git ./ "$T"/; H=$(mktemp -d)
    (cd "$T" && HOME="$H" AGENTCAT_HOME="$H/.agentcat" TMPDIR=/tmp \
      python3 -m unittest discover -s tests -q </dev/null)

## Releases: auto-update is mandatory, so verify the update path before Latest

Every managed install updates itself to the GitHub **Latest** release within an hour, so a broken updater
reaches everyone. Incident 2026-10-03: 26.40.4 went Latest, the macOS daemon's installer called
`launchctl bootout` and was killed with the job's process group, and daemons stayed stopped.

1. The release workflow marks the new tag Latest; immediately set it to pre-release and point Latest back.
2. On a real Mac, put the connector on the current Latest, add `AGENTCAT_CONNECTORS_MANIFEST_URL=<pre-release
   manifest>` and `AGENTCAT_AUTO_UPDATE_INITIAL_DELAY_SECONDS=20` to the LaunchAgent, reload it, and confirm the
   **daemon itself** installs the new version and serves it (no `Terminated` in `auto-update.err.log`).
3. On Windows, run `agentcat update-check --apply` from PowerShell 7 and confirm the new version is served.
4. Only then promote the release to Latest. Never turn auto-update off or make it opt-in to work around a failure.
