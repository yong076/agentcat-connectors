"""Recoverable compaction of known, closed installer backup directories."""
from __future__ import annotations

import hashlib
import datetime as dt
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import time


def _digest(handle):
    value = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        value.update(chunk)
    return value.hexdigest()


def compact_old_backups(root: Path, pattern: str, *, keep: int = 3,
                        minimum_age_days: int = 30, now=None) -> list[str]:
    """Keep recent folders; older known backups become verified private archives.

    Never visit symlinked roots/folders, unknown names or legacy-dirty recovery
    material. A complete archive is atomically published and verified against
    every regular source file before the old directory is removed. Archives
    remain available for manual recovery; no credential or backup is uploaded.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - minimum_age_days * 86400
    def created(path):
        match = re.search(r"(\d{8}-\d{6})(?:-\d+)?$", path.name)
        if match:
            try:
                return dt.datetime.strptime(match[1], "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                pass
        return path.stat().st_mtime
    candidates = sorted((p for p in root.iterdir() if not p.is_symlink() and p.is_dir()
                         and re.fullmatch(pattern, p.name)),
                        key=lambda p: (created(p), p.name), reverse=True)
    archived = []
    for source in candidates[max(1, keep):]:
        if created(source) >= cutoff:
            continue
        archive_root = root / "archive"
        if archive_root.is_symlink():
            raise ValueError("backup archive directory must not be a symlink")
        archive_root.mkdir(mode=0o700, exist_ok=True)
        target = archive_root / (source.name + ".tar.gz")
        if target.exists() or target.is_symlink():
            continue  # Never replace existing recovery material.
        with tempfile.TemporaryDirectory(prefix=".compact-", dir=archive_root) as temporary:
            candidate = Path(temporary) / "backup.tar.gz"
            with tarfile.open(candidate, "w:gz", dereference=False) as output:
                output.add(source, arcname=source.name, recursive=True)
            os.chmod(candidate, 0o600)
            with tarfile.open(candidate, "r:gz") as archive:
                members = archive.getmembers()
                regular = {m.name for m in members if m.isfile()}
                expected = {str(p.relative_to(source.parent)).replace(os.sep, "/")
                            for p in source.rglob("*") if p.is_file() and not p.is_symlink()}
                if regular != expected:
                    raise OSError("backup changed while archiving")
                for member in members:
                    if not member.isfile():
                        continue
                    local = source.parent / Path(member.name)
                    with local.open("rb") as before, archive.extractfile(member) as copied:
                        if _digest(before) != _digest(copied):
                            raise OSError("backup archive verification failed")
            # target did not exist; link publishes without an overwrite race.
            os.link(candidate, target)
        # Recovery is complete and verified before removing the closed source.
        shutil.rmtree(source)
        archived.append(target.name)
    return archived
