import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from agentcat_managed_accounts import AUTH_STATES, ManagedAccounts
import agentcat_managed_accounts as managed_accounts


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


class DedupAdapter(FakeAdapter):
    def __init__(self, *, tenant_for_operation=None, include_identity=True):
        super().__init__()
        self.tenant_for_operation = tenant_for_operation or (lambda operation: "tenant-a")
        self.include_identity = include_identity
        self.promotions = []

    def poll(self, profile, operation):
        if not self.connected:
            return {"status": "pending_device"}
        payload = {
            "status": "connected", "authenticated": True,
            "identity": {"email": "verified@example.test", "verification": True, "source": "fixture", "accountID": "provider-user-42"},
            "usage": {"source": "fake", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False},
        }
        if self.include_identity:
            payload["providerIdentity"] = {"accountID": "provider-user-42", "tenantID": self.tenant_for_operation(operation)}
        return payload

    def promote_verified_profile(self, source, destination, identity):
        self.promotions.append((source, destination, dict(identity)))
        return True


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
        self.adapter.poll = lambda profile, operation: {"status": "connected", "authenticated": True, "identity": {"accountID": "first", "email": "first@example.test", "verification": True, "source": "fixture"}, "providerIdentity": {"accountID": "first"}, "usage": {"source": "fake"}}
        connected = self.accounts.status("fake", started["operationID"])
        row = connected["connection"]
        self.adapter.refresh = lambda profile: {"status": "connected", "authenticated": True, "identity": {"accountID": "other", "email": "other@example.test", "verification": True, "source": "fixture"}, "providerIdentity": {"accountID": "other"}, "usage": {"source": "other"}}
        refreshed = self.accounts.refresh("fake", row["id"])
        self.assertEqual(refreshed["status"], "needs_reconnect")
        self.assertNotIn("accountID", refreshed["identity"])
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

    def test_verified_provider_identity_merges_to_canonical_and_alias_survives_restart(self):
        adapter = DedupAdapter()
        accounts = ManagedAccounts(Path(self.temp.name), {"fake": adapter}, dedup_secret=b"d" * 32)
        first = accounts.start("fake", "", "device")
        adapter.connected = True
        canonical = accounts.status("fake", first["operationID"])["connection"]
        second = accounts.start("fake", "", "device")
        merged = accounts.status("fake", second["operationID"])
        self.assertEqual(merged["status"], "connected")
        self.assertEqual(merged["connection"]["id"], canonical["id"])
        self.assertEqual(merged["supersededConnectionID"], next(item["id"] for item in accounts._rows() if item.get("operationID") == second["operationID"]))
        self.assertEqual([row["id"] for row in accounts.snapshot()], [canonical["id"]])
        self.assertEqual(len(adapter.promotions), 1)
        promoted_source, promoted_destination, _ = adapter.promotions[0]
        self.assertEqual(promoted_source.name, merged["supersededConnectionID"])
        self.assertEqual(promoted_destination.name, canonical["id"])
        registry = accounts.registry.read_text(encoding="utf-8")
        self.assertNotIn("provider-user-42", registry)
        self.assertNotIn("tenant-a", registry)
        restarted = ManagedAccounts(Path(self.temp.name), {"fake": adapter}, dedup_secret=b"d" * 32)
        durable = restarted.status("fake", second["operationID"])
        self.assertEqual(durable["connection"]["id"], canonical["id"])
        self.assertEqual(durable["supersededConnectionID"], merged["supersededConnectionID"])
        self.assertEqual(restarted.cancel("fake", second["operationID"]), "alreadyCompleted")
        self.assertEqual(restarted.status("fake", second["operationID"])["connection"]["id"], canonical["id"])

    def test_new_login_backfills_a_legacy_connected_row_before_merging(self):
        adapter = DedupAdapter()
        accounts = ManagedAccounts(Path(self.temp.name), {"fake": adapter}, dedup_secret=b"g" * 32)
        first = accounts.start("fake", "", "device")
        adapter.connected = True
        original = accounts.status("fake", first["operationID"])["connection"]
        rows = accounts._rows()
        rows[0].pop("dedupKey", None)  # pre-dedup registry from a prior release
        accounts._write(rows)
        adapter.refresh = lambda profile: {
            "status": "connected", "authenticated": True,
            "identity": {"email": "verified@example.test", "verification": True, "source": "fixture", "accountID": "provider-user-42"},
            "providerIdentity": {"accountID": "provider-user-42", "tenantID": "tenant-a"},
            "usage": {"source": "fake", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False},
        }
        second = accounts.start("fake", "", "device")
        merged = accounts.status("fake", second["operationID"])
        self.assertEqual(merged["connection"]["id"], original["id"])
        self.assertIn("supersededConnectionID", merged)
        self.assertEqual(len(accounts.snapshot()), 1)
        self.assertEqual(len(adapter.promotions), 1)

    def test_new_login_backfills_a_reconnecting_legacy_row_only_after_provider_proof(self):
        adapter = DedupAdapter()
        accounts = ManagedAccounts(Path(self.temp.name), {"fake": adapter}, dedup_secret=b"r" * 32)
        first = accounts.start("fake", "", "device")
        adapter.connected = True
        original = accounts.status("fake", first["operationID"])["connection"]
        rows = accounts._rows()
        rows[0]["status"] = "needs_reconnect"
        rows[0].pop("dedupKey", None)
        accounts._write(rows)
        adapter.refresh = lambda profile: {
            "status": "connected", "authenticated": True,
            "identity": {"email": "verified@example.test", "verification": True, "source": "fixture", "accountID": "provider-user-42"},
            "providerIdentity": {"accountID": "provider-user-42", "tenantID": "tenant-a"},
            "usage": {"source": "fake", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False},
        }
        second = accounts.start("fake", "", "device")
        merged = accounts.status("fake", second["operationID"])
        self.assertEqual(merged["connection"]["id"], original["id"])
        self.assertIn("supersededConnectionID", merged)
        self.assertEqual(len(accounts.snapshot()), 1)
        self.assertEqual(len(adapter.promotions), 1)

    def test_reconnecting_legacy_row_with_unreadable_provider_identity_never_merges_by_email(self):
        adapter = DedupAdapter()
        accounts = ManagedAccounts(Path(self.temp.name), {"fake": adapter}, dedup_secret=b"u" * 32)
        first = accounts.start("fake", "", "device")
        adapter.connected = True
        original = accounts.status("fake", first["operationID"])["connection"]
        rows = accounts._rows()
        rows[0]["status"] = "needs_reconnect"
        rows[0].pop("dedupKey", None)
        accounts._write(rows)
        adapter.refresh = lambda profile: {
            "status": "needs_reconnect",
            # A familiar verified display email is explicitly insufficient
            # without the provider's stable private account identity.
            "identity": {"email": "verified@example.test", "verification": True, "source": "fixture"},
            "identityStatus": {"status": "unavailable", "reason": "sign_in_required"},
            "usage": {"source": "fake", "freshness": "unavailable", "windows": []},
        }
        second = accounts.start("fake", "", "device")
        result = accounts.status("fake", second["operationID"])
        self.assertNotIn("supersededConnectionID", result)
        self.assertEqual(len(accounts.snapshot()), 2)
        legacy = next(row for row in accounts._rows() if row["id"] == original["id"])
        self.assertEqual(legacy["status"], "needs_reconnect")
        self.assertEqual(adapter.promotions, [])

    def test_refresh_of_existing_successor_reconciles_reconnecting_legacy_row(self):
        adapter = DedupAdapter()
        accounts = ManagedAccounts(Path(self.temp.name), {"fake": adapter}, dedup_secret=b"s" * 32)
        first = accounts.start("fake", "", "device")
        adapter.connected = True
        legacy = accounts.status("fake", first["operationID"])["connection"]
        rows = accounts._rows()
        rows[0]["status"] = "needs_reconnect"
        rows[0].pop("dedupKey", None)
        accounts._write(rows)
        second = accounts.start("fake", "", "device")
        successor = accounts.status("fake", second["operationID"])["connection"]
        adapter.refresh = lambda profile: {
            "status": "connected", "authenticated": True,
            "identity": {"email": "verified@example.test", "verification": True, "source": "fixture", "accountID": "provider-user-42"},
            "providerIdentity": {"accountID": "provider-user-42", "tenantID": "tenant-a"},
            "usage": {"source": "fake", "freshness": "live", "windows": [], "tokenUsage": None, "tokenUsageAvailable": False},
        }
        refreshed = accounts.refresh("fake", successor["id"])
        self.assertEqual(refreshed["id"], legacy["id"])
        self.assertEqual(len(accounts.snapshot()), 1)
        self.assertEqual(len(adapter.promotions), 1)

    def test_verified_identity_never_merges_across_tenant_or_when_missing(self):
        adapter = DedupAdapter(tenant_for_operation=lambda operation: "tenant-a" if operation.endswith("1") else "tenant-b")
        accounts = ManagedAccounts(Path(self.temp.name), {"fake": adapter}, dedup_secret=b"e" * 32)
        first = accounts.start("fake", "", "device"); adapter.connected = True
        accounts.status("fake", first["operationID"])
        second = accounts.start("fake", "", "device")
        accounts.status("fake", second["operationID"])
        self.assertEqual(len(accounts.snapshot()), 2)
        self.assertEqual(adapter.promotions, [])
        unknown = DedupAdapter(include_identity=False)
        other_home = Path(self.temp.name) / "unknown"
        unknown_accounts = ManagedAccounts(other_home, {"fake": unknown}, dedup_secret=b"f" * 32)
        one = unknown_accounts.start("fake", "", "device"); unknown.connected = True
        unknown_accounts.status("fake", one["operationID"])
        two = unknown_accounts.start("fake", "", "device")
        unknown_accounts.status("fake", two["operationID"])
        self.assertEqual(len(unknown_accounts.snapshot()), 2)
        self.assertEqual(unknown.promotions, [])

    def test_resolve_connection_id_follows_only_same_provider_durable_aliases(self):
        canonical = "a" * 32
        source = "b" * 32
        foreign = "c" * 32
        missing = "d" * 32
        cycle_one = "e" * 32
        cycle_two = "f" * 32
        self.accounts._write([
            {"id": canonical, "provider": "fake", "status": "connected", "scope": "managed_provider_profile"},
            {"id": source, "provider": "fake", "status": "superseded", "scope": "managed_provider_profile", "supersededBy": canonical},
            {"id": foreign, "provider": "other", "status": "connected", "scope": "managed_provider_profile"},
            {"id": missing, "provider": "fake", "status": "superseded", "scope": "managed_provider_profile", "supersededBy": foreign},
            {"id": cycle_one, "provider": "fake", "status": "superseded", "scope": "managed_provider_profile", "supersededBy": cycle_two},
            {"id": cycle_two, "provider": "fake", "status": "superseded", "scope": "managed_provider_profile", "supersededBy": cycle_one},
        ])
        self.assertEqual(self.accounts.resolve_connection_id(canonical), {
            "requestConnectionID": canonical, "canonicalConnectionID": canonical,
            "provider": "fake", "status": "connected", "alias": False,
        })
        self.assertEqual(self.accounts.resolve_connection_id(source), {
            "requestConnectionID": source, "canonicalConnectionID": canonical,
            "provider": "fake", "status": "connected", "alias": True,
        })
        self.assertEqual(self.accounts.resolve_connection_id(missing), {
            "requestConnectionID": missing, "canonicalConnectionID": None,
            "provider": "fake", "status": "missing", "alias": True,
        })
        self.assertIsNone(self.accounts.resolve_connection_id("0" * 32))
        with self.assertRaisesRegex(RuntimeError, "connection_alias_cycle"):
            self.accounts.resolve_connection_id(cycle_one)


