import importlib.util
import io
import datetime as dt
import json
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
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
            "GEMINI_TELEMETRY": agentcat.GEMINI_TELEMETRY,
            "GEMINI_USAGE_CACHE": agentcat.GEMINI_USAGE_CACHE,
            "ANTIGRAVITY_CLI_DIR": agentcat.ANTIGRAVITY_CLI_DIR,
            "ANTIGRAVITY_TELEMETRY": agentcat.ANTIGRAVITY_TELEMETRY,
            "ANTIGRAVITY_USAGE_CACHE": agentcat.ANTIGRAVITY_USAGE_CACHE,
            "LIVE_LIMITS_CACHE": agentcat.LIVE_LIMITS_CACHE,
            "JOURNAL_CURSOR_FILE": agentcat.JOURNAL_CURSOR_FILE,
            "CODEX_SESSIONS_CURSOR_FILE": agentcat.CODEX_SESSIONS_CURSOR_FILE,
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
        agentcat.GEMINI_TELEMETRY = agentcat_home / "gemini" / "telemetry.log"
        agentcat.GEMINI_USAGE_CACHE = agentcat_home / "gemini-usage-cache.json"
        agentcat.ANTIGRAVITY_CLI_DIR = home / ".gemini" / "antigravity-cli"
        agentcat.ANTIGRAVITY_TELEMETRY = agentcat_home / "gemini" / "antigravity-telemetry.log"
        agentcat.ANTIGRAVITY_USAGE_CACHE = agentcat_home / "antigravity-usage-cache.json"
        agentcat.LIVE_LIMITS_CACHE = agentcat_home / "live-limits-cache.json"
        agentcat.JOURNAL_CURSOR_FILE = agentcat_home / "jsonl-cursor.json"
        agentcat.CODEX_SESSIONS_CURSOR_FILE = agentcat_home / "codex-sessions-cursor.json"
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

    def test_connector_config_round_trips_desktop_apps_and_provider_enabled(self) -> None:
        app_path = self.root / "Applications" / "Claude.app"
        (app_path / "Contents").mkdir(parents=True)
        data_root = agentcat.HOME / "Library" / "Application Support" / "Claude"
        data_root.mkdir(parents=True)
        (data_root / "usage.json").write_text("{}", encoding="utf-8")

        result = agentcat.merge_connector_config_payload(
            {
                "providers": {
                    "claude": {"enabled": False, "limits": {"week": 1000}},
                },
                "desktopApps": {
                    "claude": {"path": str(app_path)},
                },
            }
        )

        self.assertFalse(result["providers"]["claude"]["enabled"])
        self.assertEqual(result["providers"]["claude"]["limits"]["week"], 1000)
        claude_app = result["desktopApps"]["claude"]
        self.assertEqual(claude_app["path"], str(app_path))
        self.assertTrue(claude_app["configured"])
        self.assertEqual(claude_app["status"], "ok")
        self.assertEqual(claude_app["readableFiles"], 1)
        self.assertEqual(claude_app["usageImport"], "allowlisted_usage")

    def test_connector_config_rejects_relative_desktop_app_paths(self) -> None:
        with self.assertRaises(ValueError):
            agentcat.merge_connector_config_payload(
                {"desktopApps": {"codex": {"path": "Codex.app"}}}
            )

    def test_build_snapshot_skips_disabled_provider_and_exposes_desktop_apps(self) -> None:
        app_path = self.root / "Applications" / "Codex.app"
        app_path.mkdir(parents=True)
        agentcat.write_agentcat_settings(
            {
                "providers": {"codex": {"enabled": False}},
                "desktopApps": {"codex": {"path": str(app_path)}},
            }
        )

        with patch.object(agentcat, "codex_snapshot", side_effect=AssertionError("codex should be skipped")), \
             patch.object(agentcat, "claude_snapshot", return_value={"status": "ok", "tokens": {}, "models": {}}), \
             patch.object(agentcat, "gemini_snapshot", return_value={"status": "ok", "tokens": {}, "models": {}}), \
             patch.object(agentcat, "opencode_snapshot", return_value={"status": "ok", "tokens": {}, "models": {}}), \
             patch.object(agentcat, "copilot_snapshot", return_value={"status": "ok", "tokens": {}, "models": {}}):
            snapshot = agentcat.build_snapshot()

        self.assertEqual(snapshot["providers"]["codex"]["status"], "disabled")
        self.assertIn("desktopApps", snapshot)
        self.assertEqual(snapshot["desktopApps"]["codex"]["status"], "installed_no_data")
        self.assertEqual(snapshot["providers"]["codex"]["desktopApp"]["path"], str(app_path))

    def test_codex_usage_api_payload_builds_remaining_quota_entries(self) -> None:
        limits = agentcat.codex_limits_from_usage_response(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
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
                "credits": {
                    "balance": "0",
                    "has_credits": False,
                    "unlimited": False,
                    "overage_limit_reached": False,
                    "approx_cloud_messages": [2, 3],
                    "approx_local_messages": [7],
                },
                "rate_limit_reset_credits": {"available_count": 1},
                "rate_limit_reached_type": {"type": "rate_limit_reached"},
                "spend_control": {"reached": False},
            }
        )

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(limits["planType"], "pro")
        self.assertEqual(limits["shortUsedPercent"], 3.0)
        self.assertEqual(limits["weeklyUsedPercent"], 18.0)
        self.assertEqual(limits["quotas"][0]["remainingPercent"], 97.0)
        self.assertEqual(limits["quotas"][1]["remainingPercent"], 82.0)
        self.assertEqual(limits["quotas"][2]["remainingPercent"], 100.0)
        self.assertEqual(limits["rateLimitAllowed"], True)
        self.assertEqual(limits["rateLimitReached"], False)
        self.assertEqual(limits["rateLimitReachedType"], "rate_limit_reached")
        self.assertEqual(limits["resetCreditsAvailable"], 1)
        self.assertEqual(limits["spendControlReached"], False)
        self.assertEqual(limits["codexCredits"]["approxCloudMessages"], 5)
        self.assertEqual(limits["codexCredits"]["approxLocalMessages"], 7)
        self.assertEqual(limits["codexCredits"]["hasCredits"], False)

    def test_codex_reset_credit_details_are_sanitized(self) -> None:
        limits = {}
        agentcat.merge_codex_reset_credit_details(
            limits,
            {
                "available_count": 2,
                "credits": [
                    {
                        "id": "secret-credit-id",
                        "profile_user_id": "friend-user-id",
                        "profile_image_url": "https://example.test/avatar.png",
                        "status": "available",
                        "title": "One rate limit reset",
                        "description": "Ready to redeem",
                        "reset_type": "rate_limit",
                        "expires_at": "2026-07-01T00:00:00Z",
                    }
                ],
            },
        )

        self.assertEqual(limits["resetCreditsAvailable"], 2)
        self.assertEqual(limits["resetCredits"][0]["status"], "available")
        self.assertEqual(limits["resetCredits"][0]["title"], "One rate limit reset")
        self.assertNotIn("id", limits["resetCredits"][0])
        self.assertNotIn("profile_user_id", limits["resetCredits"][0])
        self.assertNotIn("profile_image_url", limits["resetCredits"][0])

    def test_codex_live_limits_tries_wham_before_refresh_after_codex_403(self) -> None:
        (agentcat.HOME / ".codex").mkdir(parents=True)
        (agentcat.HOME / ".codex" / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "access", "refresh_token": "refresh", "account_id": "acct"}}),
            encoding="utf-8",
        )

        def usage_side_effect(_access: str, _account: str, url: str) -> dict:
            if url.endswith("/codex/usage"):
                raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
            return {
                "plan_type": "pro",
                "rate_limit": {"primary_window": {"used_percent": 20, "reset_at": 1770000000}},
                "rate_limit_reset_credits": {"available_count": 1},
                "credits": {"approx_cloud_messages": [0], "approx_local_messages": [0]},
            }

        with patch.object(agentcat, "CODEX_USAGE_URLS", ("https://chatgpt.com/backend-api/codex/usage", "https://chatgpt.com/backend-api/wham/usage")), \
            patch.object(agentcat, "codex_usage_request", side_effect=usage_side_effect), \
            patch.object(agentcat, "codex_reset_credits_request", return_value={"available_count": 2, "credits": []}), \
            patch.object(agentcat, "refresh_codex_access_token", side_effect=AssertionError("refresh should wait until all usage URLs fail")):
            limits = agentcat.codex_live_limits()

        self.assertEqual(limits["source"], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(limits["resetCreditsAvailable"], 2)
        self.assertEqual(limits["codexCredits"]["approxCloudMessages"], 0)
        self.assertEqual(limits["quotas"][0]["remainingPercent"], 80.0)

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
        self.assertIn("connector.channel", snapshot["capabilities"])
        self.assertIn("limits.quotaFallbackOn429", snapshot["capabilities"])
        self.assertIn("limits.claude.statuslineQuotas", snapshot["capabilities"])
        self.assertIn("usage.hourlyTokens", snapshot["capabilities"])
        self.assertEqual(snapshot["update"]["channel"]["channel"], "public")

    def test_update_channel_manifest_validation_and_snapshot_status(self) -> None:
        manifest = {
            "version": "26.25.0",
            "downloadUrl": "https://api.agentcat.app/v1/pro/connector/download/26.25.0",
            "sha256": "a" * 64,
            "minAppVersion": "26.25.0",
            "channel": "pro",
        }

        state = agentcat.write_update_channel_state("pro", manifest)
        status = agentcat.update_channel_status_snapshot()

        self.assertEqual(state["status"], "manifest_ready")
        self.assertEqual(state["installStatus"], "pending_install")
        self.assertEqual(status["channel"], "pro")
        self.assertEqual(status["targetVersion"], "26.25.0")
        self.assertEqual(status["installStatus"], "pending_install")
        self.assertTrue(agentcat.update_channel_state_file().exists())

    def test_update_channel_rejects_insecure_or_bad_manifest(self) -> None:
        manifest = {
            "version": "26.25.0",
            "downloadUrl": "http://api.agentcat.app/v1/pro/connector/download/26.25.0",
            "sha256": "a" * 64,
            "channel": "pro",
        }

        with self.assertRaises(ValueError):
            agentcat.write_update_channel_state("pro", manifest)

        manifest["downloadUrl"] = "https://api.agentcat.app/v1/pro/connector/download/26.25.0"
        manifest["sha256"] = "A" * 64
        with self.assertRaises(ValueError):
            agentcat.write_update_channel_state("pro", manifest)

    def test_update_channel_public_clears_pro_manifest_state(self) -> None:
        manifest = {
            "version": "26.25.0",
            "downloadUrl": "https://api.agentcat.app/v1/pro/connector/download/26.25.0",
            "sha256": "b" * 64,
            "channel": "pro",
        }

        agentcat.write_update_channel_state("pro", manifest)
        state = agentcat.write_update_channel_state("public")

        self.assertEqual(state["channel"], "public")
        self.assertNotIn("manifest", state)
        self.assertEqual(agentcat.update_channel_status_snapshot()["installStatus"], "current")

    def test_http_update_channel_endpoint_persists_manifest(self) -> None:
        server = agentcat.ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            manifest = {
                "version": "26.25.0",
                "downloadUrl": "https://api.agentcat.app/v1/pro/connector/download/26.25.0",
                "sha256": "c" * 64,
                "channel": "pro",
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/update/channel",
                data=json.dumps({"channel": "pro", "manifest": manifest}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Host": "127.0.0.1"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2.0)
            server.server_close()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["channel"]["channel"], "pro")
        self.assertEqual(agentcat.update_channel_status_snapshot()["targetVersion"], "26.25.0")

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
            "AGENTCAT_INSTALL_SH_SHA256": "a" * 64,
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
            "AGENTCAT_INSTALL_SH_SHA256": "a" * 64,
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

    # --- Codex sessions-JSONL per-class reader -------------------------------

    def _write_codex_session(self, name: str, lines: list) -> Path:
        sessions_dir = agentcat.HOME / ".codex" / "sessions" / "2026" / "06" / "18"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / name
        path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
        return path

    @staticmethod
    def _token_count_event(usage: dict, ts: str, cwd=None) -> dict:
        info = {"last_token_usage": usage}
        payload = {"type": "token_count", "info": info}
        if cwd is not None:
            payload["cwd"] = cwd
        return {"type": "event_msg", "payload": payload, "timestamp": ts}

    def test_codex_sessions_jsonl_sums_per_class_and_attributes_model(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self._write_codex_session(
            "rollout-a.jsonl",
            [
                {"type": "session_meta", "payload": {"model_provider": "openai"}},
                {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
                # input 100 (30 cached -> cacheRead, 70 uncached input),
                # output 40 + reasoning 10 -> 50 output
                self._token_count_event(
                    {
                        "input_tokens": 100,
                        "cached_input_tokens": 30,
                        "output_tokens": 40,
                        "reasoning_output_tokens": 10,
                    },
                    now,
                    cwd="/Users/me/projects/alpha",
                ),
                # second turn, same model: input 50 (0 cached), output 20
                self._token_count_event(
                    {
                        "input_tokens": 50,
                        "cached_input_tokens": 0,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 0,
                    },
                    now,
                    cwd="/Users/me/projects/alpha",
                ),
            ],
        )

        snapshot = agentcat.codex_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertTrue(str(snapshot["source"]).endswith("/sessions/**/*.jsonl"))
        # uncached input: (100-30) + 50 = 120
        self.assertEqual(snapshot["tokens"]["inputTokens"], 120)
        # output incl reasoning: (40+10) + 20 = 70
        self.assertEqual(snapshot["tokens"]["outputTokens"], 70)
        # cacheRead == cached_input: 30 + 0 = 30
        self.assertEqual(snapshot["tokens"]["cacheReadInputTokens"], 30)
        # all-time total == sum of classes
        self.assertEqual(snapshot["tokens"]["all"], 120 + 70 + 30)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 120 + 70 + 30)
        # model attributed from the preceding turn_context
        model = snapshot["models"]["gpt-5.4"]
        self.assertEqual(model["inputTokens"], 120)
        self.assertEqual(model["outputTokens"], 70)
        self.assertEqual(model["cacheReadInputTokens"], 30)
        self.assertNotIn("unknown", snapshot["models"])
        # project attributed via cwd
        self.assertEqual(snapshot["projects"]["status"], "ok")
        self.assertEqual(snapshot["projects"]["items"][0]["path"], "/Users/me/projects/alpha")
        # breakdown chat == token_count turns
        self.assertEqual(snapshot["breakdown"]["chat"], 2)
        # hourly buckets sum to all-time total
        self.assertEqual(sum(snapshot["hourlyTokens"].values()), 120 + 70 + 30)

    def test_codex_sessions_jsonl_attributes_per_model_across_turn_contexts(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self._write_codex_session(
            "rollout-b.jsonl",
            [
                {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
                self._token_count_event(
                    {"input_tokens": 10, "cached_input_tokens": 0,
                     "output_tokens": 5, "reasoning_output_tokens": 0},
                    now,
                ),
                {"type": "turn_context", "payload": {"model": "gpt-5.4-mini"}},
                self._token_count_event(
                    {"input_tokens": 7, "cached_input_tokens": 2,
                     "output_tokens": 3, "reasoning_output_tokens": 1},
                    now,
                ),
            ],
        )

        snapshot = agentcat.codex_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["models"]["gpt-5.4"]["inputTokens"], 10)
        self.assertEqual(snapshot["models"]["gpt-5.4"]["outputTokens"], 5)
        # second turn switched model: uncached 5, output 3+1=4, cacheRead 2
        self.assertEqual(snapshot["models"]["gpt-5.4-mini"]["inputTokens"], 5)
        self.assertEqual(snapshot["models"]["gpt-5.4-mini"]["outputTokens"], 4)
        self.assertEqual(snapshot["models"]["gpt-5.4-mini"]["cacheReadInputTokens"], 2)

    def test_codex_sessions_jsonl_preferred_over_state_sqlite(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self._write_codex_session(
            "rollout-c.jsonl",
            [
                {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
                self._token_count_event(
                    {"input_tokens": 8, "cached_input_tokens": 0,
                     "output_tokens": 4, "reasoning_output_tokens": 0},
                    now,
                ),
            ],
        )
        # Also drop a state.sqlite with a different total; JSONL must win.
        db_path = agentcat.HOME / ".codex" / "state_test.sqlite"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("create table threads(tokens_used integer, model text, updated_at text)")
            conn.execute(
                "insert into threads(tokens_used, model, updated_at) values (?, ?, ?)",
                (9999, "gpt-old", now),
            )
            conn.commit()

        snapshot = agentcat.codex_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertTrue(str(snapshot["source"]).endswith("/sessions/**/*.jsonl"))
        self.assertEqual(snapshot["tokens"]["all"], 12)
        self.assertIn("gpt-5.4", snapshot["models"])
        self.assertNotIn("gpt-old", snapshot["models"])

    def test_codex_sessions_without_token_events_falls_back_to_sqlite(self) -> None:
        # Session file with a turn_context but no token_count -> must fall back.
        self._write_codex_session(
            "rollout-empty.jsonl",
            [{"type": "turn_context", "payload": {"model": "gpt-5.4"}}],
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        db_path = agentcat.HOME / ".codex" / "state_test.sqlite"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("create table threads(tokens_used integer, model text, updated_at text)")
            conn.execute(
                "insert into threads(tokens_used, model, updated_at) values (?, ?, ?)",
                (55, "gpt-old", now),
            )
            conn.commit()

        snapshot = agentcat.codex_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        # Fell back to the sqlite path.
        self.assertTrue(str(snapshot["source"]).endswith("state_test.sqlite"))
        self.assertEqual(snapshot["tokens"]["all"], 55)

    def test_codex_sessions_cursor_dedupes_on_reread(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        path = self._write_codex_session(
            "rollout-d.jsonl",
            [
                {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
                self._token_count_event(
                    {"input_tokens": 10, "cached_input_tokens": 0,
                     "output_tokens": 0, "reasoning_output_tokens": 0},
                    now,
                ),
            ],
        )

        first = agentcat.codex_snapshot()
        self.assertEqual(first["tokens"]["all"], 10)

        # Re-read with no file change: cursor must prevent double counting.
        second = agentcat.codex_snapshot()
        self.assertEqual(second["tokens"]["all"], 10)

        # Append a new token_count turn and re-read: only the delta is added,
        # and the model attribution survives across the incremental read.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    self._token_count_event(
                        {"input_tokens": 5, "cached_input_tokens": 0,
                         "output_tokens": 0, "reasoning_output_tokens": 0},
                        now,
                    )
                )
                + "\n"
            )
        third = agentcat.codex_snapshot()
        self.assertEqual(third["tokens"]["all"], 15)
        self.assertEqual(third["models"]["gpt-5.4"]["inputTokens"], 15)
        self.assertNotIn("unknown", third["models"])

    def test_claude_snapshot_adds_periods_to_daily_model_tokens(self) -> None:
        claude_dir = agentcat.HOME / ".claude"
        claude_dir.mkdir(parents=True)
        # Claude dailyModelTokens dates are LOCAL calendar days, and periods
        # bucket "today" on the local date — so the fixture must use the local
        # date, not UTC (which only coincide outside the KST-morning window).
        today = dt.datetime.now().date()
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

    def test_periods_today_matches_daily_today_across_utc_local_boundary(self) -> None:
        # Regression: add_to_periods bucketed "today" on the UTC date while
        # add_to_daily buckets on the LOCAL date. Near midnight (when UTC and
        # local calendar dates differ) the period "today" total and the daily
        # today bucket disagreed. Both must now key on the LOCAL date.
        import os
        import time

        if not hasattr(time, "tzset"):
            self.skipTest("requires POSIX tzset")

        old_tz = os.environ.get("TZ")
        # UTC+14: an instant at 23:00 UTC lands on the *next* local calendar day,
        # so its UTC date and local date differ.
        os.environ["TZ"] = "Pacific/Kiritimati"
        time.tzset()
        try:
            fixed_now = dt.datetime(2026, 6, 18, 23, 30, tzinfo=dt.timezone.utc)
            # An event 30 minutes earlier: still the same LOCAL "today" as now,
            # but its UTC date (2026-06-18) differs from the local date
            # (2026-06-19).
            when = dt.datetime(2026, 6, 18, 23, 0, tzinfo=dt.timezone.utc)
            self.assertNotEqual(when.date(), when.astimezone().date())
            self.assertEqual(when.astimezone().date(), fixed_now.astimezone().date())

            class _FixedNow(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed_now if tz is None else fixed_now.astimezone(tz)

            periods: dict = {}
            daily: dict = {}
            with patch.object(agentcat.dt, "datetime", _FixedNow):
                agentcat.add_to_periods(periods, 100, when)
                agentcat.add_to_daily(daily, 100, when)
                today_key = agentcat.day_key_for_timestamp(fixed_now)

            # The event counts toward "today" (local) in periods...
            self.assertEqual(periods["today"], 100)
            # ...and lands in the local today bucket of daily, and they agree.
            self.assertEqual(daily[today_key], 100)
            self.assertEqual(periods["today"], daily[today_key])
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

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

    def test_claude_snapshot_reads_desktop_local_agent_usage_allowlist(self) -> None:
        project_dir = (
            agentcat.HOME
            / "Library"
            / "Application Support"
            / "Claude"
            / "local-agent-mode-sessions"
            / "account"
            / "workspace"
            / "local-session"
            / ".claude"
            / "projects"
            / "desktop-project"
        )
        project_dir.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        session_path = project_dir / "session.jsonl"
        session_path.write_text(
            json.dumps(
                {
                    "timestamp": now.isoformat().replace("+00:00", "Z"),
                    "cwd": "/Users/tester/work/desktop-app",
                    "requestId": "desktop_req_1",
                    "message": {
                        "id": "desktop_msg_1",
                        "model": "claude-sonnet-4-6",
                        "content": [
                            {"type": "text", "text": "must never be emitted"},
                            {"type": "tool_use", "input": {"prompt": "must never be emitted"}},
                        ],
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "cache_read_input_tokens": 30,
                            "cache_creation_input_tokens": 40,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        snapshot = agentcat.claude_snapshot()

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["tokens"]["inputTokens"], 100)
        self.assertEqual(snapshot["tokens"]["outputTokens"], 20)
        self.assertEqual(snapshot["tokens"]["cacheReadInputTokens"], 30)
        self.assertEqual(snapshot["tokens"]["cacheCreationInputTokens"], 40)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 190)
        self.assertEqual(snapshot["models"]["claude-sonnet-4-6"]["all"], 190)
        self.assertEqual(snapshot["projects"]["items"][0]["tokens"], 190)
        cursor = json.loads(agentcat.JOURNAL_CURSOR_FILE.read_text(encoding="utf-8"))
        self.assertIn(str(session_path), cursor["offsets"])
        self.assertNotIn("must never be emitted", json.dumps(cursor, ensure_ascii=False))

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

    @staticmethod
    def _project_daily_providers(tokens: int, path: str = "/Users/tester/work/repo") -> dict:
        return {
            "claude": {
                "status": "ok",
                "projects": {
                    "status": "ok",
                    "items": [
                        {
                            "id": path,
                            "path": path,
                            "name": agentcat.display_project_name(path),
                            "tokens": tokens,
                        }
                    ],
                },
            }
        }

    def test_project_daily_books_cumulative_growth_to_local_today(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()

        first = agentcat.update_project_daily(self._project_daily_providers(100))
        second = agentcat.update_project_daily(self._project_daily_providers(160))

        self.assertEqual(first, {})
        self.assertEqual(second, {today: {"repo": 60}})
        ledger = json.loads(agentcat.project_daily_file().read_text(encoding="utf-8"))
        self.assertEqual(ledger, {today: {"repo": 60}})
        self.assertNotIn("/Users/tester/work/repo", ledger[today])
        state = json.loads(agentcat.project_daily_state_file().read_text(encoding="utf-8"))
        self.assertEqual(state["baselines"]["claude|/Users/tester/work/repo"], 160)

    def test_project_daily_counter_reset_books_no_negative_delta(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()
        agentcat.update_project_daily(self._project_daily_providers(100))
        agentcat.update_project_daily(self._project_daily_providers(160))

        after_reset = agentcat.update_project_daily(self._project_daily_providers(40))
        resumed = agentcat.update_project_daily(self._project_daily_providers(50))

        self.assertEqual(after_reset, {today: {"repo": 60}})
        self.assertEqual(resumed, {today: {"repo": 70}})
        state = json.loads(agentcat.project_daily_state_file().read_text(encoding="utf-8"))
        self.assertEqual(state["baselines"]["claude|/Users/tester/work/repo"], 50)

    def test_project_daily_sums_provider_deltas_into_one_label(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).astimezone().date().isoformat()
        path = "/Users/tester/work/repo"

        def providers(claude_tokens: int, codex_tokens: int) -> dict:
            merged = self._project_daily_providers(claude_tokens, path)
            merged["codex"] = self._project_daily_providers(codex_tokens, path)["claude"]
            return merged

        agentcat.update_project_daily(providers(100, 30))
        booked = agentcat.update_project_daily(providers(150, 50))

        self.assertEqual(booked, {today: {"repo": 70}})

    def test_project_daily_prunes_days_beyond_retention(self) -> None:
        today_date = dt.datetime.now(dt.timezone.utc).astimezone().date()
        old_day = (today_date - dt.timedelta(days=agentcat.PROJECT_DAILY_RETENTION_DAYS + 5)).isoformat()
        kept_day = (today_date - dt.timedelta(days=30)).isoformat()
        agentcat.write_json_atomic(
            agentcat.project_daily_file(),
            {old_day: {"repo": 5}, kept_day: {"repo": 7}, "not-a-day": {"repo": 9}},
        )

        snapshot_slice = agentcat.update_project_daily({})

        ledger = json.loads(agentcat.project_daily_file().read_text(encoding="utf-8"))
        self.assertEqual(ledger, {kept_day: {"repo": 7}})
        self.assertEqual(snapshot_slice, {})

    def test_project_daily_snapshot_slice_is_capped_to_fourteen_days(self) -> None:
        today_date = dt.datetime.now(dt.timezone.utc).astimezone().date()
        inside = (today_date - dt.timedelta(days=agentcat.PROJECT_DAILY_SNAPSHOT_DAYS - 1)).isoformat()
        outside = (today_date - dt.timedelta(days=agentcat.PROJECT_DAILY_SNAPSHOT_DAYS)).isoformat()
        agentcat.write_json_atomic(
            agentcat.project_daily_file(),
            {inside: {"repo": 3}, outside: {"repo": 4}},
        )

        snapshot_slice = agentcat.update_project_daily({})

        self.assertEqual(snapshot_slice, {inside: {"repo": 3}})
        ledger = json.loads(agentcat.project_daily_file().read_text(encoding="utf-8"))
        self.assertEqual(ledger, {inside: {"repo": 3}, outside: {"repo": 4}})

    def test_build_snapshot_exposes_projects_daily_and_capability(self) -> None:
        snapshot = agentcat.build_snapshot()

        self.assertIn("projects.daily", snapshot["capabilities"])
        self.assertIsInstance(snapshot.get("projectsDaily"), dict)

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

    def test_split_google_cli_snapshots_does_not_borrow_gemini_tokens_for_antigravity(self) -> None:
        # Antigravity bills server-side and never writes a local telemetry outfile,
        # so it must stay a SEPARATE provider with empty tokens — Gemini-CLI usage
        # is never inferred onto it, even when Antigravity history/activity exists.
        agentcat.ANTIGRAVITY_CLI_DIR.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        today = agentcat.day_key_for_timestamp(now)
        yesterday = agentcat.day_key_for_timestamp(now - dt.timedelta(days=1))
        (agentcat.ANTIGRAVITY_CLI_DIR / "history.jsonl").write_text(
            json.dumps({"timestamp": int(now.timestamp() * 1000), "conversationId": "agy-conv"}) + "\n",
            encoding="utf-8",
        )
        common_usage = {
            "tokens": {"inputTokens": 210, "outputTokens": 90, "totalTokens": 300, "all": 300},
            "models": {"gemini-test": {"inputTokens": 210, "outputTokens": 90, "all": 300}},
            "dailyTokens": {yesterday: 100, today: 200},
            "hourlyTokens": {f"{yesterday}T12": 100, f"{today}T12": 200},
            "modelDailyTokens": {"gemini-test": {yesterday: 100, today: 200}},
            "events": 12,
            "cache": {"source": "full-log-index"},
        }
        gemini = {
            "status": "ok",
            "source": str(agentcat.GEMINI_TELEMETRY),
            "canonicalProvider": "google",
            "providerLabel": "Gemini",
            "displayName": "Gemini",
            "sources": {
                "geminiCli": {"installed": True, "status": "ok", "tokens": 300},
                "antigravityCli": {
                    "installed": True,
                    "status": "no_telemetry_yet",
                    "source": str(agentcat.ANTIGRAVITY_TELEMETRY),
                    "tokens": 0,
                },
            },
            "_sourceUsages": {"geminiCli": common_usage},
            "tokens": common_usage["tokens"],
            "models": {},
            "dailyTokens": common_usage["dailyTokens"],
            "hourlyTokens": common_usage["hourlyTokens"],
            "events": 12,
            "cache": {"source": "full-log-index"},
            "breakdown": agentcat.empty_breakdown(),
        }

        gemini_provider, antigravity_provider = agentcat.split_google_cli_snapshots(
            gemini,
            {"countsByProvider": {"gemini": 0, "antigravity": 1}},
        )

        # Gemini keeps the full, real usage — no day was split off to Antigravity.
        self.assertEqual(gemini_provider["tokens"]["totalTokens"], 300)
        self.assertEqual(gemini_provider["tokens"]["inputTokens"], 210)
        self.assertEqual(gemini_provider["tokens"]["outputTokens"], 90)
        self.assertEqual(gemini_provider["dailyTokens"], {yesterday: 100, today: 200})

        # Antigravity is its own provider, but empty and honest — no Gemini tokens.
        self.assertEqual(antigravity_provider["providerLabel"], "Antigravity")
        self.assertEqual(antigravity_provider["displayName"], "Antigravity")
        self.assertEqual(antigravity_provider["canonicalProvider"], "google")
        self.assertEqual(antigravity_provider["status"], "no_telemetry_yet")
        self.assertEqual(antigravity_provider["tokens"], {})
        self.assertEqual(antigravity_provider["models"], {})
        self.assertEqual(antigravity_provider["dailyTokens"], {})
        self.assertEqual(antigravity_provider["events"], 0)
        self.assertNotEqual(antigravity_provider["sourceAttribution"], "inferred-from-gemini-telemetry")

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

    def test_classify_antigravity_cli_processes_as_antigravity(self) -> None:
        self.assertEqual(
            agentcat.classify_process("agy --print hello"),
            "antigravity",
        )
        self.assertEqual(
            agentcat.classify_process("/Users/me/.local/bin/agy --prompt hello"),
            "antigravity",
        )
        self.assertEqual(
            agentcat.classify_process(r'"C:\Users\me\.local\bin\agy.exe" --print hello'),
            "antigravity",
        )
        self.assertEqual(
            agentcat.classify_process("/Applications/Antigravity.app/Contents/MacOS/antigravity"),
            "antigravity",
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
        self.assertEqual(snapshot["countsByProvider"], {"codex": 1, "claude": 1, "gemini": 1, "antigravity": 1})
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

    def test_host_header_allow_list_accepts_loopback_any_port(self) -> None:
        for host in (
            "127.0.0.1",
            "127.0.0.1:8765",
            "127.0.0.1:9999",  # non-default --port must pass (port is stripped)
            "localhost",
            "localhost:8765",
            "localhost:12345",
            "::1",
            "[::1]",
            "[::1]:8765",
            "[::1]:54321",
            "LOCALHOST:8765",
            # Missing/empty Host is treated as local: the listener binds loopback
            # only, so the connection already originates on this machine.
            "",
            None,
        ):
            self.assertTrue(agentcat.host_header_allowed(host), host)

    def test_host_header_allow_list_rejects_rebinding(self) -> None:
        for host in (
            "evil.example.com",
            "attacker.local:8765",
            "192.168.1.10:8765",
            "127.0.0.1.evil.com",
        ):
            self.assertFalse(agentcat.host_header_allowed(host), host)

    def test_http_get_rejects_foreign_host_header_with_403(self) -> None:
        server = agentcat.ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/healthz",
                headers={"Host": "attacker.example.com"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=2.0)
            self.assertEqual(ctx.exception.code, 403)

            ok_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/healthz",
                headers={"Host": "127.0.0.1"},
            )
            with urllib.request.urlopen(ok_request, timeout=2.0) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"ok\n")
        finally:
            server.shutdown()
            thread.join(timeout=2.0)
            server.server_close()

    def test_resolve_bind_host_forces_loopback(self) -> None:
        self.assertEqual(agentcat.resolve_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(agentcat.resolve_bind_host("::1"), "::1")
        self.assertEqual(agentcat.resolve_bind_host("localhost"), "localhost")
        self.assertEqual(agentcat.resolve_bind_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(agentcat.resolve_bind_host("192.168.0.5"), "127.0.0.1")
        self.assertEqual(agentcat.resolve_bind_host(""), "127.0.0.1")

    def test_free_channel_auto_update_off_by_default(self) -> None:
        with patch.dict(agentcat.os.environ, {}, clear=False):
            agentcat.os.environ.pop("AGENTCAT_AUTO_UPDATE", None)
            agentcat.os.environ.pop("AGENTCAT_INSTALL_SH_SHA256", None)
            agentcat.os.environ.pop("AGENTCAT_CONNECTOR_VERSION", None)
            enabled, reason = agentcat.auto_update_enabled_status()
        self.assertFalse(enabled)
        self.assertIn("off by default", reason)

    def test_free_channel_opt_in_requires_pinned_digest(self) -> None:
        install_dir = (agentcat.AGENTCAT_HOME / "connectors").resolve()
        with patch.dict(agentcat.os.environ, {
            "AGENTCAT_AUTO_UPDATE": "1",
            "AGENTCAT_CONNECTOR_VERSION": "",
            "AGENTCAT_CONNECTORS_DIR": str(install_dir),
        }), patch.object(agentcat, "current_connector_repo_dir", return_value=install_dir):
            agentcat.os.environ.pop("AGENTCAT_INSTALL_SH_SHA256", None)
            enabled, reason = agentcat.auto_update_enabled_status()
            self.assertFalse(enabled)
            self.assertIn("pinned install.sh digest", reason)

            agentcat.os.environ["AGENTCAT_INSTALL_SH_SHA256"] = "a" * 64
            enabled_ok, _ = agentcat.auto_update_enabled_status()
            self.assertTrue(enabled_ok)

    def test_start_auto_update_install_verifies_digest_before_exec(self) -> None:
        body = b"#!/bin/sh\necho hardened\n"
        good = agentcat.hashlib.sha256(body).hexdigest()

        class FakeResp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner, _n=None):
                return body

        with patch.dict(agentcat.os.environ, {"AGENTCAT_INSTALL_SH_SHA256": "b" * 64}), \
                patch.object(agentcat.urllib.request, "urlopen", return_value=FakeResp()):
            with self.assertRaises(ValueError) as ctx:
                agentcat.start_auto_update_install("99.0.0")
            self.assertIn("digest mismatch", str(ctx.exception))

        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return type("Proc", (), {"pid": 4242})()

        with patch.dict(agentcat.os.environ, {"AGENTCAT_INSTALL_SH_SHA256": good}), \
                patch.object(agentcat.urllib.request, "urlopen", return_value=FakeResp()), \
                patch.object(agentcat.subprocess, "Popen", side_effect=fake_popen):
            proc = agentcat.start_auto_update_install("99.0.0")

        self.assertEqual(proc.pid, 4242)
        # Verified bytes are executed from a local file, never piped from curl.
        script_name = "auto-update-install.ps1" if agentcat.IS_WINDOWS else "auto-update-install.sh"
        script_path = agentcat.AGENTCAT_HOME / script_name
        self.assertTrue(script_path.exists())
        self.assertEqual(script_path.read_bytes(), body)
        command_text = " ".join(str(part) for part in captured["cmd"]).lower()
        self.assertNotIn("curl", command_text)
        self.assertNotIn("irm", command_text)
        self.assertNotIn("iex", command_text)

    def test_sanitize_payload_redacts_path_and_secret_values(self) -> None:
        sanitized = agentcat.sanitize_payload(
            {
                "note": "/Users/alice/Projects/secret-repo/file.py",
                "label": "deploy",
                "token": "sk-ant-api03-abcdefgh12345678",
                "level": "high",
                "version": "26.25.0",
                "generatedAt": "2026-05-01T00:00:00Z",
            }
        )
        self.assertEqual(sanitized["note"], "[redacted]")
        self.assertEqual(sanitized["token"], "[redacted]")
        self.assertEqual(sanitized["label"], "deploy")
        self.assertEqual(sanitized["level"], "high")
        self.assertEqual(sanitized["version"], "26.25.0")
        self.assertEqual(sanitized["generatedAt"], "2026-05-01T00:00:00Z")

    def test_sanitize_payload_redacts_command_and_args_keys(self) -> None:
        sanitized = agentcat.sanitize_payload(
            {
                "command": "rm -rf /tmp/x",
                "description": "private description",
                "title": "private title",
                "args": ["--secret", "value"],
                "text": "free text body",
                "context_window": {"context_window_size": 1000000},
            }
        )
        self.assertEqual(sanitized["command"], "[redacted]")
        self.assertEqual(sanitized["description"], "[redacted]")
        self.assertEqual(sanitized["title"], "[redacted]")
        self.assertEqual(sanitized["args"], "[redacted]")
        self.assertEqual(sanitized["text"], "[redacted]")
        # context_window must survive — it is legitimate usage metadata.
        self.assertEqual(sanitized["context_window"]["context_window_size"], 1000000)

    def test_sanitize_payload_redacts_compound_prompt_keys_keeps_metric_keys(self) -> None:
        # Compound prompt-bearing keys redact via a whole-token match (not just
        # the bare key); numeric metadata that merely contains a sensitive token
        # survives (a count/size is not the content).
        sanitized = agentcat.sanitize_payload(
            {
                "user_message": "hello there",
                "assistant_message": "hi back",
                "system_prompt": "you are a helpful assistant",
                "message_count": 12,
                "command_count": 3,
                "path_segments": 4,
                "content_length": 900,
            }
        )
        self.assertEqual(sanitized["user_message"], "[redacted]")
        self.assertEqual(sanitized["assistant_message"], "[redacted]")
        self.assertEqual(sanitized["system_prompt"], "[redacted]")
        self.assertEqual(sanitized["message_count"], 12)
        self.assertEqual(sanitized["command_count"], 3)
        self.assertEqual(sanitized["path_segments"], 4)
        self.assertEqual(sanitized["content_length"], 900)

    @unittest.skipIf(agentcat.IS_WINDOWS, "POSIX file permissions only")
    def test_ensure_dirs_tightens_permissions(self) -> None:
        import os as _os
        import stat as _stat

        agentcat.init_db()
        agentcat.store_event("claude", "test", "ping", {"ok": True})

        home_mode = _stat.S_IMODE(_os.stat(agentcat.AGENTCAT_HOME).st_mode)
        self.assertEqual(home_mode, 0o700)
        db_mode = _stat.S_IMODE(_os.stat(agentcat.EVENTS_DB).st_mode)
        self.assertEqual(db_mode, 0o600)

        agentcat.write_json_atomic(agentcat.LATEST_SNAPSHOT, {"ok": True})
        snap_mode = _stat.S_IMODE(_os.stat(agentcat.LATEST_SNAPSHOT).st_mode)
        self.assertEqual(snap_mode, 0o600)


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

    def test_build_snapshot_includes_pricing_block(self) -> None:
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
            # No pricing cache in the temp AGENTCAT_HOME -> bundled fallback.
            self.assertEqual(snap["pricing"], {"source": "bundled", "fetchedAt": None})
        finally:
            self._stop(patches)


class InsightsTests(unittest.TestCase):
    """Slice B — derive_insights() port of Swift AgentInsights.derive()."""

    def setUp(self) -> None:
        # Hermetic pricing: ignore any real ~/.agentcat/pricing-cache.json so
        # bundled MODEL_PRICING rates drive the cost assertions below.
        self.pricing_tmp = tempfile.TemporaryDirectory()
        self._pricing_cache_patch = patch.object(
            agentcat,
            "pricing_cache_file",
            return_value=Path(self.pricing_tmp.name) / "pricing-cache.json",
        )
        self._pricing_cache_patch.start()
        agentcat._PRICING_TABLE_MEMO = None

    def tearDown(self) -> None:
        self._pricing_cache_patch.stop()
        agentcat._PRICING_TABLE_MEMO = None
        self.pricing_tmp.cleanup()

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

    def setUp(self) -> None:
        # Hermetic pricing: ignore any real ~/.agentcat/pricing-cache.json so
        # bundled MODEL_PRICING rates drive the cost assertions below.
        self.pricing_tmp = tempfile.TemporaryDirectory()
        self._pricing_cache_patch = patch.object(
            agentcat,
            "pricing_cache_file",
            return_value=Path(self.pricing_tmp.name) / "pricing-cache.json",
        )
        self._pricing_cache_patch.start()
        agentcat._PRICING_TABLE_MEMO = None

    def tearDown(self) -> None:
        self._pricing_cache_patch.stop()
        agentcat._PRICING_TABLE_MEMO = None
        self.pricing_tmp.cleanup()

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


class PricingFeedTests(unittest.TestCase):
    """LiteLLM pricing feed — conversion, tiers, merge precedence, fallback."""

    # Small offline stand-in for model_prices_and_context_window.json. Key
    # names verified against a live sample during development; tests never
    # touch the network.
    LITELLM_FIXTURE = {
        "sample_spec": {
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
        },
        "gpt-test": {
            "input_cost_per_token": 2e-06,
            "output_cost_per_token": 8e-06,
            "cache_read_input_token_cost": 5e-07,
            "mode": "chat",
        },
        "anthropic/claude-test": {
            "input_cost_per_token": 3e-06,
            "output_cost_per_token": 1.5e-05,
            "cache_read_input_token_cost": 3e-07,
            "cache_creation_input_token_cost": 3.75e-06,
            "input_cost_per_token_above_200k_tokens": 6e-06,
            "output_cost_per_token_above_200k_tokens": 2.25e-05,
            "cache_read_input_token_cost_above_200k_tokens": 6e-07,
            "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
            "cache_creation_input_token_cost_above_1hr": 1e-05,
            "mode": "chat",
        },
        "text-embedding-test": {
            "input_cost_per_token": 1e-07,
            "output_cost_per_token": 0.0,
            "mode": "embedding",
        },
        "gpt-5": {
            "input_cost_per_token": 9e-06,
            "output_cost_per_token": 9e-05,
        },
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "pricing-cache.json"
        self._cache_patch = patch.object(
            agentcat, "pricing_cache_file", return_value=self.cache_path
        )
        self._cache_patch.start()
        agentcat._PRICING_TABLE_MEMO = None

    def tearDown(self) -> None:
        self._cache_patch.stop()
        agentcat._PRICING_TABLE_MEMO = None
        self.tmp.cleanup()

    def _write_cache(self, fetched_at=None) -> dict:
        payload = {
            "fetchedAt": fetched_at or agentcat.now_iso(),
            "models": agentcat.litellm_price_table(self.LITELLM_FIXTURE),
        }
        agentcat.write_json_atomic(self.cache_path, payload)
        return payload

    def test_per_token_rates_convert_to_per_million(self) -> None:
        table = agentcat.litellm_price_table(self.LITELLM_FIXTURE)
        self.assertAlmostEqual(table["gpt-test"]["input"], 2.0)
        self.assertAlmostEqual(table["gpt-test"]["output"], 8.0)
        self.assertAlmostEqual(table["gpt-test"]["cache_read"], 0.5)
        self.assertNotIn("cache_write", table["gpt-test"])

    def test_tier_keys_parse_from_fixture(self) -> None:
        table = agentcat.litellm_price_table(self.LITELLM_FIXTURE)
        entry = table["anthropic/claude-test"]
        self.assertEqual(len(entry["tiers"]), 1)
        tier = entry["tiers"][0]
        self.assertEqual(tier["threshold"], 200_000)
        self.assertAlmostEqual(tier["input"], 6.0)
        self.assertAlmostEqual(tier["output"], 22.5)
        self.assertAlmostEqual(tier["cache_read"], 0.6)
        self.assertAlmostEqual(tier["cache_write"], 7.5)
        # The time-based "_above_1hr" cache TTL key must not become a tier:
        # 200k is the only threshold parsed from the fixture.
        self.assertEqual([t["threshold"] for t in entry["tiers"]], [200_000])
        # Provider-prefixed keys register their basename as an alias.
        self.assertEqual(table["claude-test"], entry)

    def test_skips_sample_spec_and_models_without_chat_rates(self) -> None:
        table = agentcat.litellm_price_table(self.LITELLM_FIXTURE)
        self.assertNotIn("sample_spec", table)
        self.assertNotIn("text-embedding-test", table)

    def test_no_cache_no_network_falls_back_to_bundled(self) -> None:
        with patch.object(agentcat, "_fetch_litellm_raw", side_effect=OSError("offline")):
            state = agentcat.refresh_litellm_pricing(force=True)
        self.assertEqual(state["status"], "error")
        self.assertFalse(self.cache_path.exists())
        _table, meta = agentcat.merged_pricing_table()
        self.assertEqual(meta, {"source": "bundled", "fetchedAt": None})
        cost = agentcat.estimate_cost("gpt-5", 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(cost["input"], agentcat.MODEL_PRICING["gpt-5"]["input"])

    def test_merge_precedence_litellm_wins_and_bundled_fills_gaps(self) -> None:
        payload = self._write_cache()
        table, meta = agentcat.merged_pricing_table()
        self.assertEqual(meta["source"], "litellm")
        self.assertEqual(meta["fetchedAt"], payload["fetchedAt"])
        # LiteLLM rate overrides the stale bundled gpt-5 entry...
        self.assertAlmostEqual(table["gpt-5"]["input"], 9.0)
        self.assertAlmostEqual(
            agentcat.estimate_cost("gpt-5", 1_000_000, 0, 0, 0)["input"], 9.0
        )
        # ...while bundled stays for models LiteLLM lacks.
        self.assertAlmostEqual(
            table["claude-haiku-4-5"]["input"],
            agentcat.MODEL_PRICING["claude-haiku-4-5"]["input"],
        )

    def test_fetch_failure_keeps_existing_cache(self) -> None:
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat().replace("+00:00", "Z")
        self._write_cache(fetched_at=stale)
        with patch.object(agentcat, "_fetch_litellm_raw", side_effect=OSError("offline")):
            state = agentcat.refresh_litellm_pricing()
        self.assertEqual(state["status"], "error")
        kept = agentcat.read_pricing_cache()
        self.assertEqual(kept["fetchedAt"], stale)
        _table, meta = agentcat.merged_pricing_table()
        self.assertEqual(meta["source"], "litellm")

    def test_fresh_cache_skips_fetch_within_24h(self) -> None:
        self._write_cache()
        with patch.object(agentcat, "_fetch_litellm_raw", side_effect=AssertionError("must not fetch")):
            state = agentcat.refresh_litellm_pricing()
        self.assertEqual(state["status"], "fresh")

    def test_stale_cache_refetches_and_rewrites(self) -> None:
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat().replace("+00:00", "Z")
        self._write_cache(fetched_at=stale)
        with patch.object(agentcat, "_fetch_litellm_raw", return_value=self.LITELLM_FIXTURE):
            state = agentcat.refresh_litellm_pricing()
        self.assertEqual(state["status"], "fetched")
        kept = agentcat.read_pricing_cache()
        self.assertNotEqual(kept["fetchedAt"], stale)
        self.assertIn("gpt-test", kept["models"])

    def test_tiered_rate_applies_only_with_context_tokens(self) -> None:
        self._write_cache()
        base = agentcat.estimate_cost("claude-test", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(base["input"], 3.0)
        self.assertAlmostEqual(base["output"], 15.0)
        self.assertAlmostEqual(base["cache_read"], 0.3)
        self.assertAlmostEqual(base["cache_write"], 3.75)
        tiered = agentcat.estimate_cost(
            "claude-test", 1_000_000, 1_000_000, 1_000_000, 1_000_000,
            context_tokens=250_000,
        )
        self.assertAlmostEqual(tiered["input"], 6.0)
        self.assertAlmostEqual(tiered["output"], 22.5)
        self.assertAlmostEqual(tiered["cache_read"], 0.6)
        self.assertAlmostEqual(tiered["cache_write"], 7.5)
        below = agentcat.estimate_cost(
            "claude-test", 1_000_000, 0, 0, 0, context_tokens=100_000
        )
        self.assertAlmostEqual(below["input"], 3.0)

    def test_missing_cache_rates_default_safely(self) -> None:
        self._write_cache()
        cost = agentcat.estimate_cost("gpt-test", 0, 0, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost["cache_read"], 0.5)
        # cache_write absent from the feed entry -> falls back to input rate.
        self.assertAlmostEqual(cost["cache_write"], 2.0)

    def test_pricing_feed_capability_and_status(self) -> None:
        self.assertIn("pricing.feed", agentcat.CONNECTOR_CAPABILITIES)
        self.assertEqual(
            agentcat.pricing_status_snapshot(),
            {"source": "bundled", "fetchedAt": None},
        )
        payload = self._write_cache()
        self.assertEqual(
            agentcat.pricing_status_snapshot(),
            {"source": "litellm", "fetchedAt": payload["fetchedAt"]},
        )


if __name__ == "__main__":
    unittest.main()
