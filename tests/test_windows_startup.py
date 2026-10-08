import importlib.util
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader(
    "windows_startup_install_module", str(REPO_ROOT / "scripts" / "install.py")
)
SPEC = importlib.util.spec_from_loader("windows_startup_install_module", LOADER)
install = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(install)


def completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


class WindowsStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.legacy_script = root / "AgentCatD.vbs"
        self.legacy_script.write_text("legacy", encoding="utf-8")
        # The hidden launcher is written under AGENTCAT_HOME: keep it in the
        # sandbox, never in the developer's real profile.
        self.home = root / "home"
        self.agentcat_home = self.home / ".agentcat"
        self.bin_path = self.home / ".local" / "bin" / "agentcat.cmd"
        self.patches = [
            mock.patch.object(install, "WINDOWS_LEGACY_STARTUP_SCRIPT", self.legacy_script),
            mock.patch.object(install, "HOME", self.home),
            mock.patch.object(install, "AGENTCAT_HOME", self.agentcat_home),
            mock.patch.object(install, "BIN_PATH", self.bin_path),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def launcher(self) -> Path:
        return self.agentcat_home / "AgentCatD.vbs"

    @staticmethod
    def assert_hidden_launch(test: unittest.TestCase, command: str, launcher: str) -> None:
        # Mirrors the Windows app's repair check: a cmd.exe launcher is the
        # visible one it rewrites (agent-cat-releases#46).
        test.assertEqual(command, f'wscript.exe //B //NoLogo "{launcher}"')
        lowered = command.lower().lstrip('"')
        test.assertFalse(lowered.startswith("cmd"))
        test.assertNotIn("cmd.exe", lowered)

    @mock.patch.object(install, "start_windows_daemon")
    @mock.patch.object(install, "stop_windows_daemon")
    @mock.patch.object(install, "run")
    def test_logon_task_launches_the_daemon_hidden(
        self, run_mock: mock.Mock, stop_mock: mock.Mock, start_mock: mock.Mock
    ) -> None:
        run_mock.side_effect = [completed(0), completed(0)]

        install.load_windows_daemon()

        task_args = run_mock.call_args_list[0].args[0]
        self.assertEqual(task_args[:7], [
            "schtasks.exe", "/Create", "/TN", "AgentCatD", "/SC", "ONLOGON", "/TR",
        ])
        self.assertEqual(task_args[8:], ["/F"])
        self.assert_hidden_launch(self, task_args[7], str(self.launcher()))

        raw = self.launcher().read_bytes()
        self.assertTrue(raw.startswith(b"\xff\xfe"))
        self.assertEqual(
            raw[2:].decode("utf-16-le"),
            'Set shell = CreateObject("WScript.Shell")\r\n'
            f'shell.Run """{self.bin_path}"" daemon", 0, False\r\n',
        )

    @mock.patch.object(install, "start_windows_daemon")
    @mock.patch.object(install, "stop_windows_daemon")
    @mock.patch.object(install, "run")
    def test_run_value_fallback_launches_the_daemon_hidden(
        self, run_mock: mock.Mock, stop_mock: mock.Mock, start_mock: mock.Mock
    ) -> None:
        run_mock.side_effect = [completed(1, "access denied"), completed(0)]

        install.load_windows_daemon()

        registry_args = run_mock.call_args_list[1].args[0]
        self.assertEqual(registry_args, [
            "reg.exe", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
            "/v", "AgentCatD", "/t", "REG_EXPAND_SZ", "/d", registry_args[8], "/f",
        ])
        self.assert_hidden_launch(self, registry_args[8], r"%USERPROFILE%\.agentcat\AgentCatD.vbs")
        self.assertTrue(self.launcher().is_file())

    def test_launcher_keeps_a_non_ascii_profile_path(self) -> None:
        korean_bin = self.home.parent / "사용자" / ".local" / "bin" / "agentcat.cmd"
        with mock.patch.object(install, "BIN_PATH", korean_bin):
            path = install.write_windows_hidden_launcher()
        text = path.read_bytes()[2:].decode("utf-16-le")
        self.assertIn(f'"""{korean_bin}"" daemon", 0, False', text)

    def test_registration_names_stay_stable(self) -> None:
        # The Windows app's uninstaller deletes these by name.
        self.assertEqual(install.WINDOWS_TASK_NAME, "AgentCatD")
        self.assertEqual(install.WINDOWS_RUN_VALUE, "AgentCatD")
        self.assertEqual(install.WINDOWS_HIDDEN_LAUNCHER_NAME, "AgentCatD.vbs")

    @mock.patch.object(install, "start_windows_daemon")
    @mock.patch.object(install, "stop_windows_daemon")
    @mock.patch.object(install, "run")
    def test_task_registration_removes_stale_fallbacks(
        self, run_mock: mock.Mock, stop_mock: mock.Mock, start_mock: mock.Mock
    ) -> None:
        run_mock.side_effect = [completed(0), completed(0)]

        install.load_windows_daemon()

        self.assertEqual(run_mock.call_args_list[0].args[0][0], "schtasks.exe")
        self.assertEqual(run_mock.call_args_list[1].args[0][0:2], ["reg.exe", "delete"])
        self.assertFalse(self.legacy_script.exists())
        stop_mock.assert_called_once_with()
        start_mock.assert_called_once_with()

    @mock.patch.object(install, "start_windows_daemon")
    @mock.patch.object(install, "stop_windows_daemon")
    @mock.patch.object(install, "run")
    def test_task_failure_uses_per_user_registry_entry(
        self, run_mock: mock.Mock, stop_mock: mock.Mock, start_mock: mock.Mock
    ) -> None:
        run_mock.side_effect = [completed(1, "access denied"), completed(0)]

        install.load_windows_daemon()

        registry_args = run_mock.call_args_list[1].args[0]
        self.assertEqual(registry_args[:6], [
            "reg.exe", "add", install.WINDOWS_RUN_KEY, "/v", install.WINDOWS_RUN_VALUE, "/t",
        ])
        self.assertIn("REG_EXPAND_SZ", registry_args)
        self.assertIn("%USERPROFILE%", registry_args[8])
        self.assertFalse(self.legacy_script.exists())
        stop_mock.assert_called_once_with()
        start_mock.assert_called_once_with()

    @mock.patch.object(install, "start_windows_daemon")
    @mock.patch.object(install, "stop_windows_daemon")
    @mock.patch.object(install, "run")
    def test_install_fails_when_both_startup_methods_fail(
        self, run_mock: mock.Mock, stop_mock: mock.Mock, start_mock: mock.Mock
    ) -> None:
        run_mock.side_effect = [completed(1, "task denied"), completed(1, "registry denied")]

        with self.assertRaisesRegex(RuntimeError, "Scheduled Task: task denied; HKCU Run: registry denied"):
            install.load_windows_daemon()

        self.assertTrue(self.legacy_script.exists())
        stop_mock.assert_not_called()
        start_mock.assert_not_called()

    @mock.patch.object(install, "stop_windows_daemon")
    @mock.patch.object(install, "run")
    def test_unload_removes_task_registry_entry_and_legacy_script(
        self, run_mock: mock.Mock, stop_mock: mock.Mock
    ) -> None:
        run_mock.return_value = completed(0)
        install.write_windows_hidden_launcher()

        install.unload_windows_daemon()

        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(run_mock.call_args_list[2].args[0][0:2], ["reg.exe", "delete"])
        self.assertFalse(self.legacy_script.exists())
        self.assertFalse(self.launcher().exists())
        stop_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
