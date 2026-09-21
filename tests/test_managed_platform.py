import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

import agentcat_managed_platform as platform
from private_fs import assert_owner_private, assert_same_path, platform_cli_name, write_noop_cli


class ManagedPlatformTests(unittest.TestCase):
    def test_augment_search_path_uses_os_pathsep(self):
        fallback = os.pathsep.join(["/trusted/a", "/trusted/b"])
        current = os.pathsep.join(["/usr/bin", "/bin"])
        result = platform.augment_search_path(current, fallback)
        self.assertEqual(result.split(os.pathsep), ["/usr/bin", "/bin", "/trusted/a", "/trusted/b"])
        self.assertEqual(platform.augment_search_path("", fallback), fallback)
        self.assertEqual(platform.augment_search_path(None, fallback), fallback)

    def test_cli_names_follow_pathext_when_windows(self):
        with patch.object(platform, "IS_WINDOWS", True), patch.dict(os.environ, {"PATHEXT": ".EXE;.CMD"}, clear=False):
            self.assertEqual(
                platform.cli_names("gemini", bare=False),
                ["gemini.EXE", "gemini.CMD"],
            )
            self.assertEqual(
                platform.path_key("gemini.CMD"),
                platform.path_key("gemini.cmd"),
            )

    def test_restrict_private_is_owner_only_on_posix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.json"
            path.write_text("{}", encoding="utf-8")
            self.assertTrue(platform.restrict_private(path))
            assert_owner_private(self, path)

    def test_platform_cli_name_matches_resolver_discovery(self):
        self.assertEqual(platform_cli_name("gemini").lower().startswith("gemini"), True)
        if os.name == "nt":
            self.assertTrue(Path(platform_cli_name("gemini")).suffix)
        else:
            self.assertEqual(platform_cli_name("gemini"), "gemini")

    def test_write_noop_cli_is_discovered_as_home_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = write_noop_cli(Path(tmp) / ".local" / "bin" / "gemini")
            with patch.dict(os.environ, {
                "AGENTCAT_GEMINI_CLI": "",
                "HOME": tmp,
                "USERPROFILE": tmp,
                "PATH": "",
            }):
                assert_same_path(self, platform.resolve_cli("gemini", "AGENTCAT_GEMINI_CLI"), executable)

    def test_configured_executable_wins_over_which_and_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            configured = write_noop_cli(Path(tmp) / "configured-gemini")
            with patch.dict(os.environ, {"AGENTCAT_GEMINI_CLI": str(configured)}):
                assert_same_path(
                    self,
                    platform.resolve_cli(
                        "gemini",
                        "AGENTCAT_GEMINI_CLI",
                        which=lambda name: "/opt/homebrew/bin/gemini",
                    ),
                    configured,
                )

    def test_injected_which_path_wins_over_installed_discovery(self):
        with patch.dict(os.environ, {"AGENTCAT_GEMINI_CLI": ""}):
            self.assertEqual(
                platform.resolve_cli(
                    "gemini",
                    "AGENTCAT_GEMINI_CLI",
                    which=lambda name: "/fake/gemini",
                ),
                "/fake/gemini",
            )

    def test_windows_pathext_applies_only_to_discovery_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "gemini"
            explicit.write_text("echo", encoding="utf-8")
            bindir = Path(tmp) / ".local" / "bin"
            bindir.mkdir(parents=True)
            (bindir / "gemini").write_text("echo", encoding="utf-8")
            cmd = bindir / "gemini.cmd"
            cmd.write_text("echo", encoding="utf-8")
            with patch.object(platform, "IS_WINDOWS", True), patch.dict(os.environ, {
                "AGENTCAT_GEMINI_CLI": str(explicit),
                "HOME": tmp,
                "USERPROFILE": tmp,
                "PATH": "",
                "PATHEXT": ".CMD;.EXE",
            }, clear=False):
                assert_same_path(
                    self,
                    platform.resolve_cli("gemini", "AGENTCAT_GEMINI_CLI", which=lambda name: None),
                    explicit,
                )
            with patch.object(platform, "IS_WINDOWS", True), patch.dict(os.environ, {
                "AGENTCAT_GEMINI_CLI": "",
                "HOME": tmp,
                "USERPROFILE": tmp,
                "PATH": "",
                "PATHEXT": ".CMD;.EXE",
            }, clear=False):
                discovered = platform.resolve_cli(
                    "gemini", "AGENTCAT_GEMINI_CLI", which=lambda name: None
                )
                self.assertIsNotNone(discovered)
                on_disk = next(entry for entry in bindir.iterdir() if entry.name.lower() == "gemini.cmd")
                assert_same_path(self, discovered, on_disk)
                self.assertEqual(Path(discovered).name, on_disk.name)

    def test_augment_search_path_folds_windows_drive_case(self):
        current = r"C:\Windows\System32"
        fallback = ";".join([r"c:\windows\system32", r"C:\Trusted"])
        self.assertEqual(
            platform.augment_search_path(current, fallback, pathsep=";", windows=True).split(";"),
            [r"C:\Windows\System32", r"C:\Trusted"],
        )

    def test_owner_private_check_ignores_patched_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.json"
            path.write_text("{}", encoding="utf-8")
            self.assertTrue(platform.restrict_private(path))
            with patch.object(subprocess, "run", return_value=Mock(returncode=0)), \
                 patch.object(subprocess, "Popen", side_effect=AssertionError("patched Popen")):
                assert_owner_private(self, path)
