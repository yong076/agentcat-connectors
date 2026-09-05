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

    def plan_metadata(self, multiplier="5x"):
        return {"accountUuid": "native-private-account", "emailAddress": "private@example.test",
                "organizationUuid": "private-org", "organizationType": "claude_max",
                "organizationRateLimitTier": "default_claude_max_" + multiplier,
                "profileFetchedAt": NOW * 1000}

    def write_plan(self, metadata=None, system=False):
        metadata = self.plan_metadata("20x" if system else "5x") if metadata is None else metadata
        if system:
            path = agentcat.HOME / ".claude.json"
            payload = {"oauthAccount": metadata}
        else:
            data_root = agentcat.HOME / "orca-data"
            os.environ["AGENTCAT_ORCA_DATA_DIR"] = str(data_root)
            path = data_root / "claude-accounts" / "managed-a" / "auth" / "oauth-account.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            (path.parent / ".orca-managed-claude-auth").write_text("managed-a\n", encoding="utf-8")
            payload = metadata
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_plans_stay_per_profile_and_display_max_multiplier(self):
        self.write_plan(system=True)
        self.write_plan()
        rows = self.rows()
        self.assertEqual(rows[0]["planType"], "Max 20x")
        self.assertEqual(self.managed(rows)["planType"], "Max 5x")
        self.assertEqual(rows[0]["limits"]["planType"], "Max 20x")
        self.assertEqual(self.managed(rows)["limits"]["planType"], "Max 5x")
        self.assertEqual(rows[0]["planSource"], "local-profile-metadata")
        self.assertIn("planUpdatedAt", rows[0])
        serialized = json.dumps(rows)
        for secret in ("native-private-account", "private@example.test", "private-org", "orca-data", "default_claude_max"):
            self.assertNotIn(secret, serialized)

    def test_selected_managed_does_not_inherit_default_plan(self):
        self.write_plan(system=True)
        self.write_plan()
        self.payload["result"]["claude"]["activeAccountIdsByRuntime"]["host"] = "managed-a"
        self.payload["result"]["rateLimits"]["claude"] = quota(42, "managed:managed-a")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["planType"], "Max 5x")

    def test_missing_managed_plan_never_inherits_system(self):
        self.write_plan(system=True)
        self.assertEqual(self.rows()[0]["planType"], "Max 20x")
        self.assertNotIn("planType", self.managed())

    def test_missing_system_plan_never_inherits_managed(self):
        self.write_plan()
        self.assertNotIn("planType", self.rows()[0])
        self.assertEqual(self.managed()["planType"], "Max 5x")

    def test_plan_can_survive_missing_quota_without_inventing_usage(self):
        self.write_plan()
        self.payload["result"]["rateLimits"]["inactiveClaudeAccounts"] = []
        row = self.managed()
        self.assertEqual(row["planType"], "Max 5x")
        self.assertEqual(row["limits"]["quotas"], [])
        self.assertFalse(row["tracked"])

    def test_plan_identity_mismatch_or_missing_identity_is_unknown(self):
        for key, value in (("emailAddress", "other@example.test"), ("organizationUuid", "other-org"),
                           ("emailAddress", ""), ("accountUuid", None), ("organizationUuid", [])):
            with self.subTest(key=key, value=value):
                metadata = self.plan_metadata()
                metadata[key] = value
                self.write_plan(metadata)
                self.assertNotIn("planType", self.managed())

    def test_missing_or_wrong_marker_is_unknown(self):
        path = self.write_plan()
        marker = path.parent / ".orca-managed-claude-auth"
        marker.write_text("different-profile", encoding="utf-8")
        self.assertNotIn("planType", self.managed())
        marker.unlink()
        self.assertNotIn("planType", self.managed())

    def test_plan_path_traversal_and_wsl_are_rejected_before_io(self):
        account = copy.deepcopy(self.payload["result"]["claude"]["accounts"][0])
        with patch.object(Path, "open", side_effect=AssertionError("unexpected read")):
            for account_id in ("../elsewhere", "/absolute", "..\\elsewhere", "a/b", "", "a" * 129):
                account["id"] = account_id
                self.assertIsNone(agentcat.orca_claude_profile_plan(account, NOW))
            account.update(id="managed-a", managedAuthRuntime="wsl")
            self.assertIsNone(agentcat.orca_claude_profile_plan(account, NOW))

    def test_plan_symlink_outside_profile_is_rejected(self):
        path = self.write_plan()
        outside = agentcat.HOME / "outside-metadata.json"
        outside.write_text(json.dumps(self.plan_metadata()), encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable on this host")
        self.assertNotIn("planType", self.managed())

    def test_plan_freshness_and_authentication_boundary(self):
        for stamp in (None, True, float("inf"), (NOW - 86401) * 1000, (NOW + 61) * 1000):
            with self.subTest(stamp=stamp):
                metadata = self.plan_metadata()
                metadata["profileFetchedAt"] = stamp
                self.write_plan(metadata)
                self.assertNotIn("planType", self.managed())
        self.write_plan()
        self.payload["result"]["claude"]["accounts"][0]["lastAuthenticatedAt"] = (NOW + 6) * 1000
        self.assertNotIn("planType", self.managed())

    def test_profile_fetch_can_precede_login_commit_by_five_seconds(self):
        self.write_plan()
        self.payload["result"]["claude"]["accounts"][0]["lastAuthenticatedAt"] = (NOW + 2) * 1000
        self.assertEqual(self.managed()["planType"], "Max 5x")

    def test_unknown_plan_or_tier_is_not_guessed(self):
        for organization in (None, [], "private@example.test", "unrecognized_plan"):
            metadata = self.plan_metadata()
            metadata["organizationType"] = organization
            self.write_plan(metadata)
            self.assertNotIn("planType", self.managed())
        metadata = self.plan_metadata()
        metadata["userRateLimitTier"] = "unrecognized_individual_tier"
        self.write_plan(metadata)
        self.assertEqual(self.managed()["planType"], "Max")

    def test_individual_tier_overrides_organization_multiplier(self):
        metadata = self.plan_metadata("20x")
        metadata["userRateLimitTier"] = "default_claude_max_5x"
        self.write_plan(metadata)
        self.assertEqual(self.managed()["planType"], "Max 5x")

    def test_coarse_plans_do_not_inherit_max_multiplier(self):
        for kind, expected in (("claude_pro", "Pro"), ("claude_team", "Team"),
                               ("claude_enterprise", "Enterprise"), ("claude_free", "Free")):
            metadata = self.plan_metadata("20x")
            metadata["organizationType"] = kind
            self.write_plan(metadata)
            self.assertEqual(self.managed()["planType"], expected)

    def test_malformed_and_oversized_plan_files_leave_quotas_intact(self):
        path = self.write_plan()
        for body in ("not-json", "[]", "x" * (2 * 1024 * 1024 + 1)):
            path.write_text(body, encoding="utf-8")
            row = self.managed()
            self.assertNotIn("planType", row)
            self.assertEqual(row["limits"]["shortUsedPercent"], 100)

    def test_plan_permission_error_leaves_quotas_intact(self):
        self.write_plan()
        # Keep HMAC identity generation outside the mocked metadata reads.
        with patch.object(Path, "open", side_effect=PermissionError("private-path")):
            account = self.payload["result"]["claude"]["accounts"][0]
            self.assertIsNone(agentcat.orca_claude_profile_plan(account, NOW))

    def test_plan_opt_out_does_not_disable_quotas(self):
        self.write_plan()
        self.write_plan(system=True)
        os.environ["AGENTCAT_ORCA_PLANS"] = "0"
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("planType" not in row for row in rows))
        self.assertEqual(self.managed(rows)["limits"]["shortUsedPercent"], 100)

    def test_cli_overrides_require_explicit_metadata_scope(self):
        self.write_plan(system=True)
        for variable in ("ORCA_DEV_REPO_ROOT", "ORCA_CLI_COMMAND", "AGENTCAT_ORCA_CLI"):
            with patch.dict(os.environ, {variable: "/different/runtime"}):
                self.assertNotIn("planType", self.rows()[0])

    def test_default_plan_ignores_shell_claude_config_override(self):
        self.write_plan(system=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(agentcat.HOME / "unrelated-profile")
        self.assertEqual(self.rows()[0]["planType"], "Max 20x")

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
