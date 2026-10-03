import importlib.util
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import verify_update_path as verify
import check_promotion as promotion


class VerifyUpdatePathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.verifier = verify.Verification("26.40.6", "macos", self.root, self.root)
        self.verifier.install_dir.mkdir(parents=True)
        self.verifier.plist.parent.mkdir(parents=True)
        self.original = plistlib.dumps({"EnvironmentVariables": {"HOME": "sandbox"}})
        self.verifier.plist.write_bytes(self.original)
        self.report = {}
        uid = patch.object(verify.os, "getuid", return_value=1000, create=True)
        uid.start()
        self.addCleanup(uid.stop)

    def setup_execute(self):
        payloads = [{"prerelease": True}, {"version": "26.40.6", "sha256": "a" * 64},
                    {"version": "26.40.5"}, {"version": "26.40.3"}]
        for name, kwargs in (("get_json", {"side_effect": payloads}),):
            p = patch.object(verify, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        mocks = {}
        for name in ("download", "install", "wait_served", "wait_installed", "trigger", "reload", "check_macos", "snapshot_version", "stop_test_updater"):
            p = patch.object(self.verifier, name)
            mocks[name] = p.start()
            self.addCleanup(p.stop)
        mocks["download"].side_effect = ["target", "baseline", "recovery"]
        mocks["snapshot_version"].return_value = "26.40.3"
        mocks["wait_served"].side_effect = lambda version: version
        return mocks

    def test_success_attests_actual_baseline_and_target(self):
        mocks = self.setup_execute()
        self.verifier.execute(self.report)
        self.assertEqual(self.report, dict(archiveSha256="a" * 64, fromVersion="26.40.5", servedVersion="26.40.6"))
        mocks["install"].assert_called_once_with("baseline")
        mocks["check_macos"].assert_called_once_with(self.original)

    def test_failure_restores_original_release_plist_and_service(self):
        mocks = self.setup_execute()
        def fail():
            self.verifier.plist.write_bytes(b"temporary settings")
            raise RuntimeError("failed update")
        mocks["trigger"].side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "failed update"):
            self.verifier.execute(self.report)
        self.assertEqual([call.args[0] for call in mocks["install"].call_args_list], ["baseline", "recovery"])
        self.assertEqual(self.verifier.plist.read_bytes(), self.original)
        mocks["reload"].assert_called_once()
        mocks["wait_served"].assert_called_with("26.40.3")

    def test_terminated_log_rejects_and_restores(self):
        mocks = self.setup_execute()
        mocks["trigger"].side_effect = lambda: (self.root / ".agentcat/auto-update.err.log").write_text("Terminated\n")
        with self.assertRaisesRegex(RuntimeError, "Terminated"):
            self.verifier.execute(self.report)
        mocks["install"].assert_called_with("recovery")

    def test_macos_injects_test_environment_and_restores_lingering_values(self):
        with patch.object(self.verifier, "reload") as reload, patch.object(self.verifier, "wait_served"), patch.object(verify, "run"):
            self.verifier.trigger()
            env = plistlib.loads(self.verifier.plist.read_bytes())["EnvironmentVariables"]
            self.assertEqual(env[verify.TEST_ENV[1]], "20")
            self.assertEqual(env[verify.TEST_ENV[0]], self.verifier.manifest_url)
            self.verifier.check_macos(self.original)
            self.assertEqual(self.verifier.plist.read_bytes(), self.original)
            self.assertEqual(reload.call_count, 2)

    def test_windows_uses_powershell7_force_and_handles_legacy_baseline(self):
        self.verifier.system = "windows"
        for help_text, force in (("--force", True), ("--apply", False)):
            with patch.object(verify, "run") as run:
                run.return_value.stdout = help_text
                self.verifier.trigger()
                command = run.call_args.args[0]
                self.assertEqual(command[0], "pwsh")
                self.assertEqual("--force" in command[-1], force)
                self.assertIn("update-check --apply", command[-1])
                self.assertEqual(run.call_args.kwargs["env"][verify.TEST_ENV[0]], self.verifier.manifest_url)

    def test_upload_report_has_only_contract_fields_and_no_private_details(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        def execute(report):
            report.update(archiveSha256="a" * 64, fromVersion="26.40.5", servedVersion="26.40.6")
        with patch.object(verify.platform, "system", return_value="Darwin"), \
             patch.object(verify.Verification, "execute", side_effect=execute), patch.object(verify, "run") as run:
            self.assertEqual(verify.main(["--target", "26.40.6", "--upload"]), 0)
        report = json.loads((self.root / "verify-macos.json").read_text())
        self.assertEqual(set(report), {"version", "archiveSha256", "fromVersion", "servedVersion", "os", "arch", "durationSec", "passed", "checkedAt"})
        self.assertTrue(report["passed"])
        self.assertNotIn(str(self.root), json.dumps(report))
        self.assertIn("--clobber", run.call_args.args[0])

    def test_promotion_requires_both_matching_successful_attestations(self):
        (self.root / "connector-manifest.json").write_text(json.dumps({"version": "26.40.6", "sha256": "a" * 64}))
        for system in ("macos", "windows"):
            report = dict(version="26.40.6", servedVersion="26.40.6", fromVersion="26.40.5", os=system, passed=True, archiveSha256="a" * 64)
            (self.root / f"verify-{system}.json").write_text(json.dumps(report))
        self.assertEqual(promotion.validate("26.40.6", self.root)["version"], "26.40.6")
        path = self.root / "verify-windows.json"
        original = json.loads(path.read_text())
        for key, value in (("passed", False), ("passed", "true"), ("version", "26.40.4"),
                           ("archiveSha256", "b" * 64), ("servedVersion", "26.40.5"), ("os", "macos"), ("fromVersion", "26.40.6")):
            path.write_text(json.dumps(dict(original, **{key: value})))
            with self.assertRaises(ValueError):
                promotion.validate("26.40.6", self.root)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            promotion.validate("26.40.6", self.root)

    def test_baseline_install_failure_still_restores_previous_release(self):
        mocks = self.setup_execute()
        mocks["install"].side_effect = [RuntimeError("baseline failed"), None]
        with self.assertRaisesRegex(RuntimeError, "baseline failed"):
            self.verifier.execute(self.report)
        mocks["install"].assert_called_with("recovery")
        mocks["stop_test_updater"].assert_called_once()

    def test_windows_failure_restores_previous_release_without_launchctl(self):
        mocks = self.setup_execute()
        self.verifier.system = "windows"
        mocks["trigger"].side_effect = RuntimeError("update failed")
        with self.assertRaisesRegex(RuntimeError, "update failed"):
            self.verifier.execute(self.report)
        mocks["install"].assert_called_with("recovery")
        mocks["reload"].assert_not_called()

    @unittest.skipUnless(os.name == "posix", "requires POSIX process signals")
    def test_recovery_stops_detached_descendants_before_parent(self):
        import signal
        from types import SimpleNamespace
        state = self.root / ".agentcat/auto-update.json"
        state.write_text(json.dumps({"status": "update_started", "installPid": 123}))
        process_list = f"123 1 bash {self.verifier.install_dir}/install.sh\n124 123 python public_channel_install.py\n125 124 python install.py\n"
        with patch.object(verify, "run", return_value=SimpleNamespace(stdout=process_list)), patch.object(verify.os, "kill") as kill:
            self.verifier.stop_test_updater()
        self.assertEqual([call.args for call in kill.call_args_list], [(125, signal.SIGKILL), (124, signal.SIGKILL), (123, signal.SIGKILL)])

    def test_old_success_record_cannot_pass_a_new_attempt(self):
        log = self.root / ".agentcat/auto-update.out.log"
        log.write_text(json.dumps({"status": "installed", "version": "26.40.6"}))
        self.verifier.out_offset = log.stat().st_size
        with patch.object(verify.time, "monotonic", side_effect=[0, 1, 61]), patch.object(verify.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "successful install result"):
                self.verifier.wait_installed()

    def test_failed_attempt_replaces_upload_with_failed_attestation(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with patch.object(verify.platform, "system", return_value="Darwin"), \
             patch.object(verify.Verification, "execute", side_effect=RuntimeError("rehearsal failed")), \
             patch.object(verify, "run") as run:
            self.assertEqual(verify.main(["--target", "26.40.6", "--upload"]), 1)
        self.assertFalse(json.loads((self.root / "verify-macos.json").read_text())["passed"])
        run.assert_called_once()
