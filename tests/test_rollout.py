import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

loader = SourceFileLoader("rollout_connector", str(Path(__file__).resolve().parents[1] / "bin/agentcat"))
spec = importlib.util.spec_from_loader(loader.name, loader)
agentcat = importlib.util.module_from_spec(spec)
loader.exec_module(agentcat)


class RolloutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patcher = patch.object(agentcat, "AGENTCAT_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.version = "99.0.0"

    def payload(self, **kwargs):
        return dict(version=self.version, percent=100, halted=False, reason=None, **kwargs)

    def test_identifier_is_stable_private_and_bucket_is_version_specific(self):
        identity = agentcat.connector_rollout_id()
        self.assertRegex(identity, r"^[0-9a-f]{32}$")
        self.assertEqual(identity, agentcat.connector_rollout_id())
        if os.name == "posix":
            self.assertEqual((self.home / "rollout-id").stat().st_mode & 0o777, 0o600)
        for version in ("99.0.0", "99.0.1"):
            bucket = int(hashlib.sha256(f"{identity}:{version}".encode()).hexdigest()[:8], 16) % 100
            for percent, expected in ((bucket, False), (bucket + 1, True)):
                with patch.object(agentcat, "fetch_rollout_json", return_value=dict(version=version, percent=percent, halted=False, reason=None)):
                    status = agentcat.connector_rollout_status(version)
                    self.assertEqual(status["bucketAllowed"], expected)
                    self.assertEqual(set(status), {"percent", "bucketAllowed", "halted", "reason"})
                    self.assertNotIn(identity, json.dumps(status))

    def test_halt_overrides_full_rollout_and_never_uses_fallback(self):
        with patch.object(agentcat, "fetch_rollout_json", return_value=dict(version=self.version, percent=100, halted=True, reason="health_halt")) as fetch:
            status = agentcat.connector_rollout_status(self.version)
        self.assertFalse(status["bucketAllowed"])
        self.assertTrue(status["halted"])
        self.assertEqual(status["reason"], "health_halt")
        fetch.assert_called_once_with(agentcat.ROLLOUT_URL + "?version=" + self.version)

    def test_malformed_and_wrong_version_responses_fail_closed(self):
        for payload in ([], {}, self.payload() | {"percent": True}, self.payload() | {"percent": 101},
                        self.payload() | {"halted": "false"}, self.payload() | {"version": "1.0.0"},
                        self.payload() | {"reason": []}):
            with self.subTest(payload=payload), patch.object(agentcat, "fetch_rollout_json", return_value=payload) as fetch:
                self.assertFalse(agentcat.connector_rollout_status(self.version)["bucketAllowed"])
                self.assertEqual(fetch.call_count, 1)

    def test_unreachable_service_requires_matching_promotion_older_than_72_hours(self):
        for hours, expected in ((-1, False), (1, False), (71.9, False), (72.1, True), (100, True)):
            promoted = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
            with self.subTest(hours=hours), patch.object(agentcat, "fetch_rollout_json", side_effect=[OSError(), {"version": self.version, "promotedAt": promoted}]):
                self.assertEqual(agentcat.connector_rollout_status(self.version)["bucketAllowed"], expected)
        for public in ({"version": "1.0.0"}, {"version": self.version, "promotedAt": "bad"},
                       {"version": self.version, "promotedAt": "2020-01-01T00:00:00"}, OSError()):
            with patch.object(agentcat, "fetch_rollout_json", side_effect=[OSError(), public]):
                self.assertFalse(agentcat.connector_rollout_status(self.version)["bucketAllowed"])

    def test_force_bypasses_network_and_identifier(self):
        with patch.object(agentcat, "fetch_rollout_json", side_effect=AssertionError()), patch.object(agentcat, "connector_rollout_id", side_effect=AssertionError()):
            self.assertTrue(agentcat.connector_rollout_status(self.version, force=True)["bucketAllowed"])

    def test_corrupt_identity_defers_without_replacing_it(self):
        (self.home / "rollout-id").write_text("bad")
        self.assertEqual(agentcat.connector_rollout_status(self.version)["reason"], "rollout_id_unavailable")
        self.assertEqual((self.home / "rollout-id").read_text(), "bad")

    def test_staged_check_is_recorded_in_snapshot_and_retries(self):
        staged = dict(percent=10, bucketAllowed=False, halted=False, reason="cohort_wait")
        allowed = dict(staged, percent=100, bucketAllowed=True, reason=None)
        with patch.object(agentcat, "auto_update_enabled_status", return_value=(True, "enabled")), \
             patch.object(agentcat, "fetch_remote_connector_version", return_value=self.version), \
             patch.object(agentcat, "connector_rollout_status", side_effect=[staged, allowed]), \
             patch.object(agentcat, "start_auto_update_install", return_value=type("Proc", (), {"pid": 123})()) as install:
            self.assertEqual(agentcat.check_auto_update_once()["status"], "staged")
            install.assert_not_called()
            self.assertEqual(agentcat.auto_update_status_snapshot()["rollout"], staged)
            self.assertEqual(agentcat.check_auto_update_once()["status"], "update_started")
            install.assert_called_once_with(self.version)

    def test_check_only_does_not_apply_or_fetch_rollout(self):
        with patch.object(agentcat, "auto_update_enabled_status", return_value=(True, "enabled")), \
             patch.object(agentcat, "fetch_remote_connector_version", return_value=self.version), \
             patch.object(agentcat, "connector_rollout_status") as rollout:
            self.assertEqual(agentcat.check_auto_update_once(apply_update=False)["status"], "update_available")
            rollout.assert_not_called()

    def test_operator_manifest_url_bypasses_the_gate_only_when_overridden(self):
        # verify_update_path.py points a daemon at a pre-release; the rollout
        # service answers "not_latest" for it, which would block the gate itself.
        def run(manifest_url):
            with patch.object(agentcat, "AUTO_UPDATE_MANIFEST_URL", manifest_url), \
                 patch.object(agentcat, "auto_update_enabled_status", return_value=(True, "enabled")), \
                 patch.object(agentcat, "fetch_remote_connector_version", return_value=self.version), \
                 patch.object(agentcat, "fetch_rollout_json", return_value=dict(version=self.version, percent=0, halted=False, reason="not_latest")), \
                 patch.object(agentcat, "start_auto_update_install", return_value=type("P", (), {"pid": 7})()):
                return agentcat.check_auto_update_once(apply_update=True)

        self.assertEqual(run(agentcat.PUBLIC_MANIFEST_URL)["status"], "staged")
        pre = "https://github.com/yong076/agentcat-connectors/releases/download/v99.0.0/connector-manifest.json"
        self.assertEqual(run(pre)["status"], "update_started")

    def test_cli_forwards_force_flag(self):
        import argparse
        with patch.object(agentcat, "check_auto_update_once", return_value={"status": "update_started"}) as check, patch("sys.stdout", new=io.StringIO()):
            agentcat.command_update_check(argparse.Namespace(apply=True, force=True, json=True))
            check.assert_called_once_with(apply_update=True, force=True)
