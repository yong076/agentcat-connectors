#!/usr/bin/env python3
import datetime as dt
import importlib.util
import os
import sqlite3
import tempfile
from contextlib import closing
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Callable, Dict, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


def write_hermes(home: Path) -> None:
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    db_path = hermes_home / "state.db"
    now_s = dt.datetime.now(dt.timezone.utc).timestamp()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            create table sessions (
              id text primary key,
              model text,
              billing_provider text,
              started_at real,
              message_count integer,
              input_tokens integer,
              output_tokens integer,
              cache_read_tokens integer,
              cache_write_tokens integer,
              reasoning_tokens integer,
              estimated_cost_usd real,
              actual_cost_usd real
            )
            """
        )
        conn.executemany(
            """
            insert into sessions(
              id, model, billing_provider, started_at, message_count,
              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
              reasoning_tokens, estimated_cost_usd, actual_cost_usd
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("actual", "claude-sonnet-4-6", "anthropic", now_s, 3, 1200, 300, 50, 20, 10, 0.12, 0.34),
                ("skip-zero", "claude-sonnet-4-6", "anthropic", now_s, 1, 0, 0, 0, 0, 0, 0, 0),
                ("repriced", "claude-sonnet-4-6", "anthropic", now_s, 1, 1_000_000, 0, 0, 0, 0, 0, 0),
            ],
        )
        conn.commit()


ProviderFixture = Tuple[str, Callable[[Path], None], Callable[[], Dict[str, object]]]
PROVIDERS: Tuple[ProviderFixture, ...] = (
    ("hermes", write_hermes, agentcat.hermes_snapshot),
)


def run_provider(provider: ProviderFixture) -> None:
    provider_id, writer, snapshot_fn = provider
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        agentcat_home = root / "agentcat"
        home.mkdir()
        agentcat_home.mkdir()

        old_home = agentcat.HOME
        old_agentcat_home = agentcat.AGENTCAT_HOME
        old_env = os.environ.get("HERMES_HOME")
        try:
            agentcat.HOME = home
            agentcat.AGENTCAT_HOME = agentcat_home
            os.environ["HERMES_HOME"] = str(home / ".hermes")
            writer(home)
            snapshot = snapshot_fn()
        finally:
            agentcat.HOME = old_home
            agentcat.AGENTCAT_HOME = old_agentcat_home
            if old_env is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_env

    assert snapshot["status"] == "ok", snapshot
    assert snapshot["events"] == 2, snapshot
    tokens = snapshot["tokens"]
    assert tokens["totalTokens"] == 1_001_580, snapshot
    assert abs(snapshot["actualCostUsd"] - 0.34) < 0.000001, snapshot
    assert abs(snapshot["repricedCostUsd"] - 3.0) < 0.000001, snapshot
    print(f"ok {provider_id}: {snapshot['events']} sessions, {tokens['totalTokens']} tokens")


def main() -> int:
    for provider in PROVIDERS:
        run_provider(provider)
    print(f"provider fixture e2e: {len(PROVIDERS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
