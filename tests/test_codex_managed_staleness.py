import datetime as dt
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    # Pure-function tests, but load under a throwaway HOME so module-level
    # paths never point at a real install.
    home = tempfile.mkdtemp()
    with patch.dict(os.environ, {"HOME": home, "AGENTCAT_HOME": str(Path(home) / ".agentcat")}):
        return SourceFileLoader(
            "codex_managed_staleness_agentcat", str(REPO_ROOT / "bin" / "agentcat")
        ).load_module()


agentcat = _load()


def _row(synced_at, freshness="live"):
    return {
        "lastSuccessfulSyncAt": synced_at,
        "usage": {
            "source": "codex-app-server",
            "freshness": freshness,
            "windows": [{"id": "codex:7d", "windowDurationMins": 10080, "usedPercent": 100, "primary": True}],
        },
    }


def _iso(seconds_ago):
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)
    return moment.isoformat().replace("+00:00", "Z")


class CodexManagedStalenessTests(unittest.TestCase):
    def test_recent_live_reading_is_fresh(self):
        limits = agentcat.codex_managed_connection_limits(_row(_iso(30)))
        self.assertFalse(limits["stale"])
        self.assertNotEqual(limits.get("reason"), "codex_managed_usage_stale")

    def test_live_reading_older_than_window_is_stale(self):
        # A row stored as "live" two days ago must not present as current.
        limits = agentcat.codex_managed_connection_limits(_row(_iso(43 * 3600)))
        self.assertTrue(limits["stale"])
        self.assertEqual(limits["reason"], "codex_managed_usage_stale")

    def test_missing_or_unparseable_sync_time_is_stale(self):
        for synced in (None, "not-a-date", "2026-09-22T13:05:00"):
            with self.subTest(synced=synced):
                self.assertTrue(agentcat.codex_managed_connection_limits(_row(synced))["stale"])

    def test_non_live_freshness_stays_stale(self):
        self.assertTrue(agentcat.codex_managed_connection_limits(_row(_iso(5), freshness="cached"))["stale"])


if __name__ == "__main__":
    unittest.main()
