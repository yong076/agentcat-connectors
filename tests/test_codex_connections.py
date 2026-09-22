import importlib.util
import base64
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import Mock, patch
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
    attempts = {}
    fail_logout = False
    instances = []
    fail_promotion_for = set()
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
        self.instances.append(self)

    def is_alive(self):
        return self.alive

    def login(self, mode):
        # The installed protocol uses a UUID-shaped login id, accepted by the
        # loopback route's conservative identifier validation.
        attempt = self.attempts.get(self.connection_id, 0) + 1
        self.attempts[self.connection_id] = attempt
        login = {"loginId": self.connection_id + format(attempt, "x")}
        if mode == "device":
            login.update({"verificationUrl": "https://auth.example/device", "userCode": "TEST-CODE"})
        else:
            login["authUrl"] = "https://auth.example/" + self.connection_id
        return login

    def account(self):
        if self.connection_id in self.fail_promotion_for:
            return None
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
        if method == "account/logout" and self.fail_logout:
            raise app_server.CodexAppServerError("logout failed")
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
        self.prerequisite = patch.object(agentcat, "app_server_prerequisite", return_value=None)
        self.prerequisite.start()
        agentcat._CODEX_APP_SERVERS.clear()
        agentcat._CODEX_COMPLETED_OPERATIONS.clear()
        FakeCodexAppServer.accounts = {}
        FakeCodexAppServer.attempts = {}
        FakeCodexAppServer.fail_logout = False
        FakeCodexAppServer.instances = []
        FakeCodexAppServer.fail_promotion_for = set()
        assert_sandboxed(agentcat, self.home, self.agentcat_home)

    def tearDown(self):
        self.prerequisite.stop()
        agentcat._CODEX_APP_SERVERS.clear()
        agentcat._CODEX_COMPLETED_OPERATIONS.clear()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def _start(self, mode="browser"):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            return agentcat.codex_start_oauth("Personal", mode)

    def _write_managed_auth(self, row, account="account-a", user="user-a", membership=None):
        def token(payload):
            encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
            return "fixture." + encoded + ".fixture"
        auth_claims = {"chatgpt_account_id": account, "chatgpt_user_id": user}
        access_claims = {"chatgpt_account_id": account}
        if membership:
            access_claims["chatgpt_account_user_id"] = membership
        profile = agentcat._codex_profile_dir(row["id"])
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "auth.json").write_text(json.dumps({"tokens": {"account_id": account, "id_token": token({"https://api.openai.com/auth": auth_claims}), "access_token": token({"https://api.openai.com/auth": access_claims})}}))

    def _connect_with_native_identity(self, started, account="account-a", user="user-a", membership=None):
        row = next(item for item in agentcat._codex_connections() if item.get("operationID") == started["operationID"])
        self._write_managed_auth(row, account, user, membership)
        FakeCodexAppServer.accounts[row["id"]] = {"email": f"{row['id']}@example.invalid", "planType": "pro"}
        return agentcat.codex_oauth_status(started["operationID"]), row

    def test_pending_then_connected_normalizes_native_windows_and_token_activity(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        self.assertEqual(started["status"], "pending_browser")
        self.assertTrue(started["authorizationURL"].startswith("https://auth.example/"))
        FakeCodexAppServer.accounts[row["id"]] = {"email": "me@example.com", "planType": "pro"}
        status = agentcat.codex_oauth_status(started["operationID"])
        connection = status["connection"]
        self.assertEqual(status["status"], "connected")
        self.assertEqual(connection["identity"]["email"], "me@example.com")
        self.assertTrue(connection["identity"]["verification"])
        self.assertEqual(connection["usage"]["windows"][0]["windowDurationMins"], 300)
        self.assertEqual(connection["usage"]["windows"][1]["windowDurationMins"], 10080)
        self.assertEqual(connection["usage"]["tokenUsage"]["dailyBuckets"][0]["tokens"], 123)

    def test_list_refresh_throttles_connected_profile_but_explicit_refresh_is_unbounded(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        FakeCodexAppServer.accounts[row["id"]] = {"email": "me@example.com", "planType": "pro"}
        agentcat.codex_oauth_status(started["operationID"])

        # Completion set lastSyncAt. A normal protected list returns the cached
        # connected row without another app-server usage request.
        with patch.object(agentcat, "_codex_refresh_row", side_effect=AssertionError("implicit list probe")):
            listed = agentcat.codex_connections_snapshot()
        self.assertEqual(listed[0]["status"], "connected")

        # Once the durable attempt timestamp is older than the short cadence,
        # list may refresh. An explicit POST refresh remains immediate.
        row = agentcat._codex_connections()[0]
        row["lastSyncAt"] = "2000-01-01T00:00:00Z"
        agentcat._write_codex_connections([row])
        with patch.object(agentcat, "_codex_refresh_row", return_value=True) as implicit:
            agentcat.codex_connections_snapshot()
        implicit.assert_called_once()
        cached_before = agentcat.codex_connections_snapshot(allow_implicit_refresh=False)
        with patch.object(agentcat, "_codex_refresh_row", side_effect=AssertionError("cache-only probe")):
            cached_after = agentcat.codex_connections_snapshot(allow_implicit_refresh=False)
        self.assertEqual(cached_after, cached_before)
        self.assertEqual(cached_after[0]["id"], row["id"])
        self.assertEqual(cached_after[0]["lastSuccessfulSyncAt"], row["lastSuccessfulSyncAt"])
        self.assertEqual(cached_after[0]["usage"], row["usage"])
        with patch.object(agentcat, "_codex_refresh_row", return_value=True) as explicit:
            agentcat.codex_connections_snapshot(refresh=True)
        explicit.assert_called_once()

    def test_completed_operation_remains_idempotently_readable_for_the_live_lease(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        FakeCodexAppServer.accounts[row["id"]] = {"email": "me@example.com", "planType": "pro"}

        first = agentcat.codex_oauth_status(started["operationID"])
        repeated = agentcat.codex_oauth_status(started["operationID"])

        self.assertEqual(first["status"], "connected")
        self.assertEqual(repeated["status"], "connected")
        self.assertEqual(repeated["connection"]["id"], row["id"])
        self.assertEqual(repeated["connection"]["identity"]["email"], "me@example.com")
        self.assertEqual(repeated["connection"]["usage"]["tokenUsage"]["summary"]["lifetimeTokens"], 999)
        self.assertNotIn("operationID", repeated["connection"])
        remembered = agentcat._CODEX_COMPLETED_OPERATIONS[started["operationID"]]
        self.assertEqual(remembered[0], row["id"])
        self.assertEqual(len(remembered), 2)

    def test_cancel_and_registry_never_persist_raw_auth_material(self):
        started = self._start("device")
        self.assertEqual(agentcat.codex_cancel_oauth(started["operationID"]), "canceled")
        stored = json.loads(agentcat.CODEX_CONNECTIONS_FILE.read_text())
        encoded = json.dumps(stored)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("refresh_token", encoded)
        self.assertNotIn("authUrl", encoded)
        self.assertEqual(stored["connections"][0]["status"], "canceled")

    def test_pending_status_can_resume_only_from_live_memory_and_never_registry(self):
        started = self._start()
        status = agentcat.codex_oauth_status(started["operationID"])
        self.assertEqual(status["status"], "pending_browser")
        self.assertEqual(status["resume"], {"mode": "browser", "authorizationURL": started["authorizationURL"]})
        self.assertIn("expiresAt", status["lease"])
        stored = agentcat.CODEX_CONNECTIONS_FILE.read_text()
        self.assertNotIn("authorizationURL", stored)
        self.assertNotIn("auth.example", stored)

    def test_retry_reuses_one_terminal_connection_and_its_profile(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            started = agentcat.codex_start_oauth("Personal")
            row = agentcat._codex_connections()[0]
            self.assertEqual(agentcat.codex_cancel_oauth(started["operationID"]), "canceled")
            retried = agentcat.codex_retry_oauth(row["id"], "browser")
        rows = agentcat._codex_connections()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row["id"])
        self.assertEqual(rows[0]["status"], "pending_browser")
        self.assertNotEqual(retried["operationID"], started["operationID"])

    def test_retry_rejects_connected_or_pending_connection(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        with self.assertRaisesRegex(RuntimeError, "codex_oauth_retry_not_allowed"):
            agentcat.codex_retry_oauth(row["id"])

    def test_parallel_retry_creates_one_new_pending_lease(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            started = agentcat.codex_start_oauth("Personal")
            row = agentcat._codex_connections()[0]
            agentcat.codex_cancel_oauth(started["operationID"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(agentcat.codex_retry_oauth, row["id"], "browser") for _ in range(2)]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except RuntimeError as exc:
                        outcomes.append(str(exc))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertIn("codex_oauth_retry_not_allowed", outcomes)
        rows = agentcat._codex_connections()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending_browser")
        self.assertEqual(len(agentcat._CODEX_APP_SERVERS), 1)

    def test_late_cancel_cannot_clobber_completed_connection(self):
        started = self._start()
        row = agentcat._codex_connections()[0]
        FakeCodexAppServer.accounts[row["id"]] = {"email": "me@example.com", "planType": "pro"}
        self.assertEqual(agentcat.codex_oauth_status(started["operationID"])["status"], "connected")
        self.assertEqual(agentcat.codex_cancel_oauth(started["operationID"]), "notFound")
        persisted = agentcat._codex_connections()[0]
        self.assertEqual(persisted["status"], "connected")
        self.assertEqual(persisted["identity"]["email"], "me@example.com")

    def test_capability_probe_reports_missing_official_app_server(self):
        with patch.object(agentcat, "app_server_prerequisite", return_value="codex_app_server_not_installed"):
            payload = agentcat.codex_connection_capabilities()
        self.assertTrue(payload["supported"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "codex_app_server_not_installed")
        self.assertEqual(payload["modes"], [])

    def test_capability_probe_hides_unsupported_installed_cli(self):
        with patch.object(agentcat, "app_server_prerequisite", return_value="codex_app_server_unsupported"):
            payload = agentcat.codex_connection_capabilities()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "codex_app_server_unsupported")

    def test_app_server_probe_is_bounded_and_cached(self):
        old_cache = app_server._app_server_probe_cache
        app_server._app_server_probe_cache = None
        try:
            with patch.object(app_server, "_app_server_executable", return_value="/tmp/codex"), \
                 patch.object(app_server.subprocess, "run", return_value=Mock(returncode=2)) as run:
                self.assertEqual(app_server.app_server_prerequisite(), "codex_app_server_unsupported")
                self.assertEqual(app_server.app_server_prerequisite(), "codex_app_server_unsupported")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs["timeout"], 3)
        finally:
            app_server._app_server_probe_cache = old_cache

    def test_retry_does_not_start_over_a_profile_that_failed_logout(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            started = agentcat.codex_start_oauth("Personal")
            row = agentcat._codex_connections()[0]
            FakeCodexAppServer.accounts[row["id"]] = {"email": "first@example.com", "planType": "pro"}
            agentcat.codex_oauth_status(started["operationID"])
            FakeCodexAppServer.accounts[row["id"]] = {"email": "other@example.com", "planType": "plus"}
            agentcat.codex_connection_mutation(row["id"], "refresh")
            FakeCodexAppServer.fail_logout = True
            with self.assertRaisesRegex(RuntimeError, "codex_oauth_retry_logout_failed"):
                agentcat.codex_retry_oauth(row["id"])
        stored = agentcat._codex_connections()[0]
        self.assertEqual(stored["status"], "needs_reconnect")
        self.assertEqual(stored["identity"]["email"], "first@example.com")
        self.assertIn("Could not prepare", stored["error"])

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
        # This mirrors the native app: it reads the token before sending its
        # first connection request, so daemon startup must bootstrap it.
        self.assertFalse(agentcat.LOOPBACK_CONTROL_TOKEN_FILE.exists())
        agentcat.initialize_loopback_control()
        token = agentcat.LOOPBACK_CONTROL_TOKEN_FILE.read_text().strip()
        self.assertTrue(token)
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(base + "/v1/connections"), timeout=3)
                self.assertEqual(denied.exception.code, 401)
                cached_request = Request(base + "/v1/connections?refresh=false", headers={"Authorization": f"Bearer {token}"})
                with patch.object(agentcat, "codex_connections_snapshot", wraps=agentcat.codex_connections_snapshot) as cached_snapshot:
                    with urlopen(cached_request, timeout=3) as response:
                        self.assertIn("connections", json.loads(response.read().decode()))
                cached_snapshot.assert_called_once_with(allow_implicit_refresh=False)
                request = Request(base + "/v1/connections/codex/oauth/start", data=b'{"mode":"browser"}', method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(request, timeout=3) as response:
                    started = json.loads(response.read().decode())
                status_request = Request(base + "/v1/connections/codex/oauth/" + started["operationID"], headers={"Authorization": f"Bearer {token}"})
                with urlopen(status_request, timeout=3) as response:
                    pending = json.loads(response.read().decode())
                self.assertEqual(pending["resume"]["mode"], "browser")
                self.assertIn("expiresAt", pending["lease"])
                cancel = Request(base + "/v1/connections/codex/oauth/" + started["operationID"] + "/cancel", data=b"{}", method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(cancel, timeout=3) as response:
                    self.assertEqual(json.loads(response.read().decode())["status"], "canceled")
                connection_id = agentcat._codex_connections()[0]["id"]
                retry = Request(base + "/v1/connections/codex/" + connection_id + "/oauth/retry", data=b'{"mode":"device"}', method="POST", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                with urlopen(retry, timeout=3) as response:
                    retried = json.loads(response.read().decode())
                self.assertEqual(retried["status"], "pending_device")
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

    def test_same_managed_codex_member_promotes_new_profile_and_keeps_durable_alias(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            first, canonical_row = self._connect_with_native_identity(self._start(), membership="member-a")
            second_start = self._start()
            second, incoming_row = self._connect_with_native_identity(second_start, membership="member-a")
        self.assertEqual(first["status"], "connected")
        self.assertEqual(second["status"], "connected")
        self.assertEqual(second["connection"]["id"], canonical_row["id"])
        self.assertEqual(second["supersededConnectionID"], incoming_row["id"])
        rows = agentcat._codex_connections()
        source = next(row for row in rows if row["id"] == incoming_row["id"])
        self.assertEqual(source["status"], "superseded")
        self.assertEqual(source["supersededBy"], canonical_row["id"])
        self.assertEqual((agentcat._codex_profile_dir(canonical_row["id"]) / "auth.json").read_bytes(), (agentcat._codex_profile_dir(incoming_row["id"]) / "auth.json").read_bytes())
        # A new daemon has no in-memory lease but resolves the durable source
        # operation alias to the canonical row without guessing by email.
        agentcat._CODEX_COMPLETED_OPERATIONS.clear()
        repeated = agentcat.codex_oauth_status(second_start["operationID"])
        self.assertEqual(repeated["connection"]["id"], canonical_row["id"])
        self.assertEqual(repeated["supersededConnectionID"], incoming_row["id"])
        self.assertEqual(len(agentcat.codex_connections_snapshot()), 1)
        stored = agentcat.CODEX_CONNECTIONS_FILE.read_text()
        self.assertNotIn("member-a", stored)
        self.assertNotIn("account-a", stored)

    def test_managed_codex_membership_or_person_difference_never_merges(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            self._connect_with_native_identity(self._start(), membership="member-a")
            second, _ = self._connect_with_native_identity(self._start(), membership="member-b")
        self.assertNotIn("supersededConnectionID", second)
        self.assertEqual(len(agentcat.codex_connections_snapshot()), 2)

    def test_managed_codex_workspace_difference_never_merges_even_for_same_member(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            self._connect_with_native_identity(self._start(), account="workspace-a", membership="member-a")
            second, _ = self._connect_with_native_identity(self._start(), account="workspace-b", membership="member-a")
        self.assertNotIn("supersededConnectionID", second)
        self.assertEqual(len(agentcat.codex_connections_snapshot()), 2)

    def test_legacy_codex_without_key_is_backfilled_before_new_login_merge(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            first_start = self._start()
            _, legacy = self._connect_with_native_identity(first_start, membership="member-a")
            rows = agentcat._codex_connections()
            legacy_row = next(row for row in rows if row["id"] == legacy["id"])
            legacy_row.pop("dedupKey", None)
            legacy_row.pop("authProofFingerprint", None)
            agentcat._write_codex_connections(rows)
            second, source = self._connect_with_native_identity(self._start(), membership="member-a")
        self.assertEqual(second["connection"]["id"], legacy["id"])
        self.assertEqual(second["supersededConnectionID"], source["id"])

    def test_promotion_rolls_back_and_recreates_managed_servers_when_destination_validation_fails(self):
        with patch.object(agentcat, "CodexAppServer", FakeCodexAppServer):
            _, canonical = self._connect_with_native_identity(self._start(), membership="member-a")
            old_auth = (agentcat._codex_profile_dir(canonical["id"]) / "auth.json").read_bytes()
            next_start = self._start()
            source_row = next(row for row in agentcat._codex_connections() if row.get("operationID") == next_start["operationID"])
            self._write_managed_auth(source_row, membership="member-a")
            FakeCodexAppServer.accounts[source_row["id"]] = {"email": "source@example.invalid", "planType": "pro"}
            FakeCodexAppServer.fail_promotion_for.add(canonical["id"])
            result = agentcat.codex_oauth_status(next_start["operationID"])
        self.assertEqual(result["connection"]["id"], source_row["id"])
        self.assertEqual((agentcat._codex_profile_dir(canonical["id"]) / "auth.json").read_bytes(), old_auth)
        self.assertIn(canonical["id"], agentcat._CODEX_APP_SERVERS)
        self.assertIn(source_row["id"], agentcat._CODEX_APP_SERVERS)
        self.assertTrue(any(instance.closed for instance in FakeCodexAppServer.instances if instance.connection_id == canonical["id"]))

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
