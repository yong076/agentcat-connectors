import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_install_module", str(REPO_ROOT / "scripts" / "install.py"))
SPEC = importlib.util.spec_from_loader("agentcat_install_module", LOADER)
install = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(install)


class AgentCatInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths = {
            "HOME": install.HOME,
            "AGENTCAT_HOME": install.AGENTCAT_HOME,
            "BACKUPS_DIR": install.BACKUPS_DIR,
            "GEMINI_TELEMETRY": install.GEMINI_TELEMETRY,
            "ANTIGRAVITY_TELEMETRY": install.ANTIGRAVITY_TELEMETRY,
        }
        install.HOME = self.root / "home"
        install.AGENTCAT_HOME = self.root / "agentcat"
        install.BACKUPS_DIR = install.AGENTCAT_HOME / "backups"
        install.GEMINI_TELEMETRY = install.AGENTCAT_HOME / "gemini" / "telemetry.log"
        install.ANTIGRAVITY_TELEMETRY = install.AGENTCAT_HOME / "gemini" / "antigravity-telemetry.log"
        install.HOME.mkdir()
        install.BACKUPS_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        for name, value in self.old_paths.items():
            setattr(install, name, value)
        self.tmp.cleanup()

    def test_write_json_retries_a_briefly_locked_target_on_windows(self) -> None:
        # Claude Code holds ~/.claude/settings.json open for a moment; one
        # PermissionError used to abort the whole connector install.
        target = self.root / "settings.json"
        real_replace = Path.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        with (
            mock.patch.object(install, "IS_WINDOWS", True),
            mock.patch.object(Path, "replace", flaky_replace),
            mock.patch.object(install.time, "sleep", lambda _s: None),
        ):
            install.write_json(target, {"hooks": {}})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"hooks": {}})
        self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_write_json_gives_up_and_cleans_up_when_the_lock_persists(self) -> None:
        target = self.root / "settings.json"

        def always_locked(src, dst):
            raise PermissionError(5, "Access is denied")

        with (
            mock.patch.object(install, "IS_WINDOWS", True),
            mock.patch.object(Path, "replace", always_locked),
            mock.patch.object(install.time, "sleep", lambda _s: None),
        ):
            with self.assertRaises(PermissionError):
                install.write_json(target, {"hooks": {}})
        self.assertFalse(target.with_suffix(".json.tmp").exists())

    def test_install_gemini_settings_configures_gemini_only_not_antigravity(self) -> None:
        # Antigravity bills server-side and ignores the local telemetry sub-keys;
        # the install must only write a telemetry block for the real gemini-cli and
        # must never touch antigravity-cli/settings.json.
        backup_dir = install.BACKUPS_DIR / "test"
        backup_dir.mkdir(parents=True)

        install.install_gemini_settings(backup_dir)

        gemini_settings = json.loads((install.HOME / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(gemini_settings["telemetry"]["outfile"], str(install.GEMINI_TELEMETRY))
        self.assertFalse(gemini_settings["telemetry"]["logPrompts"])

        antigravity_path = install.HOME / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertFalse(antigravity_path.exists())

    def test_remove_gemini_settings_cleans_prior_antigravity_cli_telemetry(self) -> None:
        # A prior install version may have written an Agent Cat telemetry block into
        # antigravity-cli/settings.json; uninstall must still clean it up.
        backup_dir = install.BACKUPS_DIR / "test"
        backup_dir.mkdir(parents=True)
        antigravity_path = install.HOME / ".gemini" / "antigravity-cli" / "settings.json"
        antigravity_path.parent.mkdir(parents=True, exist_ok=True)
        antigravity_path.write_text(
            json.dumps(
                {
                    "telemetry": {
                        "enabled": True,
                        "target": "local",
                        "outfile": str(install.ANTIGRAVITY_TELEMETRY),
                        "logPrompts": False,
                    }
                }
            ),
            encoding="utf-8",
        )
        install.install_gemini_settings(backup_dir)

        install.remove_agentcat_gemini_settings(backup_dir)

        gemini_settings = json.loads((install.HOME / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        antigravity_settings = json.loads(antigravity_path.read_text(encoding="utf-8"))
        self.assertNotIn("telemetry", gemini_settings)
        self.assertNotIn("telemetry", antigravity_settings)

    def test_unload_launch_agent_prefers_service_label_and_waits_for_bootout(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, check=False):
            calls.append(args)
            return mock.Mock(returncode=1 if args[1] == "print" else 0, stderr="")

        with (
            mock.patch.object(install, "IS_WINDOWS", False),
            mock.patch.object(install.os, "getuid", return_value=501, create=True),
            mock.patch.object(install, "run", side_effect=fake_run),
        ):
            install.unload_launch_agent()

        service = "gui/501/com.trappist.agentcatd"
        self.assertEqual(calls[0], ["launchctl", "bootout", service])
        self.assertEqual(calls[1], ["launchctl", "print", service])
        self.assertFalse(any(command[1] == "unload" for command in calls))

    def test_load_launch_agent_force_restarts_registered_service(self) -> None:
        calls: list[list[str]] = []
        plist = self.root / "com.trappist.agentcatd.plist"

        def fake_run(args, check=False):
            calls.append(args)
            return mock.Mock(returncode=0, stderr="")

        with (
            mock.patch.object(install, "IS_WINDOWS", False),
            mock.patch.object(install.os, "getuid", return_value=501, create=True),
            mock.patch.object(install, "PLIST_PATH", plist),
            mock.patch.object(install, "unload_launch_agent"),
            mock.patch.object(install, "run", side_effect=fake_run),
        ):
            install.load_launch_agent()

        service = "gui/501/com.trappist.agentcatd"
        self.assertTrue(plist.is_file())
        self.assertIn(["launchctl", "bootstrap", "gui/501", str(plist)], calls)
        self.assertIn(["launchctl", "kickstart", "-k", service], calls)

    @unittest.skipIf(os.name == "nt", "launchd install path; PosixPath cannot be built on Windows")
    def test_main_detaches_before_launchctl_and_recovers_booted_out_job(self) -> None:
        plist = self.root / "com.trappist.agentcatd.plist"
        service = "gui/501/com.trappist.agentcatd"
        for error in (None, PermissionError("already a leader"), OSError("setsid failed")):
            with self.subTest(error=error):
                calls = []

                def fake_run(args, **kwargs):
                    calls.append(args)
                    missing = args[0] == "launchctl" and args[1] in ("bootout", "unload", "print")
                    return mock.Mock(returncode=1 if missing else 0, stdout="", stderr="")

                def fake_setsid():
                    calls.append(["setsid"])
                    if error is not None:
                        raise error

                with (
                    mock.patch.object(install.os, "setsid", side_effect=fake_setsid, create=True),
                    mock.patch.object(install.os, "name", "posix"),
                    mock.patch.object(install.os, "getuid", return_value=501, create=True),
                    mock.patch.object(install, "IS_WINDOWS", False),
                    mock.patch.object(install, "PLIST_PATH", plist),
                    mock.patch.object(install, "mkdirs"),
                    mock.patch.object(install, "install_binary"),
                    mock.patch.object(install, "install_claude_settings"),
                    mock.patch.object(install, "install_gemini_settings"),
                    mock.patch.object(install, "install_codex_config"),
                    mock.patch.object(install.subprocess, "run", side_effect=fake_run),
                ):
                    self.assertEqual(install.main(["install"]), 0)

                self.assertEqual(calls[:2], [["setsid"], ["launchctl", "bootout", service]])
                bootstrap = ["launchctl", "bootstrap", "gui/501", str(plist)]
                kickstart = ["launchctl", "kickstart", "-k", service]
                self.assertLess(calls.index(bootstrap), calls.index(kickstart))
                self.assertTrue(plist.is_file())

    def test_main_does_not_detach_on_windows(self) -> None:
        with (
            mock.patch.object(install, "os", wraps=install.os) as mocked_os,
            mock.patch.object(install, "install", return_value=0) as run_install,
        ):
            mocked_os.name = "nt"
            mocked_os.setsid = mock.Mock()
            self.assertEqual(install.main(["install"]), 0)
            mocked_os.setsid.assert_not_called()
            run_install.assert_called_once()

    def test_install_uses_bounded_version_check_instead_of_snapshot(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, check=False):
            calls.append(args)
            return mock.Mock(returncode=0, stdout='{"connectorVersion":"26.34.7"}\n', stderr="")

        with (
            mock.patch.object(install, "install_binary"),
            mock.patch.object(install, "load_launch_agent"),
            mock.patch.object(install, "install_claude_settings"),
            mock.patch.object(install, "install_gemini_settings"),
            mock.patch.object(install, "install_codex_config"),
            mock.patch.object(install, "run", side_effect=fake_run),
        ):
            self.assertEqual(install.install(REPO_ROOT), 0)

        self.assertEqual(calls, [[str(install.BIN_PATH), "version", "--json"]])


if __name__ == "__main__":
    unittest.main()
