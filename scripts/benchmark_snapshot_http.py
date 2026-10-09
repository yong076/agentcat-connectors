#!/usr/bin/env python3
"""Synthetic loopback latency benchmark; no real homes, scans, or providers."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch


def run():
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temp:
        with patch.dict(os.environ, {"HOME": temp, "AGENTCAT_HOME": temp + "/.agentcat"}):
            loader = SourceFileLoader("benchmark_agentcat", str(repo / "bin/agentcat"))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        sys.path.insert(0, str(repo / "tests"))
        from sandbox import redirect_module_paths, restore_module_paths
        original = redirect_module_paths(module, Path(temp), Path(temp) / ".agentcat")
        fixture = {"schemaVersion": 4, "generatedAt": "2026-10-01T00:00:00Z",
                   "providers": {"fixture": {"rows": [dict(tokens=i, label="x" * 100) for i in range(1500)]}},
                   "activity": {"status": "ok", "processes": []}}
        module.write_json_atomic(module.LATEST_SNAPSHOT, fixture)
        if hasattr(module, "initialize_http_snapshot_cache"):
            module.initialize_http_snapshot_cache()
        entered, release = threading.Event(), threading.Event()
        def blocked_build():
            entered.set()
            release.wait()
            return fixture
        def fake_scan():
            time.sleep(0.25)  # deterministic slow process sample, no subprocess
            return fixture["activity"]
        with patch.object(module, "_build_snapshot_impl", side_effect=blocked_build), \
             patch.object(module, "terminal_activity_snapshot", side_effect=fake_scan), \
             patch.object(module, "desktop_app_sources_snapshot", return_value={}), \
             patch.object(module, "auto_update_status_snapshot", return_value={}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), module.AgentCatHandler)
            serving = threading.Thread(target=server.serve_forever)
            building = threading.Thread(target=module.build_snapshot)
            serving.start()
            building.start()
            entered.wait()
            try:
                for suffix in ("?sections=activity", ""):
                    timings = []
                    for _ in range(100):
                        # Exercise the old overlay's cache miss without waiting two seconds.
                        if hasattr(module, "_HTTP_LIVE_CACHE"):
                            module._HTTP_LIVE_CACHE = None
                        start = time.perf_counter()
                        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/snapshot{suffix}", timeout=5) as response:
                            response.read()
                        timings.append((time.perf_counter() - start) * 1000)
                    print(json.dumps({"endpoint": "/v1/snapshot" + suffix, "requests": 100,
                                      "p50_ms": round(statistics.median(timings), 3),
                                      "p95_ms": round(sorted(timings)[94], 3)}), flush=True)
            finally:
                release.set()
                building.join()
                server.shutdown()
                server.server_close()
                serving.join()
                restore_module_paths(module, original)


if __name__ == "__main__":
    run()
