import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))
import agentcat_cli_probe as cli_probe  # noqa: E402
from sandbox import redirect_module_paths, restore_module_paths  # noqa: E402

agentcat = SourceFileLoader("cli_probe_agentcat_module", str(REPO_ROOT / "bin" / "agentcat")).load_module()

USAGE_TEXT = """You are currently using your subscription to power your Claude Code usage

Current session: 8% used · resets Sep 24 at 11:10pm (Asia/Seoul)
Current week (all models): 27% used · resets Sep 29 at 8pm (Asia/Seoul)
Current week (Fable): 13% used · resets Sep 29 at 8pm (Asia/Seoul)

What's contributing to your limits usage?
Last 24h · 2115 requests · 8 sessions
  97% of your usage came from subagent-heavy sessions
"""

SEOUL_NOON = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))


class ClaudeUsageParsingTests(unittest.TestCase):
    def test_parses_session_week_and_model_windows(self):
        windows = cli_probe.parse_claude_usage(USAGE_TEXT, SEOUL_NOON)
        self.assertEqual([w["id"] for w in windows], ["claude:5h", "claude:7d", "claude:7d:fable"])
        self.assertEqual([w["usedPercent"] for w in windows], [8.0, 27.0, 13.0])
        self.assertEqual(windows[0]["windowDurationMins"], 300)
        self.assertTrue(windows[1]["primary"])
        self.assertEqual(windows[2]["model"], "Fable")
        # "97% of your usage came from ..." is commentary, not a window.
        self.assertEqual(len(windows), 3)

    def test_reset_times_resolve_in_the_stated_zone(self):
        reset = cli_probe.parse_claude_reset("Sep 24 at 11:10pm (Asia/Seoul)", SEOUL_NOON)
        expected = dt.datetime(2026, 9, 24, 23, 10, tzinfo=dt.timezone(dt.timedelta(hours=9)))
        self.assertEqual(reset, int(expected.timestamp()))
        eight = cli_probe.parse_claude_reset("Sep 29 at 8pm (Asia/Seoul)", SEOUL_NOON)
        self.assertEqual(eight, int(dt.datetime(2026, 9, 29, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()))

    def test_reset_in_early_january_rolls_into_next_year(self):
        december = dt.datetime(2026, 12, 30, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
        reset = cli_probe.parse_claude_reset("Jan 2 at 9am (Asia/Seoul)", december)
        self.assertEqual(dt.datetime.fromtimestamp(reset, dt.timezone.utc).year, 2027)

    def test_unparseable_output_yields_no_windows(self):
        self.assertEqual(cli_probe.parse_claude_usage("/usage isn't available in this environment."), [])


class ClaudeProbeTests(unittest.TestCase):
    def test_non_default_home_sets_config_dir_and_disables_session_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            default, other = Path(tmp) / ".claude", Path(tmp) / ".claude2"
            other.mkdir()
            (other / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "yo@example.com", "accountUuid": "u-1"}}))
            seen = {}

            def run(args, **kwargs):
                seen["args"], seen["env"] = args, kwargs["env"]
                return subprocess.CompletedProcess(args, 0, stdout=USAGE_TEXT, stderr="")

            row = cli_probe.probe_claude_home(other, default, "/bin/claude", run=run)
        self.assertEqual(seen["args"], ["/bin/claude", "-p", "/usage", "--no-session-persistence"])
        self.assertEqual(seen["env"]["CLAUDE_CONFIG_DIR"], str(other))
        self.assertEqual(row["email"], "yo@example.com")
        self.assertEqual(row["accountID"], "u-1")
        self.assertEqual(row["status"], "ok")
        self.assertNotIn(str(other), json.dumps(row))

    def test_timeout_is_an_error_row_not_an_exception(self):
        def run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 60)

        with tempfile.TemporaryDirectory() as tmp:
            row = cli_probe.probe_claude_home(Path(tmp), Path(tmp) / "x", "/bin/claude", run=run)
        self.assertEqual((row["status"], row["reason"]), ("error", "cli_timeout"))


class _FakeTokenModule:
    def __init__(self, candidates, usage=None):
        self.candidates, self.usage, self.calls = candidates, usage, 0

    def _credential_candidates(self, _home):
        return self.candidates

    def _live_usage(self, _token):
        self.calls += 1
        return self.usage


class TokenProbeTests(unittest.TestCase):
    def test_expired_token_is_reported_and_never_used(self):
        module = _FakeTokenModule([{"access": "t", "expires": int(time.time()) - 10, "raw": {}}])
        row = cli_probe.probe_token_home("kimi", Path("/nonexistent"), module, "access")
        self.assertEqual(row["reason"], "cli_login_expired")
        self.assertEqual(module.calls, 0)

    def test_live_token_reads_usage(self):
        module = _FakeTokenModule(
            [{"token": "t", "expires": None, "raw": {}}],
            {"windows": [{"id": "grok:7d", "usedPercent": 33.0}], "subscriptionTier": "SuperGrok"},
        )
        row = cli_probe.probe_token_home("grok", Path("/nonexistent"), module, "token")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["windows"][0]["windowDurationMins"], 10080)
        self.assertEqual(row["plan"], "SuperGrok")


