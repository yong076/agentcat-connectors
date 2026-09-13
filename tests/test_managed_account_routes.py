import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sandbox import redirect_module_paths, restore_module_paths

REPO = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("managed_routes_agentcat", str(REPO / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("managed_routes_agentcat", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class FakeAdapter:
    def __init__(self):
        self.number = 0
        self.connected = set()

    def adapter_capability(self):
        return {"provider": "kimi", "supported": True, "available": True, "reason": None, "modes": ["device"]}

    def start(self, profile, mode):
        self.number += 1
        return {"operationID": f"device-{self.number:08d}", "status": "pending_device", "verificationURL": "https://login.example.test/device", "userCode": "CODE-123"}

    def poll(self, profile, operation):
        if operation in self.connected:
            return {"status": "connected", "authenticated": True, "identity": {"name": "Local"}, "usage": {"source": "fixture", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False}}
        return {"status": "pending_device"}

    def cancel(self, profile, operation):
        return "canceled"

    def refresh(self, profile):
        return {"status": "connected", "authenticated": True, "usage": {"source": "fixture", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False}}

    def remove(self, profile):
        return None


GEMINI_UNAVAILABLE_USAGE = {
    "source": "gemini_code_assist",
    "freshness": "unavailable",
    "windows": [],
    "credits": None,
    "spendControl": None,
    "rateLimitReachedType": None,
    "tokenUsage": None,
    "tokenUsageAvailable": False,
    "scope": "gemini_code_assist_request_quota",
    "reason": "gemini_consumer_tier_unsupported",
}


class ManagedAccountRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.agentcat_home = Path(self.tmp.name) / "agentcat"
        self.home.mkdir(); self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        agentcat.LOOPBACK_CONTROL_TOKEN_FILE = self.agentcat_home / "loopback-control-token"
        self.adapter = FakeAdapter()
        agentcat._MANAGED_ACCOUNTS = agentcat.ManagedAccounts(self.agentcat_home, {"kimi": self.adapter})
        agentcat._MANAGED_ACCOUNTS_HOME = str(self.agentcat_home)
        self.token = agentcat.loopback_control_token()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        agentcat._MANAGED_ACCOUNTS = None; agentcat._MANAGED_ACCOUNTS_HOME = None
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def request(self, path, *, body=None, method=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        request = Request(self.base + path, data=body, method=method, headers=headers)
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode())

    def test_capability_start_resume_cancel_retry_and_memory_only_registry(self):
        with self.assertRaises(HTTPError) as denied:
            urlopen(self.base + "/v1/connections/capabilities", timeout=3)
        self.assertEqual(denied.exception.code, 401)
        _, caps = self.request("/v1/connections/capabilities")
        kimi = next(item for item in caps["providers"] if item["provider"] == "kimi")
        self.assertTrue(kimi["available"])
        _, started = self.request("/v1/connections/kimi/oauth/start", body={"label": "Work", "mode": "device"}, method="POST")
        self.assertEqual(started["status"], "pending_device")
        operation = started["operationID"]
        _, pending = self.request(f"/v1/connections/kimi/oauth/{operation}")
        self.assertEqual(pending["resume"]["userCode"], "CODE-123")
        stored = (self.agentcat_home / "managed-connections.json").read_text()
        self.assertNotIn("CODE-123", stored)
        self.assertNotIn("login.example.test", stored)
        _, canceled = self.request(f"/v1/connections/kimi/oauth/{operation}/cancel", body={}, method="POST")
        self.assertEqual(canceled["status"], "canceled")
        _, listed = self.request("/v1/connections")
        row = next(item for item in listed["connections"] if item["provider"] == "kimi")
        _, retried = self.request(f"/v1/connections/kimi/{row['id']}/oauth/retry", body={"mode": "device"}, method="POST")
        self.assertEqual(retried["status"], "pending_device")
        self.assertEqual(agentcat.managed_accounts().snapshot()[0]["id"], row["id"])

    def test_http_managed_connection_preserves_normalized_unavailable_usage(self):
        class GeminiFixtureAdapter(FakeAdapter):
            def adapter_capability(self):
                return {"provider": "gemini", "supported": True, "available": True, "reason": None, "modes": ["browser"]}

            def start(self, profile, mode):
                self.number += 1
                return {"operationID": f"browser-{self.number:08d}", "status": "pending_browser", "browserLaunchMode": "provider"}

            def poll(self, profile, operation):
                return {
                    "status": "connected", "authenticated": True,
                    "identity": {"email": "verified@example.invalid", "verification": True, "source": "google_userinfo"},
                    "usage": GEMINI_UNAVAILABLE_USAGE,
                }

        self.adapter = GeminiFixtureAdapter()
        agentcat._MANAGED_ACCOUNTS = agentcat.ManagedAccounts(self.agentcat_home, {"gemini": self.adapter})
        _, started = self.request("/v1/connections/gemini/oauth/start", body={"mode": "browser"}, method="POST")
        _, connected = self.request(f"/v1/connections/gemini/oauth/{started['operationID']}")
        row = connected["connection"]
        self.assertEqual(row["identity"], {"email": "verified@example.invalid", "verification": True, "source": "google_userinfo"})
        self.assertEqual(row["usage"], GEMINI_UNAVAILABLE_USAGE)
        _, listed = self.request("/v1/connections")
        listed_row = next(item for item in listed["connections"] if item["provider"] == "gemini")
        self.assertEqual(listed_row["usage"], GEMINI_UNAVAILABLE_USAGE)

    def test_connected_refresh_and_remove_are_per_account(self):
        _, started = self.request("/v1/connections/kimi/oauth/start", body={"mode": "device"}, method="POST")
        self.adapter.connected.add(started["operationID"])
        _, connected = self.request(f"/v1/connections/kimi/oauth/{started['operationID']}")
        row = connected["connection"]
        self.assertEqual(row["usage"]["source"], "fixture")
        _, refreshed = self.request(f"/v1/connections/kimi/{row['id']}/refresh", body={}, method="POST")
        self.assertEqual(refreshed["connection"]["status"], "connected")
        _, removed = self.request(f"/v1/connections/kimi/{row['id']}", body={}, method="DELETE")
        self.assertEqual(removed["connection"]["status"], "removed")


if __name__ == "__main__":
    unittest.main()
