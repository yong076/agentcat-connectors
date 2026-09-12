import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(REPO_ROOT / "lib"))
from sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths
import agentcat_codex_app_server as app_server


LOADER = SourceFileLoader("codex_connection_agentcat_module", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("codex_connection_agentcat_module", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class FakeCodexAppServer:
    accounts = {}
    usage_payload = {
        "primary": {"usedPercent": 31, "windowDurationMins": 300, "resetsAt": 1_800_000_000},
        "secondary": {"usedPercent": 8, "windowDurationMins": 10080, "resetsAt": 1_800_500_000},
        "credits": {"hasCredits": True, "unlimited": False, "balance": "12"},
    }

    def __init__(self, profile_dir):
        self.profile_dir = Path(profile_dir)
        self.connection_id = self.profile_dir.name
        self.closed = False
        self.generation = 1
        self.alive = True

    def is_alive(self):
        return self.alive

    def login(self, mode):
        # The installed protocol uses a UUID-shaped login id, accepted by the
        # loopback route's conservative identifier validation.
        return {"loginId": self.connection_id, "authUrl": "https://auth.example/" + self.connection_id}

    def account(self):
        return self.accounts.get(self.connection_id)

    def usage(self):
        return {
            "source": "codex-app-server", "freshness": "live", "windows": [
                {"id": "primary", "usedPercent": 31, "remainingPercent": 69, "windowDurationMins": 300, "resetsAt": 1_800_000_000, "primary": True},
                {"id": "secondary", "usedPercent": 8, "remainingPercent": 92, "windowDurationMins": 10080, "resetsAt": 1_800_500_000, "primary": False},
            ], "credits": {"hasCredits": True, "unlimited": False, "balance": "12"},
            "spendControl": None, "rateLimitReachedType": None,
            "tokenUsage": {"dailyBuckets": [{"startDate": "2026-09-12", "tokens": 123}], "summary": {"lifetimeTokens": 999}},
            "tokenUsageAvailable": True,
        }

    def cancel_login(self, login_id):
        return {"status": "canceled"}

    def request(self, method, params):
        return {}

    def close(self):
        self.closed = True
        self.alive = False


class CodexConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.agentcat_home = Path(self.tmp.name) / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        agentcat._CODEX_APP_SERVERS.clear()
        FakeCodexAppServer.accounts = {}
        assert_sandboxed(agentcat, self.home, self.agentcat_home)

    def tearDown(self):
        agentcat._CODEX_APP_SERVERS.clear()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def _start(self, mode="browser"):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            return agentcat.codex_start_oauth("Personal", mode)

    def test_pending_then_connected_normalizes_native_windows_and_token_activity(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        self.assertEqual(started["status"], "pending_browser")
        self.assertTrue(started["authorizationURL"].startswith("https://auth.example/"))
        FakeCodexAppServer.accounts[row["id"]] = {"email": "me@example.com", "planType": "pro"}
        status = agentcat.codex_oauth_status(started["operationID"])
        connection = status["connection"]
        self.assertEqual(status["status"], "connected")
        self.assertEqual(connection["identity"], {"email": "me@example.com", "planType": "pro"})
        self.assertEqual(connection["usage"]["windows"][0]["windowDurationMins"], 300)
        self.assertEqual(connection["usage"]["windows"][1]["windowDurationMins"], 10080)
        self.assertEqual(connection["usage"]["tokenUsage"]["dailyBuckets"][0]["tokens"], 123)

    def test_cancel_and_registry_never_persist_raw_auth_material(self):
        started = self._start("device")
        self.assertEqual(agentcat.codex_cancel_oauth(started["operationID"]), "canceled")
        stored = json.loads(agentcat.CODEX_CONNECTIONS_FILE.read_text())
        encoded = json.dumps(stored)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("refresh_token", encoded)
        self.assertNotIn("authUrl", encoded)
        self.assertEqual(stored["connections"][0]["status"], "canceled")

    def test_account_mismatch_requires_reconnect_without_reassigning_identity(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        FakeCodexAppServer.accounts[row["id"]] = {"email": "first@example.com", "planType": "pro"}
        agentcat.codex_oauth_status(started["operationID"])
        FakeCodexAppServer.accounts[row["id"]] = {"email": "other@example.com", "planType": "plus"}
        refreshed = agentcat.codex_connection_mutation(row["id"], "refresh")
        self.assertEqual(refreshed["status"], "needs_reconnect")
        self.assertEqual(refreshed["identity"]["email"], "first@example.com")

    def test_http_routes_require_loopback_bearer_and_cancel(self):
        token = agentcat.loopback_control_token()
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(base + "/v1/connections"), timeout=3)
                self.assertEqual(denied.exception.code, 401)
                request = Request(base + "/v1/connections/codex/oauth/start", data=b'{"mode":"browser"}', method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(request, timeout=3) as response:
                    started = json.loads(response.read().decode())
                cancel = Request(base + "/v1/connections/codex/oauth/" + started["operationID"] + "/cancel", data=b"{}", method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(cancel, timeout=3) as response:
                    self.assertEqual(json.loads(response.read().decode())["status"], "canceled")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_normalization_keeps_null_distinct_from_zero_and_preserves_both_windows(self):
        usage = app_server.normalize_usage(
            {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": None, "resetsAt": None}, "secondary": {"usedPercent": 100, "windowDurationMins": 60, "resetsAt": 123}}, "rateLimitsByLimitId": {"codex": {"limitName": "Codex", "primary": {"usedPercent": 0, "windowDurationMins": None, "resetsAt": None}, "secondary": {"usedPercent": 100, "windowDurationMins": 60, "resetsAt": 123}}, "gpt-5": {"limitName": "GPT-5", "primary": {"usedPercent": 20, "windowDurationMins": 30, "resetsAt": 456}}}, "rateLimitResetCredits": {"availableCount": 2, "credits": [{"id": "opaque", "title": "Reset", "status": "available"}]}},
            {"summary": {"lifetimeTokens": 0}, "dailyUsageBuckets": [{"startDate": "2026-09-12", "tokens": 0}]},
            token_usage_available=True,
        )
        self.assertEqual(usage["windows"][0]["usedPercent"], 0.0)
        self.assertEqual(usage["windows"][0]["remainingPercent"], 100.0)
        self.assertIsNone(usage["windows"][0]["windowDurationMins"])
        self.assertEqual(usage["windows"][1]["usedPercent"], 100.0)
        self.assertEqual(len(usage["windows"]), 3)
        self.assertEqual(usage["windows"][2]["limitID"], "gpt-5")
        self.assertEqual(usage["resetCredits"]["availableCount"], 2)
        self.assertNotIn("id", usage["resetCredits"]["details"][0])
        self.assertEqual(usage["tokenUsage"]["summary"]["lifetimeTokens"], 0)
        self.assertEqual(usage["tokenUsage"]["dailyBuckets"][0]["tokens"], 0)

    def test_usage_falls_back_only_when_the_live_method_is_unsupported(self):
        server = app_server.CodexAppServer(self.agentcat_home / "profile")
        calls = []
        def request(method, params):
            calls.append(method)
            if method == "account/rateLimits/read":
                return {"rateLimits": {"primary": {"usedPercent": 5}}}
            if method == "account/usage/read":
                raise app_server.CodexAppServerUnsupported("unknown variant")
            return {"summary": {"lifetimeTokens": 77}, "dailyUsageBuckets": []}
        server.request = request
        usage = server.usage()
        self.assertEqual(calls, ["account/rateLimits/read", "account/usage/read", "account/tokenUsage/read"])
        self.assertTrue(usage["tokenUsageAvailable"])
        self.assertEqual(usage["tokenUsage"]["summary"]["lifetimeTokens"], 77)

    def test_exited_pending_child_becomes_terminal_failed_without_restart(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        child = agentcat._CODEX_APP_SERVERS[row["id"]]
        child.alive = False
        result = agentcat.codex_oauth_status(started["operationID"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(row["id"], agentcat._CODEX_APP_SERVERS)
        self.assertIn("interrupted", result["error"])

    def test_installed_app_server_unauthenticated_smoke_uses_isolated_profile(self):
        if not Path("/opt/homebrew/bin/codex").exists():
            self.skipTest("installed Codex app-server unavailable")
        profile = self.agentcat_home / "isolated-codex-profile"
        server = app_server.CodexAppServer(profile, executable="/opt/homebrew/bin/codex")
        try:
            self.assertIsNone(server.account())
            self.assertTrue(profile.is_dir())
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
