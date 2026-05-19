import importlib.util
import io
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


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
                "seven_day_omelette": {"utilization": 57.0, "resets_at": "2026-05-08T07:00:00Z"},
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
        self.assertEqual(limits["quotas"][4]["label"], "Claude 모델 7일")
        self.assertIsNone(limits["quotas"][4]["model"])
        self.assertNotIn("omelette", limits["quotas"][4]["id"])
        self.assertNotIn("omelette", limits["quotas"][4]["label"])

    def test_claude_cached_limits_sanitizes_unknown_internal_quota_names(self) -> None:
        limits = agentcat.empty_limits(status="auto")
        limits["quotas"] = [
            {
                "id": "claude:seven_day_omelette",
                "label": "seven day omelette",
                "model": "unknown",
                "remainingPercent": 43.0,
                "usedPercent": 57.0,
            }
        ]

        sanitized = agentcat.sanitize_claude_cached_limits(limits)

        self.assertEqual(sanitized["quotas"][0]["label"], "Claude 모델 7일")
        self.assertIsNone(sanitized["quotas"][0]["model"])
        self.assertNotIn("omelette", sanitized["quotas"][0]["id"])

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

    def test_live_limit_cache_keeps_error_for_backoff(self) -> None:
        limits = agentcat.live_limit_error(RuntimeError("HTTP Error 429: Too Many Requests"), "usage-api")
        agentcat.write_live_limits_cache("codex", limits)

        cached = agentcat.cached_live_limits("codex", -1)

        self.assertIsNotNone(cached)
        self.assertEqual(cached["status"], "error")
        self.assertIn("Too Many Requests", cached["error"])
        self.assertFalse(cached.get("stale", False))

    def test_snapshot_exposes_connector_metadata_and_capabilities(self) -> None:
        snapshot = agentcat.build_snapshot()

        self.assertEqual(snapshot["schemaVersion"], 4)
        self.assertEqual(snapshot["connectorVersion"], agentcat.CONNECTOR_VERSION)
        self.assertIn("activity.memory", snapshot["capabilities"])
        self.assertIn("limits.quotaFallbackOn429", snapshot["capabilities"])
        self.assertIn("limits.claude.statuslineQuotas", snapshot["capabilities"])
        self.assertIn("usage.hourlyTokens", snapshot["capabilities"])

    def test_version_json_matches_snapshot_schema_version(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(agentcat.command_version(agentcat.argparse.Namespace(json=True)), 0)
        payload = json.loads(buf.getvalue())

        self.assertEqual(payload["connectorVersion"], agentcat.CONNECTOR_VERSION)
        self.assertEqual(payload["schemaVersion"], agentcat.SCHEMA_VERSION)
        self.assertEqual(payload["schemaVersion"], 4)

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

        # PR-A: hourly buckets emitted next to dailyTokens. Sum of all
        # buckets must match the all-time total.
        hourly = snapshot.get("hourlyTokens", {})
        self.assertIsInstance(hourly, dict)
        self.assertGreater(len(hourly), 0)
        self.assertEqual(sum(hourly.values()), 100)
        # Keys are sortable YYYY-MM-DDTHH strings.
        for key in hourly:
            self.assertRegex(key, r"^\d{4}-\d{2}-\d{2}T\d{2}$")

    def test_codex_snapshot_infers_missing_model_from_rollout_metadata(self) -> None:
        codex_dir = agentcat.HOME / ".codex"
        codex_dir.mkdir(parents=True)
        sessions_dir = codex_dir / "sessions" / "2026" / "05" / "15"
        sessions_dir.mkdir(parents=True)
        rollout_path = sessions_dir / "rollout-test.jsonl"
        rollout_path.write_text(
            json.dumps({"type": "session_meta", "payload": {"model_provider": "openai"}}) + "\n"
            + json.dumps(
                {
                    "type": "turn_context",
                    "payload": {
                        "model": "gpt-5.4",
                        "collaboration_mode": {"settings": {"model": "gpt-5.4"}},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        db_path = codex_dir / "state_test.sqlite"
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "create table threads(tokens_used integer, model text, updated_at integer, rollout_path text)"
            )
            conn.execute(
                "insert into threads(tokens_used, model, updated_at, rollout_path) values (?, ?, ?, ?)",
                (42, None, now, str(rollout_path)),
            )
            conn.commit()

        snapshot = agentcat.codex_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["models"]["gpt-5.4"]["all"], 42)
        self.assertNotIn("unknown", snapshot["models"])

    def test_claude_snapshot_adds_periods_to_daily_model_tokens(self) -> None:
        claude_dir = agentcat.HOME / ".claude"
        claude_dir.mkdir(parents=True)
        today = dt.datetime.now(dt.timezone.utc).date()
        old = today - dt.timedelta(days=45)
        stats = {
            "dailyModelTokens": [
                {
                    "date": today.isoformat(),
                    "tokensByModel": {
                        "claude-sonnet-4-6": 100,
                        "unknown": 9,
                    },
                },
                {
                    "date": old.isoformat(),
                    "tokensByModel": {
                        "claude-sonnet-4-6": 50,
                    },
                },
            ]
        }
        (claude_dir / "stats-cache.json").write_text(json.dumps(stats), encoding="utf-8")

        snapshot = agentcat.claude_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["tokens"]["today"], 109)
        self.assertEqual(snapshot["tokens"]["week"], 109)
        self.assertEqual(snapshot["tokens"]["all"], 159)
        self.assertEqual(snapshot["models"]["claude-sonnet-4-6"]["today"], 100)
        self.assertEqual(snapshot["models"]["claude-sonnet-4-6"]["week"], 100)
        self.assertEqual(snapshot["models"]["claude-sonnet-4-6"]["all"], 150)
        self.assertEqual(snapshot["models"]["unknown"]["week"], 9)

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
        self.assertEqual(snapshot["models"]["gemini-test"]["today"], 450)
        self.assertEqual(snapshot["models"]["gemini-test"]["week"], 450)
        self.assertEqual(snapshot["models"]["gemini-test"]["month"], 450)
        self.assertEqual(snapshot["models"]["gemini-test"]["all"], 450)

    def test_opencode_snapshot_reads_sqlite_message_tokens(self) -> None:
        data_home = agentcat.HOME / ".local" / "share"
        opencode_dir = data_home / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                create table session (
                  id text primary key,
                  parent_id text,
                  directory text,
                  title text,
                  time_archived integer
                )
                """
            )
            conn.execute(
                "create table message (id text primary key, session_id text, time_created integer, data text)"
            )
            conn.execute(
                "insert into session(id, parent_id, directory, title, time_archived) values (?, ?, ?, ?, ?)",
                ("session-1", None, "/tmp/project", "Project", None),
            )
            conn.execute(
                "insert into message(id, session_id, time_created, data) values (?, ?, ?, ?)",
                (
                    "message-1",
                    "session-1",
                    now_ms,
                    json.dumps(
                        {
                            "role": "assistant",
                            "modelID": "anthropic/claude-sonnet-4-6",
                            "tokens": {
                                "input": 100,
                                "output": 25,
                                "reasoning": 5,
                                "cache": {"read": 20, "write": 10},
                            },
                        }
                    ),
                ),
            )
            conn.commit()

        with patch.dict(agentcat.os.environ, {"XDG_DATA_HOME": str(data_home)}):
            snapshot = agentcat.opencode_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 160)
        self.assertEqual(snapshot["tokens"]["week"], 160)
        self.assertEqual(snapshot["models"]["anthropic/claude-sonnet-4-6"]["inputTokens"], 100)
        self.assertEqual(snapshot["models"]["anthropic/claude-sonnet-4-6"]["week"], 160)

    def test_copilot_snapshot_reads_legacy_and_vscode_transcript_tokens(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        legacy_dir = agentcat.HOME / ".copilot" / "session-state" / "legacy-session"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "events.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session.model_change",
                            "timestamp": now,
                            "data": {"newModel": "gpt-4.1"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "timestamp": now,
                            "data": {"messageId": "assistant-1", "outputTokens": 75},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        transcript_dir = (
            agentcat.HOME
            / "Library"
            / "Application Support"
            / "Code"
            / "User"
            / "workspaceStorage"
            / "workspace-1"
            / "GitHub.copilot-chat"
            / "transcripts"
        )
        transcript_dir.mkdir(parents=True)
        (transcript_dir / "transcript.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"type": "session.start", "timestamp": now, "data": {"sessionId": "session-2", "producer": "copilot-agent"}}),
                    json.dumps({"type": "user.message", "timestamp": now, "data": {"content": "abcd" * 10}}),
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "timestamp": now,
                            "data": {
                                "messageId": "assistant-2",
                                "content": "done",
                                "reasoningText": "thinking",
                                "toolRequests": [{"toolCallId": "call_abc", "name": "read_file"}],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        snapshot = agentcat.copilot_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 2)
        self.assertEqual(snapshot["models"]["gpt-4.1"]["outputTokens"], 75)
        self.assertEqual(snapshot["models"]["gpt-4.1"]["week"], 75)
        self.assertGreater(snapshot["models"]["copilot-openai-auto"]["inputTokens"], 0)
        self.assertGreater(snapshot["models"]["copilot-openai-auto"]["week"], 0)

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

    def test_classify_ignores_codex_desktop_electron_helpers_on_windows(self) -> None:
        self.assertIsNone(
            agentcat.classify_process(
                r'"C:\Program Files\WindowsApps\OpenAI.Codex_26.513.4821.0_x64__2p2nqsd0c76g0\app\Codex.exe"'
            )
        )
        self.assertIsNone(
            agentcat.classify_process(
                r'"C:\Program Files\WindowsApps\OpenAI.Codex_26.513.4821.0_x64__2p2nqsd0c76g0\app\Codex.exe" --type=renderer --user-data-dir="C:\Users\me\AppData\Roaming\Codex"'
            )
        )
        self.assertEqual(
            agentcat.classify_process(
                r'"C:\Program Files\WindowsApps\OpenAI.Codex_26.513.4821.0_x64__2p2nqsd0c76g0\app\resources\codex.exe" app-server --analytics-default-enabled'
            ),
            "codex",
        )

    def test_classify_ignores_vscode_chatgpt_extension_codex_server(self) -> None:
        self.assertIsNone(
            agentcat.classify_process(
                r"c:\Users\me\.vscode\extensions\openai.chatgpt-26.513.21555-win32-x64\bin\windows-x86_64\codex.exe app-server --analytics-default-enabled"
            )
        )

    def test_classify_ignores_codex_desktop_stdio_app_servers(self) -> None:
        self.assertIsNone(
            agentcat.classify_process(
                r'"C:\Users\me\AppData\Local\OpenAI\Codex\bin\76ac88818493fc45\codex.exe" app-server --listen stdio://'
            )
        )

    def test_windows_activity_ignores_codex_desktop_helper_processes(self) -> None:
        completed = agentcat.subprocess.CompletedProcess(
            args=["powershell.exe"],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "ProcessId": 6036,
                        "ParentProcessId": 14000,
                        "Name": "Codex.exe",
                        "CommandLine": r'"C:\Program Files\WindowsApps\OpenAI.Codex_26.513.4821.0_x64__2p2nqsd0c76g0\app\Codex.exe"',
                        "CpuPercent": 0,
                    },
                    {
                        "ProcessId": 8044,
                        "ParentProcessId": 6036,
                        "Name": "Codex.exe",
                        "CommandLine": r'"C:\Program Files\WindowsApps\OpenAI.Codex_26.513.4821.0_x64__2p2nqsd0c76g0\app\Codex.exe" --type=renderer',
                        "CpuPercent": 0,
                    },
                    {
                        "ProcessId": 13496,
                        "ParentProcessId": 6036,
                        "Name": "codex.exe",
                        "CommandLine": r'"C:\Program Files\WindowsApps\OpenAI.Codex_26.513.4821.0_x64__2p2nqsd0c76g0\app\resources\codex.exe" app-server --analytics-default-enabled',
                        "CpuPercent": 0,
                    },
                    {
                        "ProcessId": 38772,
                        "ParentProcessId": 14196,
                        "Name": "codex.exe",
                        "CommandLine": r"c:\Users\me\.vscode\extensions\openai.chatgpt-26.513.21555-win32-x64\bin\windows-x86_64\codex.exe app-server --analytics-default-enabled",
                        "CpuPercent": 0,
                    },
                    {
                        "ProcessId": 23020,
                        "ParentProcessId": 21436,
                        "Name": "codex.exe",
                        "CommandLine": r'"C:\Users\me\AppData\Local\OpenAI\Codex\bin\76ac88818493fc45\codex.exe" app-server --listen stdio://',
                        "CpuPercent": 0,
                    },
                ]
            ),
            stderr="",
        )

        with patch.object(agentcat.subprocess, "run", return_value=completed):
            snapshot = agentcat.terminal_activity_snapshot_windows()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["processCount"], 1)
        self.assertEqual(snapshot["countsByProvider"]["codex"], 1)
        self.assertEqual(snapshot["processes"][0]["pid"], 13496)

    def test_motion_stage_uses_granular_activity_thresholds(self) -> None:
        self.assertEqual(agentcat.motion_stage(0, 100, 10), "sleeping")
        self.assertEqual(agentcat.motion_stage(1, 0, 0), "walking")
        self.assertEqual(agentcat.motion_stage(1, 6.9, 0), "walking")
        self.assertEqual(agentcat.motion_stage(1, 7, 0), "running")
        self.assertEqual(agentcat.motion_stage(1, 21.9, 0), "running")
        self.assertEqual(agentcat.motion_stage(1, 22, 0), "sprinting")
        self.assertEqual(agentcat.motion_stage(1, 0, 2), "running")
        self.assertEqual(agentcat.motion_stage(1, 0, 6), "sprinting")

    def test_terminal_activity_snapshot_reports_memory_usage(self) -> None:
        completed = agentcat.subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=(
                "101 1 3.5 524288 R /opt/homebrew/bin/codex --model gpt-5.5\n"
                "102 1 1.0 262144 S /opt/homebrew/bin/claude\n"
                "103 1 0.5 131072 S /opt/homebrew/bin/gemini\n"
            ),
            stderr="",
        )

        with patch.object(agentcat.subprocess, "run", return_value=completed):
            snapshot = agentcat.terminal_activity_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["totalMemoryBytes"], 939_524_096)
        self.assertEqual(snapshot["memoryBytesByProvider"]["codex"], 536_870_912)
        self.assertEqual(snapshot["memoryBytesByProvider"]["claude"], 268_435_456)
        self.assertEqual(snapshot["memoryBytesByProvider"]["gemini"], 134_217_728)
        self.assertEqual(snapshot["processes"][0]["memoryBytes"], 536_870_912)
        self.assertEqual(snapshot["processes"][0]["rssKilobytes"], 524_288)
        self.assertEqual(snapshot["processes"][0]["command"], "codex pid 101")
        self.assertNotIn("gpt-5.5", snapshot["processes"][0]["command"])

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
        self.assertEqual([quota["id"] for quota in limits["quotas"]], ["claude:five_hour", "claude:seven_day", "claude:session"])
        self.assertEqual(limits["quotas"][0]["remainingPercent"], 96.0)
        self.assertEqual(limits["quotas"][1]["remainingPercent"], 90.0)
        self.assertEqual(limits["quotas"][2]["limit"], 1000000.0)

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

    def test_runtime_hooks_do_not_block_on_snapshot_refresh(self) -> None:
        with patch.object(agentcat, "build_snapshot", side_effect=AssertionError("hook should not refresh snapshot")):
            self.assertEqual(agentcat.command_codex_notify(agentcat.argparse.Namespace()), 0)
            self.assertEqual(agentcat.command_claude_hook(agentcat.argparse.Namespace(event="Stop")), 0)
            self.assertEqual(agentcat.command_gemini_hook(agentcat.argparse.Namespace(event="Stop")), 0)

    def test_claude_statusline_does_not_block_on_snapshot_refresh(self) -> None:
        with patch.object(agentcat, "build_snapshot", side_effect=AssertionError("statusline should not refresh snapshot")):
            self.assertEqual(agentcat.command_claude_statusline(agentcat.argparse.Namespace()), 0)

    def test_setup_prompt_includes_install_skill_and_privacy_rules(self) -> None:
        prompt = agentcat.setup_prompt_text()

        self.assertIn("Codex", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("Gemini CLI", prompt)
        self.assertIn("agentcat snapshot --json", prompt)
        self.assertIn("skills/agentcat-usage", prompt)
        self.assertIn("Never store or report prompt text", prompt)


class InsightsIntegrationTests(unittest.TestCase):
    """Slice C — build_snapshot() includes insights, schema is v3."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_home = agentcat.HOME
        self.old_agentcat_home = agentcat.AGENTCAT_HOME
        self.old_events_db = agentcat.EVENTS_DB
        self.old_latest = agentcat.LATEST_SNAPSHOT
        agentcat.HOME = self.root
        agentcat.AGENTCAT_HOME = self.root / ".agentcat"
        agentcat.EVENTS_DB = agentcat.AGENTCAT_HOME / "events.sqlite"
        agentcat.LATEST_SNAPSHOT = agentcat.AGENTCAT_HOME / "latest-snapshot.json"

    def tearDown(self) -> None:
        agentcat.HOME = self.old_home
        agentcat.AGENTCAT_HOME = self.old_agentcat_home
        agentcat.EVENTS_DB = self.old_events_db
        agentcat.LATEST_SNAPSHOT = self.old_latest
        self.tmp.cleanup()

    def _stub_providers(self, providers_dict):
        patches = []
        for name, data in providers_dict.items():
            p = patch.object(agentcat, f"{name}_snapshot", return_value=data)
            patches.append(p)
            p.start()
        return patches

    def _stop(self, patches):
        for p in patches:
            p.stop()

    def test_build_snapshot_schema_version_is_4(self) -> None:
        patches = self._stub_providers({
            "codex": {"status": "ok", "tokens": {}, "models": {}},
            "claude": {"status": "ok", "tokens": {}, "models": {}},
            "gemini": {"status": "ok", "tokens": {}, "models": {}},
            "opencode": {"status": "not_found"},
            "copilot": {"status": "not_found"},
        })
        try:
            with patch.object(agentcat, "terminal_activity_snapshot", return_value={"status": "ok"}):
                snap = agentcat.build_snapshot()
            self.assertEqual(snap["schemaVersion"], 4)
        finally:
            self._stop(patches)

    def test_build_snapshot_includes_insights_object(self) -> None:
        patches = self._stub_providers({
            "codex": {"status": "ok", "tokens": {}, "models": {}},
            "claude": {
                "status": "ok",
                "tokens": {"inputTokens": 1_000_000},
                "models": {"claude-opus-4-7": {"week": {
                    "inputTokens": 1_000_000, "outputTokens": 0,
                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            },
            "gemini": {"status": "ok", "tokens": {}, "models": {}},
            "opencode": {"status": "not_found"},
            "copilot": {"status": "not_found"},
        })
        try:
            with patch.object(agentcat, "terminal_activity_snapshot", return_value={"status": "ok"}):
                snap = agentcat.build_snapshot()
            self.assertIn("insights", snap)
            self.assertEqual(snap["insights"]["status"], "ok")
            self.assertGreater(snap["insights"]["summary"]["total_tokens"], 0)
            self.assertEqual(snap["insights"]["summary"]["top_provider"], "claude")
        finally:
            self._stop(patches)

    def test_insights_status_error_caught_not_raised(self) -> None:
        patches = self._stub_providers({
            "codex": {"status": "ok", "tokens": {}, "models": {}},
            "claude": {"status": "ok", "tokens": {}, "models": {}},
            "gemini": {"status": "ok", "tokens": {}, "models": {}},
            "opencode": {"status": "not_found"},
            "copilot": {"status": "not_found"},
        })
        try:
            with patch.object(agentcat, "terminal_activity_snapshot", return_value={"status": "ok"}), \
                 patch.object(agentcat, "derive_insights", side_effect=RuntimeError("kaboom")):
                snap = agentcat.build_snapshot()
            self.assertEqual(snap["insights"]["status"], "error")
            self.assertIn("kaboom", snap["insights"]["error"])
        finally:
            self._stop(patches)


class InsightsTests(unittest.TestCase):
    """Slice B — derive_insights() port of Swift AgentInsights.derive()."""

    def _snapshot(self, **providers) -> dict:
        return {"providers": providers}

    def test_handles_empty_snapshot(self) -> None:
        result = agentcat.derive_insights(self._snapshot(), period="week")
        self.assertEqual(result["summary"]["total_tokens"], 0)
        self.assertEqual(result["summary"]["estimated_cost_usd"], 0.0)
        self.assertEqual(result["providers"], [])
        self.assertEqual(result["models"], [])
        self.assertEqual(result["findings"], [])

    def test_splits_cache_tokens_correctly(self) -> None:
        # Q1 fix verification: when cache tokens dominate, cost is much
        # lower than if we'd lumped them as input. Same model, same total
        # token count → two scenarios.
        s_all_input = self._snapshot(claude={
            "tokens": {"inputTokens": 4_000_000},
            "models": {
                "claude-opus-4-7": {
                    "week": {"inputTokens": 4_000_000, "outputTokens": 0,
                              "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}
                }
            },
        })
        s_mostly_cache = self._snapshot(claude={
            "tokens": {"cacheReadInputTokens": 3_900_000, "inputTokens": 100_000},
            "models": {
                "claude-opus-4-7": {
                    "week": {"inputTokens": 100_000, "outputTokens": 0,
                              "cacheReadInputTokens": 3_900_000, "cacheCreationInputTokens": 0}
                }
            },
        })
        r1 = agentcat.derive_insights(s_all_input, period="week")
        r2 = agentcat.derive_insights(s_mostly_cache, period="week")
        # Same total tokens, but cache-heavy should be at least 5x cheaper.
        self.assertGreater(r1["summary"]["estimated_cost_usd"], 0)
        self.assertGreater(r2["summary"]["estimated_cost_usd"], 0)
        self.assertGreater(
            r1["summary"]["estimated_cost_usd"],
            r2["summary"]["estimated_cost_usd"] * 5,
        )

    def test_emits_pricing_missing_for_unknown_model(self) -> None:
        s = self._snapshot(unknown_provider={
            "tokens": {"inputTokens": 1000},
            "models": {
                "my-totally-unknown-model": {
                    "week": {"inputTokens": 1000, "outputTokens": 0,
                              "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}
                }
            },
        })
        r = agentcat.derive_insights(s, period="week")
        finding_ids = [f["id"] for f in r["findings"]]
        self.assertTrue(any(fid.startswith("pricing_missing:") for fid in finding_ids))

    def test_sums_across_providers(self) -> None:
        s = self._snapshot(
            claude={
                "tokens": {"inputTokens": 1_000_000},
                "models": {"claude-opus-4-7": {"week": {
                    "inputTokens": 1_000_000, "outputTokens": 0,
                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            },
            codex={
                "tokens": {"inputTokens": 500_000},
                "models": {"gpt-5": {"week": {
                    "inputTokens": 500_000, "outputTokens": 0,
                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            },
        )
        r = agentcat.derive_insights(s, period="week")
        kinds = {p["kind"] for p in r["providers"]}
        self.assertEqual(kinds, {"claude", "codex"})
        self.assertEqual(r["summary"]["total_tokens"], 1_500_000)

    def test_top_model_and_provider_picked(self) -> None:
        s = self._snapshot(
            small={
                "tokens": {"inputTokens": 100_000},
                "models": {"gpt-5-mini": {"week": {
                    "inputTokens": 100_000, "outputTokens": 0,
                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            },
            big={
                "tokens": {"inputTokens": 5_000_000},
                "models": {"claude-opus-4-7": {"week": {
                    "inputTokens": 5_000_000, "outputTokens": 0,
                    "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            },
        )
        r = agentcat.derive_insights(s, period="week")
        self.assertEqual(r["summary"]["top_provider"], "big")
        self.assertEqual(r["summary"]["top_model"], "claude-opus-4-7")

    def test_high_weekly_usage_finding(self) -> None:
        s = self._snapshot(claude={
            "tokens": {"inputTokens": 1000},
            "models": {"claude-opus-4-7": {"week": {
                "inputTokens": 1000, "outputTokens": 0,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
            "limits": {"weeklyUsedPercent": 96},
        })
        r = agentcat.derive_insights(s, period="week")
        high = [f for f in r["findings"] if f["id"].startswith("high_weekly_usage:")]
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0]["severity"], "high")  # ≥95 = high


class PricingTests(unittest.TestCase):
    """Slice A — split-bucket pricing module."""

    def test_known_model_returns_split_cost(self) -> None:
        cost = agentcat.estimate_cost(
            "claude-opus-4-7",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        self.assertIsInstance(cost, dict)
        for k in ("input", "output", "cache_read", "cache_write", "total"):
            self.assertIn(k, cost)
        self.assertAlmostEqual(
            cost["total"],
            cost["input"] + cost["output"] + cost["cache_read"] + cost["cache_write"],
            places=4,
        )

    def test_cache_read_is_cheaper_than_input(self) -> None:
        cost = agentcat.estimate_cost(
            "claude-opus-4-7",
            input_tokens=1_000_000, output_tokens=0,
            cache_read_tokens=1_000_000, cache_write_tokens=0,
        )
        # The Q1 fix: cache reads must price much cheaper than fresh input.
        self.assertLess(cost["cache_read"], cost["input"])
        self.assertLess(cost["cache_read"] * 5, cost["input"])

    def test_unknown_model_returns_none(self) -> None:
        cost = agentcat.estimate_cost(
            "totally-made-up-model-99",
            input_tokens=1_000_000, output_tokens=1_000_000,
            cache_read_tokens=0, cache_write_tokens=0,
        )
        self.assertIsNone(cost)

    def test_model_alias_normalization(self) -> None:
        cost_a = agentcat.estimate_cost(
            "claude-opus-4-7-20260101",
            input_tokens=1_000_000, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0,
        )
        cost_b = agentcat.estimate_cost(
            "claude-opus-4-7",
            input_tokens=1_000_000, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0,
        )
        self.assertIsNotNone(cost_a)
        self.assertEqual(cost_a["input"], cost_b["input"])

    def test_zero_tokens_returns_zero_not_none(self) -> None:
        cost = agentcat.estimate_cost(
            "claude-opus-4-7",
            input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0,
        )
        self.assertEqual(cost["total"], 0.0)


if __name__ == "__main__":
    unittest.main()
