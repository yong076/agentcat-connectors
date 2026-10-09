"""Signature engine tests use only temporary directories and injected IO."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import agentcat_home_signatures as signatures


class RecordingFS(signatures.LocalFS):
    def __init__(self):
        self.reads = []

    def read(self, path, limit):
        self.reads.append((path, limit))
        return super().read(path, limit)


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

    def test_home_id_uses_realpath_and_twelve_hex_digits(self):
        home = self.home("id")
        alias = self.root / "alias"
        alias.symlink_to(home)
        self.assertRegex(signatures.home_id(home), r"^[0-9a-f]{12}$")
        self.assertEqual(signatures.home_id(home), signatures.home_id(alias))


if __name__ == "__main__":
    unittest.main()
