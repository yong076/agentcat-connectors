import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")


class InstallPowerShellSafetyTests(unittest.TestCase):
    def test_explicit_interpreter_is_probed_before_path_candidates(self) -> None:
        resolver = INSTALL_PS1.split("function Resolve-Python {", 1)[1].split(
            "function Invoke-Python", 1
        )[0]
        self.assertIn(
            'if (Test-Python3 @($env:AGENTCAT_PYTHON)) { return @($env:AGENTCAT_PYTHON) }',
            resolver,
        )
        self.assertLess(resolver.index("Test-Python3"), resolver.index("Get-Command python"))
        self.assertLess(resolver.index("Get-Command python"), resolver.index("Get-Command py -"))

    def test_path_candidates_require_successful_probe_including_launcher_flag(self) -> None:
        self.assertIn(
            'if ($python -and (Test-Python3 @($python.Source))) { return @($python.Source) }',
            INSTALL_PS1,
        )
        self.assertIn(
            'if ($py -and (Test-Python3 @($py.Source, "-3"))) { return @($py.Source, "-3") }',
            INSTALL_PS1,
        )
        self.assertIn('throw "Python 3 is required', INSTALL_PS1)

    def test_probe_rejects_store_stub_nonzero_exit_and_execution_errors(self) -> None:
        probe = INSTALL_PS1.split("function Test-Python3", 1)[1].split(
            "function Resolve-Python", 1
        )[0]
        self.assertIn('$Prefix[1..($Prefix.Length - 1)]', probe)
        self.assertIn('import sys; sys.exit(0 if sys.version_info.major == 3 else 1)', probe)
        self.assertIn('& $command @probeArguments *> $null', probe)
        self.assertIn('return $LASTEXITCODE -eq 0', probe)
        self.assertRegex(probe, r"catch\s*\{\s*return \$false")

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
