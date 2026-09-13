import json
import os
import queue
import sys
import tempfile
import time
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
             patch.object(managed, "refresh", return_value={"status": "connected", "usage": {"status": "available", "quotas": []}}):
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
            process.complete_authentication()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                state = managed.poll(self.profile, started["operationID"])
                if state["status"] != "pending_browser":
                    break
                time.sleep(0.01)
            self.assertEqual(state["status"], "connected")
            self.assertEqual(state["usage"]["status"], "available")

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
            if request.full_url.endswith(":loadCodeAssist"):
                return _Response({"paidTier": {"name": "Managed tier"}})
            return _Response({"buckets": [{"modelId": "gemini-pro", "remainingFraction": 0.75, "resetTime": "2026-10-01T00:00:00Z", "tokenType": "REQUESTS"}]})

        with patch.object(managed.urllib.request, "urlopen", side_effect=urlopen):
            result = managed.refresh(self.profile)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["usage"]["scope"], "gemini_code_assist_request_quota")
        self.assertEqual(result["usage"]["quotas"][0]["remainingPercent"], 75.0)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer managed-token")
        self.assertNotIn("owner-token", str(calls))

    def test_missing_managed_auth_is_unknown_not_zero(self):
        result = managed.refresh(self.profile)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["usage"], {"status": "unavailable", "reason": "sign_in_required", "scope": "gemini_code_assist_request_quota", "quotas": []})

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
