"""Private cold metadata preserving exact journal dedup after cursor pruning."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import urllib.request


def lookup_record(path: Path, epoch: str, key: str):
    if path.is_symlink():
        raise OSError("usage archive must not be a symlink")
    uri = "file:" + urllib.request.pathname2url(str(path.resolve())) + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
        row = connection.execute("select record_json from usage_records where epoch=? and record_key=?", (epoch, key)).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    if not isinstance(value, dict):
        raise sqlite3.DatabaseError("invalid archived usage metadata")
    return value


def archive_records(path: Path, epoch: str, records):
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("usage archive must not be a symlink")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        connection.execute("create table if not exists usage_records(epoch text not null, record_key text not null, record_json text not null, primary key(epoch, record_key))")
        with connection:
            connection.executemany("insert or replace into usage_records values (?, ?, ?)",
                ((epoch, key, json.dumps(value, separators=(",", ":"))) for key, value in records.items()))
