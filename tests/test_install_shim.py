import importlib.util
import unittest
import tempfile
import unittest.mock
from importlib.machinery import SourceFileLoader
from pathlib import Path, PureWindowsPath


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("install_module", str(REPO_ROOT / "scripts" / "install.py"))
SPEC = importlib.util.spec_from_loader("install_module", LOADER)
install = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(install)


class WindowsShimPathTests(unittest.TestCase):
    def test_path_under_home_rewrites_to_userprofile(self) -> None:
        home = PureWindowsPath("C:\\Users\\아트심")  # Korean username
        target = home / ".agentcat" / "connectors" / "bin" / "agentcat"
        with unittest.mock.patch.object(install, "HOME", home):
            result = install._windows_shim_path(target)
        self.assertEqual(
            result, "%USERPROFILE%\\.agentcat\\connectors\\bin\\agentcat"
        )
        self.assertTrue(
            result.isascii(),
            "shim path must be ASCII so cmd.exe's OEM codepage parsing cannot mojibake it",
        )

    def test_agentcat_home_rewrites_to_userprofile(self) -> None:
        home = PureWindowsPath("C:\\Users\\田中")  # Japanese/Chinese username
        agentcat_home = home / ".agentcat"
        with unittest.mock.patch.object(install, "HOME", home):
            result = install._windows_shim_path(agentcat_home)
        self.assertEqual(result, "%USERPROFILE%\\.agentcat")
        self.assertTrue(result.isascii())

    def test_path_outside_home_falls_back_to_absolute(self) -> None:
        home = PureWindowsPath("C:\\Users\\alice")
        target = PureWindowsPath("D:\\custom\\install\\bin\\agentcat")
        with unittest.mock.patch.object(install, "HOME", home):
            result = install._windows_shim_path(target)
        self.assertEqual(result, "D:\\custom\\install\\bin\\agentcat")


class WindowsShimGenerationTests(unittest.TestCase):
    def test_install_pins_interpreter_and_forwards_arguments(self) -> None:
        for username in ("Alice Smith", "아트 심", "田中"):
            for outside_home in (False, True):
                with self.subTest(username=username, outside_home=outside_home), tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / username
                    repo = home / ".agentcat" / "connectors"
                    src = repo / "bin" / "agentcat"
                    src.parent.mkdir(parents=True)
                    src.write_text("# fixture", encoding="ascii")
                    shim = home / ".local" / "bin" / "agentcat.cmd"
                    shim.parent.mkdir(parents=True)
                    executable = (
                        Path(tmp) / "Program Files" / "Python" / "python.exe"
                        if outside_home else home / ".agentcat" / "python" / "3.13.16" / "python.exe"
                    )
                    with unittest.mock.patch.multiple(
                        install, HOME=home, AGENTCAT_HOME=home / ".agentcat",
                        BIN_PATH=shim, IS_WINDOWS=True,
                    ), unittest.mock.patch.object(install.sys, "executable", str(executable)), \
                            unittest.mock.patch.object(install, "ensure_windows_user_path"), \
                            unittest.mock.patch.object(install, "log"):
                        install.install_binary(repo, home / "backups")
                        python_ref = install._windows_shim_path(executable)
                    expected = (
                        '@echo off\r\n'
                        'set "AGENTCAT_HOME=%USERPROFILE%\\.agentcat"\r\n'
                        f'"{python_ref}" "%USERPROFILE%\\.agentcat\\connectors\\bin\\agentcat" %*\r\n'
                    )
                    self.assertEqual(shim.read_bytes(), expected.encode("ascii"))


if __name__ == "__main__":
    unittest.main()
