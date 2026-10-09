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
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from unittest.mock import patch


def simulate_polling(module, base_url, interval, duration=60):
    """Virtual elapsed time, real HTTP and background threads, mocked /bin/ps."""
    clock = [0.0]
    count = [0]
    worker_names = []
    def ps(*args, **kwargs):
        assert args[0][0] == "/bin/ps"
        count[0] += 1
        worker_names.append(threading.current_thread().name)
        return SimpleNamespace(stdout="")
    def wait_refresh():
        assert module._HTTP_ACTIVITY_REFRESH_LOCK.acquire(timeout=2), "refresh stuck"
        module._HTTP_ACTIVITY_REFRESH_LOCK.release()
    wait_refresh()
    module._HTTP_ACTIVITY_CACHE = None
    module.publish_http_snapshot({"providers": {}, "generatedAt": "old",
                                  "activity": {"status": "ok", "updatedAt": "0.0"}})
    ages = []
    with patch.object(module, "_activity_monotonic", side_effect=lambda: clock[0]), \
         patch.object(module, "now_iso", side_effect=lambda: str(clock[0])), \
         patch.object(module, "IS_WINDOWS", False), \
         patch.object(module, "safe_runtime_modes_snapshot", return_value=[]), \
         patch.object(module.subprocess, "run", side_effect=ps):
        for tick in range(0, duration, interval):
            clock[0] = float(tick)
            with urllib.request.urlopen(base_url + "/v1/snapshot?sections=activity", timeout=1) as response:
                payload = json.loads(response.read())
            ages.append(clock[0] - float(payload["activity"]["updatedAt"]))
            wait_refresh()
        requested_count = count[0]
        clock[0] += duration  # No requests: elapsed time alone must cause no scans.
        time.sleep(0.01)
        assert count[0] == requested_count
    return {"poll_interval_s": interval, "simulated_duration_s": duration,
            "requests": len(ages), "ps_invocations": count[0],
            "max_served_sample_age_s": max(ages),
            "background_only": all("_refresh_http_activity" in name for name in worker_names),
            "ps_without_requests": count[0] - requested_count}


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
        real_scan = module.terminal_activity_snapshot
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
                        # Span multiple cache expirations; pacing is outside timing.
                        time.sleep(0.025)
                    print(json.dumps({"endpoint": "/v1/snapshot" + suffix, "requests": 100,
                                      "p50_ms": round(statistics.median(timings), 3),
                                      "p95_ms": round(sorted(timings)[94], 3)}), flush=True)
                if hasattr(module, "refresh_activity_if_stale"):
                    # Use the real activity parser with an injected ps result for counts.
                    with patch.object(module, "terminal_activity_snapshot", real_scan):
                        for interval in (20, 2):
                            print(json.dumps(simulate_polling(
                                module, f"http://127.0.0.1:{server.server_port}", interval)), flush=True)
            finally:
                if hasattr(module, "_HTTP_ACTIVITY_REFRESH_LOCK"):
                    assert module._HTTP_ACTIVITY_REFRESH_LOCK.acquire(timeout=5)
                    module._HTTP_ACTIVITY_REFRESH_LOCK.release()
                release.set()
                building.join()
                server.shutdown()
                server.server_close()
                serving.join()
                restore_module_paths(module, original)


if __name__ == "__main__":
    run()
