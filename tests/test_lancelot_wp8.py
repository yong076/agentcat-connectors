"""Sandboxed regression tests for Lancelot WP8 rate-limit handling."""

import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from contextlib import closing, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from tests.sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_lancelot_wp8", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_lancelot_wp8", LOADER)
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


class LancelotWP8TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.agentcat_home = self.root / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        assert_sandboxed(agentcat, self.home, self.agentcat_home)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.network = patch.object(
            agentcat.urllib.request,
            "urlopen",
            side_effect=AssertionError("network must be mocked in WP8 tests"),
        )
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def write_claude_credentials(self, oauth):
        agentcat.CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = agentcat.CLAUDE_CONFIG_DIR / ".credentials.json"
        original = {"claudeAiOauth": oauth}
        path.write_text(json.dumps(original), encoding="utf-8")
        return path, original


class ClaudeOAuthRefreshTests(LancelotWP8TestCase):
    def test_refresh_request_is_form_encoded_with_ten_second_timeout(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return FakeResponse({"access_token": " refreshed-token "})

        with patch.object(agentcat.urllib.request, "urlopen", side_effect=fake_urlopen):
            token = agentcat.refresh_claude_access_token("refresh-token")

        self.assertEqual(token, "refreshed-token")
        request, timeout = calls[0]
        self.assertEqual(request.full_url, agentcat.CLAUDE_OAUTH_TOKEN_URL)
        self.assertEqual(timeout, 10)
        self.assertEqual(
            urllib.parse.parse_qs(request.data.decode("utf-8")),
            {
                "grant_type": ["refresh_token"],
                "refresh_token": ["refresh-token"],
                "client_id": [agentcat.CLAUDE_OAUTH_CLIENT_ID],
            },
        )
        self.assertEqual(request.headers["Content-type"], "application/x-www-form-urlencoded")

    def test_near_expiry_refreshes_in_memory_before_usage_request(self):
        credentials_path, original = self.write_claude_credentials(
            {
                "accessToken": "old-token",
                "refreshToken": "refresh-token",
                "expiresAt": int(time.time() * 1000) + 60_000,
            }
        )
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            if request.full_url == agentcat.CLAUDE_OAUTH_TOKEN_URL:
                return FakeResponse({"access_token": "new-token"})
            self.assertEqual(request.get_header("Authorization"), "Bearer new-token")
            return FakeResponse({"five_hour": {"utilization": 12, "resets_at": 1770000100}})

        with patch.object(agentcat.sys, "platform", "linux"), patch.object(
            agentcat.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            limits = agentcat.claude_live_limits(force=True)

        self.assertEqual(limits["status"], "auto")
        self.assertEqual([request.full_url for request, _timeout in requests], [
            agentcat.CLAUDE_OAUTH_TOKEN_URL,
            agentcat.CLAUDE_USAGE_URL,
        ])
        self.assertEqual(json.loads(credentials_path.read_text(encoding="utf-8")), original)

    def test_refresh_failure_uses_token_expired_path_without_usage_request(self):
        self.write_claude_credentials(
            {
                "accessToken": "old-token",
                "refreshToken": "refresh-token",
                "expiresAt": 1,
            }
        )

        with patch.object(agentcat.sys, "platform", "linux"), patch.object(
            agentcat.urllib.request, "urlopen", side_effect=OSError("refresh failed")
        ) as urlopen:
            limits = agentcat.claude_live_limits(force=True)

        self.assertEqual(limits["status"], "not_configured")
        self.assertEqual(limits["reason"], "token_expired")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.args[0].full_url, agentcat.CLAUDE_OAUTH_TOKEN_URL)


class LiveLimitErrorTests(LancelotWP8TestCase):
    def http_error(self, status, body=b"", headers=None):
        return urllib.error.HTTPError(
            "https://usage.example.test",
            status,
            "failed",
            headers or {},
            io.BytesIO(body),
        )

    def test_http_statuses_map_to_stable_reason_codes(self):
        with patch.object(agentcat.time, "time", return_value=1_000):
            rate_limited = agentcat.live_limit_error(
                self.http_error(429, headers={"Retry-After": "120"}), "usage-api"
            )
            default_retry = agentcat.live_limit_error(self.http_error(429), "usage-api")

        self.assertEqual(rate_limited["reason"], "rate_limited")
        self.assertEqual(rate_limited["retryAt"], 1_120)
        self.assertEqual(default_retry["retryAt"], 1_300)
        self.assertEqual(agentcat.live_limit_error(self.http_error(401), "x")["reason"], "token_expired")
        self.assertEqual(
            agentcat.live_limit_error(self.http_error(403, b'missing "user:profile" scope'), "x")["reason"],
            "missing_scope",
        )
        self.assertEqual(agentcat.live_limit_error(self.http_error(403), "x")["reason"], "token_expired")
        self.assertEqual(agentcat.live_limit_error(self.http_error(503), "x")["reason"], "server_error")

    def test_parse_and_network_failures_have_distinct_reasons(self):
        parse_error = json.JSONDecodeError("bad JSON", "{", 0)
        self.assertEqual(agentcat.live_limit_error(parse_error, "x")["reason"], "parse_error")
        self.assertEqual(
            agentcat.live_limit_error(urllib.error.URLError("DNS failed"), "x")["reason"],
            "network_error",
        )
        self.assertEqual(agentcat.live_limit_error(TimeoutError("timed out"), "x")["reason"], "network_error")

    def test_retry_after_cache_blocks_until_retry_time(self):
        with patch.object(agentcat.time, "time", return_value=1_000):
            limits = agentcat.live_limit_error(self.http_error(429), "usage-api")
            agentcat.write_live_limits_cache("claude", limits)

        with patch.object(agentcat.time, "time", return_value=1_299):
            cached = agentcat.cached_live_limits("claude", -1)
        with patch.object(agentcat.time, "time", return_value=1_301):
            expired = agentcat.cached_live_limits("claude", 10_000)

        self.assertEqual(cached["reason"], "rate_limited")
        self.assertEqual(cached["retryAt"], 1_300)
        self.assertIsNone(expired)

    def test_retry_after_is_persisted_when_serving_stale_limits(self):
        good = agentcat.empty_limits(status="auto")
        good["source"] = "usage-api"
        good["quotas"] = [{"id": "claude:5h", "usedPercent": 10.0}]
        with patch.object(agentcat.time, "time", return_value=1_000):
            agentcat.write_live_limits_cache("claude", good)
        with patch.object(agentcat.time, "time", return_value=2_000):
            stale = agentcat.cached_live_limits("claude", 1, allow_stale=True)
            served = agentcat.serve_stale_live_limits(
                "claude", stale, self.http_error(429, headers={"Retry-After": "120"})
            )
        with patch.object(agentcat.time, "time", return_value=2_050):
            cached = agentcat.cached_live_limits("claude", -1)

        self.assertTrue(served["stale"])
        self.assertEqual(served["liveErrorReason"], "rate_limited")
        self.assertEqual(cached["status"], "auto")
        self.assertEqual(cached["retryAt"], 2_120)


class ClaudeUnauthorizedRefreshTests(LancelotWP8TestCase):
    def test_unauthorized_usage_refreshes_once_then_retries(self):
        self.write_claude_credentials(
            {
                "accessToken": "old-token",
                "refreshToken": "refresh-token",
                "expiresAt": int(time.time() * 1000) + 3_600_000,
            }
        )
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
            if request.full_url == agentcat.CLAUDE_OAUTH_TOKEN_URL:
                return FakeResponse({"access_token": "new-token"})
            self.assertEqual(request.get_header("Authorization"), "Bearer new-token")
            return FakeResponse({"five_hour": {"utilization": 5}})

        with patch.object(agentcat.sys, "platform", "linux"), patch.object(
            agentcat.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            limits = agentcat.claude_live_limits(force=True)

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(len(calls), 3)

    def test_second_unauthorized_response_reports_token_expired(self):
        self.write_claude_credentials(
            {
                "accessToken": "old-token",
                "refreshToken": "refresh-token",
                "expiresAt": int(time.time() * 1000) + 3_600_000,
            }
        )
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            if request.full_url == agentcat.CLAUDE_OAUTH_TOKEN_URL:
                return FakeResponse({"access_token": "new-token"})
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

        with patch.object(agentcat.sys, "platform", "linux"), patch.object(
            agentcat.urllib.request, "urlopen", side_effect=fake_urlopen
        ):
            limits = agentcat.claude_live_limits(force=True)

        self.assertEqual(limits["status"], "error")
        self.assertEqual(limits["reason"], "token_expired")
        self.assertEqual(len(calls), 3)


class LiveLimitBackoffTests(LancelotWP8TestCase):
    def cache_entry(self, provider="claude"):
        return json.loads(agentcat.LIVE_LIMITS_CACHE.read_text(encoding="utf-8"))[provider]

    def test_backoff_curve_starts_at_thirty_seconds_and_caps_at_nine_hundred(self):
        self.assertEqual(
            [agentcat.live_limits_error_backoff_seconds(streak) for streak in range(1, 8)],
            [30, 60, 120, 240, 480, 900, 900],
        )

    def test_consecutive_failures_increment_provider_streak_and_extend_backoff(self):
        failure = agentcat.live_limit_error(RuntimeError("failed"), "usage-api")
        with patch.object(agentcat.time, "time", return_value=1_000):
            agentcat.write_live_limits_cache("claude", failure)
        self.assertEqual(self.cache_entry()["failureStreak"], 1)

        with patch.object(agentcat.time, "time", return_value=1_029):
            self.assertIsNotNone(agentcat.cached_live_limits("claude", -1))
        with patch.object(agentcat.time, "time", return_value=1_031):
            self.assertIsNone(agentcat.cached_live_limits("claude", 10_000))
            agentcat.write_live_limits_cache("claude", failure)
        self.assertEqual(self.cache_entry()["failureStreak"], 2)

        with patch.object(agentcat.time, "time", return_value=1_090):
            self.assertIsNotNone(agentcat.cached_live_limits("claude", -1))
        with patch.object(agentcat.time, "time", return_value=1_092):
            self.assertIsNone(agentcat.cached_live_limits("claude", 10_000))

    def test_success_resets_failure_streak(self):
        failure = agentcat.live_limit_error(RuntimeError("failed"), "usage-api")
        with patch.object(agentcat.time, "time", return_value=1_000):
            agentcat.write_live_limits_cache("claude", failure)
        with patch.object(agentcat.time, "time", return_value=1_031):
            agentcat.write_live_limits_cache("claude", failure)

        success = agentcat.empty_limits(status="auto")
        success["quotas"] = [{"id": "claude:5h", "usedPercent": 10.0}]
        with patch.object(agentcat.time, "time", return_value=1_100):
            agentcat.write_live_limits_cache("claude", success)

        self.assertEqual(self.cache_entry()["failureStreak"], 0)
        with patch.object(agentcat.time, "time", return_value=1_200):
            self.assertIsNotNone(agentcat.cached_live_limits("claude", 200))

    def test_stale_result_failures_also_increment_streak(self):
        success = agentcat.empty_limits(status="auto")
        success["source"] = "usage-api"
        success["quotas"] = [{"id": "claude:5h", "usedPercent": 10.0}]
        with patch.object(agentcat.time, "time", return_value=1_000):
            agentcat.write_live_limits_cache("claude", success)
        with patch.object(agentcat.time, "time", return_value=2_000):
            stale = agentcat.cached_live_limits("claude", 1, allow_stale=True)
            agentcat.serve_stale_live_limits("claude", stale, OSError("offline"))

        self.assertEqual(self.cache_entry()["failureStreak"], 1)

    def test_not_applicable_keeps_five_minute_ttl_without_a_failure_streak(self):
        limits = agentcat.empty_limits(reason="not_applicable")
        with patch.object(agentcat.time, "time", return_value=1_000):
            agentcat.write_live_limits_cache("claude", limits)
        self.assertEqual(self.cache_entry()["failureStreak"], 0)

        with patch.object(agentcat.time, "time", return_value=1_299):
            self.assertIsNotNone(agentcat.cached_live_limits("claude", 10_000))
        with patch.object(agentcat.time, "time", return_value=1_301):
            self.assertIsNone(agentcat.cached_live_limits("claude", 10_000))


class ClaudeStatuslineThrottleTests(LancelotWP8TestCase):
    def run_statusline(self, payload, now):
        with patch.object(agentcat, "read_stdin_payload", return_value=payload), patch.object(
            agentcat.time, "time", return_value=now
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(agentcat.command_claude_statusline(agentcat.argparse.Namespace()), 0)

    def event_count(self):
        with closing(sqlite3.connect(agentcat.EVENTS_DB)) as conn:
            return conn.execute(
                "select count(*) from events where source = 'claude-statusline'"
            ).fetchone()[0]

    def test_per_session_floor_and_duplicate_window_drop_streaming_updates(self):
        first = {"session_id": "session-a", "model": "claude", "rate_limits": {"five_hour": 10}}
        changed = {"session_id": "session-a", "model": "claude", "rate_limits": {"five_hour": 11}}

        self.run_statusline(first, 1_000)
        self.run_statusline(changed, 1_010)
        self.run_statusline(first, 1_016)
        self.run_statusline(first, 1_030)

        self.assertEqual(self.event_count(), 2)

    def test_different_sessions_have_independent_floors(self):
        self.run_statusline({"session_id": "session-a", "model": "claude"}, 1_000)
        self.run_statusline({"session_id": "session-b", "model": "claude"}, 1_001)

        self.assertEqual(self.event_count(), 2)

    def test_missing_session_id_falls_back_to_parent_process(self):
        with patch.object(agentcat.os, "getppid", return_value=4242):
            self.run_statusline({"model": "claude-a"}, 1_000)
            self.run_statusline({"model": "claude-b"}, 1_001)

        self.assertEqual(self.event_count(), 1)


class CodexWindowClassificationTests(LancelotWP8TestCase):
    def write_runtime_limits(self, primary, secondary):
        session_dir = self.home / ".codex" / "sessions" / "2026" / "09" / "04"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "rollout-wp8.jsonl").write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "token_count",
                        "rate_limits": {"primary": primary, "secondary": secondary},
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_runtime_windows_accept_one_minute_duration_drift(self):
        self.write_runtime_limits(
            {"used_percent": 11, "window_minutes": 299},
            {"used_percent": 22, "window_minutes": 10081},
        )

        limits = agentcat.codex_runtime_limits()

        self.assertEqual(limits["shortUsedPercent"], 11.0)
        self.assertEqual(limits["shortWindowMinutes"], 299)
        self.assertEqual(limits["weeklyUsedPercent"], 22.0)

    def test_runtime_unknown_durations_fall_back_to_field_position(self):
        self.write_runtime_limits(
            {"used_percent": 11, "window_minutes": 1440},
            {"used_percent": 22, "window_minutes": 60},
        )

        limits = agentcat.codex_runtime_limits()

        self.assertEqual(limits["shortUsedPercent"], 11.0)
        self.assertEqual(limits["weeklyUsedPercent"], 22.0)

    def test_http_windows_use_tolerance_and_positional_fallback(self):
        tolerant = agentcat.codex_limits_from_usage_response(
            {
                "rate_limit": {
                    "primary_window": {"used_percent": 11, "limit_window_seconds": 299 * 60},
                    "secondary_window": {"used_percent": 22, "limit_window_seconds": 10081 * 60},
                }
            }
        )
        positional = agentcat.codex_limits_from_usage_response(
            {
                "rate_limit": {
                    "primary_window": {"used_percent": 33, "limit_window_seconds": 86400},
                    "secondary_window": {"used_percent": 44, "limit_window_seconds": 3600},
                }
            }
        )

        self.assertEqual([quota["id"] for quota in tolerant["quotas"]], ["codex:5h", "codex:7d"])
        self.assertEqual(tolerant["shortUsedPercent"], 11.0)
        self.assertEqual(tolerant["weeklyUsedPercent"], 22.0)
        self.assertEqual([quota["id"] for quota in positional["quotas"]], ["codex:5h", "codex:7d"])
        self.assertEqual(positional["shortUsedPercent"], 33.0)
        self.assertEqual(positional["weeklyUsedPercent"], 44.0)

    def test_codex_http_requests_use_cli_compatibility_headers(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({})

        with patch.object(agentcat.urllib.request, "urlopen", side_effect=fake_urlopen):
            agentcat.codex_usage_request("token", "account", agentcat.CODEX_USAGE_URLS[0])
            agentcat.codex_reset_credits_request("token", "account")

        self.assertEqual(len(requests), 2)
        for request, timeout in requests:
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["user-agent"], "codex-cli")
            self.assertEqual(headers["openai-beta"], "codex-1")
            self.assertEqual(headers["chatgpt-account-id"], "account")
            self.assertEqual(timeout, 12)


class ClaudeCredentialSourceTests(LancelotWP8TestCase):
    def test_scoped_keychain_service_is_read_before_legacy(self):
        config_dir = self.root / "claude-work"
        config_dir.mkdir()
        expected_hash = agentcat.hashlib.sha256(
            str(config_dir.resolve()).encode("utf-8")
        ).hexdigest()[:8]

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}), patch.object(
            agentcat.sys, "platform", "darwin"
        ), patch.object(
            agentcat.subprocess,
            "check_output",
            return_value=json.dumps({"claudeAiOauth": {"accessToken": "scoped-token"}}),
        ) as check_output:
            credentials = agentcat.read_claude_oauth_credentials()

        self.assertEqual(credentials["oauth"]["accessToken"], "scoped-token")
        self.assertEqual(credentials["credentialSource"], "scoped-keychain")
        self.assertEqual(check_output.call_count, 1)
        command = check_output.call_args.args[0]
        self.assertEqual(command[command.index("-s") + 1], f"Claude Code-credentials-{expected_hash}")

    def test_legacy_keychain_is_fallback_for_missing_scoped_item(self):
        config_dir = self.root / "claude-work"
        config_dir.mkdir()
        services = []

        def fake_check_output(command, **_kwargs):
            service = command[command.index("-s") + 1]
            services.append(service)
            if len(services) == 1:
                raise agentcat.subprocess.CalledProcessError(44, command)
            return json.dumps({"claudeAiOauth": {"accessToken": "legacy-token"}})

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}), patch.object(
            agentcat.sys, "platform", "darwin"
        ), patch.object(agentcat.subprocess, "check_output", side_effect=fake_check_output):
            credentials = agentcat.read_claude_oauth_credentials()

        self.assertEqual(credentials["oauth"]["accessToken"], "legacy-token")
        self.assertEqual(credentials["credentialSource"], "legacy-keychain")
        self.assertEqual(services[1], "Claude Code-credentials")

    def test_credentials_file_source_is_reported_in_live_limits(self):
        self.write_claude_credentials(
            {"accessToken": "file-token", "expiresAt": int(time.time() * 1000) + 3_600_000}
        )
        with patch.object(agentcat.sys, "platform", "linux"), patch.object(
            agentcat.urllib.request,
            "urlopen",
            return_value=FakeResponse({"five_hour": {"utilization": 7}}),
        ):
            limits = agentcat.claude_live_limits(force=True)

        self.assertEqual(limits["status"], "auto")
        self.assertEqual(limits["credentialSource"], "credentials-file")
        cached = agentcat.cached_live_limits("claude", agentcat.LIVE_LIMITS_MAX_AGE_SECONDS)
        self.assertEqual(cached["credentialSource"], "credentials-file")


if __name__ == "__main__":
    unittest.main()
