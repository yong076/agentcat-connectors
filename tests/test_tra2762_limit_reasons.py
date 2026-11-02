"""TRA-2762: Codex and Gemini limits carry a reason whenever they are not_configured.

App 26.41.9 shows no recovery for a reasonless not_configured (agents without a quota
source). Codex and Gemini used to send that same shape both when the CLI was logged out
and when an authenticated account simply had no quota, so a logged-out Codex lost its
"sign in" prompt. Logged out is now token_missing; no quota is not_applicable.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from tests.sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_tra2762", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_tra2762", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class LimitReasonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.agentcat_home = root / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        assert_sandboxed(agentcat, self.home, self.agentcat_home)
        self.env = patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env.start()
        os.environ.pop("GOOGLE_GENAI_USE_GCA", None)
        self.network = patch.object(
            agentcat.urllib.request,
            "urlopen",
            side_effect=AssertionError("network must be mocked in TRA-2762 tests"),
        )
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()


class CodexLimitReasonTests(LimitReasonTestCase):
    def test_logged_out_codex_home_needs_a_login(self):
        with patch.object(agentcat, "read_codex_auth", return_value=None):
            limits = agentcat.codex_live_limits(force=True)
        self.assertEqual(limits["status"], "not_configured")
        self.assertEqual(limits["reason"], "token_missing")

    def test_auth_without_an_access_token_needs_a_login(self):
        limits = agentcat.codex_live_limits_for_auth({"tokens": {}}, "codex:test-no-token", force=True)
        self.assertEqual(limits["reason"], "token_missing")

    def test_api_key_login_has_no_subscription_quota(self):
        limits = agentcat.codex_live_limits_for_auth({"OPENAI_API_KEY": "sk-test"}, "codex:test-api-key", force=True)
        self.assertEqual(limits["status"], "not_configured")
        self.assertEqual(limits["reason"], "not_applicable")

    def test_authenticated_usage_without_quota_or_plan_is_not_applicable(self):
        limits = agentcat.codex_limits_from_usage_response({})
        self.assertEqual(limits["status"], "not_configured")
        self.assertEqual(limits["reason"], "not_applicable")


class GeminiLimitReasonTests(LimitReasonTestCase):
    def write_settings(self, selected_type):
        path = self.home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"security": {"auth": {"selectedType": selected_type}}}), encoding="utf-8")

    def test_api_key_auth_has_no_code_assist_quota(self):
        self.write_settings("gemini-api-key")
        limits = agentcat.gemini_live_limits(force=True)
        self.assertEqual(limits["reason"], "not_applicable")

    def test_oauth_without_a_token_needs_a_login(self):
        self.write_settings("oauth-personal")
        with patch.object(agentcat, "gemini_access_token", return_value=None):
            limits = agentcat.gemini_live_limits(force=True)
        self.assertEqual(limits["reason"], "token_missing")

    def test_gemini_never_set_up_stays_reasonless(self):
        with patch.object(agentcat, "read_gemini_auth_type", return_value=None):
            limits = agentcat.gemini_live_limits(force=True)
        self.assertEqual(limits["status"], "not_configured")
        self.assertIsNone(limits.get("reason"))

    def test_authenticated_quota_without_buckets_is_not_applicable(self):
        limits = agentcat.gemini_limits_from_quota_response({})
        self.assertEqual(limits["status"], "not_configured")
        self.assertEqual(limits["reason"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
