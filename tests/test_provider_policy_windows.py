"""No provider calls, session writes or real credential access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).resolve().parents[1] / "lib" / "agentcat_cli_probe.py"
spec = importlib.util.spec_from_file_location("policy_probe", path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProviderPolicyWindowTests(unittest.TestCase):
    def test_claude_weekly_only_does_not_synthesize_session_or_lose_model_scope(self):
        rows = probe.parse_claude_usage("Current week (all models): 25% used\nCurrent week (Fable): 40% used")
        self.assertEqual([r["id"] for r in rows], ["claude:7d", "claude:7d:fable"])
        self.assertFalse(any(r["windowDurationMins"] == 300 for r in rows))

    def test_reported_session_is_kept_for_unverified_ultra_or_other_plans(self):
        rows = probe.parse_claude_usage("Current session: 10% used\nCurrent week (all models): 25% used")
        self.assertEqual([r["windowDurationMins"] for r in rows], [300, 10080])

    def test_ultra_alias_is_only_a_name_not_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".claude.json"
            config.write_text(json.dumps({"oauthAccount": {"organizationRateLimitTier": "default_claude_max_20x"}}))
            details = probe.claude_account_details(home, home)
            self.assertEqual(details["plan"], "Ultra")
            self.assertNotIn("verified", details)

    def test_kimi_unscoped_or_new_usage_is_not_assumed_weekly(self):
        usage = {"limit": 100, "used": 20}
        for plan in [None, "Plus", "Pro", "Ultra", "Vivace"]:
            rows = probe.parse_kimi_usage({"plan": plan, "usage": usage})
            self.assertEqual(rows[0]["id"], "kimi:usage")
            self.assertIsNone(rows[0]["windowDurationMins"])
        rows = probe.parse_kimi_usage({"plan": "Moderato", "usage": usage})
        self.assertEqual(rows[0]["id"], "kimi:7d")

    def test_copilot_credit_counter_is_not_renamed_to_premium_requests(self):
        result = probe.parse_copilot_user({"access_type_sku": "paid", "quota_snapshots": {
            "ai_credits": {"entitlement": 1500, "remaining": 1200},
        }})
        self.assertEqual(result["windows"][0]["id"], "copilot:ai_credits")
        self.assertEqual(result["windows"][0]["label"], "AI credits")
        self.assertEqual(result["windows"][0]["usedPercent"], 20)


if __name__ == "__main__":
    unittest.main()