SHARED_EMAIL = "same-person@example.test"
CODEX_ACCESS = "codex-secret-access-token-LEAKME"
CODEX_REFRESH = "codex-secret-refresh-token-LEAKME"
DEFAULT_CODEX_ACCESS = "default-codex-access-token-LEAKME"
CLAUDE_ACCESS = "claude-secret-accessToken-LEAKME"
KEYCHAIN_ACCESS = "keychain-secret-blob-LEAKME"
PLANTED_PATH = "/tmp/agentcat-secret-credential/auth.json"
FUTURE_UNIX = 4_102_444_800
PAST_UNIX = 1


class ManagedAccountAuthSnapshotTests(unittest.TestCase):
    CODEX_MISSING = "a" * 32
    CODEX_MALFORMED = "b" * 32
    CODEX_EXPIRED = "c" * 32
    CODEX_ACTIVE = "d" * 32
    CODEX_INACTIVE = "e" * 32
    CLAUDE_MISSING = "f" * 32
    CLAUDE_MALFORMED = "g" * 32
    CLAUDE_EXPIRED = "h" * 32
    CLAUDE_KEYCHAIN_DENIED = "i" * 32
    CLAUDE_ACTIVE = "j" * 32
    CLAUDE_INACTIVE = "k" * 32
    CLAUDE_KEYCHAIN_HEALTHY = "m" * 32
    KIMI = "n" * 32

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cli_home = root / "cli-home"
        self.cli_home.mkdir()
        self.accounts = ManagedAccounts(root / "agentcat", {
            "codex": FakeAdapter(), "claude": FakeAdapter(), "kimi": FakeAdapter(),
        })
        self.home_patch = patch.object(managed_accounts, "_cli_home", return_value=self.cli_home)
        self.keychain_patch = patch.object(managed_accounts, "_read_claude_keychain", side_effect=self._keychain)
        self.home_patch.start()
        self.keychain_patch.start()
        self.secret_paths = []
        self._install_default_cli()
        self._install_rows()

    def tearDown(self):
        self.keychain_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    def _keychain(self, profile):
        name = Path(profile).name
        if name == self.CLAUDE_KEYCHAIN_DENIED:
            return "denied", None
        if name == self.CLAUDE_KEYCHAIN_HEALTHY:
            return "ok", {"accessToken": KEYCHAIN_ACCESS, "expiresAt": FUTURE_UNIX * 1000}
        return "skip", None

    def _profile(self, provider, connection_id):
        path = self.accounts.profile_root / provider / connection_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _row(self, connection_id, provider, **extra):
        row = {
            "id": connection_id, "provider": provider, "label": extra.pop("label", SHARED_EMAIL),
            "kind": "managed_native_auth", "scope": "managed_provider_profile",
            "status": extra.pop("status", "connected"), "createdAt": "2026-01-01T00:00:00Z",
            "identity": extra.pop("identity", {"email": SHARED_EMAIL, "verification": True, "source": "fixture"}),
            "usage": {"source": "managed-" + provider, "freshness": "unavailable", "windows": []},
        }
        row.update(extra)
        return row

    def _write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.secret_paths.append(path)

    def _install_default_cli(self):
        default_codex = self.cli_home / ".codex" / "auth.json"
        self._write_json(default_codex, {
            "tokens": {
                "account_id": "acct-default",
                "access_token": DEFAULT_CODEX_ACCESS,
                "refresh_token": "default-codex-refresh-token-LEAKME",
            },
            "source": PLANTED_PATH,
            "email": SHARED_EMAIL,
        })
        default_claude = self.cli_home / ".claude.json"
        self._write_json(default_claude, {
            "oauthAccount": {"accountUuid": "uuid-default", "emailAddress": SHARED_EMAIL},
            "cachedUsageUtilization": {"accountUuid": "uuid-default"},
        })

    def _install_rows(self):
        rows = [
            self._row(self.CODEX_MISSING, "codex"),
            self._row(self.CODEX_MALFORMED, "codex"),
            self._row(self.CODEX_EXPIRED, "codex"),
            self._row(self.CODEX_ACTIVE, "codex"),
            self._row(self.CODEX_INACTIVE, "codex"),
            self._row(self.CLAUDE_MISSING, "claude"),
            self._row(self.CLAUDE_MALFORMED, "claude"),
            self._row(self.CLAUDE_EXPIRED, "claude"),
            self._row(self.CLAUDE_KEYCHAIN_DENIED, "claude"),
            self._row(self.CLAUDE_ACTIVE, "claude"),
            self._row(self.CLAUDE_INACTIVE, "claude"),
            self._row(self.CLAUDE_KEYCHAIN_HEALTHY, "claude"),
            self._row(self.KIMI, "kimi", label="kimi@example.test"),
        ]
        self.accounts._write(rows)
        (self._profile("codex", self.CODEX_MALFORMED) / "auth.json").write_text("{", encoding="utf-8")
        self.secret_paths.append(self._profile("codex", self.CODEX_MALFORMED) / "auth.json")
        self._write_json(self._profile("codex", self.CODEX_EXPIRED) / "auth.json", {
            "tokens": {"account_id": "acct-expired", "access_token": CODEX_ACCESS, "expires_at": PAST_UNIX},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("codex", self.CODEX_ACTIVE) / "auth.json", {
            "tokens": {"account_id": "acct-default", "access_token": CODEX_ACCESS, "refresh_token": CODEX_REFRESH},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("codex", self.CODEX_INACTIVE) / "auth.json", {
            "tokens": {"account_id": "acct-other", "access_token": CODEX_ACCESS, "refresh_token": CODEX_REFRESH},
            "email": SHARED_EMAIL,
            "source": PLANTED_PATH,
        })
        (self._profile("claude", self.CLAUDE_MALFORMED) / ".credentials.json").write_text("{", encoding="utf-8")
        self.secret_paths.append(self._profile("claude", self.CLAUDE_MALFORMED) / ".credentials.json")
        self._write_json(self._profile("claude", self.CLAUDE_EXPIRED) / ".credentials.json", {
            "claudeAiOauth": {"accessToken": CLAUDE_ACCESS, "expiresAt": PAST_UNIX * 1000},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("claude", self.CLAUDE_ACTIVE) / ".credentials.json", {
            "claudeAiOauth": {"accessToken": CLAUDE_ACCESS, "expiresAt": FUTURE_UNIX * 1000},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("claude", self.CLAUDE_ACTIVE) / ".claude.json", {
            "oauthAccount": {"accountUuid": "uuid-default", "emailAddress": SHARED_EMAIL},
        })
        self._write_json(self._profile("claude", self.CLAUDE_INACTIVE) / ".credentials.json", {
            "claudeAiOauth": {"accessToken": CLAUDE_ACCESS, "expiresAt": FUTURE_UNIX * 1000},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("claude", self.CLAUDE_INACTIVE) / ".claude.json", {
            "oauthAccount": {"accountUuid": "uuid-other", "emailAddress": SHARED_EMAIL},
        })
        self._write_json(self._profile("claude", self.CLAUDE_KEYCHAIN_DENIED) / ".claude.json", {
            "oauthAccount": {"accountUuid": "uuid-keychain", "emailAddress": SHARED_EMAIL},
        })
        self._write_json(self._profile("claude", self.CLAUDE_KEYCHAIN_HEALTHY) / ".claude.json", {
            "oauthAccount": {"accountUuid": "uuid-keychain-healthy", "emailAddress": SHARED_EMAIL},
        })
        self._profile("codex", self.CODEX_MISSING)
        self._profile("claude", self.CLAUDE_MISSING)

    def _by_id(self):
        snapshot = self.accounts.snapshot()
        return snapshot, {row["id"]: row for row in snapshot}

    def test_auth_state_fixture_matrix(self):
        snapshot, by_id = self._by_id()
        cases = (
            (self.CODEX_MISSING, "codex", "missing", "credential_missing", False),
            (self.CODEX_MALFORMED, "codex", "malformed", "malformed_json", False),
            (self.CODEX_EXPIRED, "codex", "expired", "expired", False),
            (self.CODEX_ACTIVE, "codex", "connected", None, True),
            (self.CODEX_INACTIVE, "codex", "connected", None, False),
            (self.CLAUDE_MISSING, "claude", "missing", "credential_missing", False),
            (self.CLAUDE_MALFORMED, "claude", "malformed", "malformed_json", False),
            (self.CLAUDE_EXPIRED, "claude", "expired", "expired", False),
            (self.CLAUDE_KEYCHAIN_DENIED, "claude", "keychain_denied", "keychain_denied", False),
            (self.CLAUDE_ACTIVE, "claude", "connected", None, True),
            (self.CLAUDE_INACTIVE, "claude", "connected", None, False),
            (self.CLAUDE_KEYCHAIN_HEALTHY, "claude", "connected", None, False),
        )
        observed = {by_id[item_id]["authObservedAt"] for item_id, *_ in cases}
        self.assertEqual(len(observed), 1)
        observed_at = observed.pop()
        dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        self.assertRegex(observed_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertTrue(observed_at.endswith("Z"))
        for item_id, provider, state, reason, default_active in cases:
            with self.subTest(item_id=item_id, provider=provider, state=state):
                item = by_id[item_id]
                self.assertEqual(item["provider"], provider)
                self.assertEqual(item["status"], "connected")
                self.assertEqual(item["id"], item_id)
                self.assertEqual(item["kind"], "managed_native_auth")
                self.assertIn("usage", item)
                self.assertEqual(item["authState"], state)
                self.assertIn(item["authState"], AUTH_STATES)
                self.assertEqual(item["authObservedAt"], observed_at)
                self.assertIs(item["defaultActive"], default_active)
                if reason is None:
                    self.assertNotIn("authReason", item)
                else:
                    self.assertEqual(item["authReason"], reason)
                    self.assertRegex(item["authReason"], r"^[a-z0-9_]+$")
        kimi = by_id[self.KIMI]
        for key in ("authState", "authReason", "authObservedAt", "defaultActive"):
            self.assertNotIn(key, kimi)
        self.assertEqual(len(snapshot), 13)

    def test_default_active_never_guesses_by_email_or_alias(self):
        _, by_id = self._by_id()
        self.assertIs(by_id[self.CODEX_ACTIVE]["defaultActive"], True)
        self.assertIs(by_id[self.CODEX_INACTIVE]["defaultActive"], False)
        self.assertIs(by_id[self.CLAUDE_ACTIVE]["defaultActive"], True)
        self.assertIs(by_id[self.CLAUDE_INACTIVE]["defaultActive"], False)
        self.assertEqual(by_id[self.CODEX_INACTIVE]["identity"]["email"], SHARED_EMAIL)
        self.assertEqual(by_id[self.CLAUDE_INACTIVE]["identity"]["email"], SHARED_EMAIL)
        self.assertEqual(by_id[self.CODEX_INACTIVE]["label"], SHARED_EMAIL)

    def test_snapshot_redacts_tokens_paths_and_keychain_values(self):
        snapshot, by_id = self._by_id()
        blob = json.dumps(snapshot)
        secrets = (
            CODEX_ACCESS, CODEX_REFRESH, DEFAULT_CODEX_ACCESS, CLAUDE_ACCESS, KEYCHAIN_ACCESS,
            "default-codex-refresh-token-LEAKME", PLANTED_PATH, "Claude Code-credentials-",
        )
        for secret in secrets:
            self.assertNotIn(secret, blob)
        for path in self.secret_paths:
            self.assertNotIn(str(path), blob)
            self.assertNotIn(str(path.resolve()), blob)
        self.assertNotIn(str(self.cli_home), blob)
        self.assertNotIn(str(self.accounts.profile_root), blob)
        for key in ("access_token", "accessToken", "refresh_token", "refreshToken"):
            self.assertNotIn(key, blob)
        healthy = by_id[self.CLAUDE_KEYCHAIN_HEALTHY]
        self.assertEqual(healthy["authState"], "connected")
        denied = by_id[self.CLAUDE_KEYCHAIN_DENIED]
        self.assertEqual(denied["authState"], "keychain_denied")


if __name__ == "__main__":
    unittest.main()
