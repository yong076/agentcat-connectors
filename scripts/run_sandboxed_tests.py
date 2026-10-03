#!/usr/bin/env python3
"""Copy the tree, isolate HOME, and run tests with stdin closed on every CI OS."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import public_channel_install as public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-version", help="Download this public N-1 release for the update rehearsal")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agentcat-suite-") as temp:
        root = Path(temp)
        source = root / "source"
        shutil.copytree(Path(__file__).resolve().parents[1], source,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".agentcat-local", "dist"))
        home = root / "home"
        home.mkdir()
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("AGENTCAT_", "CLAUDE_", "CODEX_")):
                env.pop(key)
        env.update(HOME=str(home), USERPROFILE=str(home), AGENTCAT_HOME=str(home / ".agentcat"),
                   APPDATA=str(home / "AppData/Roaming"), LOCALAPPDATA=str(home / "AppData/Local"))
        if args.previous_version:
            if not re.fullmatch(r"\d+\.\d+\.\d+", args.previous_version):
                parser.error("invalid previous version")
            base = f"https://github.com/yong076/agentcat-connectors/releases/download/v{args.previous_version}"
            manifest_path = root / "previous.json"
            archive = root / "previous.zip"
            with urllib.request.urlopen(base + "/connector-manifest.json", timeout=60) as response:
                manifest_path.write_bytes(response.read())
            manifest = public.read_manifest(manifest_path)
            if manifest["version"] != args.previous_version or not manifest["archiveUrl"].startswith(base + "/"):
                raise ValueError("unexpected previous release manifest")
            with urllib.request.urlopen(manifest["archiveUrl"], timeout=60) as response:
                archive.write_bytes(response.read())
            public.verify_archive(archive, manifest)
            env.update(AGENTCAT_TEST_PREVIOUS_ARCHIVE=str(archive), AGENTCAT_TEST_PREVIOUS_MANIFEST=str(manifest_path))
        return subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
                              cwd=source, env=env, stdin=subprocess.DEVNULL).returncode


if __name__ == "__main__":
    raise SystemExit(main())
