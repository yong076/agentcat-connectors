import importlib.util
import io
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
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
            "PROVIDER_CONFIG_FILE": agentcat.PROVIDER_CONFIG_FILE,
            "GEMINI_TELEMETRY": agentcat.GEMINI_TELEMETRY,
            "GEMINI_USAGE_CACHE": agentcat.GEMINI_USAGE_CACHE,
            "ANTIGRAVITY_CLI_DIR": agentcat.ANTIGRAVITY_CLI_DIR,
            "ANTIGRAVITY_TELEMETRY": agentcat.ANTIGRAVITY_TELEMETRY,
            "ANTIGRAVITY_USAGE_CACHE": agentcat.ANTIGRAVITY_USAGE_CACHE,
            "LIVE_LIMITS_CACHE": agentcat.LIVE_LIMITS_CACHE,
            "JOURNAL_CURSOR_FILE": agentcat.JOURNAL_CURSOR_FILE,
            "CLAUDE_PROJECTS_DIR": agentcat.CLAUDE_PROJECTS_DIR,
            "_GEMINI_CACHE_KEY": agentcat._GEMINI_CACHE_KEY,
            "_GEMINI_CACHE_VALUE": agentcat._GEMINI_CACHE_VALUE,
            "_GEMINI_CACHE_LOADED_AT": agentcat._GEMINI_CACHE_LOADED_AT,
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
        agentcat.PROVIDER_CONFIG_FILE = agentcat_home / "providers.json"
        agentcat.GEMINI_TELEMETRY = agentcat_home / "gemini" / "telemetry.log"
        agentcat.GEMINI_USAGE_CACHE = agentcat_home / "gemini-usage-cache.json"
        agentcat.ANTIGRAVITY_CLI_DIR = home / ".gemini" / "antigravity-cli"
        agentcat.ANTIGRAVITY_TELEMETRY = agentcat_home / "gemini" / "antigravity-telemetry.log"
        agentcat.ANTIGRAVITY_USAGE_CACHE = agentcat_home / "antigravity-usage-cache.json"
        agentcat.LIVE_LIMITS_CACHE = agentcat_home / "live-limits-cache.json"
        agentcat.JOURNAL_CURSOR_FILE = agentcat_home / "jsonl-cursor.json"
        agentcat.CLAUDE_PROJECTS_DIR = home / ".claude" / "projects"
        agentcat._GEMINI_CACHE_KEY = None
        agentcat._GEMINI_CACHE_VALUE = None
        agentcat._GEMINI_CACHE_LOADED_AT = 0.0

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

    # --- Unified providers.json config -----------------------------------

    def _write_provider_config(self, payload) -> None:
        agentcat.PROVIDER_CONFIG_FILE.write_text(
            json.dumps(payload) if not isinstance(payload, str) else payload,
            encoding="utf-8",
        )

    def test_provider_config_capability_is_advertised(self) -> None:
        self.assertIn("config.providers", agentcat.CONNECTOR_CAPABILITIES)
        snapshot = agentcat.build_snapshot()
        self.assertIn("config.providers", snapshot["capabilities"])

    def test_disabled_provider_yields_disabled_status_with_zero_tokens(self) -> None:
        self._write_provider_config({"providers": {"cursor": {"enabled": False}}})

        snapshot = agentcat.build_snapshot()
        cursor = snapshot["providers"]["cursor"]

        self.assertEqual(cursor["status"], "disabled")
        self.assertEqual(cursor["tokens"], {"today": 0, "week": 0, "month": 0, "all": 0})
        self.assertEqual(cursor["models"], {})
        self.assertEqual(cursor["projects"]["items"], [])
        # Disabled providers still carry a limits block for shape consistency.
        self.assertIn("limits", cursor)
        # Other providers remain normal (codex unaffected by cursor's flag).
        self.assertNotEqual(snapshot["providers"]["codex"]["status"], "disabled")

    def test_provider_is_enabled_defaults_true_and_only_false_when_explicit(self) -> None:
        cfg = {
            "off": {"enabled": False},
            "on": {"enabled": True},
            "limited": {"limits": {"week": 100}},
        }
        self.assertFalse(agentcat.provider_is_enabled(cfg, "off"))
        self.assertTrue(agentcat.provider_is_enabled(cfg, "on"))
        self.assertTrue(agentcat.provider_is_enabled(cfg, "limited"))
        self.assertTrue(agentcat.provider_is_enabled(cfg, "absent"))

    def test_manual_limit_for_non_codex_provider_flows_through_snapshot(self) -> None:
        self._write_provider_config(
            {"providers": {"goose": {"limits": {"week": 50000, "session": 128000}}}}
        )

        limits_map = agentcat.configured_limits()
        self.assertEqual(limits_map["goose"]["weeklyTokens"], 50000)
        self.assertEqual(limits_map["goose"]["sessionTokens"], 128000)

        snapshot = agentcat.build_snapshot()
        goose_limits = snapshot["providers"]["goose"]["limits"]
        self.assertEqual(goose_limits["weeklyTokens"], 50000)
        self.assertEqual(goose_limits["sessionTokens"], 128000)

    def test_providers_json_overrides_limits_json_for_same_id(self) -> None:
        agentcat.LIMITS_FILE.write_text(
            json.dumps({"providers": {"codex": {"week": 1111}}}), encoding="utf-8"
        )
        self._write_provider_config({"providers": {"codex": {"limits": {"week": 9999}}}})

        limits_map = agentcat.configured_limits()
        self.assertEqual(limits_map["codex"]["weeklyTokens"], 9999)

    def test_missing_and_garbage_providers_json_are_tolerated(self) -> None:
        # No file at all.
        self.assertEqual(agentcat.provider_config(), {})
        self.assertEqual(sorted(agentcat.configured_limits().keys()), ["claude", "codex", "gemini"])
        snap = agentcat.build_snapshot()
        self.assertNotEqual(snap["providers"]["cursor"]["status"], "disabled")

        # Garbage (invalid JSON) -> read_json error sentinel -> empty config.
        self._write_provider_config("{ this is not json")
        self.assertEqual(agentcat.provider_config(), {})
        snap = agentcat.build_snapshot()
        self.assertNotEqual(snap["providers"]["cursor"]["status"], "disabled")

        # Non-dict top-level payload is tolerated too.
        self._write_provider_config([1, 2, 3])
        self.assertEqual(agentcat.provider_config(), {})

    def test_limits_json_still_works_for_mature_providers(self) -> None:
        agentcat.LIMITS_FILE.write_text(
            json.dumps(
                {
                    "providers": {
                        "codex": {"week": 7000},
                        "claude": {"month": 8000},
                        "gemini": {"session": 9000},
                    }
                }
            ),
            encoding="utf-8",
        )

        limits_map = agentcat.configured_limits()
        self.assertEqual(limits_map["codex"]["weeklyTokens"], 7000)
        self.assertEqual(limits_map["claude"]["monthlyTokens"], 8000)
        self.assertEqual(limits_map["gemini"]["sessionTokens"], 9000)

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

    def test_gemini_quota_prioritizes_constrained_models_for_compact_ui(self) -> None:
        limits = agentcat.gemini_limits_from_quota_response(
            {
                "buckets": [
                    {
                        "modelId": "gemini-3-pro-preview",
                        "remainingFraction": 1,
                        "resetTime": "2026-05-07T11:19:25Z",
                        "tokenType": "REQUESTS",
                    },
                    {
                        "modelId": "gemini-3.1-pro-preview",
                        "remainingFraction": 1,
                        "resetTime": "2026-05-07T11:19:25Z",
                        "tokenType": "REQUESTS",
                    },
                    {
                        "modelId": "gemini-3-flash-preview",
                        "remainingFraction": 0.953,
                        "resetTime": "2026-05-07T06:00:00Z",
                        "tokenType": "REQUESTS",
                    },
                    {
                        "modelId": "gemini-3.1-flash-lite",
                        "remainingFraction": 0.9925,
                        "resetTime": "2026-05-07T05:00:00Z",
                        "tokenType": "REQUESTS",
                    },
                ]
            },
            {"paidTier": {"name": "Gemini Code Assist in Google One AI Pro"}},
        )

        self.assertEqual(limits["quotas"][0]["model"], "gemini-3-flash-preview")
        self.assertAlmostEqual(limits["quotas"][0]["usedPercent"], 4.7)
        self.assertEqual(limits["quotas"][1]["model"], "gemini-3.1-flash-lite")

    def test_gemini_cached_limits_are_reprioritized_for_compact_ui(self) -> None:
        cached = agentcat.empty_limits(status="auto")
        cached["quotas"] = [
            {"id": "gemini:pro", "label": "Gemini Pro", "model": "gemini-3-pro-preview", "remainingPercent": 100},
            {"id": "gemini:pro2", "label": "Gemini 3.1 Pro", "model": "gemini-3.1-pro-preview", "remainingPercent": 100},
            {"id": "gemini:flash", "label": "Gemini 3 Flash", "model": "gemini-3-flash-preview", "remainingPercent": 95.3},
            {"id": "gemini:lite", "label": "Gemini 3.1 Flash Lite", "model": "gemini-3.1-flash-lite", "remainingPercent": 99.25},
        ]

        normalized = agentcat.normalize_gemini_limits(cached)

        self.assertEqual(normalized["quotas"][0]["model"], "gemini-3-flash-preview")
        self.assertEqual(normalized["quotas"][1]["model"], "gemini-3.1-flash-lite")

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
        self.assertIn("update", snapshot)
        self.assertIn("activity.memory", snapshot["capabilities"])
        self.assertIn("connector.autoUpdate", snapshot["capabilities"])
        self.assertIn("limits.quotaFallbackOn429", snapshot["capabilities"])
        self.assertIn("limits.claude.statuslineQuotas", snapshot["capabilities"])
        self.assertIn("usage.hourlyTokens", snapshot["capabilities"])

    def test_connector_version_parser_and_comparison(self) -> None:
        text = 'CONNECTOR_VERSION = os.environ.get("AGENTCAT_CONNECTOR_VERSION", "26.22.10")'

        self.assertEqual(agentcat.parse_connector_version_from_text(text), "26.22.10")
        self.assertTrue(agentcat.is_newer_connector_version("26.22.10", "26.22.9"))
        self.assertFalse(agentcat.is_newer_connector_version("26.22.9", "26.22.10"))

    def test_auto_update_check_starts_installer_for_new_managed_version(self) -> None:
        install_dir = (agentcat.AGENTCAT_HOME / "connectors").resolve()
        proc = type("Proc", (), {"pid": 12345})()

        with patch.dict(agentcat.os.environ, {
            "AGENTCAT_AUTO_UPDATE": "1",
            "AGENTCAT_CONNECTOR_VERSION": "",
            "AGENTCAT_CONNECTORS_DIR": str(install_dir),
        }), \
                patch.object(agentcat, "current_connector_repo_dir", return_value=install_dir), \
                patch.object(agentcat, "fetch_remote_connector_version", return_value="99.0.0"), \
                patch.object(agentcat, "start_auto_update_install", return_value=proc) as starter:
            state = agentcat.check_auto_update_once(apply_update=True)

        self.assertEqual(state["status"], "update_started")
        self.assertEqual(state["remoteVersion"], "99.0.0")
        self.assertEqual(state["installPid"], 12345)
        starter.assert_called_once_with("99.0.0")

    def test_auto_update_check_does_not_update_from_dev_checkout(self) -> None:
        with patch.dict(agentcat.os.environ, {
            "AGENTCAT_AUTO_UPDATE": "1",
            "AGENTCAT_CONNECTOR_VERSION": "",
            "AGENTCAT_CONNECTORS_DIR": str(agentcat.AGENTCAT_HOME / "connectors"),
        }), \
                patch.object(agentcat, "current_connector_repo_dir", return_value=self.root / "dev"):
            state = agentcat.check_auto_update_once(apply_update=True)

        self.assertEqual(state["status"], "disabled")
        self.assertIn("outside managed install", state["reason"])

    def test_http_snapshot_preserves_cached_provider_generated_at(self) -> None:
        agentcat.LATEST_SNAPSHOT.write_text(
            json.dumps(
                {
                    "schemaVersion": 4,
                    "generatedAt": "2026-05-01T00:00:00Z",
                    "providers": {"claude": {"status": "ok"}},
                    "activity": {"status": "old"},
                }
            ),
            encoding="utf-8",
        )

        with patch.object(agentcat, "terminal_activity_snapshot", return_value={"status": "ok"}):
            snapshot = agentcat.snapshot_for_http()

        self.assertEqual(snapshot["generatedAt"], "2026-05-01T00:00:00Z")
        self.assertIn("servedAt", snapshot)
        self.assertIn("update", snapshot)
        self.assertEqual(snapshot["activity"], {"status": "ok"})

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
        with closing(sqlite3.connect(db_path)) as conn:
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
        with closing(sqlite3.connect(db_path)) as conn:
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

    def test_current_model_capability_is_advertised(self) -> None:
        self.assertIn("usage.currentModel", agentcat.CONNECTOR_CAPABILITIES)

    def test_note_current_model_keeps_freshest_record_and_finalizes(self) -> None:
        result: dict = {}
        now = dt.datetime.now(dt.timezone.utc)
        agentcat.note_current_model(result, "model-b", now - dt.timedelta(hours=2))
        agentcat.note_current_model(result, "model-a", now, source="threads")
        # Older records, timestamp-less records, and "unknown" never win.
        agentcat.note_current_model(result, "model-c", now - dt.timedelta(hours=1))
        agentcat.note_current_model(result, "model-d", None)
        agentcat.note_current_model(result, "unknown", now + dt.timedelta(hours=1))

        agentcat.finalize_current_model(result)

        self.assertNotIn("_currentModelCandidate", result)
        current = result["currentModel"]
        self.assertEqual(current["id"], "model-a")
        self.assertEqual(current["source"], "threads")
        self.assertRegex(current["updatedAt"], r"^\d{4}-\d{2}-\d{2}T")
        json.dumps(result)  # snapshot stays JSON-serializable after finalize

    def test_classify_model_tier_uses_pricing_not_names(self) -> None:
        self.assertEqual(agentcat.classify_model_tier("claude-opus-4-7"), "flagship")
        self.assertEqual(agentcat.classify_model_tier("claude-opus-4-7[1m]"), "flagship")
        self.assertEqual(agentcat.classify_model_tier("claude-sonnet-4-6"), "standard")
        self.assertEqual(agentcat.classify_model_tier("claude-haiku-4-5"), "mini")
        self.assertEqual(agentcat.classify_model_tier("gemini-3-flash-preview"), "mini")
        # Unknown pricing -> no tier, never a name-based guess.
        self.assertIsNone(agentcat.classify_model_tier("claude-fable-5"))

    def test_finalize_current_model_attaches_tier(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        result: dict = {}
        agentcat.note_current_model(result, "claude-opus-4-7", now)
        agentcat.finalize_current_model(result)
        self.assertEqual(result["currentModel"]["tier"], "flagship")

        # Pre-set currentModel (claude statusline standalone path) gets a tier too.
        preset = {"currentModel": {"id": "claude-haiku-4-5", "source": "statusline"}}
        agentcat.finalize_current_model(preset)
        self.assertEqual(preset["currentModel"]["tier"], "mini")

        unknown: dict = {}
        agentcat.note_current_model(unknown, "claude-fable-5", now)
        agentcat.finalize_current_model(unknown)
        self.assertNotIn("tier", unknown["currentModel"])

    def test_add_usage_metrics_notes_current_model(self) -> None:
        result: dict = {}
        now = dt.datetime.now(dt.timezone.utc)
        agentcat.add_usage_metrics(result, "qwen-coder", {"inputTokens": 5, "outputTokens": 5}, now - dt.timedelta(hours=1))
        agentcat.add_usage_metrics(result, "qwen-max", {"inputTokens": 1, "outputTokens": 1}, now)

        agentcat.finalize_current_model(result)

        self.assertEqual(result["currentModel"]["id"], "qwen-max")

    def test_codex_snapshot_exposes_current_model_from_latest_thread(self) -> None:
        codex_dir = agentcat.HOME / ".codex"
        codex_dir.mkdir(parents=True)
        db_path = codex_dir / "state_test.sqlite"
        now = dt.datetime.now(dt.timezone.utc)
        rows = [
            (10, "gpt-newest", now.isoformat()),
            (999, "gpt-heavier-but-older", (now - dt.timedelta(days=3)).isoformat()),
        ]
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("create table threads(tokens_used integer, model text, updated_at text)")
            conn.executemany("insert into threads(tokens_used, model, updated_at) values (?, ?, ?)", rows)
            conn.commit()

        snapshot = agentcat.codex_snapshot()
        agentcat.finalize_current_model(snapshot)

        current = snapshot["currentModel"]
        self.assertEqual(current["id"], "gpt-newest")
        self.assertEqual(current["source"], "threads")

    def test_claude_snapshot_tracks_current_model_from_journal(self) -> None:
        project_dir = agentcat.CLAUDE_PROJECTS_DIR / "test-project"
        project_dir.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)

        def record(ts: dt.datetime, model: str, output_tokens: int) -> str:
            return json.dumps(
                {
                    "timestamp": ts.isoformat().replace("+00:00", "Z"),
                    "message": {
                        "model": model,
                        "usage": {"input_tokens": 1, "output_tokens": output_tokens},
                    },
                }
            )

        (project_dir / "session.jsonl").write_text(
            record(now - dt.timedelta(hours=3), "claude-sonnet-4-6", 5000) + "\n"
            + record(now, "claude-opus-4-8", 2) + "\n",
            encoding="utf-8",
        )

        snapshot = agentcat.claude_snapshot()
        agentcat.finalize_current_model(snapshot)

        current = snapshot["currentModel"]
        self.assertEqual(current["id"], "claude-opus-4-8")
        self.assertEqual(current["source"], "journal")

    def test_gemini_state_tracks_latest_model_for_current_model(self) -> None:
        state: dict = {}
        now = dt.datetime.now(dt.timezone.utc)
        agentcat.add_gemini_delta_to_state(
            state, token_key="inputTokens", model="gemini-3-pro", amount=5, when=now - dt.timedelta(hours=1)
        )
        agentcat.add_gemini_delta_to_state(
            state, token_key="inputTokens", model="gemini-3-flash", amount=3, when=now
        )

        usage = agentcat.gemini_usage_result_from_state(state)
        self.assertEqual(usage["latestModel"]["model"], "gemini-3-flash")

        stale = {
            "tokens": {},
            "models": {},
            "dailyTokens": {},
            "hourlyTokens": {},
            "events": 0,
            "latestModel": {"model": "gemini-older", "when": (now - dt.timedelta(days=1)).isoformat()},
        }
        merged = agentcat.merge_gemini_usage([usage, stale])
        self.assertEqual(merged["latestModel"]["model"], "gemini-3-flash")

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

    def test_claude_snapshot_prefers_jsonl_usage_over_stale_stats_cache(self) -> None:
        claude_dir = agentcat.HOME / ".claude"
        claude_dir.mkdir(parents=True)
        project_dir = agentcat.CLAUDE_PROJECTS_DIR / "test-project"
        project_dir.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        old = now - dt.timedelta(days=40)
        (claude_dir / "stats-cache.json").write_text(
            json.dumps(
                {
                    "dailyModelTokens": [
                        {
                            "date": old.date().isoformat(),
                            "tokensByModel": {"claude-sonnet-4-6": 999},
                        }
                    ],
                    "modelUsage": {
                        "claude-sonnet-4-6": {
                            "inputTokens": 999,
                            "outputTokens": 999,
                            "totalTokens": 1998,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (project_dir / "session.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": now.isoformat().replace("+00:00", "Z"),
                    "cwd": str(agentcat.HOME / "repo"),
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 2,
                            "cache_read_input_tokens": 3,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 4,
                                "ephemeral_5m_input_tokens": 5,
                            },
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        agentcat.JOURNAL_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        agentcat.JOURNAL_CURSOR_FILE.write_text(
            json.dumps({"offsets": {str(project_dir / "session.jsonl"): 999999}, "totals": {}}),
            encoding="utf-8",
        )

        snapshot = agentcat.claude_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["tokens"]["inputTokens"], 1)
        self.assertEqual(snapshot["tokens"]["outputTokens"], 2)
        self.assertEqual(snapshot["tokens"]["cacheReadInputTokens"], 3)
        self.assertEqual(snapshot["tokens"]["cacheCreationInputTokens"], 9)
        self.assertEqual(snapshot["tokens"]["today"], 15)
        self.assertEqual(snapshot["tokens"]["week"], 15)
        self.assertEqual(snapshot["tokens"]["month"], 15)
        self.assertEqual(snapshot["tokens"]["all"], 15)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 15)
        self.assertEqual(snapshot["dailyTokens"][now.astimezone().date().isoformat()], 15)
        self.assertEqual(sum(snapshot["hourlyTokens"].values()), 15)
        model = snapshot["models"]["claude-sonnet-4-6"]
        self.assertEqual(model["inputTokens"], 1)
        self.assertEqual(model["outputTokens"], 2)
        self.assertEqual(model["cacheReadInputTokens"], 3)
        self.assertEqual(model["cacheCreationInputTokens"], 9)
        self.assertEqual(model["today"], 15)
        self.assertEqual(model["week"], 15)
        self.assertEqual(model["all"], 15)
        self.assertEqual(snapshot["projects"]["items"][0]["tokens"], 15)

    def test_claude_snapshot_deduplicates_repeated_request_usage(self) -> None:
        project_dir = agentcat.CLAUDE_PROJECTS_DIR / "test-project"
        project_dir.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        base_event = {
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "cwd": str(agentcat.HOME / "repo"),
            "requestId": "req_1",
            "uuid": "line_1",
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 40,
                        "ephemeral_5m_input_tokens": 50,
                    },
                },
            },
        }
        repeated = dict(base_event)
        repeated["uuid"] = "line_2"
        smaller_repeat = json.loads(json.dumps(base_event))
        smaller_repeat["uuid"] = "line_3"
        smaller_repeat["message"]["usage"]["cache_read_input_tokens"] = 5
        distinct = json.loads(json.dumps(base_event))
        distinct["requestId"] = "req_2"
        distinct["uuid"] = "line_4"
        distinct["message"]["id"] = "msg_2"
        distinct["message"]["usage"] = {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        }
        (project_dir / "session.jsonl").write_text(
            "\n".join(json.dumps(item) for item in [base_event, repeated, smaller_repeat, distinct]) + "\n",
            encoding="utf-8",
        )

        snapshot = agentcat.claude_snapshot()

        self.assertEqual(snapshot["tokens"]["inputTokens"], 11)
        self.assertEqual(snapshot["tokens"]["outputTokens"], 22)
        self.assertEqual(snapshot["tokens"]["cacheReadInputTokens"], 33)
        self.assertEqual(snapshot["tokens"]["cacheCreationInputTokens"], 94)
        self.assertEqual(snapshot["tokens"]["all"], 160)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 160)
        self.assertEqual(snapshot["projects"]["items"][0]["tokens"], 160)
        self.assertEqual(snapshot["models"]["claude-opus-4-7"]["all"], 160)

    def test_claude_snapshot_rebuilds_legacy_cursor_for_dedupe_records(self) -> None:
        project_dir = agentcat.CLAUDE_PROJECTS_DIR / "test-project"
        project_dir.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        event = {
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "cwd": str(agentcat.HOME / "repo"),
            "requestId": "req_legacy",
            "uuid": "line_1",
            "message": {
                "id": "msg_legacy",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 11,
                    "cache_read_input_tokens": 13,
                    "cache_creation_input_tokens": 17,
                },
            },
        }
        duplicate = json.loads(json.dumps(event))
        duplicate["uuid"] = "line_2"
        session_path = project_dir / "session.jsonl"
        session_path.write_text(
            "\n".join(json.dumps(item) for item in [event, duplicate]) + "\n",
            encoding="utf-8",
        )
        agentcat.JOURNAL_CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        agentcat.JOURNAL_CURSOR_FILE.write_text(
            json.dumps(
                {
                    "version": 2,
                    "offsets": {str(session_path): session_path.stat().st_size},
                    "totals": {
                        "all_input": 7000,
                        "all_output": 11000,
                        "all_cacheRead": 13000,
                        "all_cacheWrite_1h": 17000,
                    },
                    "projects": {str(agentcat.HOME / "repo"): {"tokens": 48000}},
                    "dailyTokens": {now.astimezone().date().isoformat(): 48000},
                    "hourlyTokens": {},
                    "models": {"claude-sonnet-4-6": {"all": 48000, "totalTokens": 48000}},
                }
            ),
            encoding="utf-8",
        )

        snapshot = agentcat.claude_snapshot()
        rebuilt_cursor = json.loads(agentcat.JOURNAL_CURSOR_FILE.read_text(encoding="utf-8"))

        self.assertEqual(snapshot["tokens"]["inputTokens"], 7)
        self.assertEqual(snapshot["tokens"]["outputTokens"], 11)
        self.assertEqual(snapshot["tokens"]["cacheReadInputTokens"], 13)
        self.assertEqual(snapshot["tokens"]["cacheCreationInputTokens"], 17)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 48)
        self.assertEqual(snapshot["projects"]["items"][0]["tokens"], 48)
        self.assertEqual(snapshot["models"]["claude-sonnet-4-6"]["all"], 48)
        self.assertEqual(rebuilt_cursor["version"], agentcat.CLAUDE_JOURNAL_CURSOR_VERSION)
        self.assertIn("request:req_legacy", rebuilt_cursor["usageRecords"])

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

    def test_gemini_snapshot_merges_antigravity_cli_telemetry(self) -> None:
        agentcat.GEMINI_TELEMETRY.parent.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)

        def payload(session: str, model: str, token_type: str, value: int) -> dict:
            return {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "descriptor": {"name": "gemini_cli.token.usage"},
                                "dataPoints": [
                                    {
                                        "attributes": {
                                            "model": model,
                                            "session.id": session,
                                            "type": token_type,
                                        },
                                        "startTime": [int(now.timestamp()), 0],
                                        "endTime": "[Circular]",
                                        "value": {"sum": value},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

        agentcat.GEMINI_TELEMETRY.write_text(
            json.dumps(payload("gemini-session", "gemini-test", "input", 100)),
            encoding="utf-8",
        )
        agentcat.ANTIGRAVITY_TELEMETRY.write_text(
            json.dumps(payload("antigravity-session", "gemini-test", "output", 50)),
            encoding="utf-8",
        )

        snapshot = agentcat.gemini_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["canonicalProvider"], "google")
        self.assertEqual(snapshot["events"], 2)
        self.assertEqual(snapshot["tokens"]["inputTokens"], 100)
        self.assertEqual(snapshot["tokens"]["outputTokens"], 50)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 150)
        self.assertEqual(snapshot["models"]["gemini-test"]["all"], 150)
        self.assertEqual(snapshot["cache"]["source"], "multi-source-full-log-index")
        self.assertEqual(snapshot["sources"]["geminiCli"]["tokens"], 100)
        self.assertEqual(snapshot["sources"]["antigravityCli"]["tokens"], 50)
        self.assertTrue(agentcat.GEMINI_USAGE_CACHE.exists())
        self.assertTrue(agentcat.ANTIGRAVITY_USAGE_CACHE.exists())

    def test_gemini_snapshot_distributes_cumulative_counter_deltas_by_day(self) -> None:
        agentcat.GEMINI_TELEMETRY.parent.mkdir(parents=True)
        today = dt.datetime.now(dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        yesterday = today - dt.timedelta(days=1)
        start_time = [int(yesterday.timestamp()), 0]

        def payload(value: int, end: dt.datetime) -> dict:
            return {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "descriptor": {"name": "gemini_cli.token.usage"},
                                "dataPoints": [
                                    {
                                        "attributes": {
                                            "model": "gemini-test",
                                            "session.id": "session-a",
                                            "type": "input",
                                        },
                                        "startTime": start_time,
                                        "endTime": [int(end.timestamp()), 0],
                                        "value": {"sum": value},
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

        agentcat.GEMINI_TELEMETRY.write_text(
            json.dumps(payload(100, yesterday), indent=2)
            + "\n"
            + json.dumps(payload(150, today), indent=2),
            encoding="utf-8",
        )

        snapshot = agentcat.gemini_snapshot()
        today_key = agentcat.day_key_for_timestamp(today)
        yesterday_key = agentcat.day_key_for_timestamp(yesterday)

        self.assertEqual(snapshot["tokens"]["inputTokens"], 150)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 150)
        self.assertEqual(snapshot["dailyTokens"][yesterday_key], 100)
        self.assertEqual(snapshot["dailyTokens"][today_key], 50)
        self.assertEqual(snapshot["models"]["gemini-test"]["all"], 150)
        self.assertEqual(snapshot["events"], 2)

    def test_gemini_snapshot_indexes_full_log_across_stream_chunks(self) -> None:
        agentcat.GEMINI_TELEMETRY.parent.mkdir(parents=True)
        today = dt.datetime.now(dt.timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        old_day = today - dt.timedelta(days=9)

        def payload(session: str, value: int, when: dt.datetime) -> dict:
            return {
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "descriptor": {"name": "gemini_cli.token.usage"},
                                "dataPoints": [
                                    {
                                        "attributes": {
                                            "model": "gemini-test",
                                            "session.id": session,
                                            "type": "input",
                                        },
                                        "startTime": [int(when.timestamp()), 0],
                                        "endTime": "[Circular]",
                                        "value": value,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

        agentcat.GEMINI_TELEMETRY.write_text(
            json.dumps(payload("old", 90, old_day), indent=2)
            + "\n"
            + (" " * 512)
            + "\n"
            + json.dumps(payload("today", 10, today), indent=2)
            + "\n",
            encoding="utf-8",
        )

        with patch.object(agentcat, "GEMINI_TELEMETRY_STREAM_CHUNK_BYTES", 128):
            snapshot = agentcat.gemini_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["tokens"]["all"], 100)
        self.assertEqual(snapshot["tokens"]["today"], 10)
        self.assertEqual(snapshot["dailyTokens"][agentcat.day_key_for_timestamp(old_day)], 90)
        self.assertEqual(snapshot["dailyTokens"][agentcat.day_key_for_timestamp(today)], 10)
        self.assertEqual(snapshot["cache"]["source"], "full-log-index")
        self.assertTrue(agentcat.GEMINI_USAGE_CACHE.exists())
        cached = json.loads(agentcat.GEMINI_USAGE_CACHE.read_text(encoding="utf-8"))
        self.assertEqual(cached["offset"], agentcat.GEMINI_TELEMETRY.stat().st_size)

    def test_gemini_usage_cache_clamps_overlarge_offset_for_same_file_size(self) -> None:
        agentcat.GEMINI_TELEMETRY.parent.mkdir(parents=True)
        agentcat.GEMINI_TELEMETRY.write_text("{}\n", encoding="utf-8")
        stat = agentcat.GEMINI_TELEMETRY.stat()
        agentcat.GEMINI_USAGE_CACHE.write_text(
            json.dumps(
                {
                    "version": agentcat.GEMINI_USAGE_CACHE_VERSION,
                    "source": str(agentcat.GEMINI_TELEMETRY),
                    "offset": stat.st_size + 100,
                    "size": stat.st_size,
                    "tokenClasses": {"inputTokens": 1},
                }
            ),
            encoding="utf-8",
        )

        state = agentcat.load_gemini_usage_state(stat)

        self.assertEqual(state["offset"], stat.st_size)
        self.assertEqual(state["tokenClasses"]["inputTokens"], 1)

    def test_opencode_snapshot_reads_sqlite_message_tokens(self) -> None:
        data_home = agentcat.HOME / ".local" / "share"
        opencode_dir = data_home / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        with closing(sqlite3.connect(db_path)) as conn:
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
            agentcat.copilot_workspace_storage_dirs()[0]
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

    def test_goose_snapshot_reads_sqlite_session_tokens(self) -> None:
        # Point the goose data dir under the patched HOME regardless of any
        # ambient XDG_DATA_HOME so the fixture path matches goose_db_path().
        data_home = agentcat.HOME / ".local" / "share"
        with patch.dict(agentcat.os.environ, {"XDG_DATA_HOME": str(data_home)}):
            self._run_goose_snapshot_fixture()

    def _run_goose_snapshot_fixture(self) -> None:
        db_path = agentcat.goose_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                create table sessions (
                  id text primary key,
                  created_at text,
                  updated_at text,
                  accumulated_input_tokens integer,
                  accumulated_output_tokens integer,
                  model_config_json text
                )
                """
            )
            conn.execute(
                "insert into sessions values (?, ?, ?, ?, ?, ?)",
                (
                    "session-goose-1",
                    now,
                    now,
                    120,
                    40,
                    json.dumps({"model_name": "goose-claude-sonnet-4-6"}),
                ),
            )
            # A zero-token row must be ignored by the WHERE filter.
            conn.execute(
                "insert into sessions values (?, ?, ?, ?, ?, ?)",
                ("session-goose-2", now, now, 0, 0, json.dumps({"model_name": "goose-x"})),
            )
            conn.commit()

        snapshot = agentcat.goose_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 160)
        self.assertEqual(snapshot["tokens"]["week"], 160)
        self.assertEqual(
            snapshot["models"]["goose-claude-sonnet-4-6"]["inputTokens"], 120
        )
        self.assertEqual(
            snapshot["models"]["goose-claude-sonnet-4-6"]["outputTokens"], 40
        )

    def test_cursor_snapshot_reads_bubble_token_counts(self) -> None:
        db_path = agentcat.cursor_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("create table cursorDiskKV (key text primary key, value text)")
            # Bubble with explicit token counts.
            conn.execute(
                "insert into cursorDiskKV(key, value) values (?, ?)",
                (
                    "bubbleId:abc",
                    json.dumps(
                        {
                            "tokenCount": {"inputTokens": 80, "outputTokens": 20},
                            "modelInfo": {"modelName": "cursor-claude-sonnet-4-6"},
                            "createdAt": now_ms,
                            "text": "hello",
                            "type": 2,
                        }
                    ),
                ),
            )
            # Bubble with no token counts -> char estimate fallback (assistant bubble).
            conn.execute(
                "insert into cursorDiskKV(key, value) values (?, ?)",
                (
                    "bubbleId:def",
                    json.dumps(
                        {
                            "tokenCount": {"inputTokens": 0, "outputTokens": 0},
                            "modelInfo": {"modelName": "cursor-claude-sonnet-4-6"},
                            "createdAt": now_ms,
                            "text": "abcdefgh",
                            "type": 2,
                        }
                    ),
                ),
            )
            # Non-bubble key must be ignored.
            conn.execute(
                "insert into cursorDiskKV(key, value) values (?, ?)",
                ("composerData:zzz", json.dumps({"createdAt": now_ms})),
            )
            conn.commit()

        snapshot = agentcat.cursor_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 2)
        model = snapshot["models"]["cursor-claude-sonnet-4-6"]
        # 80 input + 20 output from the explicit bubble, plus 2 estimated output
        # tokens from the 8-char fallback bubble (ceil((8+3)/4) == 2).
        self.assertEqual(model["inputTokens"], 80)
        self.assertEqual(model["outputTokens"], 22)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 102)
        self.assertEqual(snapshot["tokens"]["week"], 102)

    def test_cursor_snapshot_sets_estimated_flag_on_char_fallback(self) -> None:
        # A bubble with no real tokenCount falls back to a char estimate, which
        # must flag the snapshot as approximate via estimated == True.
        db_path = agentcat.cursor_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("create table cursorDiskKV (key text primary key, value text)")
            conn.execute(
                "insert into cursorDiskKV(key, value) values (?, ?)",
                (
                    "bubbleId:est",
                    json.dumps(
                        {
                            "tokenCount": {"inputTokens": 0, "outputTokens": 0},
                            "modelInfo": {"modelName": "cursor-claude-sonnet-4-6"},
                            "createdAt": now_ms,
                            "text": "abcdefgh",
                            "type": 2,
                        }
                    ),
                ),
            )
            conn.commit()

        snapshot = agentcat.cursor_snapshot()
        self.assertEqual(snapshot["status"], "ok")
        self.assertTrue(snapshot.get("estimated"))

    def test_cursor_snapshot_omits_estimated_when_all_real(self) -> None:
        # Bubbles with explicit real token counts must NOT set estimated.
        db_path = agentcat.cursor_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("create table cursorDiskKV (key text primary key, value text)")
            conn.execute(
                "insert into cursorDiskKV(key, value) values (?, ?)",
                (
                    "bubbleId:real",
                    json.dumps(
                        {
                            "tokenCount": {"inputTokens": 80, "outputTokens": 20},
                            "modelInfo": {"modelName": "cursor-claude-sonnet-4-6"},
                            "createdAt": now_ms,
                            "text": "hello",
                            "type": 2,
                        }
                    ),
                ),
            )
            conn.commit()

        snapshot = agentcat.cursor_snapshot()
        self.assertEqual(snapshot["status"], "ok")
        self.assertNotIn("estimated", snapshot)

    def _write_kiro_chat(self) -> None:
        agent_dir = agentcat.kiro_agent_dir()
        workspace_dir = agent_dir / ("a" * 32)
        workspace_dir.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        (workspace_dir / "session.chat").write_text(
            json.dumps(
                {
                    "metadata": {"modelId": "kiro-claude-sonnet-4.6", "startTime": now},
                    "chat": [
                        {"role": "human", "content": "please summarize this"},
                        {"role": "bot", "content": "Here is the summary of the requested text."},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_kiro_snapshot_sets_estimated_flag(self) -> None:
        # kiro never persists real token counts — its snapshot is fully
        # char-estimated and must therefore carry estimated == True.
        self._write_kiro_chat()
        snapshot = agentcat.kiro_snapshot()
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertTrue(snapshot.get("estimated"))

    def test_goose_snapshot_does_not_set_estimated_flag(self) -> None:
        # goose reports REAL accumulated token counts, so estimated must be
        # absent — the app should treat its numbers as exact.
        data_home = agentcat.HOME / ".local" / "share"
        with patch.dict(agentcat.os.environ, {"XDG_DATA_HOME": str(data_home)}):
            self._run_goose_snapshot_fixture()
            snapshot = agentcat.goose_snapshot()
        self.assertEqual(snapshot["status"], "ok")
        self.assertNotIn("estimated", snapshot)

    def test_is_local_request_rejects_origin_referer_and_remote_host(self) -> None:
        from email.message import Message

        def headers(pairs):
            msg = Message()
            for key, value in pairs.items():
                msg[key] = value
            return msg

        # CLI/loopback request with no browser headers is allowed.
        self.assertTrue(agentcat.is_local_request(headers({"Host": "127.0.0.1:8765"})))
        self.assertTrue(agentcat.is_local_request(headers({"Host": "localhost"})))
        self.assertTrue(agentcat.is_local_request(headers({})))
        # An Origin header (browser-only) is rejected.
        self.assertFalse(
            agentcat.is_local_request(headers({"Host": "127.0.0.1:8765", "Origin": "http://evil.example"}))
        )
        # A Referer header is rejected.
        self.assertFalse(
            agentcat.is_local_request(headers({"Host": "127.0.0.1", "Referer": "http://evil.example/x"}))
        )
        # A non-loopback Host (DNS-rebind) is rejected.
        self.assertFalse(agentcat.is_local_request(headers({"Host": "attacker.example"})))
        # IPv6 loopback with a port is allowed.
        self.assertTrue(agentcat.is_local_request(headers({"Host": "[::1]:8765"})))

    def test_write_provider_config_drops_unknown_limit_keys_and_coerces(self) -> None:
        written = agentcat.write_provider_config(
            {
                "providers": {
                    "cursor": {
                        "enabled": True,
                        "limits": {
                            "weeklyTokens": "50,000",   # coerced from string
                            "monthlyTokens": 200000,
                            "sessionTokens": 0,          # non-positive -> dropped
                            "evilKey": {"nested": "junk"},  # unknown -> dropped
                            "rm": "rf",                  # unknown -> dropped
                        },
                    }
                }
            }
        )
        limits = written["providers"]["cursor"]["limits"]
        self.assertEqual(limits["weeklyTokens"], 50000)
        self.assertEqual(limits["monthlyTokens"], 200000)
        # Non-positive and unknown keys must not survive.
        self.assertNotIn("sessionTokens", limits)
        self.assertNotIn("evilKey", limits)
        self.assertNotIn("rm", limits)
        # The persisted file matches the returned, sanitized config.
        persisted = json.loads(agentcat.PROVIDER_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(persisted, written)

    def test_cline_family_skips_oversized_files_without_error(self) -> None:
        # A ui_messages.json larger than the parse cap must be SKIPPED (no
        # tokens, no crash) rather than slurped into memory.
        base_dir = agentcat.vscode_global_storage_dirs(
            ("Code", "Code - Insiders"), "rooveterinaryinc.roo-cline"
        )[0]
        task_dir = base_dir / "tasks" / "task-big"
        task_dir.mkdir(parents=True)
        ts_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        (task_dir / "api_conversation_history.json").write_text("[]", encoding="utf-8")
        big_entry = json.dumps(
            {
                "type": "say",
                "say": "api_req_started",
                "ts": ts_ms,
                "text": json.dumps({"tokensIn": 100, "tokensOut": 30}),
            }
        )
        (task_dir / "ui_messages.json").write_text(
            "[" + big_entry + "]", encoding="utf-8"
        )
        # Force the cap below the file size so the bounded reader skips it.
        small_cap = (task_dir / "ui_messages.json").stat().st_size - 1
        with patch.object(agentcat, "LOCAL_PROVIDER_PARSE_BYTES", small_cap):
            snapshot = agentcat.roo_code_snapshot()
        # No tokens parsed, but the snapshot is well-formed and not an error.
        self.assertNotEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["tokens"]["all"], 0)

    def test_roo_code_snapshot_reads_cline_family_task_tokens(self) -> None:
        base_dir = agentcat.vscode_global_storage_dirs(
            ("Code", "Code - Insiders"), "rooveterinaryinc.roo-cline"
        )[0]
        task_dir = base_dir / "tasks" / "task-roo-1"
        task_dir.mkdir(parents=True)
        ts_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        (task_dir / "api_conversation_history.json").write_text(
            json.dumps(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "context <model>anthropic/claude-sonnet-4-6</model> more"}
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        (task_dir / "ui_messages.json").write_text(
            json.dumps(
                [
                    {
                        "type": "say",
                        "say": "api_req_started",
                        "ts": ts_ms,
                        "text": json.dumps(
                            {
                                "tokensIn": 100,
                                "tokensOut": 30,
                                "cacheReads": 10,
                                "cacheWrites": 5,
                            }
                        ),
                    }
                ]
            ),
            encoding="utf-8",
        )

        snapshot = agentcat.roo_code_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 145)
        self.assertEqual(snapshot["tokens"]["week"], 145)
        model = snapshot["models"]["claude-sonnet-4-6"]
        self.assertEqual(model["inputTokens"], 100)
        self.assertEqual(model["outputTokens"], 30)
        self.assertEqual(model["cacheReadInputTokens"], 10)
        self.assertEqual(model["cacheCreationInputTokens"], 5)

    def test_cline_family_snapshot_reports_no_events_when_idle(self) -> None:
        # A task dir that exists but has no ui_messages.json must NOT fabricate
        # tokens — presence-only maps to no_token_events_yet (or not_found when
        # nothing on disk).
        snapshot = agentcat.kilo_code_snapshot()
        self.assertIn(snapshot["status"], ("not_found", "no_token_events_yet"))
        self.assertEqual(snapshot["tokens"]["all"], 0)

    def test_qwen_snapshot_reads_jsonl_usage_metadata(self) -> None:
        # qwen reads ~/.qwen/projects/*/chats/*.jsonl assistant usageMetadata.
        # Clear any ambient QWEN_DATA_DIR so the fixture resolves under HOME.
        with patch.dict(agentcat.os.environ, {}, clear=False):
            agentcat.os.environ.pop("QWEN_DATA_DIR", None)
            chats_dir = agentcat.HOME / ".qwen" / "projects" / "proj-qwen-1" / "chats"
            chats_dir.mkdir(parents=True)
            now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            lines = [
                # Assistant turn with explicit usageMetadata -> counted.
                json.dumps(
                    {
                        "type": "assistant",
                        "model": "qwen-coder-plus",
                        "timestamp": now,
                        "sessionId": "sess-1",
                        "usageMetadata": {
                            "promptTokenCount": 200,
                            "candidatesTokenCount": 50,
                            "thoughtsTokenCount": 10,
                            "cachedContentTokenCount": 30,
                        },
                    }
                ),
                # User turn must be ignored.
                json.dumps({"type": "user", "timestamp": now, "text": "hello"}),
                # Assistant turn without usageMetadata must not fabricate tokens.
                json.dumps({"type": "assistant", "model": "qwen-coder-plus", "timestamp": now}),
            ]
            (chats_dir / "chat-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

            snapshot = agentcat.qwen_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 290)
        self.assertEqual(snapshot["tokens"]["week"], 290)
        model = snapshot["models"]["qwen-coder-plus"]
        self.assertEqual(model["inputTokens"], 200)
        self.assertEqual(model["outputTokens"], 50)
        self.assertEqual(model["reasoningTokens"], 10)
        self.assertEqual(model["cacheReadInputTokens"], 30)

    def test_crush_snapshot_reads_registry_and_session_store(self) -> None:
        # crush reads a global projects.json registry that points at per-project
        # crush.db sqlite stores. Pin the registry under HOME via CRUSH_GLOBAL_DATA.
        global_data = agentcat.HOME / ".local" / "share" / "crush"
        global_data.mkdir(parents=True)
        project_dir = agentcat.HOME / "work" / "crush-proj"
        data_dir = project_dir / ".crush"
        data_dir.mkdir(parents=True)
        registry = global_data / "projects.json"
        registry.write_text(
            json.dumps(
                {
                    "p1": {"path": str(project_dir), "data_dir": ".crush"},
                }
            ),
            encoding="utf-8",
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        db_path = data_dir / "crush.db"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                create table sessions (
                  id text primary key,
                  parent_session_id text,
                  prompt_tokens integer,
                  completion_tokens integer,
                  created_at text,
                  updated_at text
                )
                """
            )
            conn.execute(
                "create table messages (id text primary key, session_id text, model text)"
            )
            conn.execute(
                "insert into sessions values (?, ?, ?, ?, ?, ?)",
                ("s1", None, 150, 60, now, now),
            )
            # A child session must be ignored (parent_session_id not null).
            conn.execute(
                "insert into sessions values (?, ?, ?, ?, ?, ?)",
                ("s2", "s1", 999, 999, now, now),
            )
            # A zero-token session must be ignored by the WHERE filter.
            conn.execute(
                "insert into sessions values (?, ?, ?, ?, ?, ?)",
                ("s3", None, 0, 0, now, now),
            )
            conn.execute(
                "insert into messages values (?, ?, ?)",
                ("m1", "s1", "crush-claude-sonnet-4-6"),
            )
            conn.commit()

        with patch.dict(agentcat.os.environ, {"CRUSH_GLOBAL_DATA": str(global_data)}):
            snapshot = agentcat.crush_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 210)
        self.assertEqual(snapshot["tokens"]["week"], 210)
        model = snapshot["models"]["crush-claude-sonnet-4-6"]
        self.assertEqual(model["inputTokens"], 150)
        self.assertEqual(model["outputTokens"], 60)

    def test_cline_snapshot_returns_expected_shape_when_idle(self) -> None:
        # cline reuses the cline-family parser (saoudrizwan.claude-dev). With no
        # globalStorage tasks on disk it must report not_found with zero tokens,
        # never fabricated presence tokens.
        snapshot = agentcat.cline_snapshot()
        self.assertIn(snapshot["status"], ("not_found", "no_token_events_yet"))
        self.assertEqual(snapshot["tokens"]["all"], 0)

    def test_continue_snapshot_reads_dev_data_tokens_generated(self) -> None:
        # continue.dev writes per-generation events to
        # ~/.continue/dev_data/<schema>/tokensGenerated.jsonl (and a flat
        # dev_data/tokensGenerated.jsonl on older schemas). Both must be read.
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        dev_data = agentcat.HOME / ".continue" / "dev_data"
        versioned = dev_data / "0.2.0"
        versioned.mkdir(parents=True)
        (versioned / "tokensGenerated.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "model": "continue-claude-sonnet-4-6",
                            "promptTokens": 300,
                            "generatedTokens": 70,
                            "timestamp": now,
                        }
                    ),
                    # Event with neither token field > 0 must be skipped.
                    json.dumps(
                        {
                            "model": "continue-claude-sonnet-4-6",
                            "promptTokens": 0,
                            "generatedTokens": 0,
                            "timestamp": now,
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        # Flat (legacy) layout in the same dev_data dir.
        (dev_data / "tokensGenerated.jsonl").write_text(
            json.dumps(
                {
                    "model": "continue-claude-sonnet-4-6",
                    "promptTokens": 5,
                    "generatedTokens": 25,
                    "timestamp": now,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        snapshot = agentcat.continue_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 2)
        # 300 + 70 (versioned) + 5 + 25 (flat) == 400.
        self.assertEqual(snapshot["tokens"]["totalTokens"], 400)
        self.assertEqual(snapshot["tokens"]["week"], 400)
        model = snapshot["models"]["continue-claude-sonnet-4-6"]
        self.assertEqual(model["inputTokens"], 305)
        self.assertEqual(model["outputTokens"], 95)

    def test_continue_snapshot_reports_no_events_when_dir_empty(self) -> None:
        # A dev_data dir that exists but has no token events -> presence only.
        (agentcat.HOME / ".continue" / "dev_data").mkdir(parents=True)
        snapshot = agentcat.continue_snapshot()
        self.assertIn(snapshot["status"], ("not_found", "no_token_events_yet"))
        self.assertEqual(snapshot["tokens"]["all"], 0)

    def test_pearai_snapshot_reuses_continue_parser(self) -> None:
        # PearAI is a Continue fork: same dev_data schema under ~/.pearai.
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        versioned = agentcat.HOME / ".pearai" / "dev_data" / "0.1.0"
        versioned.mkdir(parents=True)
        (versioned / "tokensGenerated.jsonl").write_text(
            json.dumps(
                {
                    "model": "pearai-gpt-4o",
                    "promptTokens": 180,
                    "generatedTokens": 45,
                    "timestamp": now,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        snapshot = agentcat.pearai_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 225)
        self.assertEqual(snapshot["tokens"]["week"], 225)
        model = snapshot["models"]["pearai-gpt-4o"]
        self.assertEqual(model["inputTokens"], 180)
        self.assertEqual(model["outputTokens"], 45)

    def test_llm_snapshot_reads_responses_table(self) -> None:
        # simonw's `llm` logs to a sqlite `responses` table. Pin the db under
        # HOME via LLM_USER_PATH (which `llm` itself honors).
        user_path = agentcat.HOME / ".llm"
        user_path.mkdir(parents=True)
        db_path = user_path / "logs.db"
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                """
                create table responses (
                  id text primary key,
                  model text,
                  input_tokens integer,
                  output_tokens integer,
                  datetime_utc text,
                  conversation_id text
                )
                """
            )
            conn.execute(
                "insert into responses(id, model, input_tokens, output_tokens, datetime_utc, conversation_id) values (?, ?, ?, ?, ?, ?)",
                ("r1", "llm-gpt-4o-mini", 220, 80, now, "c1"),
            )
            # Zero-token row must be excluded by the WHERE filter.
            conn.execute(
                "insert into responses(id, model, input_tokens, output_tokens, datetime_utc, conversation_id) values (?, ?, ?, ?, ?, ?)",
                ("r2", "llm-should-skip", 0, 0, now, "c2"),
            )
            conn.commit()

        with patch.dict(agentcat.os.environ, {"LLM_USER_PATH": str(user_path)}):
            snapshot = agentcat.llm_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 300)
        self.assertEqual(snapshot["tokens"]["week"], 300)
        model = snapshot["models"]["llm-gpt-4o-mini"]
        self.assertEqual(model["inputTokens"], 220)
        self.assertEqual(model["outputTokens"], 80)

    def test_gptme_snapshot_reads_assistant_usage(self) -> None:
        # gptme writes ~/.local/share/gptme/logs/<conv>/conversation.jsonl.
        # Token usage may live under metadata.usage; only assistant messages
        # with token fields are counted.
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        conv_dir = agentcat.HOME / ".local" / "share" / "gptme" / "logs" / "2026-01-01-conv"
        conv_dir.mkdir(parents=True)
        lines = [
            # Counted: assistant with metadata.usage (prompt/completion).
            json.dumps(
                {
                    "role": "assistant",
                    "content": "hi",
                    "timestamp": now,
                    "metadata": {
                        "model": "gptme-claude-sonnet-4-6",
                        "usage": {"prompt_tokens": 240, "completion_tokens": 60},
                    },
                }
            ),
            # IGNORED: user turn.
            json.dumps({"role": "user", "content": "hello", "timestamp": now}),
            # IGNORED: assistant turn with no token fields anywhere.
            json.dumps(
                {
                    "role": "assistant",
                    "content": "no usage here",
                    "timestamp": now,
                    "metadata": {"model": "gptme-claude-sonnet-4-6"},
                }
            ),
        ]
        (conv_dir / "conversation.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        snapshot = agentcat.gptme_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["events"], 1)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 300)
        self.assertEqual(snapshot["tokens"]["week"], 300)
        model = snapshot["models"]["gptme-claude-sonnet-4-6"]
        self.assertEqual(model["inputTokens"], 240)
        self.assertEqual(model["outputTokens"], 60)

    def test_gptme_snapshot_reports_no_events_without_token_fields(self) -> None:
        # When no assistant message carries token fields, gptme must report
        # presence (no_token_events_yet) and NOT fabricate/char-estimate.
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        conv_dir = agentcat.HOME / ".local" / "share" / "gptme" / "logs" / "2026-02-02-conv"
        conv_dir.mkdir(parents=True)
        lines = [
            json.dumps({"role": "user", "content": "hello", "timestamp": now}),
            json.dumps(
                {
                    "role": "assistant",
                    "content": "a reply with no usage metadata at all",
                    "timestamp": now,
                    "metadata": {"model": "gptme-claude-sonnet-4-6"},
                }
            ),
        ]
        (conv_dir / "conversation.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        snapshot = agentcat.gptme_snapshot()

        self.assertEqual(snapshot["status"], "no_token_events_yet")
        self.assertEqual(snapshot["events"], 0)
        self.assertEqual(snapshot["tokens"]["all"], 0)

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

    def test_classify_antigravity_cli_processes_as_gemini(self) -> None:
        self.assertEqual(
            agentcat.classify_process("agy --print hello"),
            "gemini",
        )
        self.assertEqual(
            agentcat.classify_process("/Users/me/.local/bin/agy --prompt hello"),
            "gemini",
        )
        self.assertEqual(
            agentcat.classify_process(r'"C:\Users\me\.local\bin\agy.exe" --print hello'),
            "gemini",
        )
        self.assertEqual(
            agentcat.classify_process("/Applications/Antigravity.app/Contents/MacOS/antigravity"),
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

        with patch.object(agentcat, "IS_WINDOWS", False), patch.object(
            agentcat.subprocess, "run", return_value=completed
        ):
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

    def test_windows_activity_prefers_pwsh_and_configured_timeout(self) -> None:
        (agentcat.AGENTCAT_HOME / "settings.json").write_text(
            json.dumps({"windowsProcessScanTimeoutSeconds": 8}),
            encoding="utf-8",
        )
        completed = agentcat.subprocess.CompletedProcess(
            args=["pwsh.exe"],
            returncode=0,
            stdout=json.dumps(
                {
                    "ProcessId": 301,
                    "ParentProcessId": 0,
                    "Name": "claude",
                    "Path": "C:\\Users\\me\\AppData\\Local\\Programs\\Claude\\claude.exe",
                    "CpuPercent": 0,
                    "WorkingSetSize": 12_345_678,
                }
            ),
            stderr="",
        )
        run_calls = []

        def which(name):
            return "C:\\Program Files\\PowerShell\\7\\pwsh.exe" if name == "pwsh.exe" else None

        def run(*args, **kwargs):
            run_calls.append((args, kwargs))
            return completed

        with patch.object(agentcat.shutil, "which", side_effect=which), patch.object(
            agentcat.subprocess, "run", side_effect=run
        ):
            snapshot = agentcat.terminal_activity_snapshot_windows()

        command = run_calls[0][0][0]
        kwargs = run_calls[0][1]
        self.assertEqual(command[0], "C:\\Program Files\\PowerShell\\7\\pwsh.exe")
        self.assertIn("Get-Process", command[-1])
        self.assertLess(command[-1].index("Get-Process"), command[-1].index("Get-CimInstance Win32_Process"))
        self.assertEqual(kwargs["timeout"], 8.0)
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["scanSource"], "powershell-get-process")
        self.assertEqual(snapshot["countsByProvider"]["claude"], 1)
        self.assertEqual(snapshot["totalMemoryBytes"], 12_345_678)
        self.assertEqual(snapshot["memoryBytesByProvider"]["claude"], 12_345_678)
        self.assertEqual(snapshot["processes"][0]["command"], "claude pid 301")
        self.assertNotIn("Claude", snapshot["processes"][0]["command"])

    def test_windows_activity_keeps_commandline_wrapper_detection(self) -> None:
        completed = agentcat.subprocess.CompletedProcess(
            args=["pwsh.exe"],
            returncode=0,
            stdout=json.dumps(
                {
                    "ProcessId": 302,
                    "ParentProcessId": 1,
                    "Name": "node.exe",
                    "CommandLine": "node C:\\Users\\me\\AppData\\Roaming\\npm\\node_modules\\@google\\gemini-cli\\index.js",
                    "CpuPercent": 0,
                    "WorkingSetSize": 10_000_000,
                }
            ),
            stderr="",
        )

        with patch.object(agentcat.shutil, "which", return_value="pwsh.exe"), patch.object(
            agentcat.subprocess, "run", return_value=completed
        ):
            snapshot = agentcat.terminal_activity_snapshot_windows()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["countsByProvider"]["gemini"], 1)
        self.assertEqual(snapshot["memoryBytesByProvider"]["gemini"], 10_000_000)

    def test_windows_activity_falls_back_to_tasklist_when_powershell_times_out(self) -> None:
        tasklist = agentcat.subprocess.CompletedProcess(
            args=["tasklist.exe"],
            returncode=0,
            stdout=(
                '"codex.exe","101","Console","1","12,345 K"\n'
                '"claude.exe","102","Console","1","9,000 K"\n'
                '"gemini.exe","103","Console","1","8,000 K"\n'
                '"agy.exe","104","Console","1","7,000 K"\n'
            ),
            stderr="",
        )
        run_calls = []

        def run(args, **kwargs):
            run_calls.append((args, kwargs))
            if args[0] == "powershell.exe":
                raise agentcat.subprocess.TimeoutExpired(args, kwargs["timeout"])
            return tasklist

        with patch.object(agentcat.shutil, "which", return_value=None), patch.object(
            agentcat.subprocess, "run", side_effect=run
        ):
            snapshot = agentcat.terminal_activity_snapshot_windows()

        self.assertEqual(run_calls[0][0][0], "powershell.exe")
        self.assertEqual(run_calls[1][0][0], "tasklist.exe")
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["scanSource"], "tasklist")
        self.assertIn("PowerShell scan failed", snapshot["scanWarning"])
        self.assertEqual(snapshot["processCount"], 4)
        self.assertEqual(snapshot["countsByProvider"], {"codex": 1, "claude": 1, "gemini": 2})
        self.assertEqual(snapshot["totalMemoryBytes"], 37_217_280)

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

    def test_claude_hook_records_ultrathink_mode_without_prompt_text(self) -> None:
        payload = {
            "session_id": "session-1",
            "prompt": "please ultrathink about this private code",
            "cwd": "/private/project",
        }

        with patch.object(agentcat.sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(agentcat.command_claude_hook(agentcat.argparse.Namespace(event="UserPromptSubmit")), 0)

        with closing(sqlite3.connect(agentcat.EVENTS_DB)) as conn:
            stored_json = conn.execute("select payload_json from events").fetchone()[0]
        stored = json.loads(stored_json)

        self.assertEqual(stored["prompt"], "[redacted]")
        self.assertEqual(stored["cwd"], "[redacted]")
        self.assertEqual(stored["agentcatRuntimeMode"]["mode"], "ultrathink")
        self.assertEqual(stored["agentcatRuntimeMode"]["confidence"], "exact")
        self.assertEqual(stored["agentcatRuntimeMode"]["privacy"], "prompt_text_discarded")
        self.assertNotIn("private code", stored_json)

        modes = agentcat.runtime_modes_snapshot()
        self.assertEqual(len(modes), 1)
        self.assertEqual(modes[0]["provider"], "claude")
        self.assertEqual(modes[0]["mode"], "ultrathink")
        self.assertEqual(modes[0]["confidence"], "exact")

    def test_claude_hook_records_ultracode_mode_without_prompt_text(self) -> None:
        payload = {
            "session_id": "session-1",
            "prompt": "please run ultra-code on this private code",
            "cwd": "/private/project",
        }

        with patch.object(agentcat.sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(agentcat.command_claude_hook(agentcat.argparse.Namespace(event="UserPromptSubmit")), 0)

        with closing(sqlite3.connect(agentcat.EVENTS_DB)) as conn:
            stored_json = conn.execute("select payload_json from events").fetchone()[0]
        stored = json.loads(stored_json)

        self.assertEqual(stored["prompt"], "[redacted]")
        self.assertEqual(stored["cwd"], "[redacted]")
        self.assertEqual(stored["agentcatRuntimeMode"]["mode"], "ultracode")
        self.assertEqual(stored["agentcatRuntimeMode"]["privacy"], "prompt_text_discarded")
        self.assertNotIn("private code", stored_json)

    def test_claude_hook_records_nested_effort_level(self) -> None:
        payload = {
            "session_id": "session-1",
            "effort": {"level": "xhigh"},
        }

        with patch.object(agentcat.sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(agentcat.command_claude_hook(agentcat.argparse.Namespace(event="SessionStart")), 0)

        modes = agentcat.runtime_modes_snapshot()
        self.assertEqual(modes[0]["provider"], "claude")
        self.assertEqual(modes[0]["mode"], "effort_xhigh")
        self.assertEqual(modes[0]["privacy"], "metadata_only")

    def test_codex_notify_records_xhigh_runtime_mode(self) -> None:
        payload = {
            "model": "gpt-5.5",
            "model_reasoning_effort": "xhigh",
        }

        with patch.object(agentcat.sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(agentcat.command_codex_notify(agentcat.argparse.Namespace()), 0)

        modes = agentcat.runtime_modes_snapshot()
        self.assertEqual(modes[0]["provider"], "codex")
        self.assertEqual(modes[0]["mode"], "effort_xhigh")
        self.assertEqual(modes[0]["source"], "codex-notify")
        self.assertEqual(modes[0]["privacy"], "metadata_only")

    def test_codex_config_runtime_mode_requires_active_codex_process(self) -> None:
        codex_dir = agentcat.HOME / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text('model_reasoning_effort = "xhigh"\n', encoding="utf-8")

        self.assertEqual(agentcat.runtime_modes_snapshot(active_providers=[]), [])

        modes = agentcat.runtime_modes_snapshot(active_providers=["codex"])
        self.assertEqual(modes[0]["provider"], "codex")
        self.assertEqual(modes[0]["mode"], "effort_xhigh")
        self.assertEqual(modes[0]["confidence"], "config_default")
        self.assertEqual(modes[0]["source"], "codex-config")

    def test_claude_stop_hook_clears_runtime_mode(self) -> None:
        with patch.object(agentcat.sys, "stdin", io.StringIO(json.dumps({"prompt": "ultrathink"}))):
            agentcat.command_claude_hook(agentcat.argparse.Namespace(event="UserPromptSubmit"))
        self.assertEqual(agentcat.runtime_modes_snapshot()[0]["mode"], "ultrathink")

        with patch.object(agentcat.sys, "stdin", io.StringIO("{}")):
            agentcat.command_claude_hook(agentcat.argparse.Namespace(event="Stop"))

        self.assertEqual(agentcat.runtime_modes_snapshot(), [])

    def test_terminal_activity_snapshot_includes_runtime_modes(self) -> None:
        with patch.object(agentcat.sys, "stdin", io.StringIO(json.dumps({"prompt": "ultrathink"}))):
            agentcat.command_claude_hook(agentcat.argparse.Namespace(event="UserPromptSubmit"))

        completed = agentcat.subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="102 1 1.0 262144 S /opt/homebrew/bin/claude\n",
            stderr="",
        )

        with patch.object(agentcat, "IS_WINDOWS", False), patch.object(
            agentcat.subprocess, "run", return_value=completed
        ):
            snapshot = agentcat.terminal_activity_snapshot()

        self.assertEqual(snapshot["runtimeModes"][0]["mode"], "ultrathink")
        self.assertEqual(snapshot["runtimeModes"][0]["privacy"], "prompt_text_discarded")

    def test_terminal_activity_snapshot_includes_codex_config_runtime_mode_when_active(self) -> None:
        codex_dir = agentcat.HOME / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text('model_reasoning_effort = "xhigh"\n', encoding="utf-8")
        completed = agentcat.subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="102 1 1.0 262144 R /opt/homebrew/bin/codex\n",
            stderr="",
        )

        with patch.object(agentcat, "IS_WINDOWS", False), patch.object(
            agentcat.subprocess, "run", return_value=completed
        ):
            snapshot = agentcat.terminal_activity_snapshot()

        self.assertEqual(snapshot["runtimeModes"][0]["provider"], "codex")
        self.assertEqual(snapshot["runtimeModes"][0]["mode"], "effort_xhigh")
        self.assertEqual(snapshot["runtimeModes"][0]["confidence"], "config_default")

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

    def test_includes_integer_period_model_buckets(self) -> None:
        s = self._snapshot(
            gemini={
                "tokens": {"inputTokens": 2_000_000, "cacheReadInputTokens": 1_000_000},
                "models": {
                    "gemini-3-flash-preview": {
                        "inputTokens": 2_000_000,
                        "cacheReadInputTokens": 1_000_000,
                        "outputTokens": 10_000,
                        "thoughtTokens": 5_000,
                        "week": 3_015_000,
                        "all": 3_015_000,
                    }
                },
                "limits": {
                    "quotas": [
                        {
                            "id": "gemini:gemini-3-flash-preview",
                            "remainingPercent": 95.3,
                            "usedPercent": 4.7,
                        }
                    ]
                },
            }
        )

        r = agentcat.derive_insights(s, period="week")

        self.assertEqual(r["providers"][0]["kind"], "gemini")
        self.assertEqual(r["providers"][0]["tokens"], 3_015_000)
        self.assertEqual(r["models"][0]["name"], "gemini-3-flash-preview")
        self.assertEqual(r["models"][0]["tokens"], 3_015_000)
        self.assertEqual(r["summary"]["top_provider"], "gemini")
        self.assertEqual(r["summary"]["top_model"], "gemini-3-flash-preview")
        self.assertEqual(r["pricing_status"], "ok")

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
