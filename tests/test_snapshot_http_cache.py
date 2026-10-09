"""HTTP remains responsive while a snapshot writer is blocked."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from sandbox import redirect_module_paths, restore_module_paths

loader = SourceFileLoader("http_cache_agentcat", str(Path(__file__).resolve().parents[1] / "bin/agentcat"))
spec = importlib.util.spec_from_loader(loader.name, loader)
agentcat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agentcat)


class SnapshotHTTPCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = redirect_module_paths(agentcat, Path(self.temp.name), Path(self.temp.name) / ".agentcat")
        agentcat._HTTP_SNAPSHOT_CACHE = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        agentcat._HTTP_SNAPSHOT_CACHE = None
        restore_module_paths(agentcat, self.original)
        self.temp.cleanup()

    def get(self, suffix=""):
        start = time.perf_counter()
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/v1/snapshot{suffix}", timeout=1) as response:
            body = response.read()
        self.assertLess(time.perf_counter() - start, 0.2)
        return json.loads(body)

    def test_blocked_build_serves_previous_generation_without_io_or_full_decode(self):
        previous = {"schemaVersion": 4, "generatedAt": "old", "providers": {"fixture": {"data": "x" * 200000}},
                    "activity": {"status": "ok", "processes": []}}
        agentcat.publish_http_snapshot(previous)
        entered, release = threading.Event(), threading.Event()
        def build():
            entered.set()
            release.wait()
            return {**previous, "generatedAt": "new", "activity": {"status": "new"}}
        with patch.object(agentcat, "_build_snapshot_impl", side_effect=build), \
             patch.object(agentcat, "read_json", side_effect=AssertionError("request read")), \
             patch.object(agentcat, "terminal_activity_snapshot", side_effect=AssertionError("request scan")), \
             patch.object(agentcat.copy, "deepcopy", side_effect=AssertionError("request copy")), \
             patch.object(agentcat, "filter_snapshot_sections", side_effect=AssertionError("full filter")):
            worker = threading.Thread(target=agentcat.build_snapshot)
            worker.start()
            self.assertTrue(entered.wait(1))
            try:
                for _ in range(5):
                    filtered = self.get("?sections=activity")
                    self.assertNotIn("providers", filtered)
                    self.assertEqual(filtered["activity"], previous["activity"])
                    self.assertEqual(self.get()["generatedAt"], "old")
            finally:
                release.set()
                worker.join(2)
            self.assertEqual(self.get("?sections=activity")["activity"], {"status": "new"})

    def test_cold_start_returns_503_without_building(self):
        with patch.object(agentcat, "build_snapshot", side_effect=AssertionError("request build")):
            start = time.perf_counter()
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.get("?sections=activity")
            self.assertEqual(error.exception.code, 503)
            self.assertLess(time.perf_counter() - start, 0.2)
            error.exception.close()

    def test_sections_and_failed_build_preserve_cached_generation(self):
        snapshot = {"generatedAt": "old", "providers": {}, "activity": {"status": "old"}, "desktopApps": {}}
        agentcat.publish_http_snapshot(snapshot)
        snapshot["activity"]["status"] = "mutated"
        with patch.object(agentcat, "_build_snapshot_impl", side_effect=RuntimeError("fake failure")):
            with self.assertRaises(RuntimeError):
                agentcat.build_snapshot()
        self.assertEqual(self.get("?sections=activity")["activity"]["status"], "old")
        self.assertIn("providers", self.get("?sections=unknown"))
        self.assertIn("providers", self.get("?sections=%20"))
        selected = self.get("?sections=activity,desktopApps")
        self.assertIn("desktopApps", selected)
        self.assertNotIn("providers", selected)
        self.assertEqual(selected["generatedAt"], "old")
        self.assertIn("servedAt", selected)

    def test_startup_loads_disk_once_and_handles_missing_or_invalid_cache(self):
        for value in (None, {"providers": []}, {"providers": {}, "activity": {"status": "cached"}}):
            with patch.object(agentcat, "read_json", return_value=value) as read:
                agentcat.initialize_http_snapshot_cache()
                read.assert_called_once_with(agentcat.LATEST_SNAPSHOT)
            if isinstance(value, dict) and isinstance(value.get("providers"), dict):
                with patch.object(agentcat, "read_json", side_effect=AssertionError("second read")):
                    self.assertEqual(self.get("?sections=activity")["activity"]["status"], "cached")
                    self.assertNotIn("providers", self.get("?sections=servedAt"))
            else:
                self.assertIsNone(agentcat.snapshot_for_http())
