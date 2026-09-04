"""Hermes Agent token usage and actual billed cost from its local SQLite state."""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._base import HomeSpec, ProviderSpec, quota as _quota


SPEC = ProviderSpec(
    id="hermes", display_name="Hermes Agent", brand_color="#F28C28", icon_hint="hermes",
    windows=(), source_hint_key="provider.source.hermes",
    homes=(HomeSpec("HERMES_HOME", ".hermes", ("state.db",), ("state.db",), ".hermes*"),),
    capabilities=("usage.hermes.stateDb", "cost.hermes.actual"), standard_coverage=True,
)


def _home(ctx) -> Optional[Path]:
    raw = ctx.invoke("home_paths", SPEC)
    return Path(raw[0]) if isinstance(raw, list) and raw else None


def discover(ctx):
    home = _home(ctx)
    return [home] if home is not None and (home / "state.db").is_file() else []


def _empty(status: str = "not_found") -> Dict[str, Any]:
    return {
        "status": status, "source": "hermes-state-db" if status == "ok" else None,
        "tokens": {"today": 0, "week": 0, "month": 0, "all": 0},
        "models": {}, "dailyTokens": {}, "hourlyTokens": {},
        "breakdown": {"status": "not_available"},
        "projects": {"status": "not_available", "items": []},
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []


def _periods(timestamp: float, now: dt.datetime) -> List[str]:
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    when = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
    periods = ["all"]
    if when >= now - dt.timedelta(days=30): periods.append("month")
    if when >= now - dt.timedelta(days=7): periods.append("week")
    if when.date() == now.date(): periods.append("today")
    return periods


def _rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    model_columns = _table_columns(conn, "session_model_usage")
    if model_columns:
        fields = [
            "COALESCE(m.model, '') AS model",
            "COALESCE(m.input_tokens, 0) AS input_tokens",
            "COALESCE(m.output_tokens, 0) AS output_tokens",
            "COALESCE(m.cache_read_tokens, 0) AS cache_read_tokens",
            "COALESCE(m.cache_write_tokens, 0) AS cache_write_tokens",
            "COALESCE(m.reasoning_tokens, 0) AS reasoning_tokens",
            "COALESCE(m.actual_cost_usd, 0) AS actual_cost_usd",
            "COALESCE(m.last_seen, m.first_seen, s.ended_at, s.started_at, 0) AS occurred_at",
        ]
        try:
            return conn.execute(
                "SELECT " + ", ".join(fields) +
                " FROM session_model_usage m LEFT JOIN sessions s ON s.id = m.session_id"
            ).fetchall()
        except sqlite3.Error:
            pass
    session_columns = set(_table_columns(conn, "sessions"))
    required = {"started_at", "input_tokens", "output_tokens"}
    if not required.issubset(session_columns):
        return []
    optional = lambda name, fallback="0": f"COALESCE({name}, {fallback})" if name in session_columns else fallback
    return conn.execute(
        "SELECT " + ", ".join((
            optional("model", "''") + " AS model",
            f"{optional('input_tokens')} AS input_tokens",
            f"{optional('output_tokens')} AS output_tokens",
            f"{optional('cache_read_tokens')} AS cache_read_tokens",
            f"{optional('cache_write_tokens')} AS cache_write_tokens",
            f"{optional('reasoning_tokens')} AS reasoning_tokens",
            f"{optional('actual_cost_usd')} AS actual_cost_usd",
            f"{optional('ended_at', 'started_at')} AS occurred_at",
        )) + " FROM sessions"
    ).fetchall()


def usage(ctx, home=None) -> Dict[str, Any]:
    resolved_home = _home(ctx)
    db_path = resolved_home / "state.db" if resolved_home is not None else None
    if db_path is None or not db_path.is_file():
        return _empty()
    result = _empty("ok")
    now = dt.datetime.now(dt.timezone.utc)
    actual_cost = 0.0
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = _rows(conn)
    except (OSError, sqlite3.Error, ValueError):
        return _empty("error")
    for row in rows:
        values = {name: int(row[name] or 0) for name in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"
        )}
        total = sum(values.values())
        occurred_at = float(row["occurred_at"] or 0)
        periods = _periods(occurred_at, now)
        for period in periods:
            result["tokens"][period] += total
        model = str(row["model"] or "unknown")
        bucket = result["models"].setdefault(model, {"today": 0, "week": 0, "month": 0, "all": 0})
        for period in periods:
            bucket[period] += total
        for key, value in values.items():
            public_key = {
                "input_tokens": "inputTokens", "output_tokens": "outputTokens",
                "cache_read_tokens": "cacheReadInputTokens", "cache_write_tokens": "cacheCreationInputTokens",
                "reasoning_tokens": "reasoningTokens",
            }[key]
            result["tokens"][public_key] = result["tokens"].get(public_key, 0) + value
            bucket[public_key] = bucket.get(public_key, 0) + value
        actual_cost += float(row["actual_cost_usd"] or 0.0)
    result["actualCostUSD"] = round(actual_cost, 6)
    result["costSource"] = "hermes-state-db"
    result["costEstimated"] = False
    return result


def quota(ctx, home=None): return _quota(ctx, SPEC, home)


def cost(ctx, usage_slice) -> Dict[str, Any]:
    return {
        "status": "ok", "totalUSD": float(usage_slice.get("actualCostUSD") or 0.0),
        "source": "hermes-state-db", "estimated": False,
    } if usage_slice.get("status") == "ok" else {}


def health(ctx) -> Dict[str, Any]:
    return {"status": "ok" if discover(ctx) else "not_found", "source": "hermes-state-db"}
