"""Hand-computed historical baselines from the pre-reader-change state format."""
from contextlib import closing
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_copilot_readers import app, event, shutdown, usage
from sandbox import redirect_module_paths, restore_module_paths


class ReaderAccountingMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        originals = redirect_module_paths(app, self.root, self.root / '.agentcat')
        self.addCleanup(restore_module_paths, app, originals)
        app.AGENTCAT_HOME.mkdir(parents=True, exist_ok=True)
        self.now = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)
        self.day = app.day_key_for_timestamp(self.now)

    def old_state(self, provider, identity, total):
        # Main's writer had only the Grok accounting version. The historical
        # reader totals below are fixed expectations, not new-reader outputs.
        key = provider + '|' + identity['id']
        state = {'version': app.PROJECT_DAILY_STATE_VERSION,
                 'grokAccountingVersion': app.GROK_ACCOUNTING_STATE_VERSION,
                 'baselines': {key: total, 'claude|unrelated': 700},
                 'modelBaselines': {key: {'test-model': {'tokens': total, 'classes': None}},
                                    'claude|unrelated': {'test-model': {'tokens': 700, 'classes': None}}}}
        app.project_daily_state_file().write_text(json.dumps(state))
        loaded = app.load_project_daily_state()
        self.assertNotIn(key, loaded['baselines'])
        self.assertNotIn(key, loaded['modelBaselines'])
        self.assertEqual(loaded['baselines']['claude|unrelated'], 700)
        self.assertEqual(loaded['modelBaselines']['claude|unrelated']['test-model']['tokens'], 700)
        return key

    def assert_saved(self, provider, key, total):
        state = json.loads(app.project_daily_state_file().read_text())
        self.assertEqual(state[provider + 'AccountingVersion'], 1)
        self.assertEqual(state['baselines'][key], total)
        self.assertNotIn(key, state['modelBaselines'])

    def test_finished_copilot_history_reseeds_then_books_only_real_growth(self):
        folder = app.HOME / '.copilot' / 'session-state' / 'fixture-session'
        folder.mkdir(parents=True)
        path = folder / 'events.jsonl'
        records = [event('session.model_change', newModel='test-model')]
        records += [event('assistant.message', outputTokens=1000) for _ in range(5)]
        records += [shutdown(**{'test-model': usage(2000000, 5000, 1800000, 0)})]
        path.write_text('\n'.join(json.dumps(row) for row in records))
        key = self.old_state('copilot', app.project_snapshot_identity('fixture-session'), 5000)
        snapshot = app.copilot_snapshot()
        self.assertEqual(snapshot['tokens']['all'], 2005000)
        self.assertEqual(app.update_project_daily({'copilot': snapshot}, now=self.now), {})
        self.assert_saved('copilot', key, 2005000)
        records.append(event('assistant.usage', model='test-model', apiCallId='new', **usage(40, 10, 0, 0)))
        path.write_text('\n'.join(json.dumps(row) for row in records))
        self.assertEqual(app.update_project_daily({'copilot': app.copilot_snapshot()}, now=self.now),
                         {self.day: {'fixture-session': 50}})

    def test_opencode_v1_history_reseeds_new_generation_then_books_growth(self):
        database = self.root / 'opencode.db'
        project = str(self.root / 'fixture-project')
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute('create table session(id text, directory text, path text, parent_id text, time_archived int)')
            conn.execute('insert into session values (?, ?, ?, null, null)', ('s', project, project))
            conn.execute('create table message(id text, session_id text, time_created int, data text)')
            conn.execute('insert into message values (?, ?, ?, ?)',
                         ('old', 's', 1700000000000, json.dumps({'role': 'assistant', 'modelID': 'test-model',
                                                               'tokens': {'input': 100, 'output': 10}})))
            conn.execute('create table session_message(id text, session_id text, time_created int, type text, data text)')
            conn.execute('insert into session_message values (?, ?, ?, ?, ?)',
                         ('historical-new', 's', 1700000000000, 'assistant',
                          json.dumps({'model': {'id': 'test-model'}, 'tokens': {'input': 200, 'output': 20}})))
        key = self.old_state('opencode', app.project_snapshot_identity(project), 110)
        with patch.object(app, 'opencode_data_dir', return_value=self.root):
            snapshot = app.opencode_snapshot()
            self.assertEqual(snapshot['tokens']['all'], 330)
            self.assertEqual(app.update_project_daily({'opencode': snapshot}, now=self.now), {})
            self.assert_saved('opencode', key, 330)
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute('insert into session_message values (?, ?, ?, ?, ?)',
                             ('real-new', 's', 1800000000000, 'assistant',
                              json.dumps({'model': {'id': 'test-model'}, 'tokens': {'input': 40, 'output': 4}})))
            self.assertEqual(app.update_project_daily({'opencode': app.opencode_snapshot()}, now=self.now),
                             {self.day: {'fixture-project': 44}})
