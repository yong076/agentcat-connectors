import importlib.util
import json
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
            mock.patch.object(install, "IS_LINUX", False),
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
            mock.patch.object(install, "IS_LINUX", False),
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

    def test_load_launch_agent_on_linux_replaces_daemon_without_launchctl(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, check=False):
            calls.append(args)
            return mock.Mock(returncode=1 if args[0] in ("pgrep", "systemctl") else 0, stderr="")

        bin_path = self.root / "bin" / "agentcat"
        with (
            mock.patch.object(install, "IS_WINDOWS", False),
            mock.patch.object(install, "IS_LINUX", True),
            mock.patch.object(install, "BIN_PATH", bin_path),
            mock.patch.object(install, "run", side_effect=fake_run),
            mock.patch.object(install.subprocess, "Popen") as popen,
        ):
            install.load_launch_agent()

        pattern = f"{bin_path} daemon"
        daemon_calls = [command for command in calls if command[0] != "systemctl"]
        self.assertEqual(daemon_calls[:2], [["pkill", "-f", pattern], ["pgrep", "-f", pattern]])
        self.assertNotIn(["systemctl", "--user", "start", "agentcatd.service"], calls)
        self.assertFalse(any(command[0] == "launchctl" for command in calls))
        self.assertEqual(popen.call_args.args[0], [str(bin_path), "daemon"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue((install.AGENTCAT_HOME / "agentcatd.out.log").is_file())

    def test_load_launch_agent_on_linux_restarts_existing_systemd_user_unit(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, check=False):
            calls.append(args)
            return mock.Mock(returncode=1 if args[0] == "pgrep" else 0, stderr="")

        with (
            mock.patch.object(install, "IS_WINDOWS", False),
            mock.patch.object(install, "IS_LINUX", True),
            mock.patch.object(install, "run", side_effect=fake_run),
            mock.patch.object(install.subprocess, "Popen") as popen,
        ):
            install.load_launch_agent()

        stop = ["systemctl", "--user", "stop", "agentcatd.service"]
        start = ["systemctl", "--user", "start", "agentcatd.service"]
        self.assertLess(calls.index(stop), calls.index(start))
        self.assertLess(calls.index(["pkill", "-f", install.linux_daemon_pattern()]), calls.index(start))
        popen.assert_not_called()

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
