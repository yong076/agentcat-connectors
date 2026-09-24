"""`agentcat profile add/list/remove`: per-account launchers, never credentials."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "tests"))

from sandbox import redirect_module_paths, restore_module_paths

LOADER = SourceFileLoader("agentcat_profiles", str(REPO / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_profiles", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


@unittest.skipIf(os.name == "nt", "profile launchers are POSIX shell scripts")
class ProfileCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        self.agentcat_home = self.home / ".agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        self.cli_dir = root / "fakebin"
        self.cli_dir.mkdir()
        for name in ("codex", "claude", "kimi"):
            cli = self.cli_dir / name
            cli.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
            cli.chmod(0o755)
        env = {k: v for k, v in os.environ.items()
               if k not in {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "KIMI_CODE_HOME"}}
        env["PATH"] = str(self.cli_dir)
        self.env_patch = patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.security_calls = []

        def fake_security(argv):
            self.security_calls.append(argv)
            return 44  # "item not found"

        agentcat.PROFILE_SECURITY_RUNNER = fake_security

    def tearDown(self):
        agentcat.PROFILE_SECURITY_RUNNER = None
        self.env_patch.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agentcat.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_add_creates_private_home_launcher_and_adopts(self):
        code, out, err = self.run_cli("profile", "add", "codex", "work")
        self.assertEqual(code, 0, err)
        home = self.agentcat_home / "homes" / "codex-work"
        self.assertTrue(home.is_dir())
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        launcher = self.home / ".local" / "bin" / "codex-work"
        text = launcher.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n# agentcat-profile-launcher provider=codex name=work\n"))
        self.assertIn(f"exec env CODEX_HOME={home} {self.cli_dir / 'codex'} \"$@\"", text)
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn("run `codex-work login`", out)
        # Adoption recorded through the existing homes machinery.
        settings = json.loads((self.agentcat_home / "settings.json").read_text(encoding="utf-8"))
        self.assertIn(str(home), settings["homes"]["codex"]["adopted"])
        self.assertIn(home, agentcat.tracked_provider_homes("codex"))
        # No credential was created or copied.
        self.assertEqual(list(home.iterdir()), [])

    def test_claude_launcher_uses_config_dir_and_login_hint(self):
        code, out, _ = self.run_cli("profile", "add", "claude", "two")
        self.assertEqual(code, 0)
        text = (self.home / ".local" / "bin" / "claude-two").read_text(encoding="utf-8")
        self.assertIn("exec env CLAUDE_CONFIG_DIR=", text)
        self.assertIn("claude-two auth login", out)

    def test_kimi_launcher_is_not_adopted(self):
        code, _, _ = self.run_cli("profile", "add", "kimi", "alt")
        self.assertEqual(code, 0)
        text = (self.home / ".local" / "bin" / "kimi-alt").read_text(encoding="utf-8")
        self.assertIn("exec env KIMI_CODE_HOME=", text)
        rows = agentcat.profile_rows()
        self.assertEqual(rows[0]["adopted"], None)

    def test_refuses_to_clobber_foreign_file(self):
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        foreign = bin_dir / "codex-work"
        foreign.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        code, _, err = self.run_cli("profile", "add", "codex", "work")
        self.assertEqual(code, 1)
        self.assertIn("not created by agentcat", err)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")
        self.assertFalse((self.agentcat_home / "homes" / "codex-work").exists())
        # Removing never deletes a file we did not write either.
        code, _, _ = self.run_cli("profile", "remove", "codex", "work")
        self.assertEqual(code, 1)
        self.assertTrue(foreign.exists())

    def test_add_is_idempotent_for_our_own_launcher(self):
        self.assertEqual(self.run_cli("profile", "add", "codex", "work")[0], 0)
        self.assertEqual(self.run_cli("profile", "add", "codex", "work")[0], 0)

    def test_list_reports_login_state_read_only(self):
        self.run_cli("profile", "add", "codex", "work")
        self.run_cli("profile", "add", "claude", "two")
        code, out, _ = self.run_cli("profile", "list")
        self.assertEqual(code, 0)
        self.assertIn("codex-work", out)
        self.assertIn("~/.agentcat/homes/codex-work", out)
        rows = {row["command"]: row for row in agentcat.profile_rows()}
        self.assertFalse(rows["codex-work"]["loggedIn"])
        self.assertTrue(rows["codex-work"]["adopted"])
        # The CLI's own login appears -> reported, never touched.
        (self.agentcat_home / "homes" / "codex-work" / "auth.json").write_text("{}", encoding="utf-8")
        rows = {row["command"]: row for row in agentcat.profile_rows()}
        self.assertTrue(rows["codex-work"]["loggedIn"])
        # Claude keychain check is existence-only (no -w).
        for argv in self.security_calls:
            self.assertEqual(argv[:2], ["security", "find-generic-password"])
            self.assertNotIn("-w", argv)
            self.assertNotIn("-g", argv)

    def test_remove_deletes_launcher_forgets_home_and_keeps_it_by_default(self):
        self.run_cli("profile", "add", "codex", "work")
        home = self.agentcat_home / "homes" / "codex-work"
        code, out, _ = self.run_cli("profile", "remove", "codex", "work")
        self.assertEqual(code, 0)
        self.assertFalse((self.home / ".local" / "bin" / "codex-work").exists())
        self.assertTrue(home.is_dir())
        self.assertIn("kept home", out)
        self.assertNotIn(home, agentcat.tracked_provider_homes("codex"))
        self.assertEqual(agentcat.profile_rows(), [])

    def test_remove_with_delete_home(self):
        self.run_cli("profile", "add", "codex", "work")
        home = self.agentcat_home / "homes" / "codex-work"
        code, _, _ = self.run_cli("profile", "remove", "codex", "work", "--delete-home")
        self.assertEqual(code, 0)
        self.assertFalse(home.exists())

    def test_rejects_default_home_and_bad_names(self):
        code, _, _ = self.run_cli("profile", "add", "codex", "work", "--home", str(self.home / ".codex"))
        self.assertEqual(code, 2)
        code, _, _ = self.run_cli("profile", "add", "codex", "../evil")
        self.assertEqual(code, 2)
        self.assertFalse((self.home / ".local" / "bin").exists())


if __name__ == "__main__":
    unittest.main()


class ProfileWindowsTests(unittest.TestCase):
    def test_windows_refuses_instead_of_writing_launchers(self):
        with patch.object(agentcat.os, "name", "nt"):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertTrue(agentcat.profile_unsupported_platform())
            self.assertIn("not available on Windows", err.getvalue())

