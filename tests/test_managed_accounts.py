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
            return {"status": "connected", "authenticated": True, "identity": {"email": "local@example.test", "verification": True, "source": "fixture"}, "usage": {"source": "fake", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False}}
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
        self.assertEqual(state["connection"]["label"], "local@example.test")
        self.assertTrue(state["connection"]["identity"]["verification"])
        self.assertNotIn("operationID", state["connection"])

    def test_completed_operation_is_idempotently_readable_for_the_live_lease(self):
        started = self.accounts.start("fake", "My account", "device")
        self.adapter.connected = True
        first = self.accounts.status("fake", started["operationID"])
        second = self.accounts.status("fake", started["operationID"])
        self.assertEqual(first["status"], "connected")
        self.assertEqual(second["status"], "connected")
        self.assertEqual(second["connection"]["id"], first["connection"]["id"])
        self.assertNotIn("operationID", second["connection"])
        self.assertIn(("fake", started["operationID"]), self.accounts.completed_operations)

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
        self.adapter.poll = lambda profile, operation: {"status": "connected", "authenticated": True, "identity": {"accountID": "first", "email": "first@example.test", "verification": True, "source": "fixture"}, "usage": {"source": "fake"}}
        connected = self.accounts.status("fake", started["operationID"])
        row = connected["connection"]
        self.adapter.refresh = lambda profile: {"status": "connected", "authenticated": True, "identity": {"accountID": "other", "email": "other@example.test", "verification": True, "source": "fixture"}, "usage": {"source": "other"}}
        refreshed = self.accounts.refresh("fake", row["id"])
        self.assertEqual(refreshed["status"], "needs_reconnect")
        self.assertEqual(refreshed["identity"]["accountID"], "first")
        self.assertEqual(refreshed["usage"]["source"], "fake")

    def test_refresh_migrates_legacy_display_label_to_verified_email(self):
        self.accounts._write([{
            "id": "a" * 32, "provider": "fake", "label": "Account 1",
            "kind": "managed_native_auth", "scope": "managed_provider_profile",
            "status": "connected", "createdAt": "2026-01-01T00:00:00Z",
            "usage": {"source": "old"},
        }])
        self.adapter.refresh = lambda profile: {
            "status": "connected", "authenticated": True,
            "identity": {"email": "real@example.test", "verification": True, "source": "fixture"},
            "usage": {"source": "fixture"},
        }
        refreshed = self.accounts.refresh("fake", "a" * 32)
        self.assertEqual(refreshed["label"], "real@example.test")
        self.assertEqual(refreshed["identity"]["email"], "real@example.test")

    def test_multiple_provider_rows_survive_restart_and_isolate_retry_remove(self):
        other = FakeAdapter()
        accounts = ManagedAccounts(Path(self.temp.name), {"fake": self.adapter, "other": other})
        first = accounts.start("fake", "First", "device")
        second = accounts.start("fake", "Second", "device")
        third = accounts.start("other", "Third", "device")
        before = accounts.snapshot()
        self.assertEqual([(row["provider"], row["label"]) for row in before], [("fake", ""), ("fake", ""), ("other", "")])
        restarted = ManagedAccounts(Path(self.temp.name), {"fake": self.adapter, "other": other})
        # Pending browser/device details are intentionally gone after restart,
        # while unrelated registered metadata remains listable.
        self.assertEqual(restarted.status("fake", first["operationID"])["status"], "failed")
        rows = restarted.snapshot()
        second_row = next(row for row in rows if row.get("operationID") == second["operationID"])
        third_row = next(row for row in rows if row.get("operationID") == third["operationID"])
        self.assertEqual(restarted.cancel("fake", second["operationID"]), "canceled")
        retried = restarted.retry("fake", second_row["id"], "device")
        self.assertNotEqual(retried["operationID"], second["operationID"])
        removed = restarted.remove("other", third_row["id"])
        self.assertEqual(removed["status"], "removed")
        remaining = restarted.snapshot()
        self.assertEqual({row["id"] for row in remaining}, {rows[0]["id"], second_row["id"]})


if __name__ == "__main__":
    unittest.main()
