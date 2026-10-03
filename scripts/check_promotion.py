#!/usr/bin/env python3
"""Validate real-machine attestations against the exact release artifact."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import re
from pathlib import Path


def validate(version, directory):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("invalid version")
    manifest = json.loads((directory / "connector-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("sha256", "")):
        raise ValueError("manifest version or checksum mismatch")
    for system in ("macos", "windows"):
        report = json.loads((directory / f"verify-{system}.json").read_text(encoding="utf-8"))
        if (report.get("passed") is not True or report.get("version") != version
                or report.get("servedVersion") != version or report.get("os") != system
                or report.get("archiveSha256") != manifest["sha256"]
                or not report.get("fromVersion") or report["fromVersion"] == version):
            raise ValueError(f"{system} update-path verification does not match this release")
    return {"version": version, "promotedAt": dt.datetime.now(dt.timezone.utc).isoformat()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.version, args.directory)
    (args.directory / "rollout.json").write_text(json.dumps(result) + "\n", encoding="utf-8")
