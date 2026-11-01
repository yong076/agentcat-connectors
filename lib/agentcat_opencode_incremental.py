"""Usage-only changed-row reads for OpenCode schemas with update timestamps."""
from __future__ import annotations

import sqlite3

def prepare_rows(connection, read_rows, cached=None):
    """Return rows, warnings, candidate cache, without publishing during a retry.

    Timestamp-only high-water marks miss deletes, backfilled rows, corrections
    and session archival. Compare narrow ID/update metadata and session fields,
    then request only changed usage rows. Unsupported schemas keep the complete
    reader. Existing generation/import dedup semantics stay in read_rows.
    """
    if not isinstance(connection, sqlite3.Connection):
        rows, warnings = read_rows(connection)
        return rows, warnings, None
    if not connection.in_transaction:
        connection.execute("begin")
    try:
        connection.execute("select json_extract('{}', '$.id')")
    except sqlite3.OperationalError:
        # The fallback reader supports older SQLite without JSON projection;
        # do not retain whole message blobs in the incremental cache there.
        rows, warnings = read_rows(connection)
        return rows, warnings, None
    schema = tuple(connection.execute("select name, sql from sqlite_master where type='table' order by name"))
    tables = {name for name, _ in schema}
    versions = {}
    generations = {}
    session_state = []
    for table in ("session_message", "message"):
        if table not in tables:
            continue
        columns = {row[1] for row in connection.execute(f"pragma table_info({table})")}
        if not {"id", "time_updated", "session_id"}.issubset(columns):
            rows, warnings = read_rows(connection)
            return rows, warnings, None
        rows = list(connection.execute(f"select id, time_updated, session_id from {table} limit 10001"))
        if len(rows) > 10000:
            result, warnings = read_rows(connection)
            return result, warnings, None
        if any(not isinstance(identifier, str) or not identifier or not isinstance(updated, int)
               for identifier, updated, _ in rows):
            result, warnings = read_rows(connection)
            return result, warnings, None
        current = {identifier: (updated, session) for identifier, updated, session in rows}
        if len(current) != len(rows):
            result, warnings = read_rows(connection)
            return result, warnings, None
        generations[table] = current
        versions.update({(table, identifier): version for identifier, version in current.items()})
    if not generations:
        result, warnings = read_rows(connection)
        return result, warnings, None
    for table in ("session", "session_v2"):
        if table not in tables:
            continue
        columns = {row[1] for row in connection.execute(f"pragma table_info({table})")}
        allowed = [name for name in ("id", "parent_id", "time_archived", "directory", "cwd", "working_directory", "path", "model")
                   if name in columns]
        if allowed:
            session_state.append((table, tuple(connection.execute(f"select {', '.join(allowed)} from {table} order by id"))))
    newer = generations.get("session_message", {})
    older = generations.get("message", {})
    new_sessions = frozenset(session for _, session in newer.values())
    shared_sessions = frozenset(session for identifier, (_, session) in older.items() if identifier in newer)
    context = (schema, session_state, new_sessions, shared_sessions)
    previous = cached if isinstance(cached, dict) and cached.get("context") == context else None
    changed = {table: [identifier for identifier, version in current.items()
                        if previous is None or previous["versions"].get((table, identifier)) != version]
               for table, current in generations.items()}
    if previous is None or sum(map(len, changed.values())) > 500:
        result, warnings = read_rows(connection)
    elif not any(changed.values()):
        result, warnings = list(previous["rows"].values()), list(previous["warnings"])
    else:
        fresh, warnings = read_rows(connection, changed)
        # Rows contain generation flag, ID, time, usage-only JSON, project, include.
        retained = {key: row for key, row in previous["rows"].items()
                    if key in versions and key[1] not in changed.get(key[0], ())}
        retained.update({("session_message" if row[0] else "message", row[1]): row for row in fresh})
        result = list(retained.values())
    # Deletes are recognized even when no payload changed.
    result = [row for row in result if ("session_message" if row[0] else "message", row[1]) in versions]
    result.sort(key=lambda row: (not row[0], str(row[1])))
    candidate = None
    if len(result) <= 10000 and not warnings:
        candidate = {"context": context, "versions": versions, "warnings": list(warnings),
                     "rows": {("session_message" if row[0] else "message", row[1]): row for row in result}}
    return result, warnings, candidate
