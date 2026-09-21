"""Snapshot payloads must never carry absolute filesystem paths."""

import datetime as dt
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import block_network, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_snapshot_privacy", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_snapshot_privacy", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


MAC_PROJECT = "/Users/alice/Code/secret-repo"
LINUX_PROJECT = "/home/alice/work/linux-repo"
WIN_PROJECT = r"C:\Users\alice\work\win-repo"


def _forbidden_needles(home: Path) -> tuple[str, ...]:
    return (
        "/Users/",
        "/home/",
        "C:\\",
        r"C:\Users",
        str(home),
    )


def _strip_allowed_paths(serialized: str, allow: tuple[str, ...]) -> str:
    for allowed in allow:
        if not allowed:
            continue
        serialized = serialized.replace(allowed, "")
        encoded = json.dumps(allowed)
        if len(encoded) >= 2 and encoded[0] == encoded[-1] == '"':
            serialized = serialized.replace(encoded[1:-1], "")
    return serialized


def _assert_no_absolute_paths(
    payload: object,
    home: Path,
    *,
    allow: tuple[str, ...] = (),
) -> None:
    serialized = _strip_allowed_paths(json.dumps(payload, ensure_ascii=False), allow)
    for needle in _forbidden_needles(home):
        if needle in serialized:
            raise AssertionError(f"snapshot leaked {needle!r}: {serialized[:2000]}")


class SnapshotPathPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "Users" / "alice"
        self.agentcat_home = self.home / ".agentcat"
        self.home.mkdir(parents=True)
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        self.env_patch = patch.dict(
            os.environ,
            {"HOME": str(self.home), "USERPROFILE": str(self.home), "AGENTCAT_HOME": str(self.agentcat_home)},
            clear=False,
        )
        self.env_patch.start()
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME", "KIMI_HOME", "GEMINI_CLI_HOME"):
            os.environ.pop(key, None)
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

    def _write_claude_project(self, cwd: str, tokens: int = 48) -> None:
        project_dir = agentcat.CLAUDE_PROJECTS_DIR / "encoded-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc)
        event = {
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "cwd": cwd,
            "requestId": "req_privacy",
            "message": {
                "id": "msg_privacy",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
        (project_dir / "session.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    def _write_codex_session(self, cwd: str, name: str = "rollout-privacy.jsonl") -> None:
        sessions_dir = agentcat.HOME / ".codex" / "sessions" / "2026" / "09" / "21"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        lines = [
            {"type": "turn_context", "payload": {"model": "gpt-5.4"}},
            {
                "type": "event_msg",
                "timestamp": now,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 0,
                            "output_tokens": 4,
                            "reasoning_output_tokens": 0,
                        }
                    },
                    "cwd": cwd,
                },
            },
        ]
        (sessions_dir / name).write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )

    def test_project_identity_is_leaf_label_and_stable_opaque_id(self) -> None:
        first = agentcat.project_snapshot_identity(MAC_PROJECT)
        second = agentcat.project_snapshot_identity(MAC_PROJECT)
        other = agentcat.project_snapshot_identity(LINUX_PROJECT)
        windows = agentcat.project_snapshot_identity(WIN_PROJECT)

        self.assertEqual(first, second)
        self.assertEqual(first["name"], "secret-repo")
        self.assertEqual(first["path"], "secret-repo")
        self.assertEqual(first["id"], agentcat.short_stable_hash(MAC_PROJECT))
        self.assertEqual(len(first["id"]), 8)
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(windows["name"], "win-repo")
        self.assertEqual(windows["path"], "win-repo")
        for item in (first, other, windows):
            _assert_no_absolute_paths(item, self.home)

    def test_claude_and_codex_snapshots_omit_absolute_paths(self) -> None:
        self._write_claude_project(MAC_PROJECT)
        self._write_codex_session(LINUX_PROJECT, "rollout-linux.jsonl")
        self._write_codex_session(WIN_PROJECT, "rollout-win.jsonl")

        claude = agentcat.claude_snapshot()
        codex = agentcat.codex_sessions_snapshot(force_rebuild=True)

        claude_item = claude["projects"]["items"][0]
        self.assertEqual(claude_item["name"], "secret-repo")
        self.assertEqual(claude_item["path"], "secret-repo")
        self.assertEqual(claude_item["id"], agentcat.short_stable_hash(MAC_PROJECT))
        self.assertTrue(str(claude["source"]).endswith("stats-cache.json+jsonl"))
        self.assertNotIn("/", str(claude["source"]).replace("stats-cache.json+jsonl", ""))

        names = {item["name"] for item in codex["projects"]["items"]}
        self.assertEqual(names, {"linux-repo", "win-repo"})
        ids = {item["id"] for item in codex["projects"]["items"]}
        self.assertEqual(
            ids,
            {
                agentcat.short_stable_hash(LINUX_PROJECT),
                agentcat.short_stable_hash(WIN_PROJECT),
            },
        )
        _assert_no_absolute_paths(claude, self.home)
        _assert_no_absolute_paths(codex, self.home)

    def test_auto_update_metadata_keeps_keys_without_paths(self) -> None:
        install_dir = self.home / "Library" / "Application Support" / "connectors"
        repo_dir = self.home / "Code" / "agentcat-connectors"
        state = {
            "status": "current",
            "installDir": str(install_dir),
            "repoDir": str(repo_dir),
            "currentVersion": "1.0.0",
        }
        agentcat.write_auto_update_state(state)

        snapshot = agentcat.auto_update_status_snapshot()
        self.assertEqual(snapshot["installDir"], "connectors")
        self.assertEqual(snapshot["repoDir"], "agentcat-connectors")
        _assert_no_absolute_paths(snapshot, self.home)

        with patch.object(agentcat, "current_connector_repo_dir", return_value=repo_dir), \
                patch.object(agentcat, "agentcat_connectors_dir", return_value=install_dir), \
                patch.object(agentcat, "auto_update_enabled_status", return_value=(False, "off")):
            written = agentcat.check_auto_update_once(apply_update=False)
        self.assertEqual(written["installDir"], "connectors")
        self.assertEqual(written["repoDir"], "agentcat-connectors")
        _assert_no_absolute_paths(written, self.home)

    def test_build_snapshot_payload_has_no_absolute_paths(self) -> None:
        self._write_claude_project(MAC_PROJECT)
        self._write_codex_session(LINUX_PROJECT)
        app_path = self.home / "Applications" / "Codex.app"
        (app_path / "Contents").mkdir(parents=True)
        agentcat.write_agentcat_settings(
            {"desktopApps": {"codex": {"path": str(app_path)}}}
        )
        agentcat.write_auto_update_state(
            {
                "status": "idle",
                "installDir": str(self.home / "connectors"),
                "repoDir": str(self.home / "src" / "agentcat-connectors"),
            }
        )
        with patch.object(agentcat, "terminal_activity_snapshot", return_value={"status": "ok", "processes": []}):
            snapshot = agentcat.build_snapshot()

        claude_item = snapshot["providers"]["claude"]["projects"]["items"][0]
        self.assertEqual(claude_item["name"], "secret-repo")
        self.assertEqual(claude_item["path"], "secret-repo")
        self.assertEqual(claude_item["id"], agentcat.short_stable_hash(MAC_PROJECT))
        self.assertEqual(snapshot["update"]["installDir"], "connectors")
        self.assertEqual(snapshot["update"]["repoDir"], "agentcat-connectors")
        self.assertEqual(snapshot["desktopApps"]["codex"]["path"], str(app_path))
        self.assertEqual(snapshot["providers"]["codex"]["desktopApp"]["path"], str(app_path))
        for root in snapshot["desktopApps"]["codex"].get("dataRoots") or []:
            _assert_no_absolute_paths(root, self.home)
        _assert_no_absolute_paths(snapshot, self.home, allow=(str(app_path),))


if __name__ == "__main__":
    unittest.main()
