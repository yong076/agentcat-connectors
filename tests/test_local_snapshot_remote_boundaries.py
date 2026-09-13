"""Regression coverage for the local snapshot / explicit remote quota boundary."""

import contextlib
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import block_network, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_local_snapshot", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_local_snapshot", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class LocalSnapshotRemoteBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.agentcat_home = root / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.network_patch = block_network(agentcat)
        self.network_patch.start()
        agentcat._HOME_DISCOVERY_SNAPSHOT_VALUE = None
        agentcat._HOME_DISCOVERY_SNAPSHOT_AT = 0.0

    def tearDown(self) -> None:
        self.network_patch.stop()
        self.env_patch.stop()
        restore_module_paths(agentcat, self.old_paths)
        agentcat._HOME_DISCOVERY_SNAPSHOT_VALUE = None
        agentcat._HOME_DISCOVERY_SNAPSHOT_AT = 0.0
        self.tmp.cleanup()

    def _write_codex_log(self) -> None:
        session = self.home / ".codex" / "sessions" / "2026" / "09" / "13" / "rollout-test.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "model_context_window": 200000,
                    "last_token_usage": {"input_tokens": 123, "output_tokens": 45, "cached_input_tokens": 0},
                    "total_token_usage": {"input_tokens": 123, "output_tokens": 45, "cached_input_tokens": 0},
                },
            },
        }) + "\n", encoding="utf-8")

    def test_build_snapshot_keeps_local_logs_without_remote_limit_helpers(self) -> None:
        self._write_codex_log()
        helper_names = (
            "codex_live_limits", "claude_live_limits", "gemini_live_limits",
            "antigravity_live_limits", "grok_live_limits", "kimi_live_limits", "copilot_live_limits",
        )
        with contextlib.ExitStack() as stack:
            remote = {
                name: stack.enter_context(
                    patch.object(agentcat, name, side_effect=AssertionError(f"local snapshot called {name}"))
                )
                for name in helper_names
            }
            snapshot = agentcat.build_snapshot()

        codex = snapshot["providers"]["codex"]
        self.assertGreater(codex["tokens"]["all"], 0)
        self.assertTrue(all(mock.call_count == 0 for mock in remote.values()))

    def test_explicit_legacy_usage_still_requests_live_provider_limits(self) -> None:
        helper_names = (
            "claude_live_limits", "codex_live_limits", "gemini_live_limits",
            "antigravity_live_limits", "grok_live_limits", "kimi_live_limits",
        )
        fixture = agentcat.empty_limits(status="fixture")
        with contextlib.ExitStack() as stack:
            remote = {
                name: stack.enter_context(patch.object(agentcat, name, return_value=fixture))
                for name in helper_names
            }
            stack.enter_context(patch.object(agentcat, "read_provider_key", return_value=None))
            payload = agentcat.fetch_llm_usage_fresh()

        self.assertEqual(set(payload["providers"]), {"claude", "codex", "gemini", "antigravity", "grok", "kimi"})
        for mock in remote.values():
            mock.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
