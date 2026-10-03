# Connector release safety

Tag builds are pre-releases. Publication and promotion require the owner's separate release decision.
The pre-release is never GitHub Latest until both real-machine attestations pass.

## Operator verification

Run from a trusted source checkout on a real machine with a running managed connector:

```sh
python3 scripts/verify_update_path.py --target 26.40.6 --upload
```

Use `python` on Windows; PowerShell 7 (`pwsh`) and `gh` must be installed. Omit `--upload` to keep the report local.
The script downloads and checks the target, current Latest, and recovery archives before changing the machine.
It installs Latest, exercises the updater, waits up to five minutes for the target snapshot, then requires the
installer's final success record. On macOS it injects the pre-release manifest and 20-second delay into the LaunchAgent,
checks that the job is loaded and this attempt's stderr has no `Terminated`, and restores the baseline plist if test
variables remain. A failed attempt stops its updater tree and restores the original release and plist.
Recovery errors fail the command too; inspect its local installer output before retrying.

On Windows the update command runs in PowerShell 7 with `--apply --force`. The 26.40.5 baseline predates `--force`:
the verifier checks its help and uses its existing ungated `--apply` behavior. It never changes provider credentials.
The current macOS rehearsal starts from 26.40.5, which also predates staged rollout. A later gated baseline will defer
an unpublished target unless the rollout service allows it; this script does not silently bypass the daemon's gate.

Reports contain only `version`, `archiveSha256`, `fromVersion`, `servedVersion`, `os`, `arch`, `durationSec`, `passed`,
and `checkedAt`. Upload replaces that OS's earlier report, including a failed result. After both reports pass, the owner
can dispatch **Promote verified connector** with `26.40.6`. Promotion rejects missing, failed, wrong-version, wrong-OS,
and wrong-checksum reports. It uploads `{version, promotedAt}` as `rollout.json` before marking Latest.

## Automatic rollout

Before applying an update, the connector requests
`https://agentcat-telemetry.vercel.app/v1/connector/rollout?version=<version>`.
A local random `rollout-id` is retained with mode 0600 on POSIX and is never sent. SHA-256 of `id:version` selects a
bucket in 0–99. A halt or an excluded bucket records `staged` and retries on the next mandatory update cycle.
Snapshot `update.rollout` contains only `{percent, bucketAllowed, halted, reason}`.
If the service is unreachable, only a matching public `rollout.json` promotion older than 72 hours allows installation.
Malformed responses fail closed. `agentcat update-check --apply --force` bypasses this gate for operators.
The default manifest download is pinned to the checked version so a moving Latest cannot select another cohort's release.

## CI rehearsal

```sh
python3 scripts/run_sandboxed_tests.py --previous-version 26.40.5
```

The runner downloads and checksum-verifies the public N-1 archive, copies the worktree into a temporary directory,
sets an empty HOME/USERPROFILE, closes stdin, and runs the suite. Keep the previous-version pin in both workflows
current when advancing the release baseline. Without the archive option, only the N-1 test is skipped.

The survival test executes `start_auto_update_install`, the shell/PowerShell bootstrap, `public_channel_install.py`,
and `install.py` as real processes. Fake download and OS-service boundaries prevent provider access or real service changes.
On POSIX, fake `launchctl bootout` kills the fake daemon's entire process group. On Windows, the mocked stop command
terminates the fake daemon process. Both require the detached installer to finish and bootstrap a replacement.
The N-1 test installs the previous release archive into the temporary HOME, updates it to an archive of the working tree,
and checks both `agentcat version` and `agentcat snapshot --json`. CI runs both tests throughout the existing OS/Python matrix.
These service fakes are regression coverage, not substitutes for the two real-machine promotion attestations.
