"""Account/window regression coverage; no real homes, credentials or providers."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_agentcat import agentcat
from sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths


class ClaudeStatuslineWindowTests(unittest.TestCase):
    now = 1791400000

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home, self.data = root / "home", root / "data"
        self.home.mkdir()
        self.data.mkdir()
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")})
        self.env.start()
        self.addCleanup(self.env.stop)
        paths = redirect_module_paths(agentcat, self.home, self.data)
        self.addCleanup(restore_module_paths, agentcat, paths)
        assert_sandboxed(agentcat, self.home, self.data)
        self.account("account-a")

    def account(self, account, organization="org-a", home=None):
        path = (home / ".claude.json") if home else self.home / ".claude.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"oauthAccount": {
            "accountUuid": account, "organizationUuid": organization,
        }}))

    def window(self, used, reset=None):
        return {"used_percentage": used, "resets_at": reset or self.now + 18000}

    def store(self, windows, at=None, **extra):
        with patch.object(agentcat.time, "time", return_value=self.now if at is None else at):
            agentcat.store_event("claude", "claude-statusline", "statusline",
                                {"rate_limits": windows, **extra})

    def limits(self, at=None):
        with patch.object(agentcat.time, "time", return_value=self.now if at is None else at):
            return agentcat.claude_runtime_limits()

    def test_late_lower_report_cannot_reduce_or_freshen_window(self):
        self.store({"five_hour": self.window(70)})
        self.store({"five_hour": self.window(20)}, at=self.now + 10)
        limits = self.limits(self.now + agentcat.LIVE_LIMITS_MAX_AGE_SECONDS + 1)
        self.assertEqual(limits["shortUsedPercent"], 70)
        self.assertEqual(limits["updatedAt"], agentcat.iso_from_timestamp(self.now))
        self.assertTrue(limits["stale"])

    def test_new_window_accepts_drop_and_old_window_cannot_return(self):
        self.store({"five_hour": self.window(95)})
        self.store({"five_hour": self.window(3, self.now + 36000)}, at=self.now + 10)
        self.store({"five_hour": self.window(99)}, at=self.now + 20)
        limits = self.limits(self.now + 20)
        self.assertEqual(limits["shortUsedPercent"], 3)
        self.assertEqual(limits["shortResetAt"], self.now + 36000)
        self.assertEqual(limits["updatedAt"], agentcat.iso_from_timestamp(self.now + 10))

    def test_partial_and_null_windows_keep_original_observation_time(self):
        self.store({"five_hour": self.window(40), "seven_day": self.window(80, self.now + 604800)})
        self.store({"five_hour": self.window(50), "seven_day": None}, at=self.now + 60)
        limits = self.limits(self.now + 60)
        self.assertEqual((limits["shortUsedPercent"], limits["weeklyUsedPercent"]), (50, 80))
        self.assertEqual(limits["updatedAt"], agentcat.iso_from_timestamp(self.now))
        self.assertEqual([q["usedPercent"] for q in limits["quotas"]], [50, 80])

    def test_equal_observation_refreshes_and_expired_window_is_stale(self):
        self.store({"five_hour": self.window(40)})
        self.store({"five_hour": self.window(40)}, at=self.now + 60)
        self.assertEqual(self.limits(self.now + 60)["updatedAt"], agentcat.iso_from_timestamp(self.now + 60))
        limits = self.limits(self.now + 18000)
        self.assertTrue(limits["stale"])
        self.assertEqual(limits["reason"], "statusline_window_reset")

    def test_accounts_and_organizations_do_not_share_high_water_marks(self):
        self.store({"five_hour": self.window(90)})
        self.account("account-b")
        self.assertEqual(self.limits()["status"], "not_configured")
        self.store({"five_hour": self.window(10)})
        self.assertEqual(self.limits()["shortUsedPercent"], 10)
        self.account("account-a", "org-b")
        self.assertEqual(self.limits()["status"], "not_configured")
        self.store({"five_hour": self.window(5)})
        self.assertEqual(self.limits()["shortUsedPercent"], 5)
        self.account("account-a")
        self.assertEqual(self.limits()["shortUsedPercent"], 90)

    def test_other_home_does_not_replace_default_home_observation(self):
        self.store({"five_hour": self.window(80)})
        other = self.home / ".claude-work"
        self.account("account-b", home=other)
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            self.store({"five_hour": self.window(15)})
            self.assertEqual(self.limits()["shortUsedPercent"], 15)
        self.assertEqual(self.limits()["shortUsedPercent"], 80)

    def test_unknown_identity_uses_latest_only_and_cannot_inherit_known_account(self):
        self.store({"five_hour": self.window(80)})
        self.account(None)
        self.assertEqual(self.limits()["status"], "not_configured")
        self.store({"five_hour": self.window(40)})
        self.store({"five_hour": self.window(10)})
        self.assertEqual(self.limits()["shortUsedPercent"], 10)
        self.account("account-b")
        self.assertEqual(self.limits()["status"], "not_configured")

    def test_invalid_windows_cannot_poison_or_refresh_saved_value(self):
        self.store({"five_hour": self.window(40)})
        for used, reset in [(float("nan"), self.now + 10), (float("inf"), self.now + 10),
                            (True, self.now + 10), (-1, self.now + 10), (101, self.now + 10),
                            (99, True), (99, float("inf")), (99, (self.now + 10) * 1000),
                            (99, None), (99, "invalid")]:
            with self.subTest(used=used, reset=reset):
                self.store({"five_hour": {"used_percentage": used, "resets_at": reset}}, at=self.now + 60)
                limits = self.limits(self.now + 60)
                self.assertEqual(limits["shortUsedPercent"], 40)
                self.assertEqual(limits["updatedAt"], agentcat.iso_from_timestamp(self.now))

    def test_context_only_report_does_not_refresh_quota(self):
        self.store({"five_hour": self.window(60)})
        self.store({}, at=self.now + 60, context_window={"context_window_size": 200000})
        limits = self.limits(self.now + 60)
        self.assertEqual(limits["sessionTokens"], 200000)
        self.assertEqual(limits["shortUsedPercent"], 60)
        self.assertEqual(limits["updatedAt"], agentcat.iso_from_timestamp(self.now))

    def test_command_collects_scope_and_does_not_trust_payload_identity(self):
        payload = {"session_id": "fixture-session", "_agentcat_statusline_scope": "forged",
                   "rate_limits": {"five_hour": self.window(30)}}
        with patch.object(agentcat.sys, "stdin", io.StringIO(json.dumps(payload))), \
                patch.object(agentcat.time, "time", return_value=self.now), redirect_stdout(io.StringIO()):
            agentcat.command_claude_statusline(agentcat.argparse.Namespace())
        self.assertEqual(self.limits()["shortUsedPercent"], 30)
        with closing(sqlite3.connect(agentcat.EVENTS_DB)) as conn:
            scope, saved = conn.execute("select scope, windows_json from claude_statusline_windows").fetchone()
        self.assertNotIn("account-a", scope + saved)
        self.assertNotIn(str(self.home), scope + saved)
        self.assertNotEqual(scope, "forged")

    def test_saved_windows_survive_event_retention_and_reopened_connections(self):
        self.store({"five_hour": self.window(75)})
        with closing(sqlite3.connect(agentcat.EVENTS_DB)) as conn:
            conn.execute("delete from events")
            conn.commit()
        self.store({"five_hour": self.window(20)}, at=self.now + 60)
        self.assertEqual(self.limits(self.now + 60)["shortUsedPercent"], 75)

    def test_concurrent_sessions_commit_one_high_water_mark(self):
        # Initialize the private scope key before racing independent SQLite writers.
        agentcat.claude_statusline_scope()
        values = [75, 10, 40, 95, 5, 80, 20, 60]
        with patch.object(agentcat.time, "time", return_value=self.now), ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda used: agentcat.store_event(
                "claude", "claude-statusline", "statusline", {"rate_limits": {"five_hour": self.window(used)}}
            ), values))
        self.assertEqual(self.limits()["shortUsedPercent"], 95)


if __name__ == "__main__":
    unittest.main()
