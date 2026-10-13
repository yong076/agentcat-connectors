"""Hand-written SQLite shapes for both OpenCode message generations."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from sandbox import redirect_module_paths, restore_module_paths

LOADER = SourceFileLoader("opencode_readers", str(Path(__file__).resolve().parents[1] / "bin/agentcat"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
app = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(app)


class OpenCodeReadersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        originals = redirect_module_paths(app, self.root, self.root / '.agentcat')
        self.addCleanup(restore_module_paths, app, originals)
        patcher = patch.object(app, 'opencode_data_dir', return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def database(self, name='opencode.db', sessions=('session', 'session_v2'), with_id=True):
        conn = sqlite3.connect(self.root / name)
        self.addCleanup(conn.close)
        for table in sessions:
            conn.execute(f'create table {table}(id text, directory text, path text, parent_id text, time_archived int, model text)')
            conn.execute(f'insert into {table} values (?, ?, ?, null, null, ?)',
                         ('s', str(self.root / 'project'), 'relative', '{"id":"compact-model"}'))
        conn.execute('create table message(' + ('id text, ' if with_id else '') + 'session_id text, time_created int, data text)')
        conn.execute('create table session_message(id text, session_id text, time_created int, type text, data text)')
        return conn

    def message(self, conn, message_id, v2=False, amount=10, kind='assistant', model=True, with_id=True):
        data = {'tokens': {'input': amount, 'output': 3, 'reasoning': 2, 'cache': {'read': 4, 'write': 1}}}
        if v2:
            if model:
                data['model'] = {'id': 'test-model'}
            data['content'] = [{'type': 'tool', 'name': 'bash'}, {'type': 'text', 'text': 'ignored'}]
            conn.execute('insert into session_message values (?, ?, ?, ?, ?)',
                         (message_id, 's', 1700000000000, kind, json.dumps(data)))
        else:
            data.update(role=kind, modelID='test-model', toolRequests=[{'toolName': 'bash'}])
            values = ([message_id] if with_id else []) + ['s', 1700000000000, json.dumps(data)]
            conn.execute('insert into message values (' + ','.join('?' for _ in values) + ')', values)
        conn.commit()

    def test_upgraded_database_exact_totals_and_directory(self):
        conn = self.database()
        self.message(conn, 'copy', amount=900)
        self.message(conn, 'copy', v2=True)  # 20; v2 wins
        self.message(conn, 'new', v2=True, amount=20)  # 30
        self.message(conn, 'summary', amount=5)  # 15; v1-only summary
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 65)
        self.assertEqual(snapshot['events'], 3)
        self.assertEqual(snapshot['tokens']['inputTokens'], 35)
        self.assertEqual(snapshot['projects']['items'][0]['tokens'], 65)
        self.assertEqual(snapshot['breakdown']['chat'], 3)
        self.assertEqual(snapshot['breakdown']['tools'], 3)
        self.assertEqual(snapshot['breakdown']['shell'], 3)
        with patch.object(app, 'project_snapshot_identity', wraps=app.project_snapshot_identity) as identity:
            app.opencode_snapshot()
        identity.assert_called_once_with(str(self.root / 'project'))

    def test_renamed_lineage_and_compaction_session_model(self):
        conn = self.database(sessions=('session_v2',))
        self.message(conn, 'copy')
        self.message(conn, 'copy', v2=True)
        self.message(conn, 'compaction', v2=True, kind='compaction', model=False, amount=2)
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 32)
        self.assertEqual(snapshot['events'], 2)
        self.assertEqual(snapshot['models']['compact-model']['inputTokens'], 2)

    def test_preview_import_and_v1_copies_across_databases(self):
        for name in ('opencode-next.db', 'opencode.db'):
            conn = self.database(name, sessions=('session',))
            self.message(conn, 'shared')
            self.message(conn, 'shared', v2=True)
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 20)
        self.assertEqual(app.opencode_snapshot()['events'], 1)

    def test_filtered_v2_claims_id(self):
        for column, value in (('time_archived', 1), ('parent_id', 'parent')):
            with self.subTest(column=column):
                conn = self.database(name='opencode-' + column + '.db')
                self.message(conn, column)
                self.message(conn, column, v2=True)
                conn.execute(f'update session_v2 set {column} = ?', (value,))
                conn.commit()
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 0)
        self.assertEqual(app.opencode_snapshot()['events'], 0)

    def test_v1_without_id_is_not_deduplicated(self):
        conn = self.database(with_id=False)
        self.message(conn, None, with_id=False)
        self.message(conn, None, with_id=False)
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 40)
        self.assertEqual(app.opencode_snapshot()['events'], 2)

    def test_no_message_tables_is_error(self):
        conn = sqlite3.connect(self.root / 'opencode.db')
        conn.close()
        self.assertEqual(app.opencode_snapshot()['status'], 'error')

    def test_json_function_fallback_matches_projection(self):
        conn = self.database()
        self.message(conn, 'old')
        self.message(conn, 'new', v2=True)
        self.message(conn, 'compact', v2=True, kind='compaction', model=False)
        self.message(conn, 'shared-user', kind='user')
        self.message(conn, 'shared-user', v2=True, kind='user')
        conn.execute("update session_message set data = ? where id = 'new'",
                     (json.dumps({'model': {'id': 'test-model'}, 'tokens': {'input': 10, 'output': 10},
                                  'content': {'a': {'type': 'tool', 'name': 'bash'}}}),))
        conn.commit()
        expected = app.opencode_snapshot()
        self.assertEqual(expected['breakdown']['tools'], 2)
        real_reader = app.read_foreign_sqlite

        class WithoutJSON:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, query):
                if 'json_extract' in query:
                    raise sqlite3.OperationalError('no such function: json_extract')
                return self.connection.execute(query)

        def reader(path, callback):
            return real_reader(path, lambda connection: callback(WithoutJSON(connection)))

        with patch.object(app, 'read_foreign_sqlite', side_effect=reader):
            actual = app.opencode_snapshot()
        self.assertEqual(actual, expected)
        self.assertEqual(actual['tokens']['all'], 60)
        self.assertEqual(actual['events'], 3)

    def test_primary_database_wins_and_exclusions_precede_all_copies(self):
        primary = self.database()
        preview = self.database('opencode-next.db')
        other = self.database('opencode-z.db')
        self.message(primary, 'shared', v2=True, amount=90)  # 100, not stale 20
        self.message(preview, 'shared', v2=True)
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 100)
        primary.execute('update session_v2 set time_archived = 1')
        primary.commit()
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 0)
        # Excluded copy encountered last must also suppress an earlier live copy.
        self.message(preview, 'late-exclusion', v2=True)
        self.message(other, 'late-exclusion', v2=True)
        other.execute("update session_v2 set parent_id = 'parent'")
        other.commit()
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 0)

    def test_old_only_rows_follow_live_session_or_fall_back(self):
        conn = self.database()
        self.message(conn, 'old-only')
        for column, value in [('time_archived', 1), ('parent_id', 'parent')]:
            conn.execute(f'update session_v2 set {column} = ?', (value,))
            conn.commit()
            self.assertEqual(app.opencode_snapshot()['tokens']['all'], 0)
            conn.execute(f'update session_v2 set {column} = null')
        conn.execute('delete from session_v2')
        conn.commit()
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 20)

    def test_only_nonempty_string_ids_deduplicate(self):
        for name in ['opencode.db', 'opencode-next.db']:
            conn = self.database(name)
            # Untyped columns retain integer IDs instead of coercing to strings.
            conn.execute('drop table message')
            conn.execute('create table message(id, session_id text, time_created int, data text)')
            for message_id in [None, None, '', '', 1, 1]:
                self.message(conn, message_id)
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 240)  # 12 * 20
        self.assertEqual(app.opencode_snapshot()['events'], 12)

    def test_each_generation_survives_the_other_schema_failure(self):
        for broken, valid_v2 in [('session_message', False), ('message', True)]:
            with self.subTest(broken=broken):
                conn = self.database('opencode-' + broken + '.db')
                self.message(conn, broken, v2=valid_v2)
                conn.execute(f'drop table {broken}')
                conn.execute(f'create table {broken}(id text, data text)')
                conn.commit()
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 40)
        self.assertEqual(snapshot['events'], 2)
        self.assertTrue(any('session_message: skipped' in warning for warning in snapshot['warnings']))
        self.assertTrue(any('message: skipped' in warning for warning in snapshot['warnings']))

    def test_generation_query_errors_retry_private_copy_only_when_retryable(self):
        conn = self.database()
        self.message(conn, 'shared', amount=100)
        self.message(conn, 'shared', v2=True, amount=100)
        for index in range(4):
            self.message(conn, 'new-' + str(index), v2=True, amount=45)
        self.message(conn, 'summary', amount=0)
        real_reader = app.read_foreign_sqlite
        for error, expected, retries in [('database is locked', 340, 2),
                                         ('disk I/O error', 340, 2),
                                         ('no such column: m.type', 120, 1)]:
            with self.subTest(error=error):
                paths = []
                failed = []

                class FailingQuery:
                    def __init__(self, connection):
                        self.connection = connection

                    def execute(self, query):
                        if 'from session_message m' in query and not failed:
                            failed.append(True)
                            raise sqlite3.OperationalError(error)
                        return self.connection.execute(query)

                def reader(path, callback):
                    def read(connection):
                        paths.append(connection.execute('pragma database_list').fetchone()[2])
                        return callback(FailingQuery(connection))
                    return real_reader(path, read)

                with patch.object(app, 'read_foreign_sqlite', side_effect=reader):
                    snapshot = app.opencode_snapshot()
                self.assertEqual(snapshot['tokens']['all'], expected)
                self.assertEqual(len(paths), retries)
                if retries == 2:
                    self.assertNotEqual(paths[0], paths[1])
                    self.assertFalse(snapshot.get('warnings'))
                else:
                    self.assertTrue(any('session_message: skipped' in warning
                                        for warning in snapshot['warnings']))

    def test_renamed_lineage_different_ids_and_unmigrated_session(self):
        conn = self.database(sessions=('session_v2',))
        self.message(conn, 'old-id', amount=90)
        self.message(conn, 'new-id', v2=True)  # 20; old migrated session is excluded
        self.message(conn, 'unmigrated', amount=5)  # 15; session has no new rows
        conn.execute("insert into session_v2 select 'unmigrated', directory, path, null, null, model from session_v2")
        conn.execute("update message set session_id = 'unmigrated' where id = 'unmigrated'")
        conn.commit()
        self.assertEqual(app.opencode_snapshot()['tokens']['all'], 35)
        self.assertEqual(app.opencode_snapshot()['events'], 2)

    def test_relative_project_path_is_not_a_project(self):
        conn = self.database(sessions=('session',))
        self.message(conn, 'relative')
        conn.execute('alter table session rename to old_session')
        conn.execute('create table session(id text, path text)')
        conn.execute("insert into session values ('s', '../../relative')")
        conn.commit()
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 20)
        self.assertEqual(snapshot['projects']['status'], 'not_available')

    def test_renamed_lineage_preserved_ids_keep_old_only_summary(self):
        conn = self.database(sessions=('session_v2',))
        self.message(conn, 'shared', amount=90)
        self.message(conn, 'shared', v2=True)  # 20, replaces old copy
        self.message(conn, 'summary', amount=5)  # 15, preserved old-only row
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 35)
        self.assertEqual(snapshot['events'], 2)

    def test_mixed_session_lineages_without_session_v2(self):
        conn = self.database(sessions=('session',))
        self.message(conn, 'shared', amount=90)
        self.message(conn, 'shared', v2=True)  # preserved session: 20 + 15
        self.message(conn, 'summary', amount=5)
        self.message(conn, 'different-old', amount=90)
        self.message(conn, 'different-new', v2=True, amount=30)  # 40 once
        self.message(conn, 'old-only', amount=2)  # 12, no new rows
        for session in ('different', 'old-only'):
            conn.execute('insert into session select ?, directory, path, null, null, model from session where id = ?',
                         (session, 's'))
        conn.execute("update message set session_id = 'different' where id = 'different-old'")
        conn.execute("update session_message set session_id = 'different' where id = 'different-new'")
        conn.execute("update message set session_id = 'old-only' where id = 'old-only'")
        conn.commit()
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 87)  # 20 + 15 + 40 + 12
        self.assertEqual(snapshot['events'], 4)

    def test_shared_user_id_preserves_old_only_assistant_usage(self):
        conn = self.database(sessions=('session_v2',))
        self.message(conn, 'shared-user', kind='user')
        self.message(conn, 'shared-user', v2=True, kind='user')
        self.message(conn, 'old-assistant', amount=5)  # 15
        self.message(conn, 'new-assistant', v2=True, amount=20)  # 30
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 45)
        self.assertEqual(snapshot['events'], 2)

    def test_session_migration_decision_does_not_cross_databases(self):
        primary = self.database()
        preview = self.database('opencode-next.db')
        self.message(primary, 'new', v2=True)  # 20
        self.message(preview, 'old', amount=5)  # 15, this DB has no new rows
        snapshot = app.opencode_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 35)
        self.assertEqual(snapshot['events'], 2)
