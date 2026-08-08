"""Tests for the 2026-07-03 connector audit fixes (agent-cat issues #38/#43/#45).

Each class states the defect it pins, because the value here is preventing a
regression back to a behaviour that was expensive to notice the first time.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_audit", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_audit", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class AuditFixTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = {
            name: getattr(agentcat, name)
            for name in ("HOME", "AGENTCAT_HOME", "TELEMETRY_LOG_MAX_BYTES")
        }
        home = self.root / "home"
        home.mkdir()
        agentcat.HOME = home
        agentcat.AGENTCAT_HOME = self.root / "agentcat"
        agentcat.AGENTCAT_HOME.mkdir()

    def tearDown(self) -> None:
        for name, value in self.old.items():
            setattr(agentcat, name, value)
        self.tmp.cleanup()


class TelemetryLogRotationTests(AuditFixTestCase):
    """#43 — the OTEL log grew to 8.9 GB because nothing ever trimmed it."""

    def _log(self, size: int) -> Path:
        path = self.root / "telemetry.log"
        path.write_bytes(b"x" * size)
        return path

    def test_oversized_and_fully_read_log_is_truncated(self) -> None:
        agentcat.TELEMETRY_LOG_MAX_BYTES = 1000
        path = self._log(2000)
        state = {"offset": 2000, "size": 2000, "mtimeNs": 1}

        result = agentcat.rotate_telemetry_log_if_oversized(state, path)

        self.assertEqual(path.stat().st_size, 0)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["size"], 0)

    def test_unread_tail_defers_rotation(self) -> None:
        """The one thing rotation must never do is destroy unread records."""
        agentcat.TELEMETRY_LOG_MAX_BYTES = 1000
        path = self._log(2000)
        state = {"offset": 1500, "size": 2000, "mtimeNs": 1}

        result = agentcat.rotate_telemetry_log_if_oversized(state, path)

        self.assertEqual(path.stat().st_size, 2000)
        self.assertEqual(result["offset"], 1500)

    def test_small_log_is_left_alone(self) -> None:
        agentcat.TELEMETRY_LOG_MAX_BYTES = 10000
        path = self._log(2000)
        state = {"offset": 2000, "size": 2000, "mtimeNs": 1}

        agentcat.rotate_telemetry_log_if_oversized(state, path)

        self.assertEqual(path.stat().st_size, 2000)

    def test_rotation_can_be_disabled(self) -> None:
        agentcat.TELEMETRY_LOG_MAX_BYTES = 0
        path = self._log(2000)
        state = {"offset": 2000, "size": 2000, "mtimeNs": 1}

        agentcat.rotate_telemetry_log_if_oversized(state, path)

        self.assertEqual(path.stat().st_size, 2000)

    def test_unwritable_log_does_not_break_the_scan(self) -> None:
        """A failed rotation must degrade to "not rotated", never raise."""
        agentcat.TELEMETRY_LOG_MAX_BYTES = 1000
        missing = self.root / "gone.log"
        state = {"offset": 2000, "size": 2000, "mtimeNs": 1}

        result = agentcat.rotate_telemetry_log_if_oversized(state, missing)

        self.assertEqual(result["offset"], 2000)

    def test_totals_survive_a_rotation(self) -> None:
        """The point of rotating only when fully read: history is not usage data.

        After truncation the loader must rescan from zero without discarding the
        accumulated totals, which is what made the earlier truncation bug a
        full history wipe.
        """
        telemetry = self.root / "telemetry.log"
        telemetry.write_bytes(b"")
        cache = self.root / "cache.json"
        state = agentcat.empty_gemini_usage_state(telemetry)
        state["tokens"] = {"all": 12345}
        state["offset"] = 5000
        state["size"] = 5000
        agentcat.save_gemini_usage_state(state, cache_path=cache)

        # The log is now smaller than the recorded offset — a rotation.
        reloaded = agentcat.load_gemini_usage_state(
            telemetry.stat(), telemetry_path=telemetry, cache_path=cache
        )

        self.assertEqual(reloaded["offset"], 0, "must rescan from the start")
        self.assertEqual(reloaded["tokens"]["all"], 12345, "totals must survive")


class WindowsHomeResolutionTests(AuditFixTestCase):
    """#45-10 — Git Bash/MSYS set HOME, pointing every provider path elsewhere."""

    def test_windows_prefers_userprofile_over_home(self) -> None:
        with patch.object(agentcat.os, "name", "nt"), patch.dict(
            os.environ,
            {"HOME": "/c/msys/home/alice", "USERPROFILE": r"C:\Users\alice"},
        ):
            self.assertEqual(agentcat.resolve_user_home_env(), r"C:\Users\alice")

    def test_windows_falls_back_to_home_without_userprofile(self) -> None:
        with patch.object(agentcat.os, "name", "nt"), patch.dict(
            os.environ, {"HOME": "/c/msys/home/alice"}, clear=True
        ):
            self.assertEqual(agentcat.resolve_user_home_env(), "/c/msys/home/alice")

    def test_posix_ignores_userprofile(self) -> None:
        with patch.object(agentcat.os, "name", "posix"), patch.dict(
            os.environ, {"HOME": "/home/alice", "USERPROFILE": r"C:\Users\bob"}
        ):
            self.assertEqual(agentcat.resolve_user_home_env(), "/home/alice")

    def test_no_env_falls_back_to_path_home(self) -> None:
        with patch.object(agentcat.os, "name", "posix"), patch.dict(
            os.environ, {}, clear=True
        ):
            self.assertEqual(agentcat.resolve_user_home_env(), str(Path.home()))


