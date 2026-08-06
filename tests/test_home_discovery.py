"""Tests for multi-home discovery, deduplication, and adoption.

The property that matters most here is that a stock install is byte-for-byte
unchanged: discovery reports, it never silently changes what gets counted. The
second property is that adopting a mirrored home does not double-count it —
mirrors hardlink their files, so plain path keys would count the same bytes
twice.
"""

import datetime as dt
import importlib.util
import json
import os
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_homes", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_homes", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


def _rollout_name(uuid: str, stamp: str = "2026-08-06T09-00-00") -> str:
    return f"rollout-{stamp}-{uuid}.jsonl"


UUID_A = "019fbf80-8f02-71e3-9485-757b77ac59b6"
UUID_B = "019fbf8e-4950-7513-929a-aa539c1646da"
UUID_C = "019fbf9c-06c0-7bb2-abfb-ccd293a8c8aa"


class HomeDiscoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = {
            name: getattr(agentcat, name)
            for name in (
                "HOME",
                "AGENTCAT_HOME",
                "CLAUDE_PROJECTS_DIR",
                "CLAUDE_CONFIG_DIR",
                "JOURNAL_CURSOR_FILE",
                "CODEX_SESSIONS_CURSOR_FILE",
                "LAUNCHD_AGENT_PLIST",
            )
        }

        home = self.root / "home"
        agentcat_home = self.root / "agentcat"
        home.mkdir()
        agentcat_home.mkdir()
        agentcat.HOME = home
        agentcat.AGENTCAT_HOME = agentcat_home
        agentcat.CLAUDE_CONFIG_DIR = home / ".claude"
        agentcat.CLAUDE_PROJECTS_DIR = home / ".claude" / "projects"
        agentcat.JOURNAL_CURSOR_FILE = agentcat_home / "jsonl-cursor.json"
        agentcat.CODEX_SESSIONS_CURSOR_FILE = agentcat_home / "codex-sessions-cursor.json"
        # Point the plist at the sandbox so a real launchd job on the developer's
        # machine can never leak into an env-drift assertion.
        agentcat.LAUNCHD_AGENT_PLIST = home / "Library" / "LaunchAgents" / "agentcatd.plist"

        # $CODEX_HOME / $CLAUDE_CONFIG_DIR in the developer's shell would silently
        # redirect every home resolution in this file.
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

        self._reset_discovery_cache()

    def tearDown(self) -> None:
        self.env_patch.stop()
        for name, value in self.old.items():
            setattr(agentcat, name, value)
        self._reset_discovery_cache()
        self.tmp.cleanup()

    @staticmethod
    def _reset_discovery_cache() -> None:
        agentcat._HOME_DISCOVERY_SNAPSHOT_VALUE = None
        agentcat._HOME_DISCOVERY_SNAPSHOT_AT = 0.0

    # -- fixtures ---------------------------------------------------------

    def _codex_session(self, home: Path, uuid: str, tokens: int = 100, day: str = "06") -> Path:
        session_dir = home / "sessions" / "2026" / "08" / day
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / _rollout_name(uuid)
        now = dt.datetime.now(dt.timezone.utc)
        path.write_text(
            json.dumps(
                {
                    "timestamp": now.isoformat(),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "model_context_window": 200000,
                            "last_token_usage": {
                                "input_tokens": tokens,
                                "output_tokens": tokens,
                                "cached_input_tokens": 0,
                            },
                            "total_token_usage": {
                                "input_tokens": tokens,
                                "output_tokens": tokens,
                                "cached_input_tokens": 0,
                            },
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _claude_journal(self, home: Path, session_id: str, project: str = "-tmp-proj") -> Path:
        project_dir = home / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "sessionId": session_id,
                    "message": {
                        "model": "claude-opus-5",
                        "usage": {"input_tokens": 10, "output_tokens": 20},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def _adopt(self, provider: str, home: Path) -> None:
        agentcat.write_agentcat_settings(
            {"homes": {provider: {"adopted": [str(home)]}}}
        )

    def _write_plist(self, env: dict) -> None:
        import plistlib

        agentcat.LAUNCHD_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
        with agentcat.LAUNCHD_AGENT_PLIST.open("wb") as fh:
            plistlib.dump({"Label": "test", "EnvironmentVariables": env}, fh)


class DedupeUsagePathsTests(HomeDiscoveryTestCase):
    def test_hardlinked_mirror_counts_once(self) -> None:
        """The shape a real mirror takes: same inode reached via two homes.

        realpath() cannot collapse these because both paths are equally real,
        which is exactly why the inode layer exists.
        """
        primary = agentcat.HOME / ".codex"
        mirror = agentcat.HOME / "mirror"
        original = self._codex_session(primary, UUID_A)
        link_dir = mirror / "sessions" / "2026" / "08" / "06"
        link_dir.mkdir(parents=True)
        link = link_dir / original.name
        os.link(original, link)

        deduped = agentcat.dedupe_usage_paths([original, link], {})

        self.assertEqual(deduped, [original])

    def test_symlinked_home_counts_once(self) -> None:
        primary = agentcat.HOME / ".codex"
        original = self._codex_session(primary, UUID_A)
        alias = agentcat.HOME / "alias"
        alias.symlink_to(primary)
        via_alias = alias / original.relative_to(primary)

        deduped = agentcat.dedupe_usage_paths([original, via_alias], {})

        self.assertEqual(deduped, [original])

    def test_true_copy_resolved_by_session_identity_keeping_longer_file(self) -> None:
        """Independent inodes, same session: the stale copy is a truncated one."""
        live = self._codex_session(agentcat.HOME / ".codex", UUID_A)
        stale_dir = agentcat.HOME / "backup" / "sessions" / "2026" / "08" / "06"
        stale_dir.mkdir(parents=True)
        stale = stale_dir / live.name
        stale.write_text("{}\n", encoding="utf-8")
        self.assertLess(stale.stat().st_size, live.stat().st_size)

        # Stale listed first, so this also proves the swap-in-place path works.
        deduped = agentcat.dedupe_usage_paths(
            [stale, live], {}, match_session_identity=True
        )

        self.assertEqual(deduped, [live])

    def test_session_identity_off_keeps_both_copies(self) -> None:
        """Inside one home, two files with one UUID is a move in progress.

        Dropping one would silently lose usage, so layer 3 stays off unless a
        second home is actually in play.
        """
        live = self._codex_session(agentcat.HOME / ".codex", UUID_A)
        archived_dir = agentcat.HOME / ".codex" / "archived_sessions" / "2026" / "08" / "06"
        archived_dir.mkdir(parents=True)
        archived = archived_dir / live.name
        archived.write_text("{}\n", encoding="utf-8")

        deduped = agentcat.dedupe_usage_paths([live, archived], {})

        self.assertEqual(deduped, [live, archived])

    def test_distinct_sessions_all_survive(self) -> None:
        home = agentcat.HOME / ".codex"
        paths = [self._codex_session(home, uuid) for uuid in (UUID_A, UUID_B, UUID_C)]

        deduped = agentcat.dedupe_usage_paths(paths, {}, match_session_identity=True)

        self.assertEqual(sorted(deduped), sorted(paths))

    def test_files_without_uuid_are_never_matched_together(self) -> None:
        """No UUID means "cannot prove same session" — never a false merge."""
        home = agentcat.HOME / ".claude" / "projects" / "-a"
        home.mkdir(parents=True)
        first = home / "notes.jsonl"
        second = home / "other.jsonl"
        first.write_text("{}\n", encoding="utf-8")
        second.write_text("{}\n", encoding="utf-8")

        deduped = agentcat.dedupe_usage_paths(
            [first, second], {}, match_session_identity=True
        )

        self.assertEqual(sorted(deduped), sorted([first, second]))

    def test_input_order_is_preserved(self) -> None:
        """Callers key cursors off these paths, so the order must be stable."""
        home = agentcat.HOME / ".codex"
        paths = [self._codex_session(home, uuid) for uuid in (UUID_C, UUID_A, UUID_B)]

        deduped = agentcat.dedupe_usage_paths(paths, {})

        self.assertEqual(deduped, paths)


class TrackedHomesTests(HomeDiscoveryTestCase):
    def test_default_only_without_adoption(self) -> None:
        self.assertEqual(
            agentcat.tracked_provider_homes("codex"), [agentcat.HOME / ".codex"]
        )
        self.assertEqual(
            agentcat.tracked_provider_homes("claude"), [agentcat.HOME / ".claude"]
        )

    def test_env_var_overrides_default_home(self) -> None:
        elsewhere = agentcat.HOME / "profile-two"
        (elsewhere / "sessions").mkdir(parents=True)
        with patch.dict(os.environ, {"CODEX_HOME": str(elsewhere)}):
            self.assertEqual(agentcat.tracked_provider_homes("codex"), [elsewhere])

    def test_adopted_home_is_appended(self) -> None:
        second = agentcat.HOME / "profile-two"
        self._codex_session(second, UUID_A)
        self._adopt("codex", second)

        self.assertEqual(
            agentcat.tracked_provider_homes("codex"),
            [agentcat.HOME / ".codex", second],
        )

    def test_exclusion_beats_adoption(self) -> None:
        second = agentcat.HOME / "profile-two"
        self._codex_session(second, UUID_A)
        agentcat.write_agentcat_settings(
            {"homes": {"codex": {"adopted": [str(second)], "excluded": [str(second)]}}}
        )

        self.assertEqual(
            agentcat.tracked_provider_homes("codex"), [agentcat.HOME / ".codex"]
        )

    def test_missing_adopted_home_is_skipped(self) -> None:
        self._adopt("codex", agentcat.HOME / "does-not-exist")

        self.assertEqual(
            agentcat.tracked_provider_homes("codex"), [agentcat.HOME / ".codex"]
        )


class CandidateDiscoveryTests(HomeDiscoveryTestCase):
    def test_known_runtime_mirror_is_discovered_untracked(self) -> None:
        mirror = agentcat.HOME / "Library" / "Application Support" / "orca" / "codex-runtime-home" / "home"
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        self._codex_session(mirror, UUID_B)

        candidates = agentcat.provider_home_candidates("codex")
        by_source = {c["source"]: c for c in candidates}

        self.assertIn("known_runtime", by_source)
        self.assertFalse(by_source["known_runtime"]["tracked"])
        self.assertTrue(by_source["default"]["tracked"])
        self.assertEqual(by_source["known_runtime"]["stats"]["files"], 1)

    def test_sibling_without_provider_marker_is_ignored(self) -> None:
        """A ~/.codex-notes folder is not a usage source."""
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        decoy = agentcat.HOME / ".codex-notes"
        decoy.mkdir()
        (decoy / "README.md").write_text("not a home", encoding="utf-8")

        paths = [str(c["path"]) for c in agentcat.provider_home_candidates("codex")]

        self.assertNotIn(str(decoy), paths)

    def test_sibling_with_marker_is_discovered(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        sibling = agentcat.HOME / ".codex-work"
        self._codex_session(sibling, UUID_B)

        paths = [str(c["path"]) for c in agentcat.provider_home_candidates("codex")]

        self.assertIn(str(sibling), paths)

    def test_default_home_listed_even_when_absent(self) -> None:
        """Its absence is itself the diagnosis, so it must never be filtered out."""
        candidates = agentcat.provider_home_candidates("codex")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source"], "default")
        self.assertFalse(candidates[0]["exists"])


class BackCompatTests(HomeDiscoveryTestCase):
    """A stock install must scan exactly what it scanned before this feature."""

    def test_codex_session_roots_unchanged_without_adoption(self) -> None:
        home = agentcat.HOME / ".codex"
        self._codex_session(home, UUID_A)

        self.assertEqual(
            agentcat.codex_session_roots(),
            [home / "sessions", home / "archived_sessions"],
        )

    def test_claude_journal_roots_unchanged_without_adoption(self) -> None:
        self._claude_journal(agentcat.HOME / ".claude", "11111111-1111-4111-8111-111111111111")

        self.assertEqual(
            agentcat.claude_journal_project_dirs(),
            [agentcat.HOME / ".claude" / "projects"],
        )

    def test_untracked_mirror_does_not_change_scanned_files(self) -> None:
        """Discovery alone must not pull the mirror's files into the scan."""
        primary = agentcat.HOME / ".codex"
        mirror = agentcat.HOME / "Library" / "Application Support" / "orca" / "codex-runtime-home" / "home"
        expected = self._codex_session(primary, UUID_A)
        self._codex_session(mirror, UUID_B)

        self.assertEqual(agentcat.codex_session_files(), [expected])

    def test_untracked_mirror_does_not_change_token_totals(self) -> None:
        primary = agentcat.HOME / ".codex"
        mirror = agentcat.HOME / "Library" / "Application Support" / "orca" / "codex-runtime-home" / "home"
        self._codex_session(primary, UUID_A, tokens=100)

        before = agentcat.codex_sessions_snapshot(force_rebuild=True)["tokens"]["all"]
        self._codex_session(mirror, UUID_B, tokens=999)
        after = agentcat.codex_sessions_snapshot(force_rebuild=True)["tokens"]["all"]

        self.assertEqual(before, after)
        self.assertGreater(before, 0)


class AdoptionTests(HomeDiscoveryTestCase):
    def test_adopting_hardlinked_mirror_does_not_double_count(self) -> None:
        """The Phase A gate: adopting a mirror adds its unique files and nothing else."""
        primary = agentcat.HOME / ".codex"
        mirror = agentcat.HOME / "mirror"
        shared = self._codex_session(primary, UUID_A, tokens=100)
        # The mirror hardlinks the shared session and holds one the primary lacks.
        link_dir = mirror / "sessions" / "2026" / "08" / "06"
        link_dir.mkdir(parents=True)
        os.link(shared, link_dir / shared.name)
        self._codex_session(mirror, UUID_B, tokens=50)

        before = agentcat.codex_sessions_snapshot(force_rebuild=True)["tokens"]["all"]
        self._adopt("codex", mirror)
        after = agentcat.codex_sessions_snapshot(force_rebuild=True)["tokens"]["all"]

        files = agentcat.codex_session_files()
        self.assertEqual(len(files), 2, "shared session must be counted once, not twice")
        # Exactly the mirror-only session was added, so the delta is its tokens
        # and not the shared session counted a second time.
        self.assertEqual(after - before, 50 * 2)

    def test_adopting_home_with_copied_session_counts_it_once(self) -> None:
        primary = agentcat.HOME / ".codex"
        second = agentcat.HOME / "profile-two"
        original = self._codex_session(primary, UUID_A, tokens=100)
        copy_dir = second / "sessions" / "2026" / "08" / "06"
        copy_dir.mkdir(parents=True)
        (copy_dir / original.name).write_text(
            original.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self._adopt("codex", second)

        files = agentcat.codex_session_files()

        self.assertEqual(len(files), 1)

    def test_adopted_claude_home_journals_are_scanned_once(self) -> None:
        primary = agentcat.HOME / ".claude"
        second = agentcat.HOME / ".claude-work"
        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"
        original = self._claude_journal(primary, first_id)
        self._claude_journal(second, second_id)
        link_target = second / "projects" / "-tmp-proj" / original.name
        os.link(original, link_target)
        self._adopt("claude", second)

        grouped = agentcat.claude_journal_files_by_root()
        scanned = [path for _root, paths in grouped for path in paths]

        self.assertEqual(len(scanned), 2, "the hardlinked journal must not be scanned twice")
        self.assertEqual({p.stem for p in scanned}, {first_id, second_id})

    def test_scan_is_stable_across_repeated_runs(self) -> None:
        """Two runs must not keep re-adding the same files under different keys."""
        primary = agentcat.HOME / ".codex"
        mirror = agentcat.HOME / "mirror"
        shared = self._codex_session(primary, UUID_A, tokens=100)
        link_dir = mirror / "sessions" / "2026" / "08" / "06"
        link_dir.mkdir(parents=True)
        os.link(shared, link_dir / shared.name)
        self._adopt("codex", mirror)

        first = agentcat.codex_session_files()
        second = agentcat.codex_session_files()

        self.assertEqual(first, second)


class DoctorCheckTests(HomeDiscoveryTestCase):
    def _checks_by_id(self) -> dict:
        return {check["id"]: check for check in agentcat.home_discovery_checks()}

    def test_untracked_home_with_usage_is_reported(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        mirror = agentcat.HOME / "Library" / "Application Support" / "orca" / "codex-runtime-home" / "home"
        self._codex_session(mirror, UUID_B)

        check = self._checks_by_id()["codex.homes"]

        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["reason"], "untracked_home")
        self.assertEqual(check["fix"], "adopt_home")

    def test_stalled_mirror_is_distinguished_from_a_second_profile(self) -> None:
        """The silent-leak shape: we read a home that stopped growing."""
        primary = self._codex_session(agentcat.HOME / ".codex", UUID_A)
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_B)
        stale = time.time() - (5 * 86400)
        os.utime(primary, (stale, stale))

        check = self._checks_by_id()["codex.homes"]

        self.assertEqual(check["reason"], "stalled_home")
        self.assertIn("gone quiet", check["detail"])

    def test_no_untracked_homes_reads_ok(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)

        self.assertEqual(self._checks_by_id()["codex.homes"]["status"], "ok")

    def test_empty_untracked_home_is_not_reported(self) -> None:
        """A home with a marker but no usage is noise, not a finding."""
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        empty = agentcat.HOME / ".codex-empty"
        (empty / "sessions").mkdir(parents=True)

        self.assertEqual(self._checks_by_id()["codex.homes"]["status"], "ok")

    def test_excluded_home_is_not_reported(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_B)
        agentcat.write_agentcat_settings(
            {"homes": {"codex": {"excluded": [str(second)]}}}
        )

        self.assertEqual(self._checks_by_id()["codex.homes"]["status"], "ok")

    def test_env_drift_between_launchd_and_shell_is_reported(self) -> None:
        self._write_plist({"CODEX_HOME": str(agentcat.HOME / "frozen-at-install")})

        check = self._checks_by_id()["daemon.env"]

        self.assertEqual(check["reason"], "env_drift")
        self.assertIn("CODEX_HOME", check["detail"])

    def test_matching_env_is_not_drift(self) -> None:
        frozen = str(agentcat.HOME / "same")
        self._write_plist({"CODEX_HOME": frozen})
        with patch.dict(os.environ, {"CODEX_HOME": frozen}):
            self.assertNotIn("daemon.env", self._checks_by_id())

    def test_details_never_leak_the_home_path(self) -> None:
        """Same privacy contract the rest of doctor is held to."""
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        mirror = agentcat.HOME / "Library" / "Application Support" / "orca" / "codex-runtime-home" / "home"
        self._codex_session(mirror, UUID_B)
        self._write_plist({"CODEX_HOME": str(agentcat.HOME / "frozen")})

        text = json.dumps(agentcat.home_discovery_checks())

        self.assertNotIn(str(agentcat.HOME), text)


class SnapshotBlockTests(HomeDiscoveryTestCase):
    def test_snapshot_block_is_tilde_relative(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)

        block = agentcat.home_discovery_snapshot(force=True)

        self.assertNotIn(str(agentcat.HOME), json.dumps(block))
        self.assertEqual(block["codex"]["tracked"], ["~/.codex"])

    def test_snapshot_block_marks_untracked_homes(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_B)

        block = agentcat.home_discovery_snapshot(force=True)
        discovered = {entry["path"]: entry for entry in block["codex"]["discovered"]}

        self.assertTrue(discovered["~/.codex"]["tracked"])
        self.assertFalse(discovered["~/.codex-work"]["tracked"])
        self.assertEqual(discovered["~/.codex-work"]["files"], 1)

    def test_snapshot_block_is_cached_between_ticks(self) -> None:
        """The 60s tick must not re-walk every home."""
        self._codex_session(agentcat.HOME / ".codex", UUID_A)
        first = agentcat.home_discovery_snapshot(force=True)

        second_home = agentcat.HOME / ".codex-work"
        self._codex_session(second_home, UUID_B)

        self.assertEqual(agentcat.home_discovery_snapshot(), first)
        self.assertNotEqual(agentcat.home_discovery_snapshot(force=True), first)

    def test_capability_is_advertised(self) -> None:
        self.assertIn("homes.discovery", agentcat.CONNECTOR_CAPABILITIES)


class SnapshotBackCompatTests(HomeDiscoveryTestCase):
    """An older Agent Cat build must keep decoding a newer connector's snapshot.

    The app decodes with an explicit CodingKeys list where only `generatedAt`
    and `providers` are required and everything else is optional/tolerant, so a
    new top-level key is ignored rather than fatal — as long as we only ever ADD
    keys. These tests fail the moment a change removes or renames one.
    """

    # Every top-level key an installed app build may already be reading.
    ESTABLISHED_KEYS = {
        "schemaVersion",
        "connectorVersion",
        "capabilities",
        "generatedAt",
        "update",
        "activity",
        "providers",
        "desktopApps",
        "events",
    }

    def test_snapshot_keeps_every_established_top_level_key(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)

        snapshot = agentcat.build_snapshot()

        self.assertTrue(
            self.ESTABLISHED_KEYS.issubset(snapshot.keys()),
            f"missing keys an older app may read: {self.ESTABLISHED_KEYS - set(snapshot)}",
        )

    def test_snapshot_required_fields_are_present_and_typed(self) -> None:
        """generatedAt and providers are the app's only non-optional fields."""
        snapshot = agentcat.build_snapshot()

        self.assertIsInstance(snapshot["generatedAt"], str)
        self.assertIsInstance(snapshot["providers"], dict)
        self.assertIsInstance(snapshot["capabilities"], list)
        self.assertTrue(all(isinstance(item, str) for item in snapshot["capabilities"]))

    def test_snapshot_carries_the_additive_homes_block(self) -> None:
        self._codex_session(agentcat.HOME / ".codex", UUID_A)

        snapshot = agentcat.build_snapshot()

        self.assertIn("homes", snapshot)
        self.assertIn("codex", snapshot["homes"])

    def test_schema_version_is_unchanged(self) -> None:
        """Adding an optional key is not a schema break, so the version must hold.

        Bumping it would make older builds treat the payload as unknown.
        """
        self.assertEqual(agentcat.SCHEMA_VERSION, 4)

    def test_homes_block_failure_cannot_block_the_snapshot(self) -> None:
        original = agentcat.home_discovery_snapshot
        agentcat.home_discovery_snapshot = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            snapshot = agentcat.build_snapshot()
        finally:
            agentcat.home_discovery_snapshot = original

        self.assertNotIn("homes", snapshot)
        self.assertIn("providers", snapshot)


class HomesCommandTests(HomeDiscoveryTestCase):
    def _run(self, **kwargs) -> int:
        import argparse

        args = argparse.Namespace(
            provider=None, adopt=None, exclude=None, forget=None, json=False
        )
        for key, value in kwargs.items():
            setattr(args, key, value)
        return agentcat.command_homes(args)

    def test_adopt_records_the_home(self) -> None:
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_A)

        code = self._run(provider="codex", adopt=str(second))

        self.assertEqual(code, 0)
        self.assertIn(second, agentcat.tracked_provider_homes("codex"))

    def test_adopt_rejects_a_directory_that_is_not_a_provider_home(self) -> None:
        decoy = agentcat.HOME / "random"
        decoy.mkdir()

        self.assertEqual(self._run(provider="codex", adopt=str(decoy)), 2)

    def test_adopt_rejects_a_missing_directory(self) -> None:
        self.assertEqual(
            self._run(provider="codex", adopt=str(agentcat.HOME / "nope")), 2
        )

    def test_adopt_clears_a_previous_exclusion(self) -> None:
        """Otherwise the stronger exclusion would silently cancel the adoption."""
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_A)
        self._run(provider="codex", exclude=str(second))

        self._run(provider="codex", adopt=str(second))

        self.assertIn(second, agentcat.tracked_provider_homes("codex"))

    def test_exclude_drops_an_adopted_home(self) -> None:
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_A)
        self._run(provider="codex", adopt=str(second))

        self._run(provider="codex", exclude=str(second))

        self.assertNotIn(second, agentcat.tracked_provider_homes("codex"))

    def test_forget_restores_the_default_state(self) -> None:
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_A)
        self._run(provider="codex", adopt=str(second))

        self._run(provider="codex", forget=str(second))

        self.assertEqual(
            agentcat.tracked_provider_homes("codex"), [agentcat.HOME / ".codex"]
        )

    def test_mutation_requires_a_provider(self) -> None:
        self.assertEqual(self._run(adopt=str(agentcat.HOME)), 2)

    def test_unknown_provider_is_rejected(self) -> None:
        self.assertEqual(self._run(provider="nope"), 2)

    def test_adopting_twice_is_idempotent(self) -> None:
        second = agentcat.HOME / ".codex-work"
        self._codex_session(second, UUID_A)
        self._run(provider="codex", adopt=str(second))
        self._run(provider="codex", adopt=str(second))

        adopted = agentcat.agentcat_settings()["homes"]["codex"]["adopted"]

        self.assertEqual(len(adopted), 1)


if __name__ == "__main__":
    unittest.main()
