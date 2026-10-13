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
        expected = app.opencode_snapshot()
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
