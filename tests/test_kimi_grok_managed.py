import io
import os
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import agentcat_grok_managed as grok
import agentcat_kimi_managed as kimi


class FakeProcess:
    def __init__(self, output):
        self.stdout = io.StringIO(output)
        self.code = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout):
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self, _limit):
        return self.payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ManagedDeviceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for module in (kimi, grok):
            module._OPERATIONS.clear()
            module._PROBE = None

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def jwt_with_sub(subject):
        return ManagedDeviceAdapterTests.jwt_with_claims({"sub": subject})

    @staticmethod
    def jwt_with_claims(claims):
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
        return "header." + payload + ".signature"

    def test_kimi_device_login_uses_only_kimi_code_home_and_allowlists_surface(self):
        # An untrusted URL and token-looking field never leave the module.
        output = '{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234","access_token":"secret"}\nhttps://evil.example/device\n'
        process = FakeProcess(output)
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process) as popen:
            profile = Path(self.tmp.name) / "kimi-profile"
            started = kimi.start(profile, "device")
            for _ in range(50):
                result = kimi.poll(profile, started["operationID"])
                if "verificationURL" in result:
                    break
            self.assertEqual(result["verificationURL"], "https://auth.kimi.com/device")
            self.assertEqual(result["userCode"], "ABCD-1234")
            self.assertNotIn("secret", str(result))
            self.assertEqual(popen.call_args.args[0], ["/test/kimi", "login"])
            self.assertEqual(popen.call_args.kwargs["env"]["KIMI_CODE_HOME"], str(profile))

    def test_grok_device_login_uses_device_auth_and_safe_surface(self):
        process = FakeProcess('Visit https://console.x.ai/device user code: ABCD-1234\n')
        with patch.object(grok, "_executable", return_value="/test/grok"), \
             patch.object(grok.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(grok.subprocess, "Popen", return_value=process) as popen:
            profile = Path(self.tmp.name) / "grok-profile"
            started = grok.start(profile, "device")
            for _ in range(50):
                result = grok.poll(profile, started["operationID"])
                if "verificationURL" in result:
                    break
            self.assertEqual(result["verificationURL"], "https://console.x.ai/device")
            self.assertEqual(result["userCode"], "ABCD-1234")
            self.assertEqual(popen.call_args.args[0], ["/test/grok", "login", "--device-auth"])
            self.assertEqual(popen.call_args.kwargs["env"]["GROK_HOME"], str(profile))

    def test_unsupported_browser_and_cancel_are_deterministic(self):
        self.assertEqual(kimi.start(Path(self.tmp.name) / "profile", "browser")["status"], "failed")
        for module in (kimi, grok):
            self.assertEqual(module.cancel(Path(self.tmp.name), "unknown"), "notFound")

    def test_probe_failure_is_safe_and_cached(self):
        for module, reason in ((kimi, "kimi_cli_unsupported"), (grok, "grok_cli_unsupported")):
            module._PROBE = None
            with patch.object(module, "_executable", return_value="/test/cli"), \
                 patch.object(module.subprocess, "run", side_effect=subprocess.TimeoutExpired("cli", 3)) as run:
                self.assertEqual(module.adapter_capability()["reason"], reason)
                self.assertEqual(module.adapter_capability()["reason"], reason)
            self.assertEqual(run.call_count, 1)

    def test_refresh_uses_only_profile_credentials_and_returns_normalized_fixture_usage(self):
        kimi_profile = Path(self.tmp.name) / "kimi"
        (kimi_profile / "credentials").mkdir(parents=True)
        (kimi_profile / "credentials" / "kimi-code.json").write_text('{"expires_at":0}', encoding="utf-8")
        (kimi_profile / "credentials" / "kimi-code-managed.json").write_text('{"access_token":"kimi-secret"}', encoding="utf-8")
        with patch.object(kimi, "urlopen", return_value=FakeResponse('{"data":{"limits":[{"detail":{"used":2,"limit":8}}]}}')) as request:
            result = kimi.refresh(kimi_profile)
        self.assertEqual(result["usage"]["windows"][0]["remainingPercent"], 75.0)
        self.assertNotIn("kimi-secret", str(result))
        self.assertIn("Bearer kimi-secret", request.call_args.args[0].get_header("Authorization"))

        grok_profile = Path(self.tmp.name) / "grok"
        grok_profile.mkdir()
        (grok_profile / "auth.json").write_text('{"oauth":{"access_token":"grok-secret"}}', encoding="utf-8")
        with patch.object(grok, "urlopen", return_value=FakeResponse('{"config":{"currentPeriod":"WEEK","creditUsagePercent":7,"productUsage":{"grok":7}}}')) as request:
            result = grok.refresh(grok_profile)
        self.assertEqual(result["usage"]["windows"], [{"id": "grok:7d", "usedPercent": 7.0, "remainingPercent": 93.0}])
        self.assertEqual(result["usage"]["credits"], {"products": {"grok": 7}})
        self.assertNotIn("subscriptionTier", result["usage"])
        self.assertNotIn("grok-secret", str(result))
        self.assertIn("Bearer grok-secret", request.call_args.args[0].get_header("Authorization"))

    def test_zero_exit_without_new_managed_auth_never_connects(self):
        cases = ((kimi, "kimi", "device", '{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n'), (grok, "grok", "device", 'Visit https://console.x.ai/device user code: ABCD-1234\n'))
        for module, name, mode, output in cases:
            process = FakeProcess(output)
            with self.subTest(provider=name), patch.object(module, "_executable", return_value="/test/cli"), patch.object(module.subprocess, "run", return_value=Mock(returncode=0)), patch.object(module.subprocess, "Popen", return_value=process):
                profile = Path(self.tmp.name) / (name + "-unchanged")
                started = module.start(profile, mode)
                process.code = 0
                result = module.poll(profile, started["operationID"])
            self.assertEqual(result, {"status": "failed", "error": name + "_auth_state_not_updated"})

    def test_zero_exit_with_unchanged_valid_auth_never_connects(self):
        profile = Path(self.tmp.name) / "kimi-existing"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        (credentials / "kimi-code.json").write_text('{"access_token":"existing-secret","account_id":"existing-account"}', encoding="utf-8")
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        with patch.object(kimi, "_executable", return_value="/test/kimi"), patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), patch.object(kimi.subprocess, "Popen", return_value=process), patch.object(kimi, "urlopen") as request:
            started = kimi.start(profile, "device")
            process.code = 0
            result = kimi.poll(profile, started["operationID"])
        self.assertEqual(result, {"status": "failed", "error": "kimi_auth_state_not_updated"})
        request.assert_not_called()
        self.assertTrue((credentials / "kimi-code.json").is_file())
        self.assertEqual(kimi._reauth_backups(profile), [])

    def test_kimi_reauth_stages_existing_native_token_and_commits_verified_successor(self):
        profile = Path(self.tmp.name) / "kimi-reauth-success"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        native = credentials / "kimi-code.json"
        native.write_text('{"access_token":"old-secret","expires_at":4102444800}', encoding="utf-8")
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        usage = '{"limits":[{"detail":{"used":1,"limit":2}}]}'
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process), \
             patch.object(kimi, "urlopen", side_effect=[FakeResponse(usage), FakeResponse('{"user_id":"account-new"}')]):
            started = kimi.start(profile, "device")
            self.assertEqual(started["status"], "pending_device")
            self.assertFalse(native.exists())
            backups = kimi._reauth_backups(profile)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            native.write_text('{"access_token":"new-secret","expires_at":2208988800}', encoding="utf-8")
            process.code = 0
            result = kimi.poll(profile, started["operationID"])
        self.assertTrue(result["authenticated"])
        self.assertEqual(json.loads(native.read_text(encoding="utf-8"))["access_token"], "new-secret")
        self.assertEqual(kimi._reauth_backups(profile), [])
        self.assertNotIn("old-secret", str(result))
        self.assertNotIn("new-secret", str(result))

    def test_kimi_reauth_cancel_restores_existing_native_token(self):
        profile = Path(self.tmp.name) / "kimi-reauth-cancel"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        native = credentials / "kimi-code.json"
        original = b'{"access_token":"old-secret","expires_at":4102444800}'
        native.write_bytes(original)
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process):
            started = kimi.start(profile, "device")
            self.assertFalse(native.exists())
            self.assertEqual(kimi.cancel(profile, started["operationID"]), "canceled")
        self.assertEqual(native.read_bytes(), original)
        self.assertEqual(native.stat().st_mode & 0o777, 0o600)
        self.assertEqual(kimi._reauth_backups(profile), [])

    def test_kimi_reauth_spawn_failure_restores_existing_native_token(self):
        profile = Path(self.tmp.name) / "kimi-reauth-spawn-failure"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        native = credentials / "kimi-code.json"
        original = b'{"access_token":"old-secret","expires_at":4102444800}'
        native.write_bytes(original)
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", side_effect=OSError("unavailable")):
            result = kimi.start(profile, "device")
        self.assertEqual(result, {"status": "failed", "error": "kimi_login_start_failed"})
        self.assertEqual(native.read_bytes(), original)
        self.assertEqual(kimi._reauth_backups(profile), [])

    def test_kimi_reauth_preserves_interrupted_backup_without_overwriting_current_token(self):
        profile = Path(self.tmp.name) / "kimi-reauth-interrupted"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        native = credentials / "kimi-code.json"
        native.write_bytes(b'{"access_token":"current-secret","expires_at":4102444800}')
        interrupted = credentials / (kimi._REAUTH_BACKUP_PREFIX + "interrupted-kimi-code.json.bak")
        interrupted.write_bytes(b'{"access_token":"older-secret","expires_at":4102444800}')
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process):
            started = kimi.start(profile, "device")
            self.assertEqual(kimi.cancel(profile, started["operationID"]), "canceled")
        self.assertIn(b"current-secret", native.read_bytes())
        self.assertIn(b"older-secret", interrupted.read_bytes())

    def test_kimi_reauth_network_proof_failure_restores_old_and_preserves_successor(self):
        profile = Path(self.tmp.name) / "kimi-reauth-unverified-successor"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        native = credentials / "kimi-code.json"
        native.write_bytes(b'{"access_token":"old-secret","expires_at":4102444800}')
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process), \
             patch.object(kimi, "urlopen", side_effect=OSError("offline")):
            started = kimi.start(profile, "device")
            native.write_bytes(b'{"access_token":"new-secret","expires_at":2208988800}')
            process.code = 0
            result = kimi.poll(profile, started["operationID"])
        self.assertEqual(result, {"status": "failed", "error": "kimi_auth_state_not_updated"})
        self.assertIn(b"old-secret", native.read_bytes())
        preserved = [path for path in kimi._reauth_backups(profile) if b"new-secret" in path.read_bytes()]
        self.assertEqual(len(preserved), 1)

    def test_kimi_nonzero_exit_connects_only_after_changed_server_authenticated_credential(self):
        profile = Path(self.tmp.name) / "kimi-post-login-provisioning"
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        live_usage = '{"limits":[{"window":{"duration":7,"timeUnit":"day"},"detail":{"limit":"8","remaining":"6","resetTime":"future"}}]}'
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process), \
             patch.object(kimi, "urlopen", side_effect=[FakeResponse(live_usage), FakeResponse('{"user_id":"kimi-account","email":"kimi@example.test"}')]):
            started = kimi.start(profile, "device")
            credentials = profile / "credentials"
            credentials.mkdir()
            (credentials / "kimi-code.json").write_text(
                '{"access_token":"new-secret","refresh_token":"refresh-secret","expires_at":2208988800}',
                encoding="utf-8",
            )
            process.code = 1
            result = kimi.poll(profile, started["operationID"])

        self.assertEqual(result["status"], "connected")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["usage"]["windows"][0]["usedPercent"], 25.0)
        self.assertEqual(result["identity"]["email"], "kimi@example.test")
        self.assertNotIn("new-secret", str(result))

    def test_kimi_verified_identity_survives_unavailable_usage(self):
        profile = Path(self.tmp.name) / "kimi-usage-unavailable"
        (profile / "credentials").mkdir(parents=True)
        (profile / "credentials" / "kimi-code.json").write_text(
            '{"access_token":"kimi-secret","refresh_token":"refresh-secret","expires_at":2208988800}',
            encoding="utf-8",
        )
        with patch.object(kimi, "urlopen", side_effect=[OSError("offline"), FakeResponse('{"user_id":"kimi-account","email":"kimi@example.test"}')]):
            result = kimi.refresh(profile)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["identity"]["email"], "kimi@example.test")
        self.assertEqual(result["usage"]["freshness"], "unavailable")
        self.assertEqual(result["usage"]["reason"], "usage_unavailable")
        self.assertEqual(result["usage"]["windows"], [])

    def test_kimi_refreshes_expired_access_token_before_server_validation(self):
        profile = Path(self.tmp.name) / "kimi-expired-access"
        (profile / "credentials").mkdir(parents=True)
        (profile / "credentials" / "kimi-code.json").write_text(
            '{"access_token":"expired-secret","refresh_token":"refresh-secret","expires_at":1}',
            encoding="utf-8",
        )
        live_usage = '{"limits":[{"detail":{"limit":"8","remaining":"6"}}]}'
        with patch.object(kimi, "urlopen", side_effect=[
            FakeResponse('{"access_token":"fresh-one","refresh_token":"refresh-one","expires_in":3600,"scope":"scope","token_type":"Bearer"}'),
            FakeResponse(live_usage),
            FakeResponse('{"user_id":"kimi-account","email":"kimi@example.test"}'),
            FakeResponse('{"access_token":"fresh-two","refresh_token":"refresh-two","expires_in":3600,"scope":"scope","token_type":"Bearer"}'),
            FakeResponse(live_usage),
            FakeResponse('{"user_id":"kimi-account","email":"kimi@example.test"}'),
        ]) as request:
            first = kimi.refresh(profile)
            persisted = json.loads((profile / "credentials" / "kimi-code.json").read_text(encoding="utf-8"))
            persisted["expires_at"] = 1
            (profile / "credentials" / "kimi-code.json").write_text(json.dumps(persisted), encoding="utf-8")
            second = kimi.refresh(profile)
        self.assertTrue(first["authenticated"])
        self.assertTrue(second["authenticated"])
        self.assertEqual(second["identity"]["email"], "kimi@example.test")
        self.assertEqual(second["usage"]["windows"][0]["usedPercent"], 25.0)
        self.assertEqual(json.loads((profile / "credentials" / "kimi-code.json").read_text(encoding="utf-8"))["refresh_token"], "refresh-two")
        self.assertFalse((profile / "credentials" / ".agentcat-kimi-refresh-backup-kimi-code.json").exists())
        self.assertIsNone(request.call_args_list[0].args[0].get_header("Authorization"))
        self.assertIn(b"refresh_token=refresh-one", request.call_args_list[3].args[0].data)
        self.assertNotIn("expired-secret", str(second))
        self.assertNotIn("fresh-two", str(second))

    def test_kimi_selects_current_credential_by_expiry_not_glob_order(self):
        profile = Path(self.tmp.name) / "kimi-multiple"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        (credentials / "kimi-code-a.json").write_text('{"access_token":"old-secret","expires_at":100}', encoding="utf-8")
        (credentials / "kimi-code-z.json").write_text('{"access_token":"new-secret","expires_at":4102444800,"account_id":"kimi-new"}', encoding="utf-8")
        with patch.object(kimi, "urlopen", return_value=FakeResponse('{"data":{"limits":[{"detail":{"used":1,"limit":2}}]}}')) as request:
            result = kimi.refresh(profile)
        self.assertTrue(result["authenticated"])
        self.assertNotIn("identity", result)
        self.assertEqual(result["identityStatus"], {"status": "unavailable", "reason": "userinfo_malformed"})
        self.assertIn("Bearer new-secret", request.call_args.args[0].get_header("Authorization"))

    def test_kimi_retry_binds_changed_credential_despite_older_credential_expiry(self):
        profile = Path(self.tmp.name) / "kimi-retry"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        (credentials / "kimi-code-a.json").write_text(
            '{"access_token":"old-secret","expires_at":4102444800,"account_id":"account-a"}',
            encoding="utf-8",
        )
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        usage = '{"data":{"limits":[{"detail":{"used":1,"limit":2}}]}}'
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process), \
             patch.object(kimi, "urlopen", side_effect=[
                 FakeResponse(usage), FakeResponse('{"user_id":"account-b"}'),
                 FakeResponse(usage), FakeResponse('{"user_id":"account-b"}'),
             ]) as request:
            started = kimi.start(profile, "device")
            (credentials / "kimi-code-b.json").write_text(
                '{"access_token":"new-secret","expires_at":2208988800,"account_id":"account-b"}',
                encoding="utf-8",
            )
            process.code = 0
            connected = kimi.poll(profile, started["operationID"])
            refreshed = kimi.refresh(profile)

        self.assertEqual(connected["identityStatus"], {"status": "unavailable", "reason": "email_not_available"})
        self.assertEqual(refreshed["identityStatus"], {"status": "unavailable", "reason": "email_not_available"})
        self.assertTrue(all("Bearer new-secret" in call.args[0].get_header("Authorization") for call in request.call_args_list))
        binding = (profile / ".agentcat-kimi-binding.json").read_text(encoding="utf-8")
        self.assertIn("account-b", binding)
        self.assertNotIn("secret", binding)
        self.assertNotIn("old-secret", str(connected))
        self.assertNotIn("new-secret", str(refreshed))

    def test_kimi_retry_binds_changed_credential_without_identity_claim(self):
        profile = Path(self.tmp.name) / "kimi-opaque-retry"
        credentials = profile / "credentials"
        credentials.mkdir(parents=True)
        (credentials / "kimi-code-a.json").write_text(
            '{"access_token":"old-secret","expires_at":4102444800}', encoding="utf-8"
        )
        process = FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n')
        usage = '{"data":{"limits":[{"detail":{"used":1,"limit":2}}]}}'
        with patch.object(kimi, "_executable", return_value="/test/kimi"), \
             patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(kimi.subprocess, "Popen", return_value=process), \
             patch.object(kimi, "urlopen", side_effect=[
                 FakeResponse(usage), FakeResponse('{"user_id":"account-b"}'),
                 FakeResponse(usage), FakeResponse('{"user_id":"account-b"}'),
             ]) as request:
            started = kimi.start(profile, "device")
            (credentials / "kimi-code-b.json").write_text(
                '{"access_token":"new-secret","expires_at":2208988800}', encoding="utf-8"
            )
            process.code = 0
            connected = kimi.poll(profile, started["operationID"])
            refreshed = kimi.refresh(profile)

        self.assertEqual(connected["identityStatus"], {"status": "unavailable", "reason": "email_not_available"})
        self.assertEqual(refreshed["identityStatus"], {"status": "unavailable", "reason": "email_not_available"})
        self.assertTrue(all("Bearer new-secret" in call.args[0].get_header("Authorization") for call in request.call_args_list))
        binding = (profile / ".agentcat-kimi-binding.json").read_text(encoding="utf-8")
        self.assertIn('"credentialName": "kimi-code-b.json"', binding)
        self.assertIn("account-b", binding)
        self.assertNotIn("secret", binding)

    def test_changed_profiles_connect_to_their_own_native_identity(self):
        processes = [FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"ABCD-1234"}\n'), FakeProcess('{"verification_uri":"https://auth.kimi.com/device","user_code":"EFGH-5678"}\n')]
        with patch.object(kimi, "_executable", return_value="/test/kimi"), patch.object(kimi.subprocess, "run", return_value=Mock(returncode=0)), patch.object(kimi.subprocess, "Popen", side_effect=processes), patch.object(kimi, "urlopen", return_value=FakeResponse('{"data":{"limits":[{"detail":{"used":1,"limit":2}}]}}')):
            profiles = [Path(self.tmp.name) / "kimi-one", Path(self.tmp.name) / "kimi-two"]
            starts = [kimi.start(profile, "device") for profile in profiles]
            for profile, account in zip(profiles, ("account-one", "account-two")):
                credential_dir = profile / "credentials"
                credential_dir.mkdir()
                token = self.jwt_with_sub(account)
                (credential_dir / "kimi-code.json").write_text('{"access_token":"' + token + '"}', encoding="utf-8")
            for process in processes:
                process.code = 0
            results = [kimi.poll(profile, started["operationID"]) for profile, started in zip(profiles, starts)]
        self.assertEqual([result["identityStatus"] for result in results], [
            {"status": "unavailable", "reason": "userinfo_malformed"},
            {"status": "unavailable", "reason": "userinfo_malformed"},
        ])
        self.assertTrue(all(result["authenticated"] for result in results))
        self.assertNotIn("signature", str(results))
        self.assertNotIn(str(profiles[0]), str(results))

    def test_kimi_verified_email_comes_only_from_authenticated_userinfo(self):
        usage = '{"data":{"limits":[{"detail":{"used":1,"limit":2}}]}}'
        profile = Path(self.tmp.name) / "kimi-verified-email"
        (profile / "credentials").mkdir(parents=True)
        token = self.jwt_with_claims({"sub": "forged-jwt-account", "email": "ignored@example.test"})
        (profile / "credentials" / "kimi-code.json").write_text(json.dumps({"access_token": token, "account_id": "forged-raw-account", "email": "not-proof@example.test"}), encoding="utf-8")
        with patch.object(kimi, "urlopen", side_effect=[FakeResponse(usage), FakeResponse('{"user_id":"kimi-account","email":"kimi@example.test"}')]):
            result = kimi.refresh(profile)
        self.assertEqual(result["identity"], {
            "email": "kimi@example.test", "verification": True,
            "source": "kimi_managed_userinfo", "accountID": "kimi-account",
        })
        self.assertNotIn("identityStatus", result)
        with patch.object(kimi, "urlopen", side_effect=[FakeResponse(usage), FakeResponse('{"user_id":"kimi-account","email":"kimi@example.test"}')]):
            kimi._verified_credential(profile, bind=True)
        binding = (profile / ".agentcat-kimi-binding.json").read_text(encoding="utf-8")
        self.assertIn("kimi-account", binding)
        self.assertNotIn("forged-raw-account", binding)
        self.assertNotIn("forged-jwt-account", binding)

    def test_missing_kimi_userinfo_email_and_grok_claims_omit_identity_with_safe_status(self):
        usage = '{"data":{"limits":[{"detail":{"used":1,"limit":2}}]}}'
        billing = '{"config":{"currentPeriod":"WEEK","creditUsagePercent":7,"subscriptionTier":"SuperGrok Heavy"}}'
        kimi_profile = Path(self.tmp.name) / "kimi-missing-email"
        (kimi_profile / "credentials").mkdir(parents=True)
        (kimi_profile / "credentials" / "kimi-code.json").write_text('{"access_token":"kimi-secret","account_id":"kimi-account","email":"not-proof@example.test"}', encoding="utf-8")
        with patch.object(kimi, "urlopen", side_effect=[FakeResponse(usage), FakeResponse('{"user_id":"kimi-account"}')]):
            kimi_result = kimi.refresh(kimi_profile)
        self.assertTrue(kimi_result["authenticated"])
        self.assertNotIn("identity", kimi_result)
        self.assertEqual(kimi_result["identityStatus"], {"status": "unavailable", "reason": "email_not_available"})
        self.assertNotIn("not-proof@example.test", str(kimi_result))

        with patch.object(kimi, "urlopen", side_effect=[FakeResponse(usage), FakeResponse('{"email":"kimi@example.test"}')]):
            malformed_result = kimi.refresh(kimi_profile)
        self.assertNotIn("identity", malformed_result)
        self.assertEqual(malformed_result["identityStatus"], {"status": "unavailable", "reason": "userinfo_malformed"})

        grok_profile = Path(self.tmp.name) / "grok-missing-email"
        grok_profile.mkdir()
        token = self.jwt_with_claims({"sub": "grok-account", "email": "grok@example.test", "email_verified": True})
        (grok_profile / "auth.json").write_text(json.dumps({"oauth": {"access_token": token}, "email": "not-proof@example.test"}), encoding="utf-8")
        with patch.object(grok, "urlopen", return_value=FakeResponse(billing)):
            grok_result = grok.refresh(grok_profile)
        self.assertTrue(grok_result["authenticated"])
        self.assertNotIn("identity", grok_result)
        self.assertEqual(grok_result["identityStatus"], {"status": "unavailable", "reason": "native_email_not_exposed"})
        self.assertEqual(grok_result["usage"]["subscriptionTier"], "SuperGrok Heavy")
        self.assertNotIn("not-proof@example.test", str(grok_result))

    def test_explicit_cli_override_works_with_minimal_path(self):
        directory = Path(self.tmp.name) / "bin"
        directory.mkdir()
        for module, variable, name in ((kimi, "AGENTCAT_KIMI_CLI", "kimi"), (grok, "AGENTCAT_GROK_CLI", "grok")):
            executable = directory / name
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            with self.subTest(provider=name), patch.dict(os.environ, {"PATH": "/definitely-empty", variable: str(executable)}, clear=False):
                self.assertEqual(module._executable(), str(executable))

    def test_capability_probe_supplies_path_for_env_interpreter_when_daemon_path_is_empty(self):
        """An absolute CLI wrapper still needs PATH for ``/usr/bin/env sh``."""
        wrapper = Path(self.tmp.name) / "kimi-env-wrapper"
        wrapper.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        wrapper.chmod(0o700)
        kimi._PROBE = None
        with patch.dict(os.environ, {"AGENTCAT_KIMI_CLI": str(wrapper), "PATH": ""}, clear=True):
            capability = kimi.adapter_capability()
        self.assertTrue(capability["available"])
        self.assertIsNone(capability["reason"])

    def test_capability_probe_augments_system_only_path_for_env_interpreter(self):
        """LaunchAgents can have a nonempty PATH that lacks the CLI runtime."""
        directory = Path(self.tmp.name) / "trusted-bin"
        directory.mkdir()
        interpreter = directory / "agentcat-test-runtime"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o700)
        for module, variable, name in ((kimi, "AGENTCAT_KIMI_CLI", "kimi"), (grok, "AGENTCAT_GROK_CLI", "grok")):
            wrapper = directory / name
            wrapper.write_text("#!/usr/bin/env agentcat-test-runtime\n", encoding="utf-8")
            wrapper.chmod(0o700)
            module._PROBE = None
            with self.subTest(provider=name), patch.object(module, "_FALLBACK_PATH", str(directory)), patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", variable: str(wrapper)}, clear=True):
                capability = module.adapter_capability()
            self.assertTrue(capability["available"])
            self.assertIsNone(capability["reason"])


if __name__ == "__main__":
    unittest.main()
