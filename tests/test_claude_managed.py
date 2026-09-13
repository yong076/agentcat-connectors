import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import agentcat_claude_managed as managed
from agentcat_managed_accounts import ManagedAccounts


class _Process:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout):
        return self.code

    def kill(self):
        self.code = -9


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self, _limit):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ClaudeManagedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "profile"
        managed._OPERATIONS.clear()
        managed._PROBE = None

    def tearDown(self):
        managed._OPERATIONS.clear()
        self.tmp.cleanup()

    def test_start_isolates_official_cli_profile_and_strips_overrides(self):
        process = _Process()
        with patch.object(managed, "_executable", return_value="/trusted/claude"), \
             patch.object(managed, "adapter_capability", return_value={"available": True}), \
             patch.object(managed.subprocess, "Popen", return_value=process) as popen, \
             patch.dict(os.environ, {
                 "ANTHROPIC_API_KEY": "do-not-inherit",
                 "ANTHROPIC_AUTH_TOKEN": "do-not-inherit",
                 "ANTHROPIC_BASE_URL": "https://proxy.invalid",
                 "CLAUDE_CONFIG_DIR": "/owner/.claude",
             }):
            started = managed.start(self.profile, "browser")
        self.assertEqual(started["status"], "pending_browser")
        self.assertEqual(started["browserLaunchMode"], "provider")
        self.assertEqual(popen.call_args.args[0], ["/trusted/claude", "auth", "login", "--claudeai"])
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.profile))
        self.assertEqual(env["CLAUDE_SECURESTORAGE_CONFIG_DIR"], str(self.profile))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o700)
        self.assertEqual(managed.cancel(self.profile, started["operationID"]), "canceled")
        self.assertTrue(process.terminated)

    def test_profile_reader_never_falls_back_to_default_credentials(self):
        owner = Path(self.tmp.name) / "owner"
        owner.mkdir()
        (owner / ".credentials.json").write_text(json.dumps({"accessToken": "owner-secret"}))
        profile = self.profile
        profile.mkdir()
        with patch.object(managed.sys, "platform", "linux"), \
             patch.object(Path, "home", return_value=owner):
            self.assertIsNone(managed._profile_oauth(profile))
        (profile / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "managed-secret"}}))
        with patch.object(managed.sys, "platform", "linux"), \
             patch.object(Path, "home", return_value=owner):
            oauth = managed._profile_oauth(profile)
        self.assertEqual(oauth["accessToken"], "managed-secret")
        self.assertNotIn("owner-secret", str(oauth))

    def test_completed_native_login_requires_same_profile_and_authenticated_profile_proof(self):
        process = _Process()
        with patch.object(managed, "_executable", return_value="/trusted/claude"), \
             patch.object(managed, "adapter_capability", return_value={"available": True}), \
             patch.object(managed.subprocess, "Popen", return_value=process), \
             patch.object(managed, "_status", return_value={"loggedIn": True, "authMethod": "claude.ai"}), \
             patch.object(managed, "_refresh_managed_oauth", return_value={"accessToken": "managed-token"}), \
             patch.object(managed, "_profile_identity", return_value={"identity": {"email": "verified@example.invalid", "verification": True, "source": "claude_oauth_profile"}, "providerIdentity": {"accountID": "account-uuid", "tenantID": "organization-uuid"}}), \
             patch.object(managed, "_quota_usage", return_value={"source": "claude_oauth_usage", "freshness": "live", "windows": [{"id": "claude:5h", "usedPercent": 25}], "credits": None, "spendControl": None, "rateLimitReachedType": None, "tokenUsage": None, "tokenUsageAvailable": False, "scope": "claude_subscription_quota"}):
            started = managed.start(self.profile, "browser")
            process.code = 0
            result = managed.poll(self.profile, started["operationID"])
        self.assertEqual(result["status"], "connected")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["identity"]["email"], "verified@example.invalid")
        self.assertEqual(result["providerIdentity"], {"accountID": "account-uuid", "tenantID": "organization-uuid"})
        self.assertNotIn("managed-token", str(result))
        self.assertNotIn("account-uuid", result["identity"])

    def test_zero_exit_without_same_profile_auth_never_connects(self):
        process = _Process()
        with patch.object(managed, "_executable", return_value="/trusted/claude"), \
             patch.object(managed, "adapter_capability", return_value={"available": True}), \
             patch.object(managed.subprocess, "Popen", return_value=process), \
             patch.object(managed, "_status", return_value={"loggedIn": False}):
            started = managed.start(self.profile, "browser")
            process.code = 0
            result = managed.poll(self.profile, started["operationID"])
        self.assertEqual(result, {"status": "failed", "error": "claude_auth_state_not_updated"})

    def test_usage_normalization_and_unavailable_quota_are_honest(self):
        with patch.object(managed.urllib.request, "urlopen", return_value=_Response({
            "five_hour": {"utilization": 12.5, "resets_at": "2026-09-14T12:00:00Z"},
            "seven_day": {"utilization": 40},
            "extra_usage": {"utilization": 5, "monthly_limit": 100},
        })):
            usage = managed._quota_usage("private-token")
        self.assertEqual([item["id"] for item in usage["windows"]], ["claude:5h", "claude:7d"])
        self.assertEqual(usage["windows"][0]["remainingPercent"], 87.5)
        self.assertEqual(usage["credits"], {"usedPercent": 5.0, "monthlyLimit": 100})
        with patch.object(managed, "_status", return_value={"loggedIn": True, "authMethod": "claude.ai"}), \
             patch.object(managed, "_refresh_managed_oauth", return_value={"accessToken": "private-token"}), \
             patch.object(managed, "_profile_identity", return_value={"identity": {"email": "verified@example.invalid", "verification": True, "source": "claude_oauth_profile"}, "providerIdentity": {"accountID": "account-uuid", "tenantID": "organization-uuid"}}), \
             patch.object(managed, "_quota_usage", side_effect=OSError()):
            result = managed.refresh(self.profile)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["usage"]["freshness"], "unavailable")
        self.assertEqual(result["usage"]["reason"], "quota_unavailable")

    def test_profile_requires_official_account_and_organization_fields(self):
        with patch.object(managed.urllib.request, "urlopen", return_value=_Response({
            "account": {"uuid": "account-uuid", "email": "verified@example.invalid"},
            "organization": {"uuid": "organization-uuid"},
        })):
            proof = managed._profile_identity("private-token")
        self.assertEqual(proof["identity"]["source"], "claude_oauth_profile")
        self.assertEqual(proof["providerIdentity"], {"accountID": "account-uuid", "tenantID": "organization-uuid"})
        with patch.object(managed.urllib.request, "urlopen", return_value=_Response({
            "account": {"uuid": "account-uuid", "email": "verified@example.invalid"},
            "organization": {},
        })):
            with self.assertRaises(ValueError):
                managed._profile_identity("private-token")

    def test_expired_managed_file_credential_rotates_without_global_fallback(self):
        self.profile.mkdir()
        credential = self.profile / ".credentials.json"
        credential.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "expired-token", "refreshToken": "old-refresh", "expiresAt": 1,
            "clientId": "9d1c250a-e61b-44d9-88ed-5944d1962f5e", "scopes": ["user:inference"],
        }}), encoding="utf-8")
        with patch.object(managed.urllib.request, "urlopen", return_value=_Response({
            "access_token": "rotated-token", "refresh_token": "rotated-refresh", "expires_in": 3600,
        })):
            refreshed = managed._refresh_managed_oauth(self.profile)
        self.assertEqual(refreshed["accessToken"], "rotated-token")
        persisted = json.loads(credential.read_text())["claudeAiOauth"]
        self.assertEqual(persisted["refreshToken"], "rotated-refresh")
        self.assertNotIn("old-refresh", str(refreshed))

    def test_profile_401_requires_reconnect_but_quota_failure_does_not(self):
        from urllib.error import HTTPError
        with patch.object(managed, "_status", return_value={"loggedIn": True, "authMethod": "claude.ai"}), \
             patch.object(managed, "_refresh_managed_oauth", return_value={"accessToken": "private-token"}), \
             patch.object(managed, "_profile_identity", side_effect=HTTPError("https://example.invalid", 401, "", {}, None)):
            result = managed.refresh(self.profile)
        self.assertEqual(result["status"], "needs_reconnect")
        self.assertFalse(result["authenticated"])

    def test_verified_same_account_promotion_replaces_canonical_profile_and_rolls_back(self):
        source, destination = self.profile / "source", self.profile / "destination"
        source.mkdir(parents=True); destination.mkdir(parents=True)
        source_file = source / ".credentials.json"
        destination_file = destination / ".credentials.json"
        source_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fresh-token"}}))
        destination_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old-token"}}))
        identity = {"accountID": "account-uuid", "tenantID": "organization-uuid"}
        proof = {"identity": {"email": "verified@example.invalid", "verification": True, "source": "claude_oauth_profile"}, "providerIdentity": identity}
        with patch.object(managed, "_profile_identity", return_value=proof):
            self.assertTrue(managed.promote_verified_profile(source, destination, identity))
        self.assertEqual(json.loads(destination_file.read_text())["claudeAiOauth"]["accessToken"], "fresh-token")
        destination_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old-token"}}))
        with patch.object(managed, "_profile_identity", side_effect=[proof, {"providerIdentity": {"accountID": "other", "tenantID": "organization-uuid"}}]):
            self.assertFalse(managed.promote_verified_profile(source, destination, identity))
        self.assertEqual(json.loads(destination_file.read_text())["claudeAiOauth"]["accessToken"], "old-token")

    def test_managed_registry_merges_same_claude_account_into_existing_id(self):
        identity = {"accountID": "account-uuid", "tenantID": "organization-uuid"}
        proof = {"identity": {"email": "verified@example.invalid", "verification": True, "source": "claude_oauth_profile"}, "providerIdentity": identity}

        class Adapter:
            count = 0
            complete = set()
            def adapter_capability(self):
                return {"available": True, "modes": ["browser"]}
            def start(self, profile, mode):
                self.count += 1
                return {"operationID": f"op-{self.count}", "status": "pending_browser"}
            def poll(self, profile, operation):
                if operation not in self.complete:
                    return {"status": "pending_browser"}
                profile.mkdir(parents=True, exist_ok=True)
                (profile / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": operation + "-token"}}))
                return {"status": "connected", "authenticated": True, **proof,
                        "usage": managed._unavailable_usage("quota_unavailable")}
            def cancel(self, profile, operation): return "canceled"
            def refresh(self, profile): return {"status": "needs_reconnect", "authenticated": False}
            def remove(self, profile): return None
            promote_verified_profile = staticmethod(managed.promote_verified_profile)

        adapter = Adapter()
        with tempfile.TemporaryDirectory() as home, patch.object(managed, "_profile_identity", return_value=proof):
            accounts = ManagedAccounts(Path(home), {"claude": adapter}, dedup_secret=b"x" * 32)
            first = accounts.start("claude", "", "browser")
            adapter.complete.add(first["operationID"])
            first_result = accounts.status("claude", first["operationID"])
            second = accounts.start("claude", "", "browser")
            adapter.complete.add(second["operationID"])
            second_result = accounts.status("claude", second["operationID"])
            rows = accounts.snapshot()
        self.assertEqual(second_result["connection"]["id"], first_result["connection"]["id"])
        self.assertIn("supersededConnectionID", second_result)
        self.assertEqual(len(rows), 1)

    def test_managed_registry_exposes_claude_browser_and_preserves_unknown_identity(self):
        adapter = Mock()
        adapter.adapter_capability.return_value = {"supported": True, "available": True, "reason": None, "modes": ["browser"]}
        adapter.start.return_value = {"operationID": "op", "status": "pending_browser", "browserLaunchMode": "provider"}
        adapter.poll.return_value = {"status": "connected", "authenticated": True,
            "identityStatus": {"status": "unavailable", "reason": "provider_identity_not_exposed"},
            "usage": managed._unavailable_usage("quota_unavailable")}
        with tempfile.TemporaryDirectory() as home:
            accounts = ManagedAccounts(Path(home), {"claude": adapter}, dedup_secret=b"x" * 32)
            started = accounts.start("claude", "", "browser")
            state = accounts.status("claude", started["operationID"])
        self.assertEqual(state["status"], "connected")
        self.assertEqual(state["connection"]["provider"], "claude")
        self.assertEqual(state["connection"]["identityStatus"]["reason"], "provider_identity_not_exposed")
        self.assertEqual(state["connection"]["label"], "")


if __name__ == "__main__":
    unittest.main()
