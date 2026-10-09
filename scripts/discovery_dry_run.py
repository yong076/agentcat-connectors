#!/usr/bin/env python3
"""Operator check: classify this machine's CLI homes without touching state.

Read-only. Runs home discovery against the real HOME with a throwaway
AGENTCAT_HOME, no provider probes and no settings writes, and prints one row per
home (state, tilde path, sources, launchers, evidence) plus the timing. Use it
after any change to discovery: fake-HOME tests missed real-scale problems
(budget, candidate order, IPC cost) three times on 2026-10-09.

Never run this inside tests (AGENTS.md R10). Usage:
    python3 scripts/discovery_dry_run.py [--json]
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="agentcat-discovery-") as state:
        # Module constants read AGENTCAT_HOME at import; ambient CLI overrides
        # would change what the real daemon sees, so drop them.
        os.environ["AGENTCAT_HOME"] = state
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(key, None)
        sys.path.insert(0, str(ROOT / "lib"))
        loader = importlib.machinery.SourceFileLoader("agentcat_discovery_dry_run", str(ROOT / "bin/agentcat"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        if not os.path.realpath(str(module.AGENTCAT_HOME)).startswith(os.path.realpath(state)):
            print("refusing to run: AGENTCAT_HOME is not the throwaway directory", file=sys.stderr)
            return 2
        start = time.monotonic()
        snapshot = module.home_discovery_snapshot(force=True)
        elapsed = time.monotonic() - start
        home = str(Path.home())
        rows = []
        for provider, block in snapshot.items():
            for item in (block or {}).get("discovered", []) if isinstance(block, dict) else []:
                rows.append({"provider": provider, "state": item.get("state"),
                             "path": str(item.get("path", "")).replace(home, "~"),
                             "sources": item.get("sources"), "launchers": item.get("launchers"),
                             "evidence": (item.get("evidence") or [])[:4]})
        if "--json" in argv:
            print(json.dumps({"seconds": round(elapsed, 2), "complete": module._HOME_CANDIDATES_COMPLETE,
                              "homes": rows}, indent=2))
        else:
            for row in rows:
                print(f"{row['provider']:7} {str(row['state']):9} {row['path']}  "
                      f"sources={row['sources']} launchers={row['launchers']} evidence={row['evidence']}")
            print(f"{elapsed:.2f}s complete={module._HOME_CANDIDATES_COMPLETE}")
    return 0


if __name__ == "__main__":  # required: the discovery worker uses multiprocessing spawn
    raise SystemExit(main(sys.argv[1:]))
