"""Automatic tracking, dedup attribution, and probe/label integration."""
import datetime as dt
from contextlib import closing
import json
import os
import shutil
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_home_discovery import HomeDiscoveryTestCase, UUID_A, UUID_B, UUID_C, agentcat


class AutoHomeDiscoveryTests(HomeDiscoveryTestCase):
    def account(self, home, native):
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": native}}))

    def probe_row(self, provider, home, native):
        return {"provider": provider, "homeKey": agentcat.cli_probe.home_key(home), "accountID": native,
                "status": "ok", "fetchedAt": int(dt.datetime.now().timestamp()),
                "windows": [{"id": provider + ":7d", "usedPercent": 5, "remainingPercent": 95,
                             "windowDurationMins": 10080}]}

    def launcher(self, name, provider, home):
        directory = agentcat.HOME / ".local/bin"
        directory.mkdir(parents=True, exist_ok=True)
        var = agentcat.PROVIDER_HOME_SPECS[provider]["envVar"]
        path = directory / name
        path.write_text(f'#!/bin/sh\nexport {var}="{home}"\nexec {provider} "$@"\n')
        path.chmod(0o700)

    def test_auto_candidate_cap_never_limits_explicit_homes(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                homes = [agentcat.HOME / f"{provider}-adopted-{i:02d}" for i in range(33)]
                for i, home in enumerate(homes):
                    sid = f"00000000-0000-4000-8000-{i:012d}"
                    if provider == "codex":
                        self._codex_session(home, sid)
                    else:
                        self._claude_journal(home, sid)
                agentcat.write_agentcat_settings({"homes": {provider: {"adopted": [str(h) for h in homes]}}})
                self.assertTrue(set(homes).issubset(agentcat.tracked_provider_homes(provider)))
                result = (agentcat.codex_sessions_snapshot(force_rebuild=True) if provider == "codex"
                          else agentcat.claude_snapshot())
                self.assertEqual(result["tokens"]["all"], 33 * (200 if provider == "codex" else 30))

    def test_global_deadline_preserves_last_good_discovery(self):
        home = agentcat.HOME / "last-good"
        self._codex_session(home, UUID_A)
        previous = agentcat._discover_provider_homes(force=True)
        clock = [100.0]
        deadlines = []
        signatures = agentcat.home_signatures

        class InlineFS(signatures.LocalFS):
            now = staticmethod(lambda: clock[0])
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def classify(path, **kwargs):
            deadlines.append(kwargs["deadline"])
            clock[0] += 0.1
            return {"kind": "provider", "provider": "codex", "layouts": ["codex"],
                    "score": 6, "evidence": [], "identity": {}}

        rows = [{"path": agentcat.HOME / f"candidate-{i}", "sources": ["auto"], "launchers": []}
                for i in range(100)]
        for row in rows:
            (row["path"] / "sessions").mkdir(parents=True)  # a layout, so each one is classified
        with patch.object(agentcat.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(agentcat, "HOME_DISCOVERY_TIME_BUDGET_SECONDS", 0.35), \
             patch.object(signatures, "BoundedFS", InlineFS, create=True), \
             patch.object(signatures, "collect_candidates", return_value=rows), \
             patch.object(signatures, "classify_home", side_effect=classify), \
             patch.object(signatures, "session_inventory", return_value={"sessions": set(), "paths": {}, "partial": False,
                          "stats": {"files": 0, "newestMtime": None, "capped": False}}):
            result = agentcat._discover_provider_homes(force=True)
        self.assertLessEqual(len(deadlines), 4)
        self.assertTrue(all(d == 100.35 for d in deadlines), deadlines)
        self.assertEqual(result, previous)
        self.assertFalse(agentcat._HOME_CANDIDATES_COMPLETE)

    def test_auto_candidate_cap_is_checked_before_classification(self):
        signatures = agentcat.home_signatures
        rows = []
        for i in range(100):
            path = agentcat.HOME / f"auto-{i:03d}"
            (path / "sessions").mkdir(parents=True)  # a layout, so the cap applies
            rows.append({"path": path, "sources": ["auto"], "launchers": []})
        with patch.object(signatures, "collect_candidates", return_value=rows), \
             patch.object(signatures, "classify_home", return_value={"kind": "provider", "provider": "codex",
                          "layouts": ["codex"], "score": 6, "evidence": [], "identity": {}}) as classify:
            agentcat._discover_provider_homes(force=True)
        self.assertLessEqual(classify.call_count, agentcat.HOME_DISCOVERY_MAX_CANDIDATES + 2)

    def test_timeout_keeps_last_good_homes_and_applies_new_explicit_choices(self):
        auto = agentcat.HOME / "last-good"
        self._codex_session(auto, UUID_A)
        agentcat._discover_provider_homes(force=True)
        adopted = agentcat.HOME / "new-explicit"
        self._codex_session(adopted, UUID_B)
        agentcat.write_agentcat_settings({"homes": {"codex": {
            "adopted": [str(adopted)], "excluded": [str(auto)]}}})
        with patch.object(agentcat, "HOME_DISCOVERY_TIME_BUDGET_SECONDS", 0):
            candidates = agentcat._discover_provider_homes(force=True)["codex"]
        states = {c["path"]: c["state"] for c in candidates}
        self.assertEqual(states[auto], "excluded")
        self.assertEqual(states[adopted], "tracked")
        self.assertEqual(states[agentcat.HOME / ".codex"], "tracked")
        self.assertFalse(agentcat._HOME_CANDIDATES_COMPLETE)

    def test_explicit_remote_homes_still_read_while_auto_homes_are_skipped(self):
        signatures = agentcat.home_signatures
        default = agentcat.HOME / ".codex"
        adopted = agentcat.HOME / "adopted"
        auto = agentcat.HOME / "automatic"
        for i, home in enumerate((default, adopted, auto)):
            self._codex_session(home, f"00000000-0000-4000-8000-{i:012d}")
        self._adopt("codex", adopted)
        class RemoteFS(signatures.LocalFS):
            def __init__(self, deadline):
                self.deadline = deadline
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def is_local(self, path):
                return not any(path == h or h in path.parents for h in (default, adopted, auto))
        with patch.object(signatures, "BoundedFS", RemoteFS):
            self.assertEqual(set(agentcat.tracked_provider_homes("codex")), {default, adopted})
            self.assertEqual(agentcat.codex_sessions_snapshot(force_rebuild=True)["tokens"]["all"], 400)

    def test_mirror_unique_usage_reaches_its_account_and_keeps_files_without_uuid(self):
        primary = agentcat.HOME / ".codex"
        for i in range(9):
            self._codex_session(primary, f"00000000-0000-4000-8000-{i:012d}")
        self._codex_auth(primary, "same-account")
        mirror = agentcat.HOME / "mirror"
        shutil.copytree(primary, mirror)
        self._codex_session(mirror, UUID_B, tokens=999)
        anonymous = self._codex_session(mirror, UUID_C, tokens=50)
        anonymous = anonymous.rename(anonymous.parent / "new-session.jsonl")
        self.assertIn(anonymous, agentcat.codex_session_files())
        result = agentcat.codex_sessions_snapshot(force_rebuild=True)
        self.assertEqual(result["tokens"]["all"], 3898)
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": [self.probe_row("codex", primary, "same-account")]})
        self.assertEqual(agentcat.cli_probe_provider_instances()[0]["usage"]["today"], 3898)

    def test_failed_probe_tries_next_home_of_same_account(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                default = agentcat.HOME / f".{provider}"
                adopted = agentcat.HOME / f"{provider}-work"
                third = agentcat.HOME / f"{provider}-other"
                for home in (default, adopted, third):
                    if provider == "codex":
                        self._codex_auth(home, "same-account")
                    else:
                        self._claude_journal(home, UUID_A)
                        self.account(home, "same-account")
                self._adopt(provider, adopted)
                success = self.probe_row(provider, adopted, "same-account")
                expired = dict(self.probe_row(provider, default, "same-account"),
                               status="error", reason="cli_login_expired", windows=[])
                other = "claude" if provider == "codex" else "codex"
                with patch.object(agentcat.cli_probe, f"probe_{provider}_home", side_effect=[expired, success]) as probe, \
                     patch.object(agentcat.cli_probe, f"probe_{other}_home", return_value={"status": "error", "homeKey": "unknown"}), \
                     patch.object(agentcat.shutil, "which", return_value=None), \
                     patch.object(agentcat, "probe_antigravity", return_value=None):
                    rows = agentcat.run_cli_probes()["results"]
                self.assertEqual([c.args[0] for c in probe.call_args_list], [default, adopted])
                self.assertIn(success, rows)

    def test_five_claude_homes_distinct_accounts_auto_tracked_with_usage(self):
        homes = [agentcat.HOME / (".claude" if i == 1 else f".claude{i}") for i in range(1, 6)]
        rows = []
        for i, home in enumerate(homes, 1):
            self._claude_journal(home, f"00000000-0000-4000-8000-{i:012d}")
            self.account(home, f"account-{i}")
            rows.append(self.probe_row("claude", home, f"account-{i}"))
        self.assertEqual(set(agentcat.tracked_provider_homes("claude")), set(homes))
        snapshot = agentcat.claude_snapshot()
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": rows})
        instances = agentcat.cli_probe_provider_instances()
        self.assertEqual(len(instances), 5)
        block = agentcat.home_discovery_snapshot()["claude"]["discovered"]
        for period in ("today", "week", "month"):
            self.assertEqual(sum(item["usage"][period] for item in block), snapshot["tokens"][period])
            self.assertEqual(sum(item["usage"][period] for item in instances), snapshot["tokens"][period])
        self.assertEqual(sum(item["usage"]["all"] for item in block), 150)
        self.assertNotIn("account-", json.dumps(block))

    def test_automatic_foreign_and_unidentified_homes_are_not_counted(self):
        buddy = agentcat.HOME / ".codebuddy"
        self._claude_journal(buddy, UUID_A)
        (buddy / ".codebuddy.json").write_text("{}")
        homes = []
        for name, brand in ((".grok", "grok"), (".trappist-grok-lean", "grok"), (".kimi-code", "kimi")):
            home = agentcat.HOME / name
            self._codex_session(home, UUID_B)
            (home / "config.toml").write_text(f'model_provider = "{brand}"\n')
            homes.append(home)
        ambiguous = agentcat.HOME / "lookalike"
        (ambiguous / "sessions").mkdir(parents=True)
        (ambiguous / "sessions/session.jsonl").write_text('{}\n')
        candidates = {c["path"]: c for c in agentcat.provider_home_candidates("codex")}
        self.assertNotIn(ambiguous, candidates)
        for home in homes:
            self.assertEqual(candidates[home]["state"], "foreign")
            self.assertNotIn(home, agentcat.tracked_provider_homes("codex"))
        self.assertNotIn(buddy, agentcat.tracked_provider_homes("claude"))
        self.assertEqual(agentcat.codex_session_files(), [])

    def test_explicit_homes_remain_tracked_despite_conflicts_or_budget(self):
        default = agentcat.HOME / ".codex"
        self._codex_session(default, UUID_A)
        (default / "config.toml").write_text('model_provider = "grok"\n')
        adopted = agentcat.HOME / ".codebuddy"
        self._claude_journal(adopted, UUID_B)
        (adopted / ".codebuddy.json").write_text("{}")
        self._adopt("claude", adopted)
        for limits in (agentcat.home_signatures.ScanLimits(), agentcat.home_signatures.ScanLimits(seconds=0)):
            with self.subTest(seconds=limits.seconds), patch.object(agentcat.home_signatures, "ScanLimits", return_value=limits):
                self._reset_discovery_cache()
                self.assertIn(default, agentcat.tracked_provider_homes("codex"))
                self.assertIn(adopted, agentcat.tracked_provider_homes("claude"))

    def test_real_size_homes_are_tracked_within_each_home_budget(self):
        codex = agentcat.HOME / ".codex"
        self._codex_auth(codex, "large-codex")
        for day in range(1, 31):
            directory = codex / "sessions/2026/09" / f"{day:02d}"
            directory.mkdir(parents=True)
            for item in range(500):
                sid = f"00000000-0000-4000-8000-{day * 500 + item:012d}"
                (directory / f"rollout-2026-09-{day:02d}-{sid}.jsonl").write_text('{"payload":{"originator":"codex_cli_rs"}}\n')
        claude = agentcat.HOME / ".claude2"
        for project in range(30):
            for item in range(20):
                self._claude_journal(claude, f"10000000-0000-4000-8000-{project * 20 + item:012d}", project=f"project-{project:02d}")
        self.account(claude, "large-claude")
        signatures = agentcat.home_signatures
        for home, provider in ((codex, "codex"), (claude, "claude")):
            with self.subTest(provider=provider):
                start = time.monotonic()
                result = signatures.classify_home(home)
                self.assertLess(time.monotonic() - start, signatures.ScanLimits().seconds)
                self.assertEqual((result["provider"], result["kind"]), (provider, "provider"))
        start = time.monotonic()
        block = agentcat.home_discovery_snapshot(force=True)
        self.assertLess(time.monotonic() - start, agentcat.HOME_DISCOVERY_TIME_BUDGET_SECONDS)
        self.assertIn("~/.codex", block["codex"]["tracked"])
        self.assertIn("~/.claude2", block["claude"]["tracked"])

    def test_owner_regression_table_and_candidate_order_independence(self):
        expected = {"claude": {}, "codex": {}}
        # The owner's HOME has ~160 entries; unrelated ones sorted before the
        # real homes must not use up the auto-candidate cap.
        for i in range(agentcat.HOME_DISCOVERY_MAX_CANDIDATES * 3):
            (agentcat.HOME / f".aaa-unrelated-{i:03d}" / "cache").mkdir(parents=True)
        for i in range(1, 6):
            home = agentcat.HOME / (".claude" if i == 1 else f".claude{i}")
            self._claude_journal(home, f"10000000-0000-4000-8000-{i:012d}")
            self.account(home, f"claude-{i}")
            if i > 1:
                self.launcher(f"claude{i}", "claude", home)
            # A generic foreign layout marker must not add a Codex row.
            (home / "sessions").mkdir()
            expected["claude"][home] = "tracked"
        buddy = agentcat.HOME / ".codebuddy"
        self._claude_journal(buddy, UUID_C)
        (buddy / ".codebuddy.json").write_text("{}")
        expected["claude"][buddy] = "foreign"
        primary = agentcat.HOME / ".codex"
        second = agentcat.HOME / ".codex-2"
        self._codex_session(primary, UUID_A)
        self._codex_session(second, UUID_B)
        orca = agentcat.HOME / "Library/Application Support/orca"
        own = orca / "codex-accounts/34c5/home"
        self._codex_session(own, UUID_C)
        # An index identifies archived/moved sessions independently of paths.
        (own / "session_index.jsonl").write_text(json.dumps({"id": UUID_C}) + "\n")
        mirror = orca / "codex-accounts/305c/home"
        shutil.copytree(primary, mirror)
        runtime = orca / "codex-runtime-home/home"
        shutil.copytree(own, runtime)
        for home in (primary, second, own):
            expected["codex"][home] = "tracked"
        for home in (mirror, runtime):
            expected["codex"][home] = "mirror"
        for name, brand in ((".grok", "openai"), (".trappist-grok-lean", "grok"), (".kimi-code", "kimi"), (".progrok", "grok")):
            home = agentcat.HOME / name
            rollout = self._codex_session(home, UUID_A)
            with rollout.open("a") as handle:
                handle.write(json.dumps({"payload": {"originator": "kimi_cli" if brand == "kimi" else "grok_cli"}}) + "\n")
            (home / "config.toml").write_text(f'model_provider = "{brand}"\n')
            expected["codex"][home] = "foreign"
        for name in (".cursor", ".gstack", ".factory", ".maestro", ".paperclip", ".paperclip-ko", "jeomjip-agent"):
            home = agentcat.HOME / name
            (home / "projects").mkdir(parents=True)
            (home / "settings.json").write_text("{}")
            (home / "config.toml").write_text('theme = "dark"\n')
            (home / "sessions").mkdir()
            (home / "sessions/unrelated.jsonl").write_text("{}\n")
        collect = agentcat.home_signatures.collect_candidates
        for reverse in (False, True):
            # Correctness, not speed: a slow runner must not turn this into a budget test.
            with self.subTest(reverse=reverse), patch.object(agentcat, "HOME_DISCOVERY_TIME_BUDGET_SECONDS", 120.0), \
                    patch.object(agentcat.home_signatures, "collect_candidates",
                    side_effect=lambda *a, **kw: list(reversed(collect(*a, **kw))) if reverse else collect(*a, **kw)):
                self._reset_discovery_cache()
                for provider in expected:
                    actual = {c["path"]: c["state"] for c in agentcat.provider_home_candidates(provider)}
                    self.assertEqual(actual, expected[provider])

    def test_indexed_mirrors_cover_moved_and_unsampled_sessions_read_only(self):
        primary = agentcat.HOME / ".codex"
        primary.mkdir()
        self._codex_auth(primary, "indexed")
        ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1000)]
        # The state index covers sessions outside the owner's filename sample.
        database = primary / "state_5.sqlite"
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO threads VALUES (?)", [(sid,) for sid in ids])
        mirror = agentcat.HOME / "Library/Application Support/orca/codex-runtime-home/home"
        for i, sid in enumerate(ids):
            self._codex_session(primary, sid)
            copied = self._codex_session(mirror, sid, day="07", tokens=200)
            with copied.open("a") as handle:
                handle.write("{}\n")
        index = mirror / "session_index.jsonl"
        index.write_text("".join(json.dumps({"id": sid}) + "\n" for sid in ids))
        unique = self._codex_session(mirror, UUID_C, day="07", tokens=999)
        before_db, before_index = database.read_bytes(), index.read_bytes()
        candidates = {c["path"]: c for c in agentcat.provider_home_candidates("codex")}
        self.assertEqual(candidates[mirror]["state"], "mirror")
        self.assertEqual(len(candidates[primary]["_sessionIds"]), 1000)
        files = agentcat.codex_session_files()
        self.assertEqual(len(files), 1001)
        self.assertIn(unique, files)
        self.assertTrue(all(mirror in p.parents for p in files))
        self.assertEqual(database.read_bytes(), before_db)
        self.assertEqual(index.read_bytes(), before_index)
        self.assertFalse((primary / "state_5.sqlite-journal").exists())

    def test_unindexed_mirror_checks_unsampled_ids_against_owner_paths(self):
        primary = agentcat.HOME / ".codex"
        for i in range(300):
            self._codex_session(primary, f"00000000-0000-4000-8000-{i:012d}")
        mirror = agentcat.HOME / "mirror"
        shutil.copytree(primary, mirror)
        unique = self._codex_session(mirror, UUID_C, tokens=999)
        candidates = {c["path"]: c for c in agentcat.provider_home_candidates("codex")}
        self.assertEqual(candidates[mirror]["state"], "mirror")
        self.assertGreaterEqual(len(candidates[mirror]["_mirrorOwners"]), 255)
        files = agentcat.codex_session_files()
        self.assertEqual(len(files), 301)
        self.assertIn(unique, files)

    def test_orca_mirror_has_zero_usage_and_no_probe(self):
        primary = agentcat.HOME / ".codex"
        first = self._codex_session(primary, UUID_A)
        mirror = agentcat.HOME / "Library/Application Support/orca/codex-accounts/mirror/home"
        shutil.copytree(primary, mirror)
        candidate = next(c for c in agentcat.provider_home_candidates("codex") if c["path"] == mirror)
        self.assertEqual(candidate["state"], "mirror")
        self.assertNotIn(mirror, agentcat.tracked_provider_homes("codex"))
        result = agentcat.codex_sessions_snapshot(force_rebuild=True)
        block = agentcat.home_discovery_snapshot()["codex"]["discovered"]
        self.assertEqual(sum(c["usage"]["all"] for c in block), result["tokens"]["all"])
        self.assertEqual(next(c["usage"]["all"] for c in block if c["state"] == "mirror"), 0)
        with patch.object(agentcat.cli_probe, "probe_codex_home", return_value=self.probe_row("codex", primary, "account")) as probe, \
             patch.object(agentcat.shutil, "which", return_value=None), \
             patch.object(agentcat, "probe_antigravity", return_value=None), \
             patch.object(agentcat.cli_probe, "probe_claude_home", return_value={}):
            agentcat.run_cli_probes()
        self.assertEqual([c.args[0] for c in probe.call_args_list], [primary])
        self.assertEqual(agentcat.codex_session_files(), [first])

    def test_ninety_percent_mirror_keeps_unique_tail_and_longer_copy_wins(self):
        primary = agentcat.HOME / ".codex"
        for i in range(9):
            self._codex_session(primary, f"00000000-0000-4000-8000-{i:012d}")
        mirror = agentcat.HOME / "mirror"
        shutil.copytree(primary, mirror)
        extra = self._codex_session(mirror, UUID_C, tokens=999)
        first = next((mirror / "sessions").rglob("*.jsonl"))
        if first != extra:
            with first.open("a") as handle:
                handle.write('{}\n')
        candidates = agentcat.provider_home_candidates("codex")
        self.assertEqual(next(c["state"] for c in candidates if c["path"] == mirror), "mirror")
        files = agentcat.codex_session_files()
        self.assertEqual(len(files), 10)
        self.assertIn(extra, files)
        self.assertIn(first, files)
        self.assertEqual(agentcat.codex_sessions_snapshot(force_rebuild=True)["tokens"]["all"], 3798)
        block = agentcat.home_discovery_snapshot()["codex"]["discovered"]
        self.assertEqual(next(c["usage"]["all"] for c in block if c["state"] == "mirror"), 1998)
        self.assertEqual(sum(c["usage"]["all"] for c in block), 3798)

    def test_launcher_outside_home_named_label_and_private_path(self):
        home = self.root / "outside/team"
        self._codex_session(home, UUID_A)
        self._codex_auth(home, "team-account")
        self.launcher("codex-team", "codex", home)
        candidate = next(c for c in agentcat.provider_home_candidates("codex") if c["path"] == home)
        self.assertEqual(candidate["source"], "launcher")
        self.assertEqual(candidate["launchers"], ["codex-team"])
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": [self.probe_row("codex", home, "team-account")]})
        agentcat.codex_sessions_snapshot(force_rebuild=True)
        row = agentcat.cli_probe_provider_instances()[0]
        self.assertEqual(row["label"], "Codex · codex-team")
        self.assertEqual(row["usage"]["today"], 200)
        block = agentcat.home_discovery_snapshot()
        self.assertNotIn(str(self.root), json.dumps(block))
        self.assertRegex(next(c["path"] for c in block["codex"]["discovered"] if c["id"] == candidate["id"]), r"^~/<external-[0-9a-f]{12}>$")

    def test_alias_free_label_keeps_default_and_claude_launcher_labels(self):
        home = agentcat.HOME / ".claude3"
        self._claude_journal(home, UUID_A)
        self.account(home, "claude-account")
        row = self.probe_row("claude", home, "claude-account")
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": [row]})
        original = agentcat.cli_probe_provider_instances()[0]["label"]
        self.assertNotEqual(original, "Claude · claude3")
        self.launcher("claude3", "claude", home)
        self._reset_discovery_cache()
        self.assertEqual(agentcat.cli_probe_provider_instances()[0]["label"], "Claude · claude3")

    def test_same_account_homes_sum_after_dedup_and_exclusion_beats_auto(self):
        homes = [agentcat.HOME / ".codex", agentcat.HOME / "unusual"]
        for home, sid in zip(homes, (UUID_A, UUID_B)):
            self._codex_session(home, sid)
            self._codex_auth(home, "same-account")
        snapshot = agentcat.codex_sessions_snapshot(force_rebuild=True)
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": [self.probe_row("codex", home, "same-account") for home in homes]})
        rows = agentcat.cli_probe_provider_instances()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["usage"]["today"], snapshot["tokens"]["today"])
        self.assertEqual(rows[0]["profileCount"], 2)
        agentcat.write_agentcat_settings({"homes": {"codex": {"excluded": [str(homes[1])]}}})
        self.assertNotIn(homes[1], agentcat.tracked_provider_homes("codex"))
        after = agentcat.codex_sessions_snapshot()
        self.assertEqual(after["tokens"]["all"], 200)
        self.assertEqual(agentcat.cli_probe_provider_instances()[0]["usage"]["today"], 200)

    def test_observed_account_switch_does_not_assign_history_to_new_account(self):
        home = agentcat.HOME / "claude-team"
        self._claude_journal(home, UUID_A)
        self.account(home, "before")
        agentcat.claude_snapshot()
        self.account(home, "after")
        self._reset_discovery_cache()
        self._claude_journal(home, UUID_B)
        agentcat.claude_snapshot()
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": [self.probe_row("claude", home, "after")]})
        self.assertEqual(agentcat.cli_probe_provider_instances()[0]["usage"]["today"], 30)
        block = agentcat.home_discovery_snapshot()["claude"]["discovered"]
        self.assertEqual(sum(c["usage"]["all"] for c in block), 60)

    def test_default_exclusion_is_respected(self):
        home = agentcat.HOME / ".codex"
        self._codex_session(home, UUID_A)
        agentcat.write_agentcat_settings({"homes": {"codex": {"excluded": [str(home)]}}})
        self.assertEqual(agentcat.tracked_provider_homes("codex"), [])
        self.assertEqual(agentcat.codex_session_files(), [])

    def test_cached_quota_cannot_reintroduce_excluded_home(self):
        home = agentcat.HOME / "team"
        self._codex_session(home, UUID_A)
        self._codex_auth(home, "account")
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": [self.probe_row("codex", home, "account")]})
        self.assertEqual(len(agentcat.cli_probe_provider_instances()), 1)
        agentcat.write_agentcat_settings({"homes": {"codex": {"excluded": [str(home)]}}})
        self.assertEqual(agentcat.cli_probe_provider_instances(), [])

    def test_runtime_mirror_is_compared_after_all_direct_homes(self):
        primary = agentcat.HOME / ".codex"
        second = agentcat.HOME / ".codex-2"
        self._codex_session(primary, UUID_A)
        second_file = self._codex_session(second, UUID_B)
        mirror = agentcat.HOME / "Library/Application Support/orca/codex-accounts/full/home"
        shutil.copytree(primary, mirror)
        target = mirror / second_file.relative_to(second)
        shutil.copy2(second_file, target)
        candidates = {c["path"]: c for c in agentcat.provider_home_candidates("codex")}
        self.assertEqual(candidates[second]["state"], "tracked")
        self.assertEqual(candidates[mirror]["state"], "mirror")
        self.assertEqual(agentcat.codex_sessions_snapshot(force_rebuild=True)["tokens"]["all"], 400)

    def test_longer_copy_appearing_after_cursor_does_not_add_history_twice(self):
        primary = agentcat.HOME / ".codex"
        original = self._codex_session(primary, UUID_A)
        first = agentcat.codex_sessions_snapshot()
        mirror = agentcat.HOME / "mirror"
        shutil.copytree(primary, mirror)
        copied = mirror / original.relative_to(primary)
        with copied.open("a") as handle:
            handle.write('{}\n')
        self._reset_discovery_cache()
        second = agentcat.codex_sessions_snapshot()
        self.assertEqual(second["tokens"], first["tokens"])
        self.assertEqual(agentcat.codex_session_files(), [copied])
        self.assertEqual(sum(c["usage"]["all"] for c in agentcat.home_discovery_snapshot()["codex"]["discovered"]), 200)

    def test_account_switch_baseline_survives_a_dedup_rebuild(self):
        home = agentcat.HOME / ".codex"
        self._codex_session(home, UUID_A)
        self._codex_auth(home, "before")
        agentcat.codex_sessions_snapshot()
        self._codex_auth(home, "after")
        self._reset_discovery_cache()
        agentcat.codex_sessions_snapshot()
        agentcat.write_json_atomic(agentcat.CLI_PROBE_CACHE, {"results": [self.probe_row("codex", home, "after")]})
        self.assertEqual(agentcat.cli_probe_provider_instances()[0]["usage"]["today"], 0)
        self._codex_session(home, UUID_B)
        agentcat.codex_sessions_snapshot()
        mirror = agentcat.HOME / "mirror"
        shutil.copytree(home, mirror)
        copied = next((mirror / "sessions").rglob("*.jsonl"))
        with copied.open("a") as handle:
            handle.write('{}\n')
        self._reset_discovery_cache()
        agentcat.codex_sessions_snapshot()
        self.assertEqual(agentcat.cli_probe_provider_instances()[0]["usage"]["today"], 200)

    def test_sandbox_discards_ambient_home_override(self):
        from sandbox import redirect_module_paths, restore_module_paths
        outside = self.root / "ambient"
        self._codex_session(outside, UUID_A)
        with patch.dict(os.environ, {"CODEX_HOME": str(outside)}):
            originals = redirect_module_paths(agentcat, agentcat.HOME, agentcat.AGENTCAT_HOME)
            try:
                self.assertNotIn("CODEX_HOME", os.environ)
                self.assertNotIn(outside, agentcat.tracked_provider_homes("codex"))
            finally:
                restore_module_paths(agentcat, originals)
            self.assertEqual(os.environ["CODEX_HOME"], str(outside))

    def test_daemon_env_alternate_claude_home_is_probed_as_an_alternate(self):
        home = agentcat.HOME / ".claude3"
        self._claude_journal(home, UUID_A)
        self.account(home, "alternate")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), \
             patch.object(agentcat.cli_probe, "probe_claude_home", return_value={}) as probe, \
             patch.object(agentcat.cli_probe, "probe_codex_home", return_value={"homeKey": "unknown", "status": "error"}), \
             patch.object(agentcat.shutil, "which", return_value=None), \
             patch.object(agentcat, "probe_antigravity", return_value=None):
            agentcat.run_cli_probes()
        self.assertEqual(probe.call_args.args[0], home)
        self.assertEqual(probe.call_args.args[1], agentcat.HOME / ".claude")


if __name__ == "__main__":
    unittest.main()
