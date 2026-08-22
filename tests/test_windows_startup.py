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
        self.legacy_script = Path(self.tmp.name) / "AgentCatD.vbs"
        self.legacy_script.write_text("legacy", encoding="utf-8")
        self.legacy_patch = mock.patch.object(
            install, "WINDOWS_LEGACY_STARTUP_SCRIPT", self.legacy_script
        )
        self.legacy_patch.start()

    def tearDown(self) -> None:
        self.legacy_patch.stop()
        self.tmp.cleanup()

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

        install.unload_windows_daemon()

        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(run_mock.call_args_list[2].args[0][0:2], ["reg.exe", "delete"])
        self.assertFalse(self.legacy_script.exists())
        stop_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
