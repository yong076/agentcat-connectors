"""Signature engine tests use only temporary directories and injected IO."""
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import agentcat_home_signatures as signatures


class RecordingFS(signatures.LocalFS):
    def __init__(self):
        self.reads = []
        self.entries = []

    def children(self, path):
        for child in super().children(path):
            self.entries.append(child)
            yield child

    def read(self, path, limit):
        self.reads.append((path, limit))
        return super().read(path, limit)


class SlowFS(signatures.LocalFS):
    def stat(self, path):
        (path / "stat-entered" if path.is_dir() else path.parent / "stat-entered").touch()
        time.sleep(0.5)
        return super().stat(path)

    def children(self, path):
        (path / "children-entered").touch()
        time.sleep(0.5)
        yield from super().children(path)


class HomeSignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def home(self, name, layout="codex", originator="codex_cli_rs"):
        path = self.root / name
        (path / ("sessions" if layout == "codex" else "projects")).mkdir(parents=True)
        if layout == "codex":
            (path / "sessions/session.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"originator": originator}}) + "\n")
        else:
            (path / "settings.json").write_text("{}")
            (path / "projects/session.jsonl").write_text(json.dumps({"message": {"model": "claude-sonnet-4"}}) + "\n")
        return path

    def test_claude_identity_is_only_an_account_hash(self):
        home = self.home("team", "claude")
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "native-private", "emailAddress": "private@example.test", "organizationRateLimitTier": "pro"}}))
        result = signatures.classify_home(home)
        self.assertEqual((result["provider"], result["kind"]), ("claude", "provider"))
        self.assertEqual(result["identity"], {"accountKey": signatures.account_key("claude", "native-private")})
        for forbidden in ("native-private", "private@example.test", str(home)):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_standard_claude_metadata_beside_home(self):
        home = self.home(".claude", "claude")
        (self.root / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"standard"}}')
        self.assertEqual(signatures.classify_home(home)["identity"]["accountKey"], signatures.account_key("claude", "standard"))

    def test_home_claude_identity_precedes_root_fallback(self):
        home = self.home(".claude", "claude")
        (self.root / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"root"}}')
        (home / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"local"}}')
        self.assertEqual(signatures.classify_home(home)["identity"]["accountKey"], signatures.account_key("claude", "local"))

    def test_all_config_fingerprints_precede_jsonl_reads(self):
        home = self.home("cheap-first")
        (home / "auth.json").write_text('{"tokens":{"id_token":"fixture","account_id":"native"}}')
        (home / "config.toml").write_text('model_provider = "openai"\n')
        fs = RecordingFS()
        signatures.classify_home(home, fs=fs)
        names = [p.name for p, _ in fs.reads]
        self.assertLess(names.index("auth.json"), names.index("session.jsonl"))
        self.assertLess(names.index("config.toml"), names.index("session.jsonl"))

    def test_partial_scan_keeps_cheap_identity_and_reject(self):
        class ExhaustedSampleFS(RecordingFS):
            elapsed = 0

            def now(self):
                return self.elapsed

            def children(self, path):
                for child in super().children(path):
                    if path.name == "sessions":
                        self.elapsed = 1
                    yield child

        home = self.home(".grok")
        (home / "auth.json").write_text('{"tokens":{"id_token":"fixture","account_id":"native"}}')
        for name, expected in ((".grok", "foreign"), ("native", "provider")):
            path = home if name == ".grok" else home.rename(self.root / name)
            result = signatures.classify_home(path, fs=ExhaustedSampleFS())
            self.assertEqual(result["kind"], expected)
            self.assertIn("scan.partial", result["evidence"])
            if expected == "provider":
                self.assertEqual(result["identity"]["accountKey"], signatures.account_key("codex", "native"))

    def test_newest_date_and_project_directories_are_sampled_without_old_tree(self):
        for layout in ("codex", "claude"):
            with self.subTest(layout=layout):
                home = self.root / layout
                root = home / ("sessions" if layout == "codex" else "projects")
                older = root / ("2025/01/01" if layout == "codex" else "older")
                newer = root / ("2026/10/09" if layout == "codex" else "newer")
                for directory in (older, newer):
                    directory.mkdir(parents=True)
                    for i in range(20):
                        (directory / f"{i}.jsonl").write_text("{}\n")
                os.utime(older, (1, 1))
                os.utime(newer, (2, 2))
                fs = RecordingFS()
                files = signatures.sample_usage_files(home, (f"{root.name}/**/*.jsonl",), fs=fs, count=4)
                self.assertEqual(len(files), 4)
                self.assertTrue(all(p.parent == newer for p in files))
                self.assertFalse(any(p.parent == older for p in fs.entries))
                self.assertLessEqual(len([p for p in fs.entries if p.suffix == ".jsonl"]), 4)

    def test_codex_index_and_state_presence_are_cheap_fingerprints(self):
        for marker in ("session_index.jsonl", "state_5.sqlite"):
            with self.subTest(marker=marker):
                home = self.root / marker
                home.mkdir()
                (home / marker).write_text("")
                result = signatures.classify_home(home)
                self.assertEqual((result["provider"], result["kind"]), ("codex", "provider"))

    def test_two_provider_fingerprints_are_conflicting_even_with_different_scores(self):
        home = self.home("both", "claude")
        (home / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"claude"},"userID":"user"}')
        (home / "auth.json").write_text('{"tokens":{"id_token":"fixture"}}')
        self.assertEqual(signatures.classify_home(home)["kind"], "ambiguous")

    def test_codebuddy_brand_beats_claude_model(self):
        home = self.home("renamed", "claude")
        (home / ".codebuddy.json").write_text("{}")
        result = signatures.classify_home(home)
        self.assertEqual((result["provider"], result["kind"]), ("codebuddy", "foreign"))

    def test_grok_and_kimi_beat_codex_filename_and_auth(self):
        for brand in ("grok", "kimi"):
            with self.subTest(brand=brand):
                home = self.home(brand, originator=brand + "_cli")
                (home / "auth.json").write_text('{"tokens":{"id_token":"private-token"}}')
                result = signatures.classify_home(home)
                self.assertEqual(result["kind"], "foreign")
                self.assertEqual(result["provider"], "grok" if brand == "grok" else "kimi-code")
                self.assertNotIn("private-token", json.dumps(result))

    def test_model_provider_does_not_override_codex_cli_ownership(self):
        for provider in ("grok", "kimi", "xai"):
            with self.subTest(provider=provider):
                home = self.home("codex-" + provider)
                (home / "auth.json").write_text('{"tokens":{"id_token":"fixture","account_id":"native"}}')
                (home / "session_index.jsonl").write_text("")
                (home / "config.toml").write_text(f'model_provider = "{provider}"\n')
                with (home / "sessions/session.jsonl").open("a") as handle:
                    handle.write(json.dumps({"payload": {"model": provider + "-fixture"}}) + "\n")
                result = signatures.classify_home(home)
                self.assertEqual((result["kind"], result["provider"]), ("provider", "codex"))
                self.assertIn("originator.codex", result["evidence"])

    def test_foreign_model_provider_alone_is_not_cli_evidence(self):
        home = self.home("unidentified", originator="unknown")
        (home / "config.toml").write_text('model_provider = "grok"\n')
        self.assertIsNone(signatures.classify_home(home)["provider"])

    def test_structure_and_filename_without_fingerprint_is_ambiguous(self):
        home = self.home("unknown", originator="unknown_cli")
        (home / "sessions/session.jsonl").rename(home / "sessions/rollout-2026-01-01-11111111-1111-4111-8111-111111111111.jsonl")
        result = signatures.classify_home(home)
        self.assertIsNone(result["provider"])
        self.assertEqual(result["kind"], "ambiguous")

    def test_ties_and_small_winner_margin_fail_closed(self):
        home = self.home("tie")
        first = replace(signatures.REGISTRY[1], negative_fingerprints=())
        second = replace(first, provider="another", kind="foreign")
        for difference in (0, 1, 2):
            candidate = replace(second, fingerprints=(replace(second.fingerprints[0], weight=6-difference),))
            result = signatures.classify_home(home, (first, candidate))
            self.assertEqual(result["kind"], "ambiguous")
            self.assertIsNone(result["provider"])

    def test_new_cli_is_only_a_registry_entry(self):
        home = self.root / "custom"
        home.mkdir()
        (home / "brand.json").write_text('{"vendor":"future-cli"}')
        sig = signatures.HomeSignature("future", "provider", "future", ("FUTURE_HOME",), (".future",),
                                       structure_all=("brand.json",),
                                       fingerprints=(signatures.Fingerprint("brand.future", "json_prefix", "brand.json", "vendor", "future-"),))
        self.assertEqual(signatures.classify_home(home, (sig,))["provider"], "future")
        found = signatures.collect_candidates(self.root, (), (), {"FUTURE_HOME": str(home)}, registry=(sig,))
        self.assertIn("env", found[0]["sources"])

    def test_toml_nested_provider_definition_is_not_root_provider(self):
        home = self.home("nested", originator="unknown")
        (home / "config.toml").write_text('model_provider = "unknown"\n[model_providers.custom]\nmodel_provider = "openai"\n')
        self.assertIsNone(signatures.classify_home(home)["provider"])
        (home / "config.toml").write_text('model_provider = "openai"\n[model_providers.custom]\nname = "grok"\n')
        self.assertEqual(signatures.classify_home(home)["provider"], "codex")

    def test_toml_default_provider_requires_valid_config(self):
        home = self.home("default-config", originator="unknown")
        (home / "config.toml").write_text('model = "gpt-fixture"\n')
        self.assertEqual(signatures.classify_home(home)["provider"], "codex")
        (home / "config.toml").write_text('this is not TOML')
        self.assertIsNone(signatures.classify_home(home)["provider"])
        (home / "config.toml").write_text('theme = "dark"\n')
        self.assertIsNone(signatures.classify_home(home)["provider"])

    def test_launcher_defaulted_override_resolves_to_its_default_home(self):
        # The owner's real launchers: CLAUDE_CONFIG_DIR="${CLAUDE3_CONFIG_DIR:-$HOME/.claude3}".
        launchers = self.root / "bin"
        launchers.mkdir()
        (launchers / "claude3").write_text(
            '#!/bin/sh\nCLAUDE_CONFIG_DIR="${CLAUDE3_CONFIG_DIR:-$HOME/.claude3}"\n'
            'export CLAUDE_CONFIG_DIR\nexec "$HOME/.local/bin/claude" "$@"\n')
        (launchers / "braced").write_text('#!/bin/sh\nCODEX_HOME="${WORK_CODEX:=${HOME}/work-codex}" exec codex\n')
        (launchers / "unsafe").write_text('#!/bin/sh\nCODEX_HOME="${X:-$(touch /tmp/never-run)}" exec codex\n')
        rows = signatures.collect_candidates(self.root, (launchers,), (), {})
        found = {str(r["path"]): r["launchers"] for r in rows}
        self.assertEqual(found.get(str(self.root / ".claude3")), ["claude3"])
        self.assertEqual(found.get(str(self.root / "work-codex")), ["braced"])
        self.assertFalse(any("never-run" in path for path in found))

    def test_launcher_echo_is_not_an_assignment(self):
        directory = self.root / "bin"
        directory.mkdir()
        (directory / "fake").write_text('#!/bin/sh\necho "CODEX_HOME=/tmp/fake"')
        self.assertEqual(signatures.collect_candidates(self.root, (directory,), (), {}), [])

    def test_launcher_semicolon_and_unknown_home_variable(self):
        directory = self.root / "bin"
        directory.mkdir()
        (directory / "valid").write_text('#!/bin/sh\nexport CODEX_HOME="$HOME/one";exec codex')
        (directory / "invalid").write_text('#!/bin/sh\nCODEX_HOME=$HOME_OTHER exec codex')
        rows = signatures.collect_candidates(self.root, (directory,), (), {})
        self.assertEqual([row["path"] for row in rows], [self.root / "one"])

    def test_traversal_cap_is_reported_even_when_entries_are_directories(self):
        home = self.home("directories")
        for i in range(5):
            (home / "sessions" / str(i)).mkdir()
        status = {}
        list(signatures.usage_files(home, signatures.CODEX_GLOBS, limit=2, status=status))
        self.assertTrue(status["capped"])

    def test_bounded_reads_newest_files_and_first_lines(self):
        home = self.home("bounded", originator="unknown")
        first = home / "sessions/session.jsonl"
        first.write_text('{}\n' * 24 + '{"payload":{"originator":"codex_cli_rs"}}\n')
        fs = RecordingFS()
        result = signatures.classify_home(home, fs=fs, limits=signatures.ScanLimits(bytes_per_file=256, newest_jsonl=1, first_lines=24))
        self.assertIsNone(result["provider"])
        self.assertTrue(all(limit <= 256 for _, limit in fs.reads))
        self.assertEqual(len([p for p, _ in fs.reads if p.suffix == ".jsonl"]), 1)

    def test_zero_budget_does_not_read(self):
        home = self.home("budget")
        fs = RecordingFS()
        result = signatures.classify_home(home, fs=fs, limits=signatures.ScanLimits(seconds=0))
        self.assertIsNone(result["provider"])
        self.assertEqual(fs.reads, [])

    def test_launcher_forms_and_path_expansion_without_execution(self):
        launchers = self.root / "bin"
        launchers.mkdir()
        forms = ("export CODEX_HOME=~/one\nexec codex", 'env CLAUDE_CONFIG_DIR="$HOME/two" claude',
                 'CODEX_HOME="${HOME}/three" exec codex')
        for index, source in enumerate(forms):
            (launchers / f"launch{index}").write_text("#!/bin/sh\n" + source)
        (launchers / "unsafe").write_text('CODEX_HOME="$(touch /tmp/never-run)" exec codex')
        (launchers / "comment").write_text('# CODEX_HOME="/tmp/never-found"')
        (launchers / "binary").write_bytes(b"\x00CODEX_HOME=/tmp/never-found")
        (launchers / "oversized").write_text("CODEX_HOME=/tmp/never-found\n" + "x" * 65536)
        rows = signatures.collect_candidates(self.root, (launchers,), (), {})
        found = {str(r["path"]): r["launchers"] for r in rows}
        self.assertEqual(set(found), {str(self.root / n) for n in ("one", "two", "three")})
        self.assertEqual(found[str(self.root / "two")], ["launch1"])

    def test_denylist_and_symlinks_ignored_and_sources_merged(self):
        valid = self.home("arbitrary")
        for name in signatures.DENYLIST:
            (self.root / name).mkdir(exist_ok=True)
        (self.root / "linked").symlink_to(valid)
        rows = signatures.collect_candidates(self.root, (), (valid,), {"CODEX_HOME": str(valid)})
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["sources"]), {"env", "known_runtime", "auto"})

    def test_remote_homes_and_launcher_targets_are_skipped_before_stat(self):
        remote = self.home("remote")
        local = self.home("local")
        launcher_dir = self.root / "bin"
        launcher_dir.mkdir()
        (launcher_dir / "remote-launcher").write_text(f'#!/bin/sh\nCODEX_HOME="{remote}" exec codex\n')
        class RemoteFS(RecordingFS):
            def is_local(self, path):
                return remote != path and remote not in path.parents
            def is_dir(self, path):
                if not self.is_local(path):
                    raise AssertionError("remote stat attempted")
                return super().is_dir(path)
            def is_symlink(self, path):
                if not self.is_local(path):
                    raise AssertionError("remote lstat attempted")
                return super().is_symlink(path)
        rows = signatures.collect_candidates(self.root, (launcher_dir, remote), (remote,),
                                             {"CODEX_HOME": str(remote)}, fs=RemoteFS())
        self.assertEqual([r["path"] for r in rows], [local])

    def test_worker_session_inventory_matches_in_process_result(self):
        # The daemon runs the whole inventory in the worker (one round trip);
        # it must equal the in-process result, local-only guard included.
        home = self.home("inventory")
        for i in range(5):
            day = home / f"sessions/2026/10/0{i + 1}"
            day.mkdir(parents=True)
            (day / f"rollout-2026-10-0{i + 1}T00-00-00-00000000-0000-4000-8000-00000000000{i}.jsonl").write_text("{}\n")
        direct = signatures.session_inventory(home, signatures.CODEX_GLOBS, fs=signatures.LocalFS())
        with signatures.BoundedFS(time.monotonic() + 10) as worker:
            via_worker = signatures.bounded_session_inventory(
                home, signatures.CODEX_GLOBS, fs=signatures.LocalOnlyFS(worker), deadline=time.monotonic() + 10)
        self.assertEqual(via_worker["sessions"], direct["sessions"])
        self.assertEqual(via_worker["stats"]["files"], direct["stats"]["files"])

    def test_stalled_filesystem_io_times_out_and_next_worker_recovers(self):
        home = self.home("slow")
        for operation in ("stat", "children"):
            with self.subTest(operation=operation):
                worker = (signatures.BoundedFS(time.monotonic() + 2, fs_type=SlowFS)
                          if hasattr(signatures, "BoundedFS") else nullcontext(SlowFS()))
                try:
                    with worker as fs:
                        self.assertTrue(fs.is_dir(home))  # Warm startup before timing the blocked IO.
                        start = time.monotonic()
                        fs.deadline = start + 0.1
                        result = getattr(fs, operation)(home)
                        if operation == "children":
                            list(result)
                except TimeoutError:
                    pass
                self.assertLess(time.monotonic() - start, 0.4)
                self.assertTrue((home / f"{operation}-entered").exists())
                if hasattr(signatures, "BoundedFS"):
                    self.assertFalse(fs.alive)
                    with signatures.BoundedFS(time.monotonic() + 2) as recovered:
                        self.assertTrue(recovered.is_dir(home))

    @unittest.skipIf(os.name == "nt", "POSIX mount prefixes; Windows uses drive types")
    def test_mount_policy_uses_longest_lexical_prefix_and_fails_closed(self):
        mounts = signatures.LocalMounts([(Path("/"), True), (Path("/remote"), False),
                                         (Path("/remote/local"), True)])
        self.assertFalse(mounts.is_local(Path("/remote/team")))
        self.assertTrue(mounts.is_local(Path("/remote/local/team")))
        self.assertTrue(mounts.is_local(Path("/remote-other/team")))
        self.assertTrue(mounts.is_local(Path("/remote/../local")))
        self.assertFalse(signatures.LocalMounts([]).is_local(self.root))

    def test_nested_remote_sqlite_index_is_skipped_without_losing_local_sessions(self):
        home = self.home("nested-remote")
        (home / "state_remote.sqlite").write_text("not a local database")
        class RemoteIndexFS(RecordingFS):
            def is_local(self, path):
                return path.name != "state_remote.sqlite"
            def sqlite_ids(self, *args):
                raise AssertionError("remote SQLite read attempted")
        inventory = signatures.session_inventory(home, signatures.CODEX_GLOBS,
                                                  fs=signatures.LocalOnlyFS(RemoteIndexFS()))
        self.assertEqual(inventory["stats"]["files"], 1)
        self.assertTrue(inventory["partial"])

    def test_home_id_uses_realpath_and_twelve_hex_digits(self):
        home = self.home("id")
        alias = self.root / "alias"
        alias.symlink_to(home)
        self.assertRegex(signatures.home_id(home), r"^[0-9a-f]{12}$")
        self.assertEqual(signatures.home_id(home), signatures.home_id(alias))


if __name__ == "__main__":
    unittest.main()
