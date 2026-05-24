import importlib.util
import unittest
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


if __name__ == "__main__":
    unittest.main()
