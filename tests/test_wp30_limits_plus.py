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

    def test_consume_request_uses_selected_credit_nonce_and_codex_headers(self):
        observed = {}

        def fake_urlopen(request, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return FakeResponse({"code": "reset", "windows_reset": 2})

        with patch.object(agentcat.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = agentcat.codex_reset_credit_consume_request(
                "access-token", "account-id", "credit-id", "confirmation-nonce"
            )

        request = observed["request"]
        self.assertEqual(request.full_url, agentcat.CODEX_RESET_CREDITS_CONSUME_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "credit_id": "credit-id",
            "redeem_request_id": "confirmation-nonce",
        })
        self.assertEqual(request.get_header("Authorization"), "Bearer access-token")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "account-id")
        self.assertEqual(result["windows_reset"], 2)

    def test_consume_refreshes_limits_and_logs_identity_free_diagnostic(self):
        auth = {"tokens": {"access_token": "token", "account_id": "account-secret"}}
        refreshed = agentcat.empty_limits(status="auto")
        refreshed["quotas"] = [{"id": "codex:5h", "remainingPercent": 100.0}]
        consumed = {
            "code": "reset",
            "windows_reset": 1,
            "credit": {
                "id": "credit-secret",
                "status": "redeemed",
                "redeemed_at": "2026-09-05T00:00:00Z",
                "profile_user_id": "identity-secret",
            },
        }
        events = []
        with patch.object(agentcat, "read_codex_auth", return_value=auth), patch.object(
            agentcat, "codex_reset_credit_consume_request", return_value=consumed
        ) as request, patch.object(
            agentcat, "codex_live_limits", return_value=refreshed
        ) as refresh, patch.object(
            agentcat, "store_event", side_effect=lambda *args: events.append(args) or {"id": 1}
        ):
            result = agentcat.consume_codex_reset_credit("credit-secret", "nonce-secret")

        request.assert_called_once_with("token", "account-secret", "credit-secret", "nonce-secret")
        refresh.assert_called_once_with(force=True)
        self.assertEqual(result["windowsReset"], 1)
        self.assertEqual(result["credit"]["id"], "credit-secret")
        self.assertEqual(result["limits"], refreshed)
        diagnostic_text = json.dumps(events)
        self.assertNotIn("account-secret", diagnostic_text)
        self.assertNotIn("credit-secret", diagnostic_text)
        self.assertNotIn("nonce-secret", diagnostic_text)
        self.assertEqual(events[0][3], {"ok": True, "windowsReset": 1})

    def test_http_consume_is_host_guarded_and_validates_confirmation(self):
        server = agentcat.ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request(
                "POST",
                "/providers/codex/reset-credits/consume",
                body=json.dumps({"creditId": "credit"}),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request(
                "POST",
                "/providers/codex/reset-credits/consume",
                body=json.dumps({"creditId": "credit", "confirmation": "nonce"}),
                headers={"Content-Type": "application/json", "Host": "evil.example"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()

            expected = {"ok": True, "windowsReset": 2, "credit": {"id": "credit"}, "limits": {}}
            with patch.object(agentcat, "consume_codex_reset_credit", return_value=expected) as consume:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request(
                    "POST",
                    "/providers/codex/reset-credits/consume",
                    body=json.dumps({"creditId": "credit", "confirmation": "nonce"}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
                connection.close()
            self.assertEqual(payload, expected)
            consume.assert_called_once_with("credit", "nonce")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_cli_shape_generates_confirmation_nonce(self):
        args = agentcat.build_parser().parse_args(
            ["codex", "reset-credit", "consume", "credit-id"]
        )
        output = io.StringIO()
        with patch.object(
            agentcat, "consume_codex_reset_credit", return_value={"ok": True}
        ) as consume, patch.object(agentcat.uuid, "uuid4", return_value="generated-nonce"), redirect_stdout(output):
            status = args.func(args)

        self.assertEqual(status, 0)
        consume.assert_called_once_with("credit-id", "generated-nonce")
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})


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


if __name__ == "__main__":
    unittest.main()
