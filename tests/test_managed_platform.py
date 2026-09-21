import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

import agentcat_managed_platform as platform
from private_fs import assert_owner_private, platform_cli_name, write_noop_cli


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
                ["gemini.EXE", "gemini.exe", "gemini.CMD", "gemini.cmd"],
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
                self.assertEqual(platform.resolve_cli("gemini", "AGENTCAT_GEMINI_CLI"), str(executable))

    def test_configured_executable_wins_over_which_and_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            configured = write_noop_cli(Path(tmp) / "configured-gemini")
            with patch.dict(os.environ, {"AGENTCAT_GEMINI_CLI": str(configured)}):
                self.assertEqual(
                    platform.resolve_cli(
                        "gemini",
                        "AGENTCAT_GEMINI_CLI",
                        which=lambda name: "/opt/homebrew/bin/gemini",
                    ),
                    str(configured),
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
                self.assertEqual(
                    platform.resolve_cli("gemini", "AGENTCAT_GEMINI_CLI", which=lambda name: None),
                    str(explicit),
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
                discovered_path = Path(discovered)
                self.assertEqual(discovered_path.parent, bindir)
                self.assertEqual(discovered_path.name.lower(), "gemini.cmd")
