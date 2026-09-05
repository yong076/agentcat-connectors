"""Orca host quota bridge: attribution, privacy, freshness, and failure isolation."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import redirect_module_paths, restore_module_paths

LOADER = SourceFileLoader("agentcat_orca_tests", str(Path(__file__).resolve().parents[1] / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agentcat)
NOW = 1_788_600_000


def quota(percent=20, provenance="system", stamp=NOW):
    return {
        "provider": "claude", "status": "ok", "error": None, "updatedAt": stamp * 1000,
        "session": {"usedPercent": percent, "windowMinutes": 300, "resetsAt": (NOW + 3600) * 1000},
        "weekly": {"usedPercent": 36, "windowMinutes": 10080, "resetsAt": (NOW + 86400) * 1000},
        "fableWeekly": {"usedPercent": 55, "windowMinutes": 10080, "resetsAt": None},
        "usageMetadata": {"authProvenance": provenance, "credentialSource": "/secret/credentials"},
    }


def fixture():
    return {"ok": True, "result": {
        "claude": {
            "accounts": [{"id": "managed-a", "email": "private@example.test", "managedAuthRuntime": "host",
                          "organizationUuid": "private-org", "label": "Private company"}],
            "activeAccountId": None, "activeAccountIdsByRuntime": {"host": None, "wsl": {}},
        },
        "rateLimits": {
            "claudeTarget": {"runtime": "host", "wslDistro": None}, "claude": quota(),
            "inactiveClaudeAccounts": [{"accountId": "managed-a", "rateLimits": quota(100, "managed:managed-a")}],
        },
    }}


class OrcaAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.originals = redirect_module_paths(agentcat, home, home / ".agentcat")
        agentcat.ORCA_ACCOUNTS_ENABLED = True
        agentcat._ORCA_ACCOUNTS_CACHE = []
        agentcat._ORCA_ACCOUNTS_CHECKED_AT = None
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.clock = patch.object(agentcat.time, "time", return_value=NOW)
        self.clock.start()
        self.payload = fixture()

    def tearDown(self):
        self.clock.stop()
        self.env.stop()
        restore_module_paths(agentcat, self.originals)
        self.tmp.cleanup()

    def rows(self):
        return agentcat.orca_claude_instances_from_accounts(self.payload, NOW)

    def managed(self, rows=None):
        ident = agentcat.provider_instance_id("claude", "orca:host:managed:managed-a")
        return next(row for row in (self.rows() if rows is None else rows) if row["id"] == ident)

    def response(self):
        return subprocess.CompletedProcess([], 0, json.dumps(self.payload).encode())

    def test_default_and_managed_are_separate_private_profiles(self):
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["active"])
        self.assertEqual(rows[0]["limits"]["shortUsedPercent"], 20)
        self.assertEqual(self.managed(rows)["limits"]["shortUsedPercent"], 100)
        self.assertEqual(len({r["id"] for r in rows}), 2)
        for row in rows:
            self.assertEqual(row["identityConfidence"], "profile_only")
        serialized = json.dumps(rows, allow_nan=False)
        for secret in ("managed-a", "private@example.test", "private-org", "Private company", "/secret/credentials", "authProvenance"):
            self.assertNotIn(secret, serialized)

    def test_milliseconds_and_model_scope_are_normalized(self):
        limits = self.rows()[0]["limits"]
        self.assertEqual(limits["shortResetAt"], NOW + 3600)
        self.assertEqual(limits["weeklyUsedPercent"], 36)
        fable = next(q for q in limits["quotas"] if q.get("model") == "fable")
        self.assertEqual(fable["usedPercent"], 55)
        self.assertEqual(fable["window"], "7d")
        self.assertFalse(limits["stale"])

    def test_selected_managed_not_duplicated_as_default(self):
        claude = self.payload["result"]["claude"]
        claude["activeAccountId"] = "managed-a"
        claude["activeAccountIdsByRuntime"]["host"] = "managed-a"
        self.payload["result"]["rateLimits"]["claude"] = quota(42, "managed:managed-a")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["active"])
        self.assertEqual(rows[0]["limits"]["shortUsedPercent"], 42)

    def test_switch_never_relabels_previous_profiles_quota(self):
        self.payload["result"]["claude"]["activeAccountIdsByRuntime"]["host"] = "managed-a"
        rows = self.rows()  # Global quota still belongs to system, inactive cache to A.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["limits"]["quotas"], [])
        self.assertTrue(rows[0]["active"])

    def test_inactive_provenance_must_match_account(self):
        raw = self.payload["result"]["rateLimits"]["inactiveClaudeAccounts"][0]["rateLimits"]
        for provenance in (None, "system", "managed:someone-else"):
            with self.subTest(provenance=provenance):
                raw["usageMetadata"]["authProvenance"] = provenance
                self.assertEqual(self.managed()["limits"]["quotas"], [])

    def test_unknown_default_provenance_does_not_create_default_row(self):
        self.payload["result"]["rateLimits"]["claude"].pop("usageMetadata")
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(self.rows()[0]["active"])

    def test_duplicate_profiles_and_quota_rows_use_newest(self):
        self.payload["result"]["claude"]["accounts"] *= 2
        self.payload["result"]["rateLimits"]["inactiveClaudeAccounts"].append(
            {"accountId": "managed-a", "rateLimits": quota(1, "managed:managed-a", NOW - 100)})
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.managed()["limits"]["shortUsedPercent"], 100)

    def test_missing_inactive_quota_never_copies_default(self):
        self.payload["result"]["rateLimits"]["inactiveClaudeAccounts"] = []
        row = self.managed()
        self.assertEqual(row["limits"]["quotas"], [])
        self.assertEqual(row["status"], "discovered")
        self.assertFalse(row["tracked"])

    def test_wsl_quota_and_profiles_are_not_mapped_to_host(self):
        self.payload["result"]["claude"]["accounts"].append({"id": "wsl-b", "managedAuthRuntime": "wsl"})
        self.payload["result"]["rateLimits"]["claudeTarget"] = {"runtime": "wsl", "wslDistro": "private-distro"}
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["limits"]["shortUsedPercent"], 100)

    def test_missing_target_never_guesses_default(self):
        self.payload["result"]["rateLimits"].pop("claudeTarget")
        self.assertEqual(len(self.rows()), 1)

    def test_older_explicit_host_selection_supported(self):
        self.payload["result"]["claude"].pop("activeAccountIdsByRuntime")
        self.assertEqual(len(self.rows()), 2)

    def test_unknown_selection_never_guesses_default(self):
        self.payload["result"]["claude"].pop("activeAccountIdsByRuntime")
        self.payload["result"]["claude"].pop("activeAccountId")
        self.assertEqual(len(self.rows()), 1)

    def test_expired_or_unknown_timestamps_hide_quota(self):
        for stamp in (None, True, float("nan"), float("inf"), 10 ** 1000, (NOW - 901) * 1000, (NOW + 61) * 1000):
            with self.subTest(stamp=str(stamp)[:30]):
                raw = quota()
                raw["updatedAt"] = stamp
                self.assertEqual(agentcat.orca_claude_limits(raw, NOW)["quotas"], [])

    def test_recent_cached_quota_is_explicitly_stale_and_errors_sanitized(self):
        raw = quota(stamp=NOW - 301)
        raw["error"] = "private@example.test /secret/credentials token-secret"
        limits = agentcat.orca_claude_limits(raw, NOW)
        self.assertTrue(limits["stale"])
        self.assertEqual(limits["shortUsedPercent"], 20)
        self.assertNotIn("token-secret", json.dumps(limits))

    def test_invalid_percent_or_window_does_not_become_zero(self):
        for percent in (True, -1, 101, "garbage", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(percent=str(percent)[:30]):
                raw = quota(percent)
                limits = agentcat.orca_claude_limits(raw, NOW)
                self.assertIsNone(limits["shortUsedPercent"])
                json.dumps(limits, allow_nan=False)
        raw = quota()
        raw["session"]["windowMinutes"] = 1440
        self.assertIsNone(agentcat.orca_claude_limits(raw, NOW)["shortUsedPercent"])

    def test_malformed_envelopes_rejected_and_malformed_rows_ignored(self):
        for payload in (None, [], {"ok": False}, {"ok": True, "result": []}):
            with self.assertRaises(ValueError):
                agentcat.orca_claude_instances_from_accounts(payload)
        self.payload["result"]["claude"]["accounts"].extend([None, [], {"id": [], "managedAuthRuntime": "host"}])
        self.payload["result"]["rateLimits"]["inactiveClaudeAccounts"].extend([None, {"accountId": []}])
        self.assertEqual(len(self.rows()), 2)

    def test_snapshot_keeps_codex_and_does_not_mutate_legacy_limits(self):
        legacy = {"claude": {"shortUsedPercent": 80}, "codex": {"status": "auto"}}
        before = copy.deepcopy(legacy)
        with patch.object(agentcat, "codex_provider_instances", return_value=[{"id": "codex:fixture"}]) as codex, \
             patch.object(agentcat, "orca_claude_provider_instances", return_value=self.rows()):
            rows = agentcat.provider_instances_snapshot(legacy)
        codex.assert_called_once_with(legacy["codex"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(legacy, before)

    def test_cache_ttl_is_single_flight_and_returns_copies(self):
        with patch.object(agentcat, "orca_account_cli", return_value="/fixture/orca"), \
             patch.object(agentcat.subprocess, "run", return_value=self.response()) as run, \
             patch.object(agentcat.time, "monotonic", return_value=100):
            with ThreadPoolExecutor(max_workers=4) as pool:
                outputs = list(pool.map(lambda _: agentcat.orca_claude_provider_instances(), range(4)))
            self.assertEqual(run.call_count, 1)
            outputs[0][0]["limits"]["shortUsedPercent"] = -9
            self.assertEqual(agentcat.orca_claude_provider_instances()[0]["limits"]["shortUsedPercent"], 20)
            kwargs = run.call_args.kwargs
            self.assertEqual(run.call_args.args[0], ["/fixture/orca", "account", "list", "--json"])
            self.assertEqual(kwargs["timeout"], 2)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("shell", kwargs)
        with patch.object(agentcat, "orca_account_cli", return_value="/fixture/orca"), \
             patch.object(agentcat.subprocess, "run", return_value=self.response()) as run, \
             patch.object(agentcat.time, "monotonic", return_value=160):
            agentcat.orca_claude_provider_instances()
            run.assert_called_once()

    def test_failures_clear_cached_selection_and_back_off(self):
        for error in (FileNotFoundError("private"), subprocess.TimeoutExpired("private", 2),
                      subprocess.CalledProcessError(1, "private", output="secret"), ValueError("private")):
            agentcat._ORCA_ACCOUNTS_CACHE = self.rows()
            agentcat._ORCA_ACCOUNTS_CHECKED_AT = 0
            with patch.object(agentcat, "orca_account_cli", return_value="/fixture/orca"), \
                 patch.object(agentcat.subprocess, "run", side_effect=error) as run, \
                 patch.object(agentcat.time, "monotonic", return_value=100):
                self.assertEqual(agentcat.orca_claude_provider_instances(), [])
                self.assertEqual(agentcat.orca_claude_provider_instances(), [])
                run.assert_called_once()

    def test_invalid_json_and_oversized_response_fall_back(self):
        for output in (b"not-json", b"x" * (2 * 1024 * 1024 + 1), b'{"ok": false}'):
            agentcat._ORCA_ACCOUNTS_CHECKED_AT = None
            with patch.object(agentcat, "orca_account_cli", return_value="/fixture/orca"), \
                 patch.object(agentcat.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
                self.assertEqual(agentcat.orca_claude_provider_instances(), [])

    def test_successful_empty_inventory_clears_previous_rows(self):
        agentcat._ORCA_ACCOUNTS_CACHE = self.rows()
        self.payload["result"]["claude"]["accounts"] = []
        self.payload["result"]["rateLimits"]["claude"] = None
        with patch.object(agentcat, "orca_account_cli", return_value="/fixture/orca"), \
             patch.object(agentcat.subprocess, "run", return_value=self.response()):
            self.assertEqual(agentcat.orca_claude_provider_instances(), [])

    def test_disabled_and_missing_cli_never_launch_process(self):
        with patch.object(agentcat.subprocess, "run") as run, \
             patch.object(agentcat, "orca_account_cli", return_value=None):
            self.assertEqual(agentcat.orca_claude_provider_instances(), [])
            os.environ["AGENTCAT_ORCA_ACCOUNTS"] = "0"
            self.assertEqual(agentcat.orca_claude_provider_instances(), [])
            run.assert_not_called()

    def test_linux_never_resolves_screen_reader(self):
        with patch.object(agentcat.sys, "platform", "linux"), \
             patch.object(agentcat.shutil, "which", return_value=None) as which:
            self.assertIsNone(agentcat.orca_account_cli())
            which.assert_called_once_with("orca-ide")

    def test_override_is_one_executable_and_never_falls_through(self):
        os.environ["AGENTCAT_ORCA_CLI"] = "/some path/orca"
        with patch.object(agentcat.shutil, "which") as which:
            self.assertEqual(agentcat.orca_account_cli(), "/some path/orca")
            which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
