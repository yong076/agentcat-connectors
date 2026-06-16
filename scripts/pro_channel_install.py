#!/usr/bin/env python3
"""Install a Pro connector archive into a managed connector directory.

Local-only helper for the Pro channel. It verifies the manifest checksum,
extracts a tarball safely, swaps the candidate into place, and keeps a rollback
backup. It does not fetch private URLs or check billing; the app/API supplies a
manifest only after entitlement exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def read_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive_checksum(archive: Path, manifest: dict[str, Any]) -> str:
    expected = str(manifest.get("sha256") or "").strip()
    if len(expected) != 64 or expected.lower() != expected:
        raise ValueError("manifest.sha256 must be 64 lowercase hex chars")
    actual = sha256_file(archive)
    if actual != expected:
        raise ValueError(f"checksum mismatch: expected {expected}, got {actual}")
    return actual


def emit_connector_event(
    event: str,
    properties: dict[str, Any],
    *,
    event_log: Path | None = None,
    event_api_url: str | None = None,
    event_bearer: str | None = None,
    device_id: str | None = None,
) -> None:
    """Best-effort Pro connector lifecycle telemetry.

    The installer must remain usable as a local/offline safe-swap helper, so
    event emission is optional and never affects install success. The payload is
    intentionally path-free: install/backup paths stay in the local result JSON,
    not in product events.
    """
    payload = {
        "event": event,
        "surface": "pro_connector",
        "plan": "pro",
        "device_id": device_id or "local_connector",
        "properties": properties,
    }
    if event_log is not None:
        try:
            event_log = event_log.expanduser().resolve()
            event_log.parent.mkdir(parents=True, exist_ok=True)
            with event_log.open("a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            pass
    if event_api_url:
        try:
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                event_api_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "agentcat-pro-connector-installer",
                },
                method="POST",
            )
            if event_bearer:
                request.add_header("Authorization", f"Bearer {event_bearer}")
            urllib.request.urlopen(request, timeout=3).close()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            pass


def _safe_tar_members(tar: tarfile.TarFile, destination: Path) -> list[tarfile.TarInfo]:
    dest = destination.resolve()
    members = tar.getmembers()
    for member in members:
        name = member.name
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise ValueError(f"unsafe archive path: {name}")
        target = (destination / name).resolve()
        if dest != target and dest not in target.parents:
            raise ValueError(f"archive path escapes destination: {name}")
        if member.islnk() or member.issym():
            link = member.linkname or ""
            if link.startswith("/") or ".." in Path(link).parts:
                raise ValueError(f"unsafe archive link: {name}")
    return members


def extract_connector_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(destination, members=_safe_tar_members(tar, destination))
    candidates = [
        path for path in destination.iterdir()
        if path.is_dir() and (path / "bin" / "agentcat").is_file() and (path / "scripts" / "install.py").is_file()
    ]
    if not candidates:
        raise ValueError("archive does not contain an Agent Cat connector root")
    if len(candidates) > 1:
        raise ValueError("archive contains multiple connector roots")
    return candidates[0]


def backup_existing_install(install_dir: Path, backup_root: Path) -> Path | None:
    if not install_dir.exists() and not install_dir.is_symlink():
        return None
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"{install_dir.name}.{timestamp()}"
    counter = 1
    while backup_path.exists():
        backup_path = backup_root / f"{install_dir.name}.{timestamp()}.{counter}"
        counter += 1
    shutil.move(str(install_dir), str(backup_path))
    return backup_path


def rollback_install(install_dir: Path, backup_path: Path | None) -> None:
    if install_dir.exists() or install_dir.is_symlink():
        if install_dir.is_dir() and not install_dir.is_symlink():
            shutil.rmtree(install_dir)
        else:
            install_dir.unlink()
    if backup_path is not None and backup_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup_path), str(install_dir))


def swap_connector(candidate_dir: Path, install_dir: Path, backup_root: Path) -> dict[str, Any]:
    install_dir = install_dir.expanduser().resolve()
    backup_root = backup_root.expanduser().resolve()
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = install_dir.parent / f".{install_dir.name}.next-{os.getpid()}-{timestamp()}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    shutil.copytree(candidate_dir, staging_dir)
    backup_path = backup_existing_install(install_dir, backup_root)
    try:
        shutil.move(str(staging_dir), str(install_dir))
    except Exception:
        rollback_install(install_dir, backup_path)
        raise
    return {
        "installDir": str(install_dir),
        "backupPath": str(backup_path) if backup_path else None,
    }


def install_pro_archive(
    archive: Path,
    manifest: dict[str, Any],
    install_dir: Path,
    backup_root: Path,
    event_log: Path | None = None,
    event_api_url: str | None = None,
    event_bearer: str | None = None,
    device_id: str | None = None,
) -> dict[str, Any]:
    archive = archive.expanduser().resolve()
    verify_archive_checksum(archive, manifest)
    with tempfile.TemporaryDirectory(prefix="agentcat-pro-connector.") as tmp:
        candidate = extract_connector_archive(archive, Path(tmp))
        event_properties = {
            "version": str(manifest.get("version") or ""),
            "sha256": str(manifest.get("sha256") or ""),
        }
        emit_connector_event(
            "pro_connector_swap_started",
            event_properties,
            event_log=event_log,
            event_api_url=event_api_url,
            event_bearer=event_bearer,
            device_id=device_id,
        )
        try:
            result = swap_connector(candidate, install_dir, backup_root)
        except Exception as exc:
            rolled_back_properties = dict(event_properties)
            rolled_back_properties["reason"] = exc.__class__.__name__
            emit_connector_event(
                "pro_connector_swap_rolled_back",
                rolled_back_properties,
                event_log=event_log,
                event_api_url=event_api_url,
                event_bearer=event_bearer,
                device_id=device_id,
            )
            raise
        emit_connector_event(
            "pro_connector_swap_succeeded",
            event_properties,
            event_log=event_log,
            event_api_url=event_api_url,
            event_bearer=event_bearer,
            device_id=device_id,
        )
    result.update(
        {
            "channel": "pro",
            "version": manifest.get("version"),
            "sha256": manifest.get("sha256"),
            "status": "installed",
        }
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install a verified Agent Cat Pro connector archive.")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, default=Path.home() / ".agentcat" / "connectors")
    parser.add_argument("--backup-root", type=Path, default=Path.home() / ".agentcat" / "backups" / "pro-channel")
    parser.add_argument("--event-log", type=Path, help="Optional JSONL lifecycle event log for local QA.")
    parser.add_argument("--event-api-url", help="Optional Pro product event endpoint, e.g. https://api.agentcat.app/v1/pro/events.")
    parser.add_argument("--event-bearer", help="Optional signed Pro session token for the event endpoint.")
    parser.add_argument("--device-id", help="Optional product-event device id. Defaults to local_connector.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = install_pro_archive(
        archive=args.archive,
        manifest=read_manifest(args.manifest),
        install_dir=args.install_dir,
        backup_root=args.backup_root,
        event_log=args.event_log,
        event_api_url=args.event_api_url,
        event_bearer=args.event_bearer,
        device_id=args.device_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
