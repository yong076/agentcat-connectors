"""Regression tests for WP30 provider limits and reset-credit support."""

import importlib.util
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from tests.sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_wp30", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_wp30", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class WP30TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.agentcat_home = self.root / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        assert_sandboxed(agentcat, self.home, self.agentcat_home)
        self.env = patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env.start()
        self.network = patch.object(
            agentcat.urllib.request,
            "urlopen",
            side_effect=AssertionError("network must be stubbed in WP30 tests"),
        )
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()


class CodexResetCreditTests(WP30TestCase):
    def test_credit_summary_includes_id_but_omits_account_identity(self):
        summaries = agentcat.codex_reset_credit_summaries(
            [{
                "id": "RateLimitResetCredit_fixture",
                "status": "available",
                "profile_user_id": "user-secret",
                "profile_image_url": "https://example.test/private.png",
            }]
        )

        self.assertEqual(summaries, [{"id": "RateLimitResetCredit_fixture", "status": "available"}])

class ClaudeExtraUsageTests(WP30TestCase):
    def test_usage_fixture_emits_extra_usage_and_oauth_plan(self):
        limits = agentcat.claude_limits_from_usage_response(
            {
                "subscription_type": "max",
                "five_hour": {
                    "utilization": 12.5,
                    "resets_at": "2026-09-05T12:00:00Z",
                },
                "extra_usage": {
                    "used_credits": 17.25,
                    "monthly_limit": 100,
                    "currency": "USD",
                    "is_enabled": True,
                },
            }
        )

        self.assertEqual(limits["planType"], "max")
        self.assertEqual(limits["extraUsage"], {
            "enabled": True,
            "usedUSD": 17.25,
            "monthlyLimitUSD": 100.0,
            "currency": "USD",
        })
        extra_quota = next(q for q in limits["quotas"] if q["id"] == "claude:extra_usage")
        self.assertEqual(extra_quota["used"], 17.25)
        self.assertEqual(extra_quota["limit"], 100.0)

    def test_disabled_extra_usage_is_emitted_without_a_quota(self):
        limits = agentcat.claude_limits_from_usage_response(
            {
                "five_hour": {"utilization": 1, "resets_at": "2026-09-05T12:00:00Z"},
                "extra_usage": {
                    "used_credits": 0,
                    "monthly_limit": 50,
                    "currency": "USD",
                    "is_enabled": False,
                },
            }
        )

        self.assertEqual(limits["extraUsage"], {
            "enabled": False,
            "usedUSD": 0.0,
            "monthlyLimitUSD": 50.0,
            "currency": "USD",
        })
        self.assertNotIn("claude:extra_usage", [q["id"] for q in limits["quotas"]])

    def test_live_limits_fall_back_to_claude_json_subscription_type(self):
        (agentcat.HOME / ".claude.json").write_text(
            json.dumps({"subscriptionType": "pro"}), encoding="utf-8"
        )
        usage = {
            "five_hour": {"utilization": 3, "resets_at": "2026-09-05T12:00:00Z"},
        }
        credentials = {
            "oauth": {"accessToken": "fixture-token"},
            "reason": None,
            "credentialSource": "fixture",
        }
        with patch.object(agentcat, "read_claude_oauth_credentials", return_value=credentials), patch.object(
            agentcat, "claude_usage_request", return_value=usage
        ):
            limits = agentcat.claude_live_limits(force=True)

        self.assertEqual(limits["planType"], "pro")


