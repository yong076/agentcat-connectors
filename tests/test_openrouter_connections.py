import importlib.util
import json
import tempfile
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("openrouter_agentcat_module", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("openrouter_agentcat_module", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class OpenRouterConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.agentcat_home = Path(self.tmp.name) / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        agentcat.OPENROUTER_CONNECTIONS_FILE = self.agentcat_home / "openrouter-connections.json"
        agentcat.LOOPBACK_CONTROL_TOKEN_FILE = self.agentcat_home / "loopback-control-token"
        assert_sandboxed(agentcat, self.home, self.agentcat_home)
        agentcat._OPENROUTER_OAUTH_PENDING.clear()

    def tearDown(self):
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def test_usage_keeps_missing_values_unknown_and_separates_byok(self):
        usage = agentcat._openrouter_usage_payload({
            "usage_daily": 1.25,
            "usage_weekly": 4.5,
            "usage_monthly": 9.75,
            "usage": 12,
            "byok_usage_daily": 0.4,
            "include_byok_in_limit": True,
            "limit_remaining": 0,
        })
        self.assertEqual(usage["daily"], 1.25)
        self.assertEqual(usage["limitRemaining"], 0.0)
        self.assertIsNone(usage["limit"])
        self.assertEqual(usage["byokDaily"], 0.4)
        self.assertIsNone(usage["byokWeekly"])
        self.assertTrue(usage["includeByokInLimit"])

    def test_registry_never_contains_key_and_public_shape_redacts_it(self):
        row = {
            "id": "a" * 32, "provider": "openrouter", "label": "Work",
            "kind": "imported_key", "scope": "api_key_scoped", "status": "connected",
            "usage": {"daily": 2.0}, "key": "sk-or-v1-secret",
        }
        agentcat._write_openrouter_connections([row])
        stored = json.loads(agentcat.OPENROUTER_CONNECTIONS_FILE.read_text())
        self.assertNotIn("sk-or-v1-secret", json.dumps(stored))
        public = agentcat._openrouter_public_connection(row)
        self.assertNotIn("key", public)
        self.assertEqual(public["usage"]["daily"], 2.0)

    def test_refresh_preserves_last_successful_usage_when_network_fails(self):
        row = {
            "id": "b" * 32, "status": "connected",
            "usage": {"currency": "USD", "daily": 6.5},
            "lastSuccessfulSyncAt": "2026-01-01T00:00:00Z",
        }
        with patch.object(agentcat, "_openrouter_keychain_read", return_value="secret"), patch.object(agentcat.urllib.request, "urlopen", side_effect=OSError("offline")):
            agentcat.openrouter_refresh_connection(row)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["usage"]["daily"], 6.5)
        self.assertIn("Last successful", row["error"])

    def test_oauth_state_is_one_time_and_expiring(self):
        started = agentcat.openrouter_start_oauth("Personal", 8765)
        nonce = started["callbackPath"].rsplit("/", 1)[1]
        self.assertIn("code_challenge_method=S256", started["authorizationURL"])
        self.assertIn(nonce, agentcat._OPENROUTER_OAUTH_PENDING)
        agentcat._OPENROUTER_OAUTH_PENDING[nonce]["created"] = time.monotonic() - 601
        with self.assertRaises(ValueError):
            agentcat.openrouter_finish_oauth(nonce, "authorization-code")
        self.assertNotIn(nonce, agentcat._OPENROUTER_OAUTH_PENDING)

    def test_oauth_claim_is_one_time_and_completion_requires_claim(self):
        nonce = "n" * 43
        agentcat._OPENROUTER_OAUTH_PENDING[nonce] = {
            "created": time.monotonic(), "key": "temporary-secret", "label": "OAuth",
        }
        self.assertEqual(agentcat.openrouter_oauth_status(nonce), "ready")
        self.assertEqual(agentcat.openrouter_claim_oauth_key(nonce), "temporary-secret")
        self.assertEqual(agentcat.openrouter_oauth_status(nonce), "claimed")
        with self.assertRaises(ValueError):
            agentcat.openrouter_claim_oauth_key(nonce)

    def test_concurrent_oauth_claim_delivers_key_once(self):
        nonce = "c" * 43
        agentcat._OPENROUTER_OAUTH_PENDING[nonce] = {
            "created": time.monotonic(), "key": "temporary-secret", "label": "OAuth",
        }
        def claim():
            try:
                return agentcat.openrouter_claim_oauth_key(nonce)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))
        self.assertEqual(results.count("temporary-secret"), 1)
        self.assertEqual(results.count(None), 1)
        self.assertNotIn("key", agentcat._OPENROUTER_OAUTH_PENDING[nonce])

    def test_handler_requires_bearer_and_rejects_foreign_origin_writes(self):
        token = agentcat.loopback_control_token()
        server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(HTTPError) as missing:
                urlopen(base + "/v1/connections", timeout=3)
            self.assertEqual(missing.exception.code, 401)
            request = Request(base + "/v1/connections", headers={"Authorization": f"Bearer {token}"})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read().decode("utf-8")), {"connections": []})
            foreign = Request(
                base + "/v1/connections/openrouter/oauth/start",
                data=b"{}", method="POST",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            with self.assertRaises(HTTPError) as rejected:
                urlopen(foreign, timeout=3)
            self.assertEqual(rejected.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_control_token_is_stable_and_not_written_to_registry(self):
        token = agentcat.loopback_control_token()
        self.assertEqual(token, agentcat.loopback_control_token())
        self.assertTrue(agentcat.LOOPBACK_CONTROL_TOKEN_FILE.exists())
        self.assertNotIn(token, agentcat._openrouter_connections())


if __name__ == "__main__":
    unittest.main()
