#!/usr/bin/env python3
"""Operator-only, real-machine update rehearsal. Never run against a user's home in CI."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import public_channel_install as public

REPO = "yong076/agentcat-connectors"
RELEASES = f"https://github.com/{REPO}/releases"
TEST_ENV = ("AGENTCAT_CONNECTORS_MANIFEST_URL", "AGENTCAT_AUTO_UPDATE_INITIAL_DELAY_SECONDS")


def get_json(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


def run(command, **kwargs):
    return subprocess.run(command, check=True, stdin=subprocess.DEVNULL, timeout=180, **kwargs)


class Verification:
    def __init__(self, target, system, home, work):
        self.target, self.system, self.home, self.work = target, system, home, work
        self.plist = home / "Library/LaunchAgents/com.trappist.agentcatd.plist"
        self.install_dir = home / ".agentcat/connectors"
        self.manifest_url = f"{RELEASES}/download/v{target}/connector-manifest.json"
        self.out_offset = 0
        self.update_state_before = None
        self.clean_env = os.environ.copy()
        for key in list(self.clean_env):
            if key.startswith(("AGENTCAT_CONNECTORS_", "AGENTCAT_AUTO_UPDATE")) or key == "AGENTCAT_CONNECTOR_VERSION":
                self.clean_env.pop(key)
        self.clean_env["AGENTCAT_CONNECTORS_DIR"] = str(self.install_dir)

    def download(self, manifest, name):
        path = self.work / f"{name}.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest = public.read_manifest(path)
        url = manifest.get("archiveUrl", "")
        if not url.startswith(f"https://github.com/{REPO}/releases/download/"):
            raise ValueError("unapproved archive URL")
        archive = self.work / f"{name}.zip"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        public.verify_archive(archive, manifest)
        return archive, path

    def install(self, assets):
        archive, manifest = assets
        run([sys.executable, str(Path(public.__file__)), "--archive", str(archive),
             "--manifest", str(manifest), "--install-dir", str(self.install_dir)], env=self.clean_env)

    def snapshot_version(self):
        return get_json("http://127.0.0.1:8765/v1/snapshot").get("connectorVersion")

    def wait_served(self, version):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                if self.snapshot_version() == version:
                    return version
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise RuntimeError("daemon did not serve the expected version within five minutes")

    def reload(self):
        domain = f"gui/{os.getuid()}"
        # bootout may fail if the failed updater already unloaded the job.
        subprocess.run(["launchctl", "bootout", f"{domain}/com.trappist.agentcatd"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
        run(["launchctl", "bootstrap", domain, str(self.plist)])
        run(["launchctl", "kickstart", "-k", f"{domain}/com.trappist.agentcatd"])

    def trigger(self):
        if self.system == "macos":
            data = plistlib.loads(self.plist.read_bytes())
            env = data.setdefault("EnvironmentVariables", {})
            env[TEST_ENV[0]] = self.manifest_url
            env[TEST_ENV[1]] = "20"
            self.plist.write_bytes(plistlib.dumps(data))
            self.reload()
        else:
            env = dict(self.clean_env, AGENTCAT_CONNECTORS_MANIFEST_URL=self.manifest_url)
            # 26.40.5 predates --force and already applies without rollout. Query
            # its parser first; every newer baseline must use the operator flag.
            cli = str(self.home / ".local/bin/agentcat.cmd")
            help_result = run(["pwsh", "-NoProfile", "-Command", "& $env:VERIFY_CLI update-check --help; exit $LASTEXITCODE"],
                              env=dict(env, VERIFY_CLI=cli), capture_output=True, text=True)
            force = " --force" if "--force" in help_result.stdout else ""
            run(["pwsh", "-NoProfile", "-Command",
                 "& $env:VERIFY_CLI update-check --apply" + force + "; exit $LASTEXITCODE"],
                env=dict(env, VERIFY_CLI=cli))

    def check_macos(self, baseline_plist):
        run(["launchctl", "print", f"gui/{os.getuid()}/com.trappist.agentcatd"], capture_output=True)
        data = plistlib.loads(self.plist.read_bytes())
        if any(key in data.get("EnvironmentVariables", {}) for key in TEST_ENV):
            self.plist.write_bytes(baseline_plist)
            self.reload()
            self.wait_served(self.target)

    def execute(self, report):
        metadata = get_json(f"https://api.github.com/repos/{REPO}/releases/tags/v{self.target}")
        if metadata.get("prerelease") is not True or metadata.get("draft"):
            raise ValueError("target must be a published pre-release")
        target_manifest = get_json(self.manifest_url)
        if target_manifest.get("version") != self.target:
            raise ValueError("target manifest version mismatch")
        report["archiveSha256"] = target_manifest["sha256"]
        # Download and verify everything before changing the installed machine.
        self.download(target_manifest, "target")
        latest = get_json(f"{RELEASES}/latest/download/connector-manifest.json")
        if latest["version"] == self.target:
            raise ValueError("target must differ from current Latest")
        report["fromVersion"] = latest["version"]
        baseline_assets = self.download(latest, "latest")
        previous_version = self.snapshot_version()
        if not previous_version or not self.install_dir.is_dir():
            raise RuntimeError("verification requires a running managed installation")
        previous = get_json(f"{RELEASES}/download/v{previous_version}/connector-manifest.json")
        if previous.get("version") != previous_version:
            raise ValueError("recovery manifest version mismatch")
        recovery_assets = self.download(previous, "recovery")
        original_plist = self.plist.read_bytes() if self.system == "macos" else None
        try:
            self.install(baseline_assets)
            self.wait_served(latest["version"])
            baseline_plist = self.plist.read_bytes() if self.system == "macos" else None
            log = self.home / ".agentcat/auto-update.err.log"
            offset = log.stat().st_size if log.exists() else 0
            out_log = self.home / ".agentcat/auto-update.out.log"
            self.out_offset = out_log.stat().st_size if out_log.exists() else 0
            state_file = self.home / ".agentcat/auto-update.json"
            self.update_state_before = state_file.read_bytes() if state_file.exists() else None
            self.trigger()
            report["servedVersion"] = self.wait_served(self.target)
            # The new daemon may answer before its parent installer finishes.
            # Require its final success record, not merely an early snapshot.
            self.wait_installed()
            if log.exists():
                with log.open("rb") as handle:
                    handle.seek(offset if log.stat().st_size >= offset else 0)
                    if b"Terminated" in handle.read():
                        raise RuntimeError("auto-update stderr contains Terminated")
            if self.system == "macos":
                self.check_macos(baseline_plist)
        except BaseException:
            # Stop this rehearsal's detached process tree before restoring source;
            # otherwise a late installer could overwrite the recovered version.
            self.stop_test_updater()
            try:
                self.install(recovery_assets)
            finally:
                if original_plist is not None:
                    self.plist.write_bytes(original_plist)
                    self.reload()
            self.wait_served(previous_version)
            raise

    def stop_test_updater(self):
        state_file = self.home / ".agentcat/auto-update.json"
        if not state_file.exists() or state_file.read_bytes() == self.update_state_before:
            return
        state = json.loads(state_file.read_text(encoding="utf-8"))
        pid = state.get("installPid")
        if state.get("status") != "update_started" or type(pid) is not int or pid <= 1:
            return
        if self.system == "windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
            return
        result = run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True)
        processes = {}
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3:
                processes[int(fields[0])] = (int(fields[1]), fields[2])
        if pid not in processes:
            return
        if str(self.install_dir / "install.sh") not in processes[pid][1]:
            raise RuntimeError("updater process identity changed; automatic recovery refused")
        descendants = [pid]
        for parent in descendants:
            descendants.extend(child for child, (ppid, _) in processes.items() if ppid == parent and child not in descendants)
        for child in reversed(descendants):
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def wait_installed(self):
        log = self.home / ".agentcat/auto-update.out.log"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if log.exists():
                with log.open("rb") as handle:
                    handle.seek(self.out_offset if log.stat().st_size >= self.out_offset else 0)
                    text = handle.read().decode("utf-8", errors="replace")
                # public_channel_install emits a final JSON object after health checks.
                decoder = json.JSONDecoder()
                for index, char in enumerate(text):
                    if char != "{":
                        continue
                    try:
                        value, _ = decoder.raw_decode(text[index:])
                    except ValueError:
                        continue
                    if isinstance(value, dict) and value.get("status") == "installed" and value.get("version") == self.target:
                        return
            time.sleep(1)
        raise RuntimeError("updater did not write its successful install result")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--os", choices=("macos", "windows"), dest="system")
    parser.add_argument("--upload", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.target):
        parser.error("target must be a numeric connector version")
    system = args.system or {"Darwin": "macos", "Windows": "windows"}.get(platform.system())
    if system is None or system != {"Darwin": "macos", "Windows": "windows"}.get(platform.system()):
        parser.error("run the selected mode on a real macOS or Windows machine")
    report = dict(version=args.target, archiveSha256=None, fromVersion=None, servedVersion=None,
                  os=system, arch=platform.machine(), durationSec=0, passed=False, checkedAt=None)
    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="agentcat-verify-") as work:
            Verification(args.target, system, Path.home(), Path(work)).execute(report)
        report["passed"] = True
    except Exception as exc:
        # No exception details in the shareable attestation (they may contain paths).
        print(f"Verification failed ({type(exc).__name__}): {exc}", file=sys.stderr)
    finally:
        report["durationSec"] = round(time.monotonic() - start, 2)
        report["checkedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        output = Path(f"verify-{system}.json")
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.upload:
        run(["gh", "release", "upload", f"v{args.target}", str(output), "--repo", REPO, "--clobber"])
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