class ClaudeApiBillingTests(AuditFixTestCase):
    """#38-4 — API/Bedrock/Vertex users have no subscription quota by design.

    Folding them into `token_missing` made a correct state look like a detection
    failure, which is part of "works for some users, not others".
    """

    def setUp(self) -> None:
        super().setUp()
        (agentcat.HOME / ".claude").mkdir(parents=True, exist_ok=True)
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CONFIG_DIR",
        ):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self.env_patch.stop()
        super().tearDown()

    def _settings(self, env: dict) -> None:
        (agentcat.HOME / ".claude" / "settings.json").write_text(
            json.dumps({"env": env}), encoding="utf-8"
        )

    def test_api_key_in_environment_is_detected(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-xxx"}):
            self.assertEqual(agentcat.claude_api_billing_mode(), "api_key")

    def test_bedrock_and_vertex_are_distinguished(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "1"}):
            self.assertEqual(agentcat.claude_api_billing_mode(), "bedrock")
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}):
            self.assertEqual(agentcat.claude_api_billing_mode(), "vertex")

    def test_settings_env_block_is_read(self) -> None:
        """The daemon's frozen environment cannot see a shell export."""
        self._settings({"ANTHROPIC_API_KEY": "sk-ant-xxx"})

        self.assertEqual(agentcat.claude_api_billing_mode(), "api_key")

    def test_disabled_flag_is_not_treated_as_enabled(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_BEDROCK": "0"}):
            self.assertIsNone(agentcat.claude_api_billing_mode())

    def test_plain_subscription_install_is_not_api_billed(self) -> None:
        self._settings({"SOMETHING_ELSE": "1"})

        self.assertIsNone(agentcat.claude_api_billing_mode())

    def test_credentials_report_api_billing_instead_of_token_missing(self) -> None:
        with patch.object(agentcat.sys, "platform", "linux"), patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-ant-xxx"}
        ):
            result = agentcat.read_claude_oauth_credentials()

        self.assertIsNone(result["oauth"])
        self.assertEqual(result["reason"], "api_billing")
        self.assertEqual(result["billingMode"], "api_key")

    def test_subscription_install_still_reports_token_missing(self) -> None:
        with patch.object(agentcat.sys, "platform", "linux"):
            result = agentcat.read_claude_oauth_credentials()

        self.assertEqual(result["reason"], "token_missing")


class CodexRebuildLoopTests(AuditFixTestCase):
    """#45-4 — a rebuild that cannot close the gap re-ran every 60s forever."""

    def _snap(self, tokens: int) -> dict:
        return {"status": "ok", "tokens": {"all": tokens}}

    def test_large_gap_still_triggers_a_rebuild(self) -> None:
        self.assertTrue(
            agentcat._codex_sessions_need_rebuild(self._snap(1_000_000), self._snap(50_000_000))
        )

    def test_small_gap_does_not_trigger_a_rebuild(self) -> None:
        self.assertFalse(
            agentcat._codex_sessions_need_rebuild(self._snap(20_000_000), self._snap(50_000_000))
        )

    def test_rebuild_is_not_retried_once_marked_ineffective(self) -> None:
        """The user deleted old rollouts; sqlite keeps lifetime totals forever.

        No rebuild can ever close that gap, so retrying every tick just
        re-parses thousands of files in a loop.
        """
        sessions, sqlite = self._snap(1_000_000), self._snap(50_000_000)
        agentcat.mark_codex_rebuild_ineffective(sessions, sqlite)

        self.assertFalse(agentcat._codex_sessions_need_rebuild(sessions, sqlite))

    def test_marker_expires_when_the_corpus_changes(self) -> None:
        """New tokens mean new files to read, so it is worth trying again."""
        sessions, sqlite = self._snap(1_000_000), self._snap(50_000_000)
        agentcat.mark_codex_rebuild_ineffective(sessions, sqlite)

        self.assertTrue(
            agentcat._codex_sessions_need_rebuild(self._snap(2_000_000), sqlite)
        )

    def test_marker_survives_a_daemon_restart(self) -> None:
        """It is persisted, not in-memory, or every restart pays the loop again."""
        sessions, sqlite = self._snap(1_000_000), self._snap(50_000_000)
        agentcat.mark_codex_rebuild_ineffective(sessions, sqlite)

        self.assertTrue(agentcat._codex_rebuild_state_file().exists())


if __name__ == "__main__":
    unittest.main()
