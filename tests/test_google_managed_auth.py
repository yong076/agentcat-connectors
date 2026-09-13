import json
import os
import queue
import sys
import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))
import agentcat_google_managed_auth as managed


class _Output:
    def __init__(self):
        self.lines = queue.Queue()

    def feed(self, payload):
        self.lines.put(json.dumps(payload) + "\n")

    def close(self):
        self.lines.put(None)

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class _Input:
    def __init__(self, process):
        self.process = process

    def write(self, line):
        request = json.loads(line)
        self.process.requests.append(request)
        if request["method"] == "initialize":
            self.process.stdout.feed({"jsonrpc": "2.0", "id": request["id"], "result": {"authMethods": [{"id": "oauth-personal"}]}})
        elif request["method"] == "authenticate":
            self.process.authenticate_id = request["id"]

    def flush(self):
        return None


class _FakePopen:
    instances = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = _Output()
        self.stdin = _Input(self)
        self.requests = []
        self.authenticate_id = None
        self.returncode = None
        self.__class__.instances.append(self)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.stdout.close()

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.stdout.close()

    def complete_authentication(self):
        self.stdout.feed({"jsonrpc": "2.0", "id": self.authenticate_id, "result": {}})

    def reject_authentication(self):
        self.stdout.feed({"jsonrpc": "2.0", "id": self.authenticate_id, "error": {"code": -32000, "message": "Code Assist setup unavailable"}})