class CopilotQuotaTests(WP30TestCase):
    def usage_fixture(self):
        return {
            "copilot_plan": "free",
            "quota_snapshots": {
                "premium_interactions": {
                    "entitlement": 500,
                    "remaining": 450,
                    "percent_remaining": 90,
                    "quota_id": "premium_interactions",
                },
                "chat": {
                    "entitlement": 300,
                    "remaining": 150,
                    "percent_remaining": 50,
                    "quota_id": "chat",
                },
            },
        }

    def test_documented_fixture_maps_monthly_quotas_plan_and_reset(self):
        now = agentcat.dt.datetime(2026, 12, 15, 9, 30, tzinfo=agentcat.dt.timezone.utc)
        limits = agentcat.copilot_limits_from_user_response(self.usage_fixture(), now=now)

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(limits["planType"], "free")
        self.assertEqual([q["id"] for q in limits["quotas"]], [
            "copilot:premium_interactions", "copilot:chat",
        ])
        premium, chat = limits["quotas"]
        self.assertEqual(premium["remainingPercent"], 90.0)
        self.assertEqual(premium["usedPercent"], 10.0)
        self.assertEqual(premium["remaining"], 450.0)
        self.assertEqual(premium["limit"], 500.0)
        self.assertEqual(chat["remainingPercent"], 50.0)
        self.assertEqual(premium["window"], "month")
        self.assertEqual(
            premium["resetAt"],
            int(agentcat.dt.datetime(2027, 1, 1, tzinfo=agentcat.dt.timezone.utc).timestamp()),
        )

    def test_hosts_and_apps_oauth_token_shapes_are_supported(self):
        root = agentcat.HOME / ".config" / "github-copilot"
        root.mkdir(parents=True)
        (root / "apps.json").write_text(
            json.dumps({"github.com": {"oauth_token": "apps-token"}}), encoding="utf-8"
        )
        self.assertEqual(agentcat.copilot_oauth_token(), "apps-token")

        (root / "hosts.json").write_text(
            json.dumps({"github.com": {"oauth_token": "hosts-token"}}), encoding="utf-8"
        )
        self.assertEqual(agentcat.copilot_oauth_token(), "hosts-token")

    def test_live_request_uses_github_oauth_headers_and_fifteen_minute_cache(self):
        root = agentcat.HOME / ".config" / "github-copilot"
        root.mkdir(parents=True)
        (root / "hosts.json").write_text(
            json.dumps({"github.com": {"oauth_token": "ghu-fixture"}}), encoding="utf-8"
        )
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeResponse(self.usage_fixture())

        with patch.object(agentcat.urllib.request, "urlopen", side_effect=fake_urlopen):
            first = agentcat.copilot_live_limits()
            second = agentcat.copilot_live_limits()

        self.assertEqual(first["planType"], "free")
        self.assertEqual(second["planType"], "free")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url, agentcat.COPILOT_USAGE_URL)
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertEqual(requests[0].get_header("Authorization"), "token ghu-fixture")
        cached = agentcat.cached_live_limits("copilot", 15 * 60)
        self.assertIsNotNone(cached)

    def test_missing_expired_and_non_applicable_reasons_use_limit_classifier(self):
        missing = agentcat.copilot_live_limits(force=True)
        self.assertEqual(missing["reason"], "token_missing")

        root = agentcat.HOME / ".config" / "github-copilot"
        root.mkdir(parents=True)
        (root / "hosts.json").write_text(
            json.dumps({"github.com": {"oauth_token": "expired-token"}}), encoding="utf-8"
        )
        error = urllib.error.HTTPError(
            agentcat.COPILOT_USAGE_URL, 401, "Unauthorized", {}, io.BytesIO(b"{}")
        )
        with patch.object(agentcat.urllib.request, "urlopen", side_effect=error):
            expired = agentcat.copilot_live_limits(force=True)
        self.assertEqual(expired["reason"], "token_expired")

        not_applicable = agentcat.copilot_limits_from_user_response(
            {
                "copilot_plan": "business",
                "token_based_billing": True,
                "quota_snapshots": {
                    "premium_interactions": {
                        "entitlement": 0,
                        "remaining": 0,
                        "percent_remaining": 100,
                    }
                },
            }
        )
        self.assertEqual(not_applicable["planType"], "business")
        self.assertEqual(not_applicable["reason"], "not_applicable")
        self.assertEqual(not_applicable["quotas"], [])


class AdaptiveLimitPollingTests(WP30TestCase):
    @staticmethod
    def limits(remaining):
        limits = agentcat.empty_limits(status="auto")
        limits["quotas"] = [
            {"id": "fixture:window", "remainingPercent": remaining, "usedPercent": 100 - remaining}
        ]
        return limits

    def test_low_window_and_recent_reset_use_120_second_polling(self):
        low = agentcat.adaptive_limit_poll_policy(self.limits(9.99), now=10_000)
        reset = agentcat.adaptive_limit_poll_policy(
            self.limits(80), now=10_000, reset_signal_at=10_000 - 7_199
        )

        self.assertEqual(low["intervalSeconds"], 120)
        self.assertEqual(low["reason"], "low_remaining")
        self.assertEqual(reset["intervalSeconds"], 120)
        self.assertEqual(reset["reason"], "recent_reset")

    def test_recovery_requires_30_minutes_at_or_above_ten_percent(self):
        low = agentcat.adaptive_limit_poll_policy(self.limits(5), now=1_000)
        recovering = agentcat.adaptive_limit_poll_policy(
            self.limits(10), now=1_120, previous_state=low
        )
        still_recovering = agentcat.adaptive_limit_poll_policy(
            self.limits(80), now=2_919, previous_state=recovering
        )
        normal = agentcat.adaptive_limit_poll_policy(
            self.limits(80), now=2_920, previous_state=recovering
        )

        self.assertEqual(recovering["reason"], "recovering")
        self.assertEqual(recovering["aboveThresholdSince"], 1_120)
        self.assertEqual(still_recovering["intervalSeconds"], 120)
        self.assertEqual(normal["intervalSeconds"], agentcat.LIVE_LIMITS_MAX_AGE_SECONDS)
        self.assertEqual(normal["reason"], "normal")

    def test_cache_expires_at_adaptive_interval_per_provider(self):
        with patch.object(agentcat.time, "time", return_value=10_000):
            agentcat.write_live_limits_cache("claude", self.limits(5))
            agentcat.write_live_limits_cache("copilot", self.limits(80))

        with patch.object(agentcat.time, "time", return_value=10_120):
            self.assertIsNone(
                agentcat.cached_live_limits("claude", agentcat.LIVE_LIMITS_MAX_AGE_SECONDS)
            )
            self.assertIsNotNone(
                agentcat.cached_live_limits("copilot", agentcat.LIVE_LIMITS_MAX_AGE_SECONDS)
            )

    def test_recorded_reset_signal_accelerates_only_that_provider(self):
        with patch.object(agentcat.time, "time", return_value=20_000):
            agentcat.write_live_limits_cache("codex", self.limits(90))
            agentcat.write_live_limits_cache("claude", self.limits(90))
            agentcat.record_live_limit_reset_signal("codex", observed_at=20_000)

        with patch.object(agentcat.time, "time", return_value=20_120):
            self.assertIsNone(
                agentcat.cached_live_limits("codex", agentcat.LIVE_LIMITS_MAX_AGE_SECONDS)
            )
            self.assertIsNotNone(
                agentcat.cached_live_limits("claude", agentcat.LIVE_LIMITS_MAX_AGE_SECONDS)
            )


if __name__ == "__main__":
    unittest.main()
