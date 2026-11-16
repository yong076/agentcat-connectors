"""Private cold metadata preserving exact journal dedup after cursor pruning."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.request


def _ensure_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise OSError("usage archive must not be a symlink")


@contextmanager
def open_lookup_connection(path: Path):
    _ensure_not_symlink(path)
    if not path.is_file():
        yield None
        return
    uri = "file:" + urllib.request.pathname2url(str(path.resolve())) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
        yield connection


def lookup_record(path: Path, epoch: str, key: str, connection: sqlite3.Connection | None = None):
    if connection is not None:
        row = connection.execute(
            "select record_json from usage_records where epoch=? and record_key=?",
            (epoch, key),
        ).fetchone()
    else:
        _ensure_not_symlink(path)
        uri = "file:" + urllib.request.pathname2url(str(path.resolve())) + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as readonly_conn:
            row = readonly_conn.execute(
                "select record_json from usage_records where epoch=? and record_key=?",
                (epoch, key),
            ).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise sqlite3.DatabaseError("invalid archived usage metadata")
    return value


def _rebuild_single_epoch(path: Path, epoch: str, extra_records=None) -> None:
    """Atomically replace a multi-epoch archive with a compact single-epoch file.

    Copying only active-epoch rows avoids an expensive in-place DELETE + VACUUM
    across millions of orphaned rows accumulated from prior rebuilds.
    """
    _ensure_not_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        kept_rows = []
        if path.is_file():
            uri = "file:" + urllib.request.pathname2url(str(path.resolve())) + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as src:
                try:
                    kept_rows = src.execute(
                        "select epoch, record_key, record_json from usage_records where epoch=?",
                        (epoch,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    kept_rows = []
        with closing(sqlite3.connect(tmp, timeout=5)) as dst:
            dst.execute(
                "create table if not exists usage_records("
                "epoch text not null, record_key text not null, record_json text not null, "
                "primary key(epoch, record_key))"
            )
            with dst:
                if kept_rows:
                    dst.executemany(
                        "insert or replace into usage_records values (?, ?, ?)",
                        kept_rows,
                    )
                if extra_records:
                    dst.executemany(
                        "insert or replace into usage_records values (?, ?, ?)",
                        (
                            (epoch, key, json.dumps(value, separators=(",", ":")))
                            for key, value in extra_records.items()
                        ),
                    )
        _ensure_not_symlink(path)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def prune_stale_epochs(path: Path, epoch: str) -> bool:
    if not isinstance(epoch, str) or not epoch or not path.exists():
        return False
    _ensure_not_symlink(path)
    if not path.is_file():
        return False
    uri = "file:" + urllib.request.pathname2url(str(path.resolve())) + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
            has_stale = conn.execute(
                "select 1 from usage_records where epoch != ? limit 1",
                (epoch,),
            ).fetchone()
    except sqlite3.Error:
        return False
    if has_stale is None:
        return False
    _rebuild_single_epoch(path, epoch)
    return True


def archive_records(path: Path, epoch: str, records):
    if not records:
        prune_stale_epochs(path, epoch)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_not_symlink(path)
    if path.is_file():
        try:
            uri = "file:" + urllib.request.pathname2url(str(path.resolve())) + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=2)) as check_conn:
                has_stale = check_conn.execute(
                    "select 1 from usage_records where epoch != ? limit 1",
                    (epoch,),
                ).fetchone()
        except sqlite3.Error:
            has_stale = None
        if has_stale is not None:
            _rebuild_single_epoch(path, epoch, extra_records=records)
            return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        connection.execute(
            "create table if not exists usage_records("
            "epoch text not null, record_key text not null, record_json text not null, "
            "primary key(epoch, record_key))"
        )
        with connection:
            connection.executemany(
                "insert or replace into usage_records values (?, ?, ?)",
                ((epoch, key, json.dumps(value, separators=(",", ":"))) for key, value in records.items()),
            )