class _Response:
    def __init__(self, value):
        self.value = value

    def read(self):
        return json.dumps(self.value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


class GoogleManagedAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "agentcat-profile"
        _FakePopen.instances.clear()
        with managed._SESSIONS_LOCK:
            managed._SESSIONS.clear()

    def tearDown(self):
        with managed._SESSIONS_LOCK:
            sessions = list(managed._SESSIONS.values())
            managed._SESSIONS.clear()
        for session in sessions:
            session.close()
        self.tmp.cleanup()

    def test_start_uses_official_acp_in_an_isolated_gemini_profile(self):
        original_home = os.environ.get("HOME")
        with patch.object(managed.shutil, "which", return_value="/fake/gemini"), \
             patch.object(managed.subprocess, "Popen", _FakePopen), \
             patch.object(managed, "refresh", return_value={"status": "connected", "identity": {"email": "managed.user@example.com", "verification": True, "source": "google_userinfo"}, "usage": {"status": "available", "quotas": []}}):
            started = managed.start(self.profile, "browser")
            self.assertEqual(started["status"], "pending_browser")
            self.assertEqual(started["browserLaunchMode"], "provider")
            process = _FakePopen.instances[0]
            self.assertEqual(process.args[:2], ["/fake/gemini", "--acp"])
            self.assertEqual(process.kwargs["env"]["GEMINI_CLI_HOME"], str(self.profile))
            self.assertEqual(process.kwargs["env"].get("HOME"), original_home)
            self.assertNotIn("GEMINI_API_KEY", process.kwargs["env"])
            self.assertEqual(process.requests[1]["method"], "authenticate")
            self.assertEqual(process.requests[1]["params"], {"methodId": "oauth-personal"})
            self.assertEqual(managed.poll(self.profile, started["operationID"])["status"], "pending_browser")
            credentials_path = self.profile / ".gemini" / "oauth_creds.json"
            credentials_path.parent.mkdir(parents=True)
            credentials_path.write_text(json.dumps({"refresh_token": "managed-refresh"}), encoding="utf-8")
            process.complete_authentication()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                state = managed.poll(self.profile, started["operationID"])
                if state["status"] != "pending_browser":
                    break
                time.sleep(0.01)
            self.assertEqual(state["status"], "connected")
            self.assertTrue(state["authenticated"])
            self.assertEqual(state["identity"]["email"], "managed.user@example.com")
            self.assertEqual(state["usage"]["status"], "available")

    def test_resolver_uses_trusted_home_fallback_when_launchagent_path_is_minimal(self):
        executable = Path(self.tmp.name) / ".local" / "bin" / "gemini"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"AGENTCAT_GEMINI_CLI": "", "HOME": self.tmp.name, "PATH": ""}):
            self.assertEqual(managed._gemini_executable(), str(executable))

    def test_acp_setup_error_keeps_a_profile_connected_only_after_verified_refresh(self):
        with patch.object(managed.shutil, "which", return_value="/fake/gemini"), \
             patch.object(managed.subprocess, "Popen", _FakePopen), \
             patch.object(managed, "refresh", return_value={
                 "status": "connected",
                 "authenticated": True,
                 "identity": {"email": "managed.user@example.com", "verification": True, "source": "google_userinfo"},
                 "usage": {"status": "unavailable", "reason": "code_assist_onboarding_required", "scope": "gemini_code_assist_request_quota", "quotas": []},
             }) as refresh:
            started = managed.start(self.profile, "browser")
            process = _FakePopen.instances[0]
            process.reject_authentication()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                state = managed.poll(self.profile, started["operationID"])
                if state["status"] != "pending_browser":
                    break
                time.sleep(0.01)
        self.assertEqual(state["status"], "connected")
        self.assertTrue(state["authenticated"])
        self.assertEqual(state["identity"]["email"], "managed.user@example.com")
        self.assertEqual(state["usage"]["reason"], "code_assist_onboarding_required")
        refresh.assert_called_once_with(self.profile)

    def test_acp_setup_error_cannot_connect_from_credentials_without_authentication_proof(self):
        with patch.object(managed.shutil, "which", return_value="/fake/gemini"), \
             patch.object(managed.subprocess, "Popen", _FakePopen), \
             patch.object(managed, "refresh", return_value={
                 "status": "needs_reconnect",
                 "identityStatus": {"status": "unavailable", "reason": "sign_in_required"},
             }):
            started = managed.start(self.profile, "browser")
            _FakePopen.instances[0].reject_authentication()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                state = managed.poll(self.profile, started["operationID"])
                if state["status"] != "pending_browser":
                    break
                time.sleep(0.01)
        self.assertEqual(state, {"status": "failed", "error": "Gemini did not accept this sign-in."})

    def test_refresh_reads_only_the_managed_profile_and_keeps_quota_scope_explicit(self):
        owner = Path(self.tmp.name) / "owner"
        (owner / ".gemini").mkdir(parents=True)
        (owner / ".gemini" / "oauth_creds.json").write_text(json.dumps({"access_token": "owner-token"}), encoding="utf-8")
        managed_creds = self.profile / ".gemini"
        managed_creds.mkdir(parents=True)
        (managed_creds / "oauth_creds.json").write_text(json.dumps({"access_token": "managed-token", "expiry_date": time.time() * 1000 + 3_600_000, "client_id": "id", "client_secret": "secret"}), encoding="utf-8")
        calls = []

        def urlopen(request, timeout):
            calls.append(request)
            if request.full_url == managed.GOOGLE_USERINFO_URL:
                return _Response({"email": "managed.user@example.com", "verified_email": True})
            if request.full_url.endswith(":loadCodeAssist"):
                return _Response({"currentTier": {"id": "managed-tier"}, "paidTier": {"name": "Managed tier"}})
            return _Response({"buckets": [{"modelId": "gemini-pro", "remainingFraction": 0.75, "resetTime": "2026-10-01T00:00:00Z", "tokenType": "REQUESTS"}]})

        with patch.object(managed.urllib.request, "urlopen", side_effect=urlopen):
            result = managed.refresh(self.profile)
        self.assertEqual(result["status"], "connected")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["identity"], {"email": "managed.user@example.com", "verification": True, "source": "google_userinfo"})
        self.assertEqual(result["usage"]["scope"], "gemini_code_assist_request_quota")
        self.assertEqual(result["usage"]["quotas"][0]["remainingPercent"], 75.0)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer managed-token")
        self.assertNotIn("owner-token", str(calls))

    def test_missing_managed_auth_is_unknown_not_zero(self):
        result = managed.refresh(self.profile)
        self.assertEqual(result["status"], "needs_reconnect")
        self.assertNotIn("identity", result)
        self.assertEqual(result["identityStatus"], {"status": "unavailable", "reason": "sign_in_required"})
        self.assertEqual(result["usage"], {"status": "unavailable", "reason": "sign_in_required", "scope": "gemini_code_assist_request_quota", "quotas": []})

    def test_missing_userinfo_email_is_explicitly_unavailable(self):
        credential_dir = self.profile / ".gemini"
        credential_dir.mkdir(parents=True)
        (credential_dir / "oauth_creds.json").write_text(json.dumps({"access_token": "managed-token", "expiry_date": time.time() * 1000 + 3_600_000}), encoding="utf-8")
        with patch.object(managed.urllib.request, "urlopen", return_value=_Response({"id": "opaque-google-id", "verified_email": True})), \
             patch.object(managed, "_usage_from_profile", return_value={"status": "available", "quotas": []}):
            result = managed.refresh(self.profile)
        self.assertEqual(result["status"], "connected")
        self.assertNotIn("identity", result)
        self.assertEqual(result["identityStatus"], {"status": "unavailable", "reason": "email_not_available"})

    def test_refresh_keeps_verified_login_when_quota_is_unavailable(self):
        credential_dir = self.profile / ".gemini"
        credential_dir.mkdir(parents=True)
        (credential_dir / "oauth_creds.json").write_text(json.dumps({"access_token": "managed-token", "expiry_date": time.time() * 1000 + 3_600_000}), encoding="utf-8")
        forbidden = urllib.error.HTTPError("https://example.invalid", 403, "forbidden", None, None)
        with patch.object(managed.urllib.request, "urlopen", return_value=_Response({"email": "managed.user@example.com", "verified_email": True})), \
             patch.object(managed, "_usage_from_profile", side_effect=forbidden):
            result = managed.refresh(self.profile)
        self.assertEqual(result["status"], "connected")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["identity"], {"email": "managed.user@example.com", "verification": True, "source": "google_userinfo"})
        self.assertEqual(result["usage"], {"status": "unavailable", "reason": "usage_unavailable", "scope": "gemini_code_assist_request_quota", "quotas": []})

    def test_usage_reports_official_onboarding_requirement_without_posting_onboard(self):
        credential_dir = self.profile / ".gemini"
        credential_dir.mkdir(parents=True)
        (credential_dir / "oauth_creds.json").write_text(json.dumps({"access_token": "managed-token", "expiry_date": time.time() * 1000 + 3_600_000}), encoding="utf-8")
        calls = []

        def post(method, payload, token):
            calls.append((method, payload, token))
            return {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}

        with patch.object(managed, "_code_assist_post", side_effect=post):
            usage = managed._usage_from_profile(self.profile)
        self.assertEqual(usage, {"status": "unavailable", "reason": "code_assist_onboarding_required", "source": "gemini_code_assist", "scope": "gemini_code_assist_request_quota", "quotas": []})
        self.assertEqual([method for method, _, _ in calls], ["loadCodeAssist"])

    def test_remove_touches_only_known_managed_credential_files(self):
        gemini_dir = self.profile / ".gemini"
        gemini_dir.mkdir(parents=True)
        (gemini_dir / "oauth_creds.json").write_text("{}", encoding="utf-8")
        (gemini_dir / "google_accounts.json").write_text("{}", encoding="utf-8")
        keep = self.profile / "keep.txt"
        keep.write_text("keep", encoding="utf-8")
        managed.remove(self.profile)
        self.assertFalse((gemini_dir / "oauth_creds.json").exists())
        self.assertFalse((gemini_dir / "google_accounts.json").exists())
        self.assertEqual(keep.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