class AntigravityParsingTests(unittest.TestCase):
    def test_quota_summary_groups_become_windows(self):
        payload = {"groups": [
            {"displayName": "Gemini Models", "buckets": [
                {"bucketId": "gemini-weekly", "window": "weekly", "resetTime": "2026-09-30T02:27:43Z", "remainingFraction": 0.9},
                {"bucketId": "gemini-5h", "window": "5h", "resetTime": "2026-09-24T15:27:43Z", "remainingFraction": 1},
            ]},
            {"displayName": "Claude and GPT models", "buckets": [
                {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.25},
            ]},
        ]}
        windows = cli_probe.parse_antigravity_quota_summary(payload)
        self.assertEqual([w["label"] for w in windows], ["Gemini 7d", "Gemini 5h", "Claude+GPT 7d"])
        self.assertAlmostEqual(windows[0]["usedPercent"], 10.0, places=3)
        self.assertEqual(windows[1]["windowDurationMins"], 300)
        self.assertIsNone(windows[2]["resetsAt"])
        self.assertEqual(cli_probe.parse_antigravity_quota_summary({"groups": "nope"}), [])


class KimiParsingTests(unittest.TestCase):
    def test_usage_is_weekly_and_limits_are_rate_windows(self):
        payload = {
            "usage": {"limit": "100", "used": "21", "remaining": "79", "resetTime": "2026-09-28T07:40:39.489321Z"},
            "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                        "detail": {"limit": "100", "remaining": "100", "resetTime": "2026-09-24T15:40:39Z"}}],
        }
        windows = cli_probe.parse_kimi_usage(payload)
        self.assertEqual([(w["label"], w["windowDurationMins"], w["usedPercent"]) for w in windows],
                         [("7d", 10080, 21.0), ("5h", 300, 0.0)])
        self.assertIsNotNone(windows[0]["resetsAt"])
        self.assertEqual(cli_probe.parse_kimi_usage({"usage": {"limit": "0"}}), [])


class IdentityHintTests(unittest.TestCase):
    def test_org_domain_beats_local_part_for_work_accounts(self):
        self.assertEqual(agentcat.cli_probe_identity_hint("hello@trappist.app"), "tr**")
        self.assertEqual(agentcat.cli_probe_identity_hint("yong@gmail.com"), "yo**")


class SnapshotReplacementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        self.agentcat_home = home / ".agentcat"
        self.agentcat_home.mkdir()
        self._saved = redirect_module_paths(agentcat, home, self.agentcat_home)

    def tearDown(self):
        restore_module_paths(agentcat, self._saved)
        self._tmp.cleanup()

    def _write(self, results, generated=None):
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"generatedAt": generated or int(time.time()), "results": results})

    def test_probed_provider_replaces_borrowed_rows(self):
        now = int(time.time())
        self._write([
            {"provider": "codex", "homeKey": "a" * 16, "email": "yo@gmail.com", "plan": "pro", "status": "ok",
             "windows": [{"id": "codex:p", "label": "7d", "windowDurationMins": 10080, "usedPercent": 100.0, "remainingPercent": 0.0, "primary": True}],
             "fetchedAt": now, "rateLimitReached": True},
            {"provider": "codex", "homeKey": "b" * 16, "email": "hello@trappist.app", "plan": "pro", "status": "ok",
             "windows": [{"id": "codex:p", "label": "7d", "windowDurationMins": 10080, "usedPercent": 0.0, "remainingPercent": 100.0, "primary": True}],
             "fetchedAt": now},
        ])
        borrowed = [{"id": "codex:old", "providerID": "codex", "limits": {"quotas": [{"usedPercent": 100}]}}]
        with patch.object(agentcat, "codex_provider_instances_with_completeness", return_value=(borrowed, True)), \
             patch.object(agentcat, "orca_claude_provider_instances", return_value=[{"id": "claude:orca", "providerID": "claude"}]):
            rows, complete = agentcat.provider_instances_snapshot_with_completeness({})
        codex = [r for r in rows if r["providerID"] == "codex"]
        self.assertEqual({r["label"] for r in codex}, {"Codex · yo**", "Codex · tr**"})
        self.assertNotIn("codex:old", {r["id"] for r in rows})
        self.assertIn("claude:orca", {r["id"] for r in rows})
        self.assertNotIn("codex", complete)
        trappist = next(r for r in codex if r["label"].endswith("tr**"))
        self.assertEqual(trappist["limits"]["quotas"][0]["remainingPercent"], 100.0)
        self.assertFalse(trappist["limits"]["stale"])
        self.assertEqual(trappist["account"]["email"], "hello@trappist.app")
        # The app draws a live meter only when the summary fields are present.
        self.assertEqual(trappist["limits"]["weeklyUsedPercent"], 0.0)
        self.assertIsNone(trappist["limits"]["shortUsedPercent"])

    def test_old_or_failed_rows_are_stale_and_do_not_replace(self):
        old = int(time.time()) - 3600
        self._write([
            {"provider": "kimi", "homeKey": "c" * 16, "email": None, "status": "error", "reason": "cli_login_expired", "windows": [], "fetchedAt": old},
            {"provider": "grok", "homeKey": "d" * 16, "email": None, "status": "ok",
             "windows": [{"id": "grok:7d", "windowDurationMins": 10080, "usedPercent": 33.0, "remainingPercent": 67.0}], "fetchedAt": old},
        ])
        rows = agentcat.cli_probe_provider_instances()
        kimi = next(r for r in rows if r["providerID"] == "kimi")
        grok = next(r for r in rows if r["providerID"] == "grok")
        self.assertEqual(kimi["limits"]["reason"], "cli_login_expired")
        self.assertTrue(kimi["limits"]["stale"])
        self.assertTrue(grok["limits"]["stale"])
        self.assertEqual(grok["limits"]["reason"], "cli_probe_stale")
        self.assertNotIn("/", json.dumps(rows).replace("://", ""))


if __name__ == "__main__":
    unittest.main()
