import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")


class InstallPowerShellSafetyTests(unittest.TestCase):
    def test_release_install_requires_manifest_and_checksum(self) -> None:
        self.assertIn("connector-manifest.json", INSTALL_PS1)
        self.assertIn("Get-FileHash -Algorithm SHA256", INSTALL_PS1)
        self.assertIn("public_channel_install.py", INSTALL_PS1)

    def test_install_dir_is_never_recursively_deleted_by_bootstrap(self) -> None:
        self.assertNotIn("Remove-Item -LiteralPath $InstallDir", INSTALL_PS1)
        self.assertNotIn("git -C $InstallDir checkout --force", INSTALL_PS1)
        self.assertNotIn("git -C $InstallDir reset --hard", INSTALL_PS1)

    def test_pinned_app_install_requires_version_and_digest(self) -> None:
        self.assertIn("AGENTCAT_CONNECTORS_ARCHIVE_URL", INSTALL_PS1)
        self.assertIn("AGENTCAT_CONNECTORS_SHA256", INSTALL_PS1)
        self.assertIn("AGENTCAT_CONNECTORS_VERSION", INSTALL_PS1)


if __name__ == "__main__":
    unittest.main()
