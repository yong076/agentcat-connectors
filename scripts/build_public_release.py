#!/usr/bin/env python3
"""Build a public connector ZIP and its checksum-bound release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


VERSION_RE = re.compile(
    r'CONNECTOR_VERSION\s*=\s*os\.environ\.get\(\s*[\'\"]AGENTCAT_CONNECTOR_VERSION[\'\"]\s*,\s*[\'\"]([^\'\"]+)[\'\"]'
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connector_version(repo: Path) -> str:
    text = (repo / "bin" / "agentcat").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if match is None:
        raise ValueError("connector version was not found")
    return match.group(1)


def connector_contract_version(repo: Path) -> int:
    payload = json.loads((repo / "contracts" / "connector-v1.json").read_text(encoding="utf-8"))
    version = int(payload.get("contractVersion") or 0)
    if version < 1:
        raise ValueError("connector contract version is invalid")
    return version


def build_release(repo: Path, output: Path, repository: str, source_ref: str) -> tuple[Path, Path]:
    version = connector_version(repo)
    output.mkdir(parents=True, exist_ok=True)
    archive_name = f"agentcat-connectors-v{version}.zip"
    archive = output / archive_name
    manifest = output / "connector-manifest.json"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "archive",
            "--format=zip",
            f"--prefix=agentcat-connectors-v{version}/",
            f"--output={archive}",
            source_ref,
        ],
        check=True,
    )
    payload = {
        "version": version,
        "contractVersion": connector_contract_version(repo),
        "archiveUrl": f"https://github.com/{repository}/releases/download/v{version}/{archive_name}",
        "sha256": sha256_file(archive),
    }
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return archive, manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", default="yong076/agentcat-connectors")
    parser.add_argument("--source-ref", default="HEAD")
    args = parser.parse_args()
    archive, manifest = build_release(args.repo.resolve(), args.output.resolve(), args.repository, args.source_ref)
    print(archive)
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
