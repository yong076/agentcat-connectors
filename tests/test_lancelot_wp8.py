"""Sandboxed regression tests for Lancelot WP8 rate-limit handling."""

import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
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

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
