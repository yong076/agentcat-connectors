"""Tests for Orca agent/model catalog parity and connector stability fixes (TRA-4417)."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import plistlib
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "lib"))

from sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths  # noqa: E402
import agentcat_home_signatures as home_signatures  # noqa: E402
import agentcat_orca_catalog as orca_catalog  # noqa: E402

LOADER = SourceFileLoader("tra4417_agentcat", str(REPO / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("tra4417_agentcat", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)

INSTALL_LOADER = SourceFileLoader("tra4417_install", str(REPO / "scripts" / "install.py"))
INSTALL_SPEC = importlib.util.spec_from_loader("tra4417_install", INSTALL_LOADER)
install = importlib.util.module_from_spec(INSTALL_SPEC)
assert INSTALL_SPEC.loader is not None
INSTALL_SPEC.loader.exec_module(install)


class OrcaCatalogAndConnectorFixesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.agentcat_home = self.home / ".agentcat"
        self.home.mkdir(parents=True)
        self.agentcat_home.mkdir(parents=True)
        self._originals = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        assert_sandboxed(agentcat, self.home, self.agentcat_home)

    def tearDown(self) -> None:
        restore_module_paths(agentcat, self._originals)
        self._tmp.cleanup()

    def test_orca_catalog_has_44_unique_agents_and_never_leaks_paths(self) -> None:
        ids = [spec.id for spec in orca_catalog.ORCA_AGENT_SPECS]
        self.assertEqual(len(ids), 44)
        self.assertEqual(len(ids), len(set(ids)))

        # Install two fake binaries in version-manager fallback dirs and persist Orca models.
        nvm_bin = self.home / ".nvm" / "versions" / "node" / "v22.14.0" / "bin"
        nvm_bin.mkdir(parents=True)
        pi_bin = nvm_bin / "pi"
        pi_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        pi_bin.chmod(0o755)

        orca_dir = self.home / "Library" / "Application Support" / "orca"
        orca_dir.mkdir(parents=True)
        (orca_dir / "agent-model-catalog.json").write_text(
            json.dumps({
                "version": 3,
                "entries": [{
                    "agent": "pi",
                    "configured": {"modelId": "anthropic/claude-opus-4-8"},
                    "discovered": {
                        "accountFingerprint": "secret-account-fp",
                        "workspacePath": "/Users/alice/secret-repo",
                        "models": [
                            {"id": "anthropic/claude-opus-4-8", "label": "Claude Opus 4.8", "isDefault": False},
                            {"id": "openai/gpt-5.6-sol", "label": "GPT-5.6 Sol", "isDefault": False},
                        ],
                    },
                }],
            }),
            encoding="utf-8",
        )

        with patch.object(agentcat.shutil, "which", return_value=None):
            payload = agentcat.available_agents_snapshot(force=True)

        serialized = json.dumps(payload)
        self.assertNotIn(str(self.home), serialized)
        self.assertNotIn("secret-account-fp", serialized)
        self.assertNotIn("secret-repo", serialized)
        self.assertEqual(payload["totalCount"], 44)
        self.assertEqual(payload["installedCount"], 1)

        by_id = {row["id"]: row for row in payload["agents"]}
        self.assertTrue(by_id["pi"]["installed"])
        self.assertEqual(by_id["pi"]["detectedCommand"], "pi")
        pi_models = {m["id"]: m for m in by_id["pi"]["models"]}
        self.assertTrue(pi_models["anthropic/claude-opus-4-8"]["isDefault"])
        self.assertEqual(pi_models["anthropic/claude-opus-4-8"]["source"], "orca")

    def test_classify_process_recognizes_orca_agents_and_node_entrypoints(self) -> None:
        cases = {
            "/usr/local/bin/pi --model gpt-5.6-sol": "pi",
            "/opt/homebrew/bin/omp chat": "omp",
            "/Users/dev/.local/bin/droid": "droid",
            "/opt/homebrew/bin/amp --mode deep": "amp",
            "/usr/local/bin/kiro-cli chat --tui": "kiro",
            "/usr/local/bin/auggie": "auggie",
            "/opt/homebrew/bin/cn": "continue",
            "/opt/homebrew/bin/acli rovodev run": "rovo",
            "/Users/dev/.cache/muse/muse-bin-0.4.2 --trust-workspace": "muse",
            "/usr/local/bin/qodercli-1.3.0": "qoder",
            "node /Users/dev/.nvm/versions/node/v22.0.0/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js": "pi",
            "node /Users/dev/.cursor/cursor-agent/versions/2026.11.0/index.js": "cursor",
            "node /Users/dev/node_modules/prime-agent/dist/bundle/cli.js": "prime-agent",
            "node /Users/dev/node_modules/@deepseek-ai/dsh/lib/bin.js": "dsh",
            "node /Users/dev/node_modules/@tencent-ai/codebuddy-code/cli.js": "codebuddy",
        }
        for cmd, expected in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(agentcat.classify_process(cmd, windows=False), expected)
        # Non-rovodev Atlassian CLI calls should not look like a coding agent.
        self.assertIsNone(agentcat.classify_process("/opt/homebrew/bin/acli jira issue view TRA-1", windows=False))

    def test_versioned_binary_patterns_match_targets_and_reject_helpers(self) -> None:
        self.assertEqual(agentcat.classify_process("codex-aarch64-apple-darwin", windows=False), "codex")
        self.assertEqual(agentcat.classify_process("grok-x86_64-pc-windows-msvc.exe", windows=True), "grok")
        self.assertIsNone(agentcat.classify_process("grok-helper.exe", windows=True))
        self.assertIsNone(agentcat.classify_process("codex-helper", windows=False))
        self.assertEqual(agentcat.estimate_cost("claude-fable-5-1", 1_000_000, 0, 0, 0)["input"], 10.0)
        self.assertEqual(agentcat.estimate_cost("claude-opus-4-7", 1_000_000, 0, 0, 0)["input"], 5.0)
        self.assertEqual(agentcat.estimate_cost("gpt-5.5", 1_000_000, 0, 0, 0)["input"], 5.0)

    def test_orca_claude_accounts_runtime_glob_is_registered(self) -> None:
        claude_sig = next(sig for sig in home_signatures.REGISTRY if sig.provider == "claude")
        self.assertIn("Library/Application Support/orca/claude-accounts/*/home", claude_sig.runtime_globs)
        self.assertIn("Library/Application Support/orca/claude-profiles/*/home", claude_sig.runtime_globs)

    def test_snapshot_source_label_deduplicates_repeated_session_files(self) -> None:
        repeated_kimi = ",".join(["/Users/dev/.kimi/sessions/a/wire.jsonl"] * 25)
        self.assertEqual(agentcat.snapshot_source_label(repeated_kimi), "wire.jsonl")

        grok_mixed = "/Users/dev/.grok/orca-status.json,/Users/dev/.grok/s1/updates.jsonl,/Users/dev/.grok/s2/updates.jsonl"
        self.assertEqual(agentcat.snapshot_source_label(grok_mixed), "orca-status.json,updates.jsonl")

        codex_mixed = (
            "/Users/dev/.codex/sessions/**/*.jsonl + "
            "/Users/dev/orca/codex-accounts/a/home/sessions/**/*.jsonl + "
            "/Users/dev/.codex/state_5.sqlite"
        )
        self.assertEqual(
            agentcat.snapshot_source_label(codex_mixed),
            "sessions/**/*.jsonl + state_5.sqlite",
        )

    def test_install_plist_skips_ephemeral_orca_claude_profile_and_escapes_xml(self) -> None:
        ephemeral = str(self.home / "Library/Application Support/orca/claude-profiles/1234/home")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ephemeral}, clear=False):
            env = plistlib.loads(install.plist_text().encode("utf-8"))["EnvironmentVariables"]
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

        custom = str(self.home / "custom&claude<dir>")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": custom}, clear=False):
            env = plistlib.loads(install.plist_text().encode("utf-8"))["EnvironmentVariables"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], custom)

    def test_atomic_writes_unlink_tmp_on_enospc_and_sweep_cleans_stale_orphans(self) -> None:
        target = self.agentcat_home / "latest-snapshot.json"
        with patch.object(agentcat.json, "dump", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                agentcat.write_json_atomic(target, {"ok": True})
        self.assertEqual(list(self.agentcat_home.glob("*.tmp")), [])

        stale = self.agentcat_home / ".latest-snapshot.json.999.1.123456.tmp"
        fresh = self.agentcat_home / ".latest-snapshot.json.999.1.999999.tmp"
        stale.write_text("partial", encoding="utf-8")
        fresh.write_text("partial", encoding="utf-8")
        old_ts = time.time() - 7200
        os.utime(stale, (old_ts, old_ts))

        removed = agentcat.sweep_stale_atomic_tmp_files(self.agentcat_home, max_age_seconds=3600)
        self.assertEqual(removed, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_strip_malloc_debug_env(self) -> None:
        fake_env = {"PATH": "/usr/bin", "MallocStackLogging": "1", "MallocScribble": "1"}
        removed = agentcat.strip_malloc_debug_env(fake_env)
        self.assertEqual(sorted(removed), ["MallocScribble", "MallocStackLogging"])
        self.assertEqual(fake_env, {"PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
