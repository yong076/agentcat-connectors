import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
from agentcat_openrouter_managed import OpenRouterManagedAuth, OpenRouterManagedError, build_adapter


class OpenRouterManagedAuthTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        self.metadata = {}
        self.completed = []
        self.auth = OpenRouterManagedAuth(
            callback_port=lambda: 8766,
            read_metadata=lambda op: self.metadata.get(op),
            write_metadata=lambda op, value: self.metadata.__setitem__(op, dict(value)),
            exchange_code=lambda code, verifier: "issued-key-" + code,
            complete=self.complete,
            clock=lambda: self.clock[0],
        )

    def complete(self, connection_id, label, kind):
        self.completed.append((connection_id, label, kind))
        return {"id": connection_id, "label": label, "kind": kind}

    def test_start_persists_no_secret_or_authorization_url(self):
        started = self.auth.start(None, label="Work")
        row = self.metadata[started["operationID"]]
        self.assertEqual(started["status"], "pending_browser")
        self.assertIn("code_challenge_method=S256", started["authorizationURL"])
        self.assertNotIn("authorizationURL", row)
        self.assertNotIn("verifier", row)
        self.assertNotIn("key", row)

    def test_factory_is_callback_only_and_has_no_probe_side_effect(self):
        adapter = build_adapter(
            callback_port=lambda: 8766,
            read_metadata=lambda op: self.metadata.get(op),
            write_metadata=lambda op, value: self.metadata.__setitem__(op, dict(value)),
            exchange_code=lambda code, verifier: "key",
            complete=self.complete,
            clock=lambda: self.clock[0],
        )
        self.assertIsInstance(adapter, OpenRouterManagedAuth)
        self.assertEqual(self.metadata, {})

    def test_callback_claim_complete_is_one_time_and_key_is_memory_only(self):
        started = self.auth.start(None, label="Work")
        operation = started["operationID"]
        self.auth.callback(operation, "code")
        self.assertEqual(self.auth.poll(None, operation)["status"], "ready")
        key = self.auth.claim(operation)
        self.assertEqual(key, "issued-key-code")
        self.assertNotIn("key", self.metadata[operation])
        with self.assertRaises(OpenRouterManagedError):
            self.auth.claim(operation)
        row = self.auth.complete(operation, "a" * 32, "Work")
        self.assertEqual(row["kind"], "oauth_pkce")
        self.assertEqual(self.completed, [("a" * 32, "Work", "oauth_pkce")])

    def test_cancel_invalidates_callback_and_claim(self):
        operation = self.auth.start(None)["operationID"]
        self.assertEqual(self.auth.cancel(None, operation)["status"], "canceled")
        with self.assertRaises(OpenRouterManagedError):
            self.auth.callback(operation, "code")
        with self.assertRaises(OpenRouterManagedError):
            self.auth.claim(operation)

    def test_missing_memory_pending_state_fails_after_restart(self):
        operation = self.auth.start(None)["operationID"]
        restarted = OpenRouterManagedAuth(
            callback_port=lambda: 8766,
            read_metadata=lambda op: self.metadata.get(op),
            write_metadata=lambda op, value: self.metadata.__setitem__(op, dict(value)),
            exchange_code=lambda code, verifier: "ignored",
            complete=self.complete,
            clock=lambda: self.clock[0],
        )
        state = restarted.poll(None, operation)
        self.assertEqual(state, {"status": "failed", "error": "daemon_restarted_restart_required"})
        self.assertEqual(self.metadata[operation]["error"], "daemon_restarted_restart_required")

    def test_expiry_and_exchange_failure_are_terminal(self):
        operation = self.auth.start(None)["operationID"]
        self.clock[0] += 601
        self.assertEqual(self.auth.poll(None, operation)["error"], "oauth_state_expired")
        failing = OpenRouterManagedAuth(
            callback_port=lambda: 8766,
            read_metadata=lambda op: self.metadata.get(op),
            write_metadata=lambda op, value: self.metadata.__setitem__(op, dict(value)),
            exchange_code=lambda code, verifier: (_ for _ in ()).throw(RuntimeError("network")),
            complete=self.complete,
            clock=lambda: self.clock[0],
        )
        operation = failing.start(None)["operationID"]
        with self.assertRaisesRegex(OpenRouterManagedError, "oauth_exchange_failed"):
            failing.callback(operation, "code")
        self.assertEqual(failing.poll(None, operation)["error"], "oauth_exchange_failed")


if __name__ == "__main__":
    unittest.main()
