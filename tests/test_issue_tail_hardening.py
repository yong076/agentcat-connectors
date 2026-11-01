"""Regression evidence for the remaining connector issue checklist tails."""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
import sqlite3
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from sandbox import redirect_module_paths, restore_module_paths
import test_release_hardening as release
import test_install as install_tests
from agentcat_backup_retention import compact_old_backups
from agentcat_jsonl_cursor import ProjectedJSONLCursor
from agentcat_usage_archive import lookup_record

app = release.app
install = install_tests.install


class IssueTailHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        original = redirect_module_paths(app, self.root, self.root / ".agentcat")
        self.addCleanup(restore_module_paths, app, original)

    def test_alias_collision_is_independent_of_feed_order_and_native_id_wins(self):
        cheap = {"input_cost_per_token": .000001, "output_cost_per_token": .000002}
        expensive = {"input_cost_per_token": .000003, "output_cost_per_token": .000004}
        items = [("z-provider/shared", expensive), ("a-provider/shared", cheap)]
        first = app.litellm_price_table(dict(items))
        second = app.litellm_price_table(dict(reversed(items)))
        self.assertEqual(first, second)
        self.assertEqual(first["shared"]["input"], 1)
        self.assertEqual(app.litellm_price_table(dict(items + [("shared", expensive)]))["shared"]["input"], 3)

    def test_copilot_byte_cursor_handles_tails_appends_rewrites_and_private_text(self):
        cursor = ProjectedJSONLCursor()
        path = self.root / "events.jsonl"
        first = {"type": "assistant.usage", "data": {"model": "m", "inputTokens": 10, "prompt": "private-prompt"}}
        second = {"type": "assistant.usage", "data": {"model": "m", "inputTokens": 20, "content": "private-transcript"}}
        path.write_text(json.dumps(first))  # valid, no newline yet
        project = Mock(wraps=app.copilot_usage_event_projection)
        self.assertEqual(len(cursor.read(path, project, 100000)), 1)
        with patch.object(Path, "open", side_effect=AssertionError("unchanged file reread")):
            self.assertEqual(len(cursor.read(path, project, 100000)), 1)
        with path.open('a') as handle:
            handle.write('\n' + json.dumps(second) + '\n')
        self.assertEqual(len(cursor.read(path, project, 100000)), 2)
        self.assertNotIn("private-prompt", repr(cursor.entries))
        self.assertNotIn("private-transcript", repr(cursor.entries))
        changed = {"type": "assistant.usage", "data": {"model": "new-model-longer", "inputTokens": 99}}
        path.write_text(json.dumps(changed) + '\n' + json.dumps(second) + '\n')
        rows = cursor.read(path, project, 100000)
        self.assertEqual(rows[0]['data']['inputTokens'], 99)
        self.assertEqual(len(rows), 2)

    def test_desktop_metadata_cache_rechecks_nested_changes_after_bounded_ttl(self):
        root = self.root / "desktop"; root.mkdir()
        nested = root / "nested"; nested.mkdir()
        (nested / "one.json").write_text('{}')
        with patch.object(app.time, "monotonic", return_value=100), patch.object(
            app, "_scan_desktop_app_data_root", wraps=app._scan_desktop_app_data_root
        ) as scan:
            first = app.scan_desktop_app_data_root(root)
            self.assertEqual(app.scan_desktop_app_data_root(root), first)
            self.assertEqual(scan.call_count, 1)
            (nested / "two.json").write_text('{}')
            with patch.object(app.time, "monotonic", return_value=401):
                refreshed = app.scan_desktop_app_data_root(root)
            self.assertEqual(refreshed['readableFiles'], first['readableFiles'] + 1)
            self.assertEqual(scan.call_count, 2)

    def test_gemini_format_drift_is_counted_without_preserving_bad_payloads(self):
        state = app.empty_gemini_usage_state()
        records = app.gemini_stream_records(state, {"scopeMetrics": [{"metrics": [{
            "descriptor": {"name": "gemini_cli.token.usage"}, "dataPoints": [
                {"attributes": {"type": "new_unknown_kind"}, "value": 12},
                {"attributes": {"type": "input"}, "value": "private-invalid"}]}]}]})
        self.assertEqual(records, [])
        result = app.gemini_usage_result_from_state(state)
        self.assertEqual(result["parseDrift"], {"invalidPoints": 2})
        self.assertNotIn("private-invalid", json.dumps(result))

    def test_cold_usage_metadata_preserves_dedup_and_generation_isolation(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = {"timestamp": (now - dt.timedelta(days=500)).isoformat(), "model": "m", "input": 10}
        recent = {"timestamp": now.isoformat(), "model": "m", "input": 20}
        cursor = {"usageRecordEpoch": "epoch-a", "usageRecords": {"request:old": old, "request:new": recent}}
        app.compact_journal_usage_records(cursor, now)
        archive = app.AGENTCAT_HOME / "journal-usage-archive.sqlite"
        self.assertEqual(cursor["usageRecords"], {"request:new": recent})
        self.assertEqual(lookup_record(archive, "epoch-a", "request:old"), old)
        self.assertIsNone(lookup_record(archive, "epoch-b", "request:old"))
        self.assertFalse(app.compact_journal_usage_records(cursor, now))

    def test_cold_archive_failure_does_not_prune_hot_dedup_metadata(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = {"timestamp": (now - dt.timedelta(days=500)).isoformat(), "input": 10}
        cursor = {"usageRecordEpoch": "epoch", "usageRecords": {"request:old": old}}
        with patch.object(app, "archive_usage_records", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                app.compact_journal_usage_records(cursor, now)
        self.assertEqual(cursor["usageRecords"], {"request:old": old})

    def test_archived_claude_record_dedups_and_corrects_after_cursor_restart(self):
        path = self.root / 'journal.jsonl'
        timestamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=500)).isoformat()
        def row(amount):
            return json.dumps({'type': 'assistant', 'timestamp': timestamp, 'requestId': 'synthetic-request',
                'message': {'model': 'claude-opus-5', 'usage': {'input_tokens': amount, 'output_tokens': 20}}}) + '\n'
        path.write_text(row(10))
        with patch.object(app, 'claude_journal_files_by_root', return_value=[(self.root, [path])]), \
             patch.object(app, '_usage_home_id', return_value='synthetic-home'), \
             patch.object(app, '_prepare_home_accounts', return_value=False):
            cursor = app.scan_claude_journal(app.load_journal_cursor())
            self.assertEqual(cursor['totals']['all_input'], 10)
            self.assertEqual(cursor['usageRecords'], {})
            self.assertTrue(app.save_journal_cursor(cursor))
            cursor = app.load_journal_cursor()
            with path.open('a') as handle:
                handle.write(row(10))
            cursor = app.scan_claude_journal(cursor)
            self.assertEqual(cursor['totals']['all_input'], 10)
            with path.open('a') as handle:
                handle.write(row(50))
            cursor = app.scan_claude_journal(cursor)
            self.assertEqual(cursor['totals']['all_input'], 50)
            self.assertEqual(cursor['totals']['all_output'], 20)

    @unittest.skipIf(os.name == 'nt', 'POSIX entrypoint; Windows uses uninstall.ps1')
    def test_posix_uninstall_uses_local_installer_without_replacing_source(self):
        source = self.root / "installed source"; (source / "scripts").mkdir(parents=True)
        evidence = self.root / "args.json"
        (source / "scripts" / "install.py").write_text(
            "import json,sys\nfrom pathlib import Path\n" +
            "Path(" + repr(str(evidence)) + ").write_text(json.dumps(sys.argv[1:]))\n")
        script = self.root / "uninstall.sh"
        script.write_bytes((Path(__file__).resolve().parents[1] / "uninstall.sh").read_bytes())
        result = subprocess.run(["bash", str(script)], env=dict(os.environ, HOME=str(self.root),
            AGENTCAT_CONNECTORS_DIR=str(source)), capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(evidence.read_text()), ["--repo-dir", str(source), "uninstall"])
        self.assertTrue((source / "scripts" / "install.py").exists())

    def test_opencode_reads_only_changed_rows_and_recognizes_deletes_and_session_changes(self):
        database = self.root / "opencode.db"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        writer.execute("create table session(id text, directory text, parent_id text, time_archived int)")
        writer.execute("insert into session values ('s', '/synthetic', null, null)")
        writer.execute("create table message(id text primary key, session_id text, time_created int, time_updated int, data text)")
        def insert(identifier, amount, updated):
            writer.execute("insert or replace into message values (?, 's', 1700000000000, ?, ?)",
                (identifier, updated, json.dumps({"role": "assistant", "modelID": "test-model", "tokens": {"input": amount}})))
            writer.commit()
        insert('a', 100, 1)
        with patch.object(app, "opencode_data_dir", return_value=self.root), patch.object(
            app, "opencode_database_rows", wraps=app.opencode_database_rows
        ) as rows:
            self.assertEqual(app.opencode_snapshot()["tokens"]["all"], 100)
            self.assertEqual(app.opencode_snapshot()["tokens"]["all"], 100)
            self.assertEqual(rows.call_count, 1)
            insert('a', 150, 2)
            self.assertEqual(app.opencode_snapshot()["tokens"]["all"], 150)
            self.assertEqual(rows.call_args.args[1], {"message": ['a']})
            insert('b', 50, 1)  # backfill below the global high-water mark
            self.assertEqual(app.opencode_snapshot()["tokens"]["all"], 200)
            writer.execute("delete from message where id='a'"); writer.commit()
            self.assertEqual(app.opencode_snapshot()["tokens"]["all"], 50)
            writer.execute("update session set time_archived=1"); writer.commit()
            self.assertEqual(app.opencode_snapshot()["tokens"]["all"], 0)

    def test_old_backups_have_verified_recovery_before_source_removal(self):
        root = self.root / "backups"; root.mkdir()
        for day in range(1, 6):
            folder = root / f"2026010{day}-000000"; folder.mkdir()
            (folder / "settings.json").write_text(json.dumps({"synthetic": day}))
        protected = root / "legacy-dirty-20250101-000000"; protected.mkdir()
        (protected / "owner-wip").write_text("preserve")
        archived = compact_old_backups(root, r"\d{8}-\d{6}",
            now=dt.datetime(2026, 11, 1).timestamp())
        self.assertEqual(len(archived), 2)
        self.assertTrue(protected.exists())
        self.assertTrue((root / "20260105-000000").exists())
        self.assertEqual(compact_old_backups(root, r"\d{8}-\d{6}", now=dt.datetime(2026, 11, 1).timestamp()), [])

    def test_failed_archive_verification_retains_original_backup(self):
        root = self.root / "backups"; root.mkdir()
        for day in range(1, 5):
            folder = root / f"2026010{day}-000000"; folder.mkdir()
            (folder / "data").write_text("recovery")
        with patch("agentcat_backup_retention._digest", side_effect=['a', 'b']):
            with self.assertRaisesRegex(OSError, "verification"):
                compact_old_backups(root, r"\d{8}-\d{6}", now=dt.datetime(2026, 11, 1).timestamp())
        self.assertTrue((root / "20260101-000000" / "data").exists())
        self.assertEqual(list((root / "archive").glob("*.tar.gz")), [])

    def test_doctor_fix_is_explicit_local_repair_and_does_not_echo_child_output(self):
        args = argparse.Namespace(fix=True, network=False, json=True, bundle=None)
        with patch.object(app.subprocess, "run", return_value=Mock(returncode=1, stdout="private", stderr="secret")) as run, \
             patch.object(app, "doctor_report", return_value={"status": "ok", "checks": []}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.command_doctor(args), 1)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "repair")
        self.assertNotIn("private", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        self.assertIn("repair_failed", output.getvalue())

    def test_doctor_system_checks_use_only_bounded_metadata(self):
        app.write_json_atomic(app.LATEST_SNAPSHOT, {"connectorVersion": "1.2.3", "secret": "never-copy"})
        with patch.object(app.sys, "platform", "darwin"), patch.object(app.os, "getuid", return_value=501, create=True), patch.object(app.subprocess, "run",
            side_effect=[Mock(returncode=0, stdout="private launchctl details"), Mock(returncode=0, stdout="p123\n")]), \
            patch.object(app, "desktop_app_data_roots", return_value=[]):
            checks = app.doctor_system_checks(8765, False)
        reasons = {row.get("reason") for row in checks}
        self.assertIn("daemon_version_mismatch", reasons)
        self.assertIn("port_in_use", reasons)
        self.assertNotIn("private", json.dumps(checks))
        self.assertNotIn("never-copy", json.dumps(checks))

    def test_windows_doctor_system_checks_use_native_task_evidence(self):
        with patch.object(app.sys, "platform", "win32"), patch.object(app, "IS_WINDOWS", True), \
            patch.object(app.subprocess, "run", return_value=Mock(returncode=0, stdout="private task details")) as run, \
            patch.object(app, "desktop_app_data_roots", return_value=[]):
            checks = app.doctor_system_checks(8765, True)
        self.assertEqual(run.call_args.args[0], ['schtasks.exe', '/Query', '/TN', 'AgentCatD', '/FO', 'LIST'])
        self.assertEqual(next(row for row in checks if row['id'] == 'daemon.registration')['status'], 'ok')
        self.assertNotIn('private task details', json.dumps(checks))


if __name__ == "__main__":
    unittest.main()
