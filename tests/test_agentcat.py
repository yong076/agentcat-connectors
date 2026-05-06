import importlib.util
import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class AgentCatConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths = {
            "HOME": agentcat.HOME,
            "AGENTCAT_HOME": agentcat.AGENTCAT_HOME,
            "EVENTS_DB": agentcat.EVENTS_DB,
            "LATEST_SNAPSHOT": agentcat.LATEST_SNAPSHOT,
            "LIMITS_FILE": agentcat.LIMITS_FILE,
            "GEMINI_TELEMETRY": agentcat.GEMINI_TELEMETRY,
        }

        home = self.root / "home"
        agentcat_home = self.root / "agentcat"
        home.mkdir()
        agentcat_home.mkdir()
        agentcat.HOME = home
        agentcat.AGENTCAT_HOME = agentcat_home
        agentcat.EVENTS_DB = agentcat_home / "events.sqlite"
        agentcat.LATEST_SNAPSHOT = agentcat_home / "latest-snapshot.json"
        agentcat.LIMITS_FILE = agentcat_home / "limits.json"
        agentcat.GEMINI_TELEMETRY = agentcat_home / "gemini" / "telemetry.log"

    def tearDown(self) -> None:
        for name, value in self.old_paths.items():
            setattr(agentcat, name, value)
        self.tmp.cleanup()

    def test_percent_parser_clamps_and_rejects_invalid_values(self) -> None:
        self.assertEqual(agentcat.float_percent("12.5%"), 12.5)
        self.assertEqual(agentcat.float_percent(140), 100.0)
        self.assertEqual(agentcat.float_percent(-8), 0.0)
        self.assertIsNone(agentcat.float_percent(True))
        self.assertIsNone(agentcat.float_percent("nope"))

    def test_normalize_limits_accepts_aliases(self) -> None:
        limits = agentcat.normalize_limits(
            {
                "week": "1,000",
                "monthly_token_limit": "2000",
                "contextWindow": 3000,
            }
        )

        self.assertEqual(limits["status"], "ok")
        self.assertEqual(limits["weeklyTokens"], 1000)
        self.assertEqual(limits["monthlyTokens"], 2000)
        self.assertEqual(limits["sessionTokens"], 3000)

    def test_merge_limits_prefers_configured_caps_and_keeps_runtime_percent(self) -> None:
        configured = agentcat.normalize_limits({"week": 1000, "session": 2000})
        detected = agentcat.empty_limits(status="auto")
        detected["weeklyUsedPercent"] = 13.0
        detected["shortUsedPercent"] = 8.0
        detected["sessionTokens"] = 128000

        merged = agentcat.merge_limits(configured, detected)

        self.assertEqual(merged["status"], "ok")
        self.assertEqual(merged["weeklyTokens"], 1000)
        self.assertEqual(merged["sessionTokens"], 2000)
        self.assertEqual(merged["weeklyUsedPercent"], 13.0)
        self.assertEqual(merged["shortUsedPercent"], 8.0)

    def test_codex_runtime_limits_reads_latest_token_count_event(self) -> None:
        session_dir = agentcat.HOME / ".codex" / "sessions" / "2026" / "05" / "06"
        session_dir.mkdir(parents=True)
        rollout = session_dir / "rollout-test.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "token_count",
                        "info": {"model_context_window": 258400},
                        "rate_limits": {
                            "plan_type": "pro",
                            "primary": {
                                "used_percent": 8,
                                "window_minutes": 300,
                                "resets_at": 1770000100,
                            },
                            "secondary": {
                                "used_percent": 13,
                                "resets_at": 1770000200,
                            },
                        },
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

        limits = agentcat.codex_runtime_limits()

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(limits["sessionTokens"], 258400)
        self.assertEqual(limits["shortUsedPercent"], 8.0)
        self.assertEqual(limits["shortWindowMinutes"], 300)
        self.assertEqual(limits["weeklyUsedPercent"], 13.0)
        self.assertEqual(limits["weeklyResetAt"], 1770000200)
        self.assertEqual(limits["planType"], "pro")

    def test_claude_runtime_limits_reads_statusline_event(self) -> None:
        agentcat.store_event(
            "claude",
            "claude-statusline",
            "statusline",
            {
                "message": "must be redacted",
                "context_window": {"context_window_size": 1000000},
                "rate_limits": {
                    "five_hour": {"used_percentage": 4, "resets_at": 1770000300},
                    "seven_day": {"used_percentage": 10, "resets_at": 1770000400},
                },
            },
        )

        limits = agentcat.claude_runtime_limits()

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(limits["sessionTokens"], 1000000)
        self.assertEqual(limits["shortUsedPercent"], 4.0)
        self.assertEqual(limits["shortWindowMinutes"], 300)
        self.assertEqual(limits["weeklyUsedPercent"], 10.0)
        self.assertEqual(limits["weeklyResetAt"], 1770000400)

    def test_sanitize_payload_redacts_content_but_keeps_limit_metadata(self) -> None:
        sanitized = agentcat.sanitize_payload(
            {
                "prompt": "secret",
                "messages": [{"content": "secret"}],
                "rate_limits": {"seven_day": {"used_percentage": 10}},
                "context_window": {"context_window_size": 1000000},
            }
        )

        self.assertEqual(sanitized["prompt"], "[redacted]")
        self.assertEqual(sanitized["messages"], "[redacted]")
        self.assertEqual(sanitized["rate_limits"]["seven_day"]["used_percentage"], 10)
        self.assertEqual(sanitized["context_window"]["context_window_size"], 1000000)

    def test_setup_prompt_includes_install_skill_and_privacy_rules(self) -> None:
        prompt = agentcat.setup_prompt_text()

        self.assertIn("Codex", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("Gemini CLI", prompt)
        self.assertIn("agentcat snapshot --json", prompt)
        self.assertIn("skills/agentcat-usage", prompt)
        self.assertIn("Never store or report prompt text", prompt)


if __name__ == "__main__":
    unittest.main()
