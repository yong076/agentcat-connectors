import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agentcat_managed_accounts import ManagedAccounts


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.connected = False

    def adapter_capability(self):
        return {"provider": "fake", "supported": True, "available": True, "reason": None, "modes": ["device"]}

    def start(self, profile, mode):
        self.calls.append(("start", profile, mode))
        return {"operationID": "op-" + str(len(self.calls)), "status": "pending_device", "verificationURL": "https://example.test/device", "userCode": "CODE"}

    def poll(self, profile, operation):
        if self.connected:
            return {"status": "connected", "authenticated": True, "identity": {"email": "local@example.test"}, "usage": {"source": "fake", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False}}
        return {"status": "pending_device"}

    def cancel(self, profile, operation):
        self.calls.append(("cancel", profile, operation))
        return "canceled"


class ManagedAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = FakeAdapter()
        self.accounts = ManagedAccounts(Path(self.temp.name), {"fake": self.adapter})

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_surface_is_memory_only_and_retry_reuses_connection(self):
        started = self.accounts.start("fake", "My account", "device")
        state = self.accounts.status("fake", started["operationID"])
        self.assertEqual(state["resume"]["userCode"], "CODE")
        registry = self.accounts.registry.read_text()
        self.assertNotIn("verificationURL", registry)
        self.assertNotIn("CODE", registry)
        self.assertEqual(self.accounts.cancel("fake", started["operationID"]), "canceled")
        row = self.accounts.snapshot()[0]
        retried = self.accounts.retry("fake", row["id"], "device")
        self.assertNotEqual(started["operationID"], retried["operationID"])
        self.assertEqual(self.accounts.snapshot()[0]["id"], row["id"])

    def test_completion_clears_operation_surface_and_preserves_usage_provenance(self):
        started = self.accounts.start("fake", "My account", "device")
        self.adapter.connected = True
        state = self.accounts.status("fake", started["operationID"])
        self.assertEqual(state["status"], "connected")
        self.assertEqual(state["connection"]["usage"]["source"], "fake")
        self.assertNotIn("operationID", state["connection"])

    def test_unverified_provider_exit_never_becomes_connected(self):
        started = self.accounts.start("fake", "My account", "device")
        self.adapter.connected = True
        original = self.adapter.poll
        self.adapter.poll = lambda profile, operation: {"status": "connected", "usage": {"source": "fake"}}
        state = self.accounts.status("fake", started["operationID"])
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "managed_auth_verification_required")
        self.adapter.poll = original

    def test_refresh_rejects_a_different_native_account_for_same_connection(self):
        started = self.accounts.start("fake", "My account", "device")
        self.adapter.poll = lambda profile, operation: {"status": "connected", "authenticated": True, "identity": {"accountID": "first"}, "usage": {"source": "fake"}}
        connected = self.accounts.status("fake", started["operationID"])
        row = connected["connection"]
        self.adapter.refresh = lambda profile: {"status": "connected", "authenticated": True, "identity": {"accountID": "other"}, "usage": {"source": "other"}}
        refreshed = self.accounts.refresh("fake", row["id"])
        self.assertEqual(refreshed["status"], "needs_reconnect")
        self.assertEqual(refreshed["identity"]["accountID"], "first")
        self.assertEqual(refreshed["usage"]["source"], "fake")


if __name__ == "__main__":
    unittest.main()
