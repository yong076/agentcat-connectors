"""Daemon log hygiene and lifecycle (#121).

agentcatd.err.log is launchd's StandardErrorPath and was never rotated (one Mac
reached 703 MB of per-tick "cannot read <absolute path>" warnings). These tests
pin the cap + single backup, the stderr reopen, the Windows copy-truncate
fallback, once-per-file ~-relative warnings, the one-line bind error, and the
launchd ThrottleInterval.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import logging
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from sandbox import redirect_module_paths, restore_module_paths  # noqa: E402

LOADER = SourceFileLoader("daemon_log_hygiene_agentcat", str(REPO / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("daemon_log_hygiene_agentcat", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)

INSTALL_LOADER = SourceFileLoader("daemon_log_hygiene_install", str(REPO / "scripts" / "install.py"))
INSTALL_SPEC = importlib.util.spec_from_loader("daemon_log_hygiene_install", INSTALL_LOADER)
install = importlib.util.module_from_spec(INSTALL_SPEC)
assert INSTALL_SPEC.loader is not None
INSTALL_SPEC.loader.exec_module(install)


def _write_lines(path: Path, count: int, prefix: str = "line") -> None:
    with path.open("ab") as fh:
        for index in range(count):
            fh.write(f"{prefix} {index:06d} {'x' * 40}\n".encode())


class DaemonLogHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.state)
        agentcat._UNREADABLE_WARNED.clear()
        self.log = agentcat.daemon_err_log_path()

    def tearDown(self):
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def test_log_path_is_inside_agentcat_home(self):
        self.assertEqual(self.log, self.state / "agentcatd.err.log")
        self.assertEqual(agentcat.DAEMON_LOG_MAX_BYTES, 10 * 1024 * 1024)

    def test_small_log_is_left_alone(self):
        _write_lines(self.log, 10)
        before = self.log.read_bytes()
        self.assertFalse(agentcat.rotate_daemon_log(max_bytes=4096))
        self.assertEqual(self.log.read_bytes(), before)
        self.assertFalse(self.log.with_name("agentcatd.err.log.1").exists())

    def test_oversized_log_rotates_to_one_bounded_backup(self):
        _write_lines(self.log, 2000, "old")
        self.assertTrue(agentcat.rotate_daemon_log(max_bytes=4096))
        backup = self.log.with_name("agentcatd.err.log.1")
        tail = backup.read_bytes()
        self.assertLessEqual(len(tail), 4096)
        self.assertTrue(tail.startswith(b"old "), tail[:20])  # starts on a line boundary
        self.assertTrue(tail.endswith(b"old 001999 " + b"x" * 40 + b"\n"))
        self.assertFalse(self.log.exists() and self.log.stat().st_size)

        # A second rotation replaces the backup: never more than two files.
        _write_lines(self.log, 2000, "new")
        self.assertTrue(agentcat.rotate_daemon_log(max_bytes=4096))
        self.assertTrue(backup.read_bytes().startswith(b"new "))
        siblings = sorted(p.name for p in self.state.iterdir() if p.name.startswith("agentcatd.err.log"))
        self.assertEqual(siblings, ["agentcatd.err.log.1"])

    def test_rename_failure_falls_back_to_copy_and_truncate(self):
        # Windows refuses to rename a file another handle holds open.
        _write_lines(self.log, 2000, "win")
        real_replace = os.replace

        def replace(src, dst):
            if Path(src) == self.log:
                raise PermissionError(13, "file in use")
            return real_replace(src, dst)

        with patch.object(agentcat.os, "replace", side_effect=replace):
            self.assertTrue(agentcat.rotate_daemon_log(max_bytes=4096))
        self.assertEqual(self.log.stat().st_size, 0)
        tail = self.log.with_name("agentcatd.err.log.1").read_bytes()
        self.assertLessEqual(len(tail), 4096)
        self.assertTrue(tail.endswith(b"win 001999 " + b"x" * 40 + b"\n"))

    @unittest.skipIf(os.name == "nt", "launchd stderr reopen is POSIX; Windows uses copy-truncate")
    def test_daemon_stderr_follows_the_rotated_log(self):
        # In a child process whose fd 2 is the log (as under launchd), rotation
        # must move stderr onto the fresh file, not keep writing into .1.
        script = textwrap.dedent(
            f"""
            import importlib.util, os, sys
            from importlib.machinery import SourceFileLoader
            from pathlib import Path
            sys.path.insert(0, {str(REPO / "tests")!r})
            from sandbox import redirect_module_paths
            loader = SourceFileLoader("child_agentcat", {str(REPO / "bin" / "agentcat")!r})
            spec = importlib.util.spec_from_loader("child_agentcat", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            redirect_module_paths(module, Path({str(self.home)!r}), Path({str(self.state)!r}))
            for index in range(2000):
                print(f"before {{index:06d}} {{'x' * 40}}", file=sys.stderr, flush=True)
            assert module.rotate_daemon_log(max_bytes=4096)
            print("after rotation", file=sys.stderr, flush=True)
            """
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTCAT_", "CLAUDE_", "CODEX_"))}
        env.update(HOME=str(self.home), AGENTCAT_HOME=str(self.state))
        with self.log.open("ab") as stderr:
            result = subprocess.run(
                [sys.executable, "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=stderr, env=env, timeout=60,
            )
        self.assertEqual(result.returncode, 0)
        fresh = self.log.read_text(encoding="utf-8")
        self.assertIn("after rotation", fresh)
        self.assertNotIn("before 000000", fresh)
        backup = self.log.with_name("agentcatd.err.log.1").read_text(encoding="utf-8")
        self.assertNotIn("after rotation", backup)
        self.assertIn("before 001999", backup)
        self.assertLessEqual(len(backup.encode()), 4096)

    def test_unreadable_file_warning_is_logged_once_with_home_relative_path(self):
        path = self.home / ".codex" / "sessions" / "rollout-a.jsonl"
        error = PermissionError(13, "Permission denied", str(path))
        with self.assertLogs(level="WARNING") as captured:
            for _ in range(5):
                agentcat.warn_unreadable_path("codex_sessions_snapshot", path, error)
            agentcat.warn_unreadable_path("codex_sessions_snapshot", self.home / "other.jsonl", error)
            logging.getLogger().warning("sentinel")
        lines = [line for line in captured.output if "cannot read" in line]
        self.assertEqual(len(lines), 2)
        self.assertIn("~/.codex/sessions/rollout-a.jsonl", lines[0])
        self.assertIn("Permission denied", lines[0])
        for line in lines:
            self.assertNotIn(str(self.home), line)

    def test_codex_sessions_read_failure_does_not_repeat_each_tick(self):
        rollout = self.home / ".codex" / "sessions" / "2026" / "10" / "08" / "rollout-x.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"type":"session_meta","payload":{}}\n', encoding="utf-8")
        real_open = Path.open

        def failing_open(path_self, *args, **kwargs):
            if path_self == rollout:
                raise PermissionError(13, "Permission denied", str(rollout))
            return real_open(path_self, *args, **kwargs)

        with patch.object(Path, "open", failing_open), self.assertLogs(level="WARNING") as captured:
            for _ in range(3):
                agentcat.codex_sessions_snapshot()
            logging.getLogger().warning("sentinel")
        lines = [line for line in captured.output if "cannot read" in line]
        self.assertEqual(len(lines), 1, captured.output)
        self.assertNotIn(str(self.home), lines[0])

    def test_error_text_collapses_home(self):
        message = f"[Errno 2] No such file or directory: '{self.home}/.claude/projects/x.jsonl'"
        self.assertEqual(
            agentcat._tilde_text(message),
            "[Errno 2] No such file or directory: '~/.claude/projects/x.jsonl'",
        )
        # A sibling directory that merely shares the prefix is not rewritten.
        self.assertEqual(agentcat._tilde_text(f"{self.home}-old/x"), f"{self.home}-old/x")

    @unittest.skipIf(os.name == "nt", "Windows SO_REUSEADDR semantics differ; the bind guard is the same code")
    def test_port_in_use_is_one_line_and_exit_code(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        stderr = io.StringIO()
        try:
            with patch.object(agentcat.logging, "basicConfig"), \
                patch.object(agentcat, "build_snapshot", side_effect=AssertionError("must not build")), \
                patch.object(agentcat.threading, "Thread", side_effect=AssertionError("must not start loops")), \
                contextlib.redirect_stderr(stderr):
                code = agentcat.run_daemon(argparse.Namespace(host="127.0.0.1", port=port))
        finally:
            blocker.close()
        self.assertEqual(code, 1)
        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"agentcatd cannot listen on 127.0.0.1:{port}", lines[0])
        self.assertNotIn("Traceback", stderr.getvalue())


class LaunchAgentPlistTests(unittest.TestCase):
    def test_plist_throttles_keepalive_restarts(self):
        plist = plistlib.loads(install.plist_text().encode("utf-8"))
        self.assertIs(plist["KeepAlive"], True)
        self.assertEqual(plist["ThrottleInterval"], 30)
        self.assertTrue(plist["StandardErrorPath"].endswith("agentcatd.err.log"))


if __name__ == "__main__":
    unittest.main()
