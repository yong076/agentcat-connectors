#!/usr/bin/env python3
"""Verified, rollback-capable installer for the public connector channel."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional


class DirtyCheckoutError(RuntimeError):
    def __init__(self, recovery_path: Path) -> None:
        super().__init__(
            "existing connector checkout has local changes; automatic replacement refused; "
            f"recovery material: {recovery_path}"
        )
        self.recovery_path = recovery_path


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    version = str(payload.get("version") or "").strip()
    digest = str(payload.get("sha256") or "").strip()
    contract_version = int(payload.get("contractVersion") or 0)
    if not version or any(ch not in "0123456789." for ch in version):
        raise ValueError("manifest.version is invalid")
    if len(digest) != 64 or digest.lower() != digest or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("manifest.sha256 must be 64 lowercase hex chars")
    if contract_version < 1:
        raise ValueError("manifest.contractVersion must be positive")
    return payload


def verify_archive(archive: Path, manifest: dict[str, Any]) -> None:
    actual = sha256_file(archive)
    if actual != manifest["sha256"]:
        raise ValueError(f"checksum mismatch: expected {manifest['sha256']}, got {actual}")


def _safe_target(destination: Path, member_name: str) -> Path:
    if not member_name or member_name.startswith(("/", "\\")):
        raise ValueError(f"unsafe archive path: {member_name}")
    parts = Path(member_name.replace("\\", "/")).parts
    if ".." in parts:
        raise ValueError(f"unsafe archive path: {member_name}")
    target = (destination / Path(*parts)).resolve()
    root = destination.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"archive path escapes destination: {member_name}")
    return target


def extract_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                _safe_target(destination, info.filename)
            package.extractall(destination)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as package:
            members = package.getmembers()
            for member in members:
                _safe_target(destination, member.name)
                if member.issym() or member.islnk():
                    _safe_target(destination, member.linkname)
            package.extractall(destination, members=members)
    else:
        raise ValueError("unsupported connector archive")

    candidates = [
        path
        for path in destination.iterdir()
        if path.is_dir()
        and (path / "bin" / "agentcat").is_file()
        and (path / "scripts" / "install.py").is_file()
        and (path / "contracts" / "connector-v1.json").is_file()
    ]
    if len(candidates) != 1:
        raise ValueError("archive must contain exactly one connector root")
    return candidates[0]


def _run_git(repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )


def dirty_checkout_status(install_dir: Path) -> list[str]:
    if not (install_dir / ".git").exists():
        return []
    result = _run_git(install_dir, "status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        raise RuntimeError("could not inspect the existing connector checkout")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _unique_backup_path(root: Path, prefix: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / f"{prefix}-{timestamp()}"
    counter = 1
    while candidate.exists():
        candidate = root / f"{prefix}-{timestamp()}-{counter}"
        counter += 1
    return candidate


def preserve_dirty_checkout(install_dir: Path, backup_root: Path, status: list[str]) -> Path:
    recovery = _unique_backup_path(backup_root, "legacy-dirty")
    recovery.mkdir(parents=True)
    (recovery / "status.txt").write_text("\n".join(status) + "\n", encoding="utf-8")

    # HEAD covers staged and unstaged tracked changes in one restorable patch.
    diff = _run_git(install_dir, "diff", "HEAD", "--binary", text=False)
    if diff.returncode == 0:
        (recovery / "tracked.patch").write_bytes(diff.stdout)

    untracked_root = recovery / "untracked"
    copied: list[str] = []
    untracked = _run_git(
        install_dir,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        text=False,
    )
    names = untracked.stdout.split(b"\0") if untracked.returncode == 0 else []
    for raw_name in names:
        if not raw_name:
            continue
        relative = os.fsdecode(raw_name).replace("/", os.sep)
        source = (install_dir / relative).resolve()
        try:
            source.relative_to(install_dir.resolve())
        except ValueError:
            continue
        if not source.is_file():
            continue
        destination = untracked_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(relative.replace(os.sep, "/"))
    (recovery / "untracked-files.json").write_text(
        json.dumps(copied, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return recovery


def validate_candidate(candidate: Path, manifest: dict[str, Any]) -> None:
    contract_path = candidate / "contracts" / "connector-v1.json"
    with contract_path.open("r", encoding="utf-8") as file:
        contract = json.load(file)
    if int(contract.get("contractVersion") or 0) != int(manifest["contractVersion"]):
        raise ValueError("candidate contract version does not match the manifest")

    env = os.environ.copy()
    for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENTCAT_CONNECTORS_DIR"):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="agentcat-candidate-home-") as temp_home:
        env["AGENTCAT_HOME"] = str(Path(temp_home) / ".agentcat")
        compile_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "py_compile",
                str(candidate / "bin" / "agentcat"),
                str(candidate / "scripts" / "install.py"),
                str(candidate / "scripts" / "public_channel_install.py"),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        if compile_result.returncode != 0:
            raise ValueError("candidate Python compilation failed")
        command = [
            sys.executable,
            str(candidate / "bin" / "agentcat"),
            "version",
            "--json",
        ]
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
    if result.returncode != 0:
        raise ValueError("candidate version check failed")
    payload = json.loads(result.stdout)
    if str(payload.get("connectorVersion") or "") != str(manifest["version"]):
        raise ValueError("candidate version does not match the manifest")
    if int(payload.get("contractVersion") or 0) != int(manifest["contractVersion"]):
        raise ValueError("candidate runtime contract does not match the manifest")
    capabilities = set(payload.get("capabilities") or [])
    if "connector.contract.v1" not in capabilities or "connector.daemon.status.v1" not in capabilities:
        raise ValueError("candidate is missing release-required capabilities")


def run_installer(install_dir: Path) -> None:
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(install_dir / "scripts" / "install.py"),
                "--repo-dir",
                str(install_dir),
                "install",
            ],
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("connector installer timed out") from exc
    if result.returncode != 0:
        raise RuntimeError(f"connector installer exited {result.returncode}")


def validate_live_connector(expected_version: str, expected_contract: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "daemon did not become healthy"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/healthz", timeout=2) as response:
                if response.read().decode("utf-8", "replace").strip() != "ok":
                    raise ValueError("unexpected health response")
            # Version is a dedicated bounded endpoint. A full snapshot can scan
            # large local histories and must never decide whether an update is
            # healthy or trigger a rollback merely because collection is slow.
            with urllib.request.urlopen("http://127.0.0.1:8765/v1/version", timeout=2) as response:
                runtime = json.load(response)
            if str(runtime.get("connectorVersion") or "") != expected_version:
                raise ValueError("daemon is serving a different connector version")
            if int(runtime.get("contractVersion") or 0) != expected_contract:
                raise ValueError("daemon is serving a different contract version")
            return
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(last_error)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def activate_candidate(candidate: Path, install_dir: Path, backup_root: Path) -> Optional[Path]:
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = install_dir.parent / f".{install_dir.name}.next-{os.getpid()}-{timestamp()}"
    _remove_path(staging)
    shutil.copytree(candidate, staging)

    previous: Optional[Path] = None
    if install_dir.exists() or install_dir.is_symlink():
        previous = _unique_backup_path(backup_root, f"{install_dir.name}-previous")
        shutil.move(str(install_dir), str(previous))
    try:
        shutil.move(str(staging), str(install_dir))
    except Exception:
        _remove_path(staging)
        if previous is not None and previous.exists():
            shutil.move(str(previous), str(install_dir))
        raise
    return previous


def rollback(install_dir: Path, previous: Optional[Path]) -> None:
    _remove_path(install_dir)
    if previous is not None and previous.exists():
        shutil.move(str(previous), str(install_dir))


def install_public_archive(
    archive: Path,
    manifest: dict[str, Any],
    install_dir: Path,
    backup_root: Path,
    *,
    candidate_validator: Callable[[Path, dict[str, Any]], None] = validate_candidate,
    installer: Callable[[Path], None] = run_installer,
    live_validator: Callable[[str, int], None] = validate_live_connector,
) -> dict[str, Any]:
    archive = archive.expanduser().resolve()
    install_dir = install_dir.expanduser().resolve()
    backup_root = backup_root.expanduser().resolve()
    try:
        backup_root.relative_to(install_dir)
    except ValueError:
        pass
    else:
        raise ValueError("backup root must be outside the connector install directory")

    verify_archive(archive, manifest)
    status = dirty_checkout_status(install_dir)
    if status:
        recovery = preserve_dirty_checkout(install_dir, backup_root, status)
        raise DirtyCheckoutError(recovery)

    with tempfile.TemporaryDirectory(prefix="agentcat-public-connector-") as temp:
        candidate = extract_archive(archive, Path(temp))
        candidate_validator(candidate, manifest)
        previous = activate_candidate(candidate, install_dir, backup_root)

    try:
        installer(install_dir)
        live_validator(str(manifest["version"]), int(manifest["contractVersion"]))
    except Exception:
        rollback(install_dir, previous)
        if previous is not None:
            try:
                installer(install_dir)
            except Exception:
                pass
        raise

    return {
        "status": "installed",
        "channel": "public",
        "version": manifest["version"],
        "contractVersion": manifest["contractVersion"],
        "sha256": manifest["sha256"],
        "installDir": str(install_dir),
        "rollbackPath": str(previous) if previous is not None else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install a verified Agent Cat public connector archive.")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, default=Path.home() / ".agentcat" / "connectors")
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path.home() / ".agentcat" / "backups" / "connector-source",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = install_public_archive(
            args.archive,
            read_manifest(args.manifest),
            args.install_dir,
            args.backup_root,
        )
    except DirtyCheckoutError as exc:
        print(json.dumps({"status": "blocked_dirty", "recoveryPath": str(exc.recovery_path)}))
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
