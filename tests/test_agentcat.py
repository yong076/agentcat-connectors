import importlib.util
import datetime as dt
import json
import sqlite3
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
            "LIVE_LIMITS_CACHE": agentcat.LIVE_LIMITS_CACHE,
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
        agentcat.LIVE_LIMITS_CACHE = agentcat_home / "live-limits-cache.json"

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
        detected["quotas"] = [
            {
                "id": "codex:7d",
                "label": "7일",
                "usedPercent": 13.0,
                "remainingPercent": 87.0,
            }
        ]

        merged = agentcat.merge_limits(configured, detected)

        self.assertEqual(merged["status"], "ok")
        self.assertEqual(merged["weeklyTokens"], 1000)
        self.assertEqual(merged["sessionTokens"], 2000)
        self.assertEqual(merged["weeklyUsedPercent"], 13.0)
        self.assertEqual(merged["shortUsedPercent"], 8.0)
        self.assertEqual(merged["quotas"][0]["remainingPercent"], 87.0)

    def test_codex_usage_api_payload_builds_remaining_quota_entries(self) -> None:
        limits = agentcat.codex_limits_from_usage_response(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 3,
                        "reset_at": 1770000100,
                        "limit_window_seconds": 18000,
                    },
                    "secondary_window": {
                        "used_percent": 18,
                        "reset_at": 1770000200,
                        "limit_window_seconds": 604800,
                    },
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {"used_percent": 0, "reset_at": 1770000300},
                        },
                    }
                ],
            }
        )

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(limits["planType"], "pro")
        self.assertEqual(limits["shortUsedPercent"], 3.0)
        self.assertEqual(limits["weeklyUsedPercent"], 18.0)
        self.assertEqual(limits["quotas"][0]["remainingPercent"], 97.0)
        self.assertEqual(limits["quotas"][1]["remainingPercent"], 82.0)
        self.assertEqual(limits["quotas"][2]["remainingPercent"], 100.0)

    def test_claude_usage_api_payload_builds_weekly_and_monthly_remaining(self) -> None:
        limits = agentcat.claude_limits_from_usage_response(
            {
                "five_hour": {"utilization": 2.0, "resets_at": "2026-05-06T15:50:00Z"},
                "seven_day": {"utilization": 17.0, "resets_at": "2026-05-08T07:00:00Z"},
                "seven_day_sonnet": {"utilization": 5.0, "resets_at": "2026-05-08T07:00:00Z"},
                "extra_usage": {
                    "is_enabled": True,
                    "monthly_limit": 5000,
                    "used_credits": 1250,
                    "currency": "USD",
                },
            }
        )

        self.assertEqual(limits["shortUsedPercent"], 2.0)
        self.assertEqual(limits["weeklyUsedPercent"], 17.0)
        self.assertEqual([q["id"] for q in limits["quotas"][:3]], ["claude:five_hour", "claude:seven_day", "claude:extra_usage"])
        self.assertEqual(limits["quotas"][0]["remainingPercent"], 98.0)
        self.assertEqual(limits["quotas"][1]["remainingPercent"], 83.0)
        self.assertEqual(limits["quotas"][2]["remaining"], 3750.0)
        self.assertEqual(limits["quotas"][2]["remainingPercent"], 75.0)

    def test_gemini_quota_api_payload_builds_model_remaining(self) -> None:
        limits = agentcat.gemini_limits_from_quota_response(
            {
                "buckets": [
                    {
                        "modelId": "gemini-2.5-flash",
                        "remainingFraction": 0.984,
                        "resetTime": "2026-05-07T02:57:47Z",
                        "tokenType": "REQUESTS",
                    },
                    {
                        "modelId": "gemini-2.5-pro",
                        "remainingFraction": 1,
                        "resetTime": "2026-05-07T11:19:25Z",
                        "tokenType": "REQUESTS",
                    },
                ]
            },
            {"paidTier": {"id": "g1-pro-tier", "name": "Gemini Code Assist in Google One AI Pro"}},
        )

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(limits["planType"], "Gemini Code Assist in Google One AI Pro")
        self.assertEqual(limits["quotas"][0]["label"], "Gemini Pro")
        self.assertEqual(limits["quotas"][0]["remainingPercent"], 100.0)
        self.assertAlmostEqual(limits["quotas"][1]["usedPercent"], 1.6)

    def test_live_limit_cache_returns_stale_limits_when_allowed(self) -> None:
        limits = agentcat.empty_limits(status="auto")
        limits["quotas"] = [
            {
                "id": "claude:seven_day",
                "label": "7일",
                "remainingPercent": 83.0,
                "usedPercent": 17.0,
            }
        ]
        agentcat.write_live_limits_cache("claude", limits)

        fresh = agentcat.cached_live_limits("claude", 300)
        stale = agentcat.cached_live_limits("claude", -1, allow_stale=True)

        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["quotas"][0]["remainingPercent"], 83.0)
        self.assertTrue(stale["stale"])

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

    def test_codex_snapshot_counts_today_week_month_and_all_tokens(self) -> None:
        codex_dir = agentcat.HOME / ".codex"
        codex_dir.mkdir(parents=True)
        db_path = codex_dir / "state_test.sqlite"
        now = dt.datetime.now(dt.timezone.utc)
        rows = [
            (10, "gpt-test", now.isoformat()),
            (20, "gpt-test", (now - dt.timedelta(days=3)).isoformat()),
            (30, "gpt-test", (now - dt.timedelta(days=20)).isoformat()),
            (40, "gpt-test", (now - dt.timedelta(days=45)).isoformat()),
        ]
        with sqlite3.connect(db_path) as conn:
            conn.execute("create table threads(tokens_used integer, model text, updated_at text)")
            conn.executemany("insert into threads(tokens_used, model, updated_at) values (?, ?, ?)", rows)
            conn.commit()

        snapshot = agentcat.codex_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["tokens"]["today"], 10)
        self.assertEqual(snapshot["tokens"]["week"], 30)
        self.assertEqual(snapshot["tokens"]["month"], 60)
        self.assertEqual(snapshot["tokens"]["all"], 100)

    def test_gemini_snapshot_reads_otel_token_metrics(self) -> None:
        agentcat.GEMINI_TELEMETRY.parent.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        start_time = [int(now.timestamp()), 0]
        next_start_time = [int(now.timestamp()) + 1, 0]

        def point(token_type: str, value: int, start: list[int]) -> dict:
            return {
                "attributes": {
                    "model": "gemini-test",
                    "session.id": "session-a",
                    "type": token_type,
                },
                "startTime": start,
                "endTime": "[Circular]",
                "value": {"sum": value},
            }

        payload = {
            "scopeMetrics": [
                {
                    "metrics": [
                        {
                            "descriptor": {"name": "gemini_cli.token.usage"},
                            "dataPoints": [
                                point("input", 100, start_time),
                                point("input", 125, start_time),
                                point("output", 20, next_start_time),
                                point("cache", 300, next_start_time),
                                point("thought", 5, next_start_time),
                                point("tool", 0, next_start_time),
                            ],
                        }
                    ]
                }
            ]
        }

        # Gemini CLI writes pretty-printed OpenTelemetry objects back-to-back,
        # not newline-delimited JSON.
        agentcat.GEMINI_TELEMETRY.write_text(
            json.dumps({"resourceMetrics": []}, indent=2) + "\n" + json.dumps(payload, indent=2),
            encoding="utf-8",
        )

        snapshot = agentcat.gemini_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 5)
        self.assertEqual(snapshot["tokens"]["inputTokens"], 125)
        self.assertEqual(snapshot["tokens"]["outputTokens"], 20)
        self.assertEqual(snapshot["tokens"]["cacheReadInputTokens"], 300)
        self.assertEqual(snapshot["tokens"]["thoughtTokens"], 5)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 450)
        self.assertEqual(snapshot["tokens"]["today"], 450)
        self.assertEqual(snapshot["tokens"]["week"], 450)
        self.assertEqual(snapshot["tokens"]["month"], 450)
        self.assertEqual(snapshot["tokens"]["all"], 450)
        self.assertEqual(snapshot["models"]["gemini-test"]["inputTokens"], 125)

    def test_classify_gemini_node_wrapper_processes(self) -> None:
        self.assertEqual(
            agentcat.classify_process("node --no-warnings=DEP0040 /opt/homebrew/bin/gemini"),
            "gemini",
        )
        self.assertEqual(
            agentcat.classify_process(
                "/opt/homebrew/Cellar/node/25.8.1_1/bin/node --no-warnings=DEP0040 /opt/homebrew/bin/gemini"
            ),
            "gemini",
        )

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
