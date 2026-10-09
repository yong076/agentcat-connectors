"""Gemini fetch-time and read-time freshness regressions; fixtures only."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agentcat import agentcat
from agentcat_managed_accounts import ManagedAccounts, USAGE_FRESH_SECONDS


class GeminiStalenessTests(unittest.TestCase):
    def test_failed_fetch_preserves_time_and_backoff_then_success_recovers(self):
        with tempfile.TemporaryDirectory() as home:
            cache = Path(home) / "limits.json"
            with patch.object(agentcat, "LIVE_LIMITS_CACHE", cache), patch.object(agentcat, "ensure_dirs"), \
                 patch.object(agentcat, "read_gemini_auth_type", return_value="oauth-personal"), \
                 patch.object(agentcat, "gemini_local_project_id", return_value=""), \
                 patch.object(agentcat, "gemini_access_token", return_value="fixture"), \
                 patch.object(agentcat, "fetch_code_assist_quota", side_effect=RuntimeError("fixture failure")) as fetch:
                fetched = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=301)
                stamp = fetched.isoformat().replace("+00:00", "Z")
                good = {"status": "auto", "updatedAt": stamp, "quotas": [{"usedPercent": 10}]}
                original = int(fetched.timestamp())
                cache.write_text(json.dumps({"gemini": {"cachedAt": original, "limits": good}}))
                for _ in range(2):
                    limits = agentcat.gemini_live_limits(force=True)
                    self.assertTrue(limits["stale"])
                    self.assertEqual(limits["updatedAt"], stamp)
                    self.assertEqual(json.loads(cache.read_text())["gemini"]["cachedAt"], original)
                cached = agentcat.gemini_live_limits()
                self.assertEqual(fetch.call_count, 2)
                self.assertTrue(cached["stale"])
                usage = agentcat.live_usage_provider_from_limits(cached)
                self.assertTrue(usage["stale"])
                self.assertEqual(usage["updated_at"], stamp)
                fetch.side_effect = None
                fetch.return_value = ({}, {"buckets": [{"modelId": "pro", "remainingFraction": 0.8}]})
                recovered = agentcat.gemini_live_limits(force=True)
                self.assertFalse(recovered["stale"])
                self.assertNotEqual(recovered["updatedAt"], stamp)

    def test_managed_rows_age_without_mutating_registry(self):
        for age, stale in ((30, False), (USAGE_FRESH_SECONDS + 1, True)):
            with self.subTest(age=age):
                stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).isoformat()
                row = {"provider": "gemini", "usage": {"freshness": "live", "updatedAt": stamp, "windows": [{"usedPercent": 10}]}}
                usage = ManagedAccounts.public(row)["usage"]
                self.assertEqual(usage["stale"], stale)
                self.assertEqual(usage["freshness"], "stale" if stale else "live")
                self.assertEqual(usage["updatedAt"], stamp)
                self.assertEqual(row["usage"]["freshness"], "live")

    def test_legacy_or_invalid_managed_timestamp_is_stale(self):
        for stamp in (None, "invalid", "2026-10-10T00:00:00"):
            row = {"provider": "gemini", "usage": {"freshness": "live", "updatedAt": stamp}}
            self.assertEqual(ManagedAccounts.public(row)["usage"]["freshness"], "stale")

    def test_legacy_cache_uses_original_cache_timestamp(self):
        with tempfile.TemporaryDirectory() as home:
            cache = Path(home) / "limits.json"
            original = int(agentcat.time.time()) - 301
            cache.write_text(json.dumps({"gemini": {"cachedAt": original, "limits": {"status": "auto", "quotas": [{"usedPercent": 10}]}}}))
            with patch.object(agentcat, "LIVE_LIMITS_CACHE", cache):
                limits = agentcat.cached_live_limits("gemini", 900)
            self.assertTrue(limits["stale"])
            self.assertEqual(dt.datetime.fromisoformat(limits["updatedAt"].replace("Z", "+00:00")).timestamp(), original)

    def test_retry_backoff_expires_independently_of_fetch_age(self):
        with tempfile.TemporaryDirectory() as home:
            cache = Path(home) / "limits.json"
            cache.write_text(json.dumps({"gemini": {
                "cachedAt": 1000, "lastAttemptAt": 2000, "failureStreak": 1,
                "limits": {"status": "auto", "liveError": "fixture", "quotas": [{"usedPercent": 10}]},
            }}))
            backoff = agentcat.live_limits_error_backoff_seconds(1)
            with patch.object(agentcat, "LIVE_LIMITS_CACHE", cache):
                with patch.object(agentcat.time, "time", return_value=2001):
                    limits = agentcat.cached_live_limits("gemini", 900)
                    self.assertEqual(limits["cachedAt"], 1000)
                    self.assertEqual(limits["cacheAgeSeconds"], 1001)
                    self.assertTrue(limits["stale"])
                with patch.object(agentcat.time, "time", return_value=2000 + backoff + 1):
                    self.assertIsNone(agentcat.cached_live_limits("gemini", 900))
