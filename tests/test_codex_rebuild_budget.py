"""Codex polling uses a persisted daily rebuild budget and an in-memory cursor."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_home_discovery import HomeDiscoveryTestCase, UUID_A, UUID_B, agentcat


class CodexRebuildBudgetTests(HomeDiscoveryTestCase):
    def setUp(self):
        super().setUp()
        self.home = agentcat.HOME / '.codex'
        self.path = self._codex_session(self.home, UUID_A)
        self.clock = 1_900_000_000.0
        self.timer = patch.object(agentcat.time, 'time', side_effect=lambda: self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def append(self, amount=100):
        event = json.loads(self.path.read_text().splitlines()[-1])
        usage = event['payload']['info']['total_token_usage']
        usage['input_tokens'] += amount
        usage['output_tokens'] += amount
        with self.path.open('a') as handle:
            handle.write(json.dumps(event) + '\n')

    def test_active_gap_rebuilds_once_per_day_and_survives_restart(self):
        sqlite = {'status': 'ok', 'tokens': {'all': 50_000_000}}
        with patch.object(agentcat, 'codex_sqlite_snapshot', return_value=sqlite), \
             patch.object(agentcat, 'codexbar_cost_cache_snapshot', return_value={}), \
             patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            first = agentcat.codex_snapshot()
            initial = rebuild.call_count
            for i in range(10):
                self.clock += 60
                self.append()
                sqlite['tokens']['all'] += 1000
                snap = agentcat.codex_snapshot()
                self.assertTrue(snap['pendingReconcile'])
                self.assertEqual(sum(snap['dailyTokens'].values()), 200 * (i + 2))
            self.assertEqual(rebuild.call_count, initial)
            agentcat.save_codex_sessions_cursor(agentcat.load_codex_sessions_cursor())
            agentcat._CODEX_CURSOR_CACHE = None
            agentcat.codex_snapshot()
            # Loading/coercing the cursor creates one empty dictionary, not a scan.
            after_restart = rebuild.call_count
            self.clock += 86400
            agentcat.codex_snapshot()
            self.assertEqual(rebuild.call_count, after_restart + 1)
            self.assertTrue(first['pendingReconcile'])

    def test_idle_tick_never_opens_cursor_and_external_change_invalidates(self):
        agentcat.codex_sessions_snapshot()
        original = Path.open
        opened = []
        def spy(path, *args, **kwargs):
            if path == agentcat.CODEX_SESSIONS_CURSOR_FILE:
                opened.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', spy), \
             patch.object(agentcat, '_read_codex_sessions_cursor', wraps=agentcat._read_codex_sessions_cursor) as parse:
            agentcat.codex_sessions_snapshot()
            self.assertEqual(opened, [])
            parse.assert_not_called()
            raw = dict(agentcat.load_codex_sessions_cursor())
            raw['externalChange'] = True
            agentcat.CODEX_SESSIONS_CURSOR_FILE.write_text(json.dumps(raw))
            agentcat.codex_sessions_snapshot()
            self.assertEqual(parse.call_count, 1)

    def test_archive_moves_and_cap_churn_keep_offsets_until_next_window(self):
        agentcat.codex_sessions_snapshot()
        archived = self.home / 'archived_sessions' / self.path.name
        archived.parent.mkdir()
        self.path.rename(archived)
        self.path = archived
        for i in range(3):
            self.append()
            snap = agentcat.codex_sessions_snapshot()
            self.assertEqual(snap['tokens']['all'], 400 + 200 * i)
            self.assertNotIn('pendingReconcile', snap)
        other = self._codex_session(self.home, UUID_B)
        with patch.object(agentcat, 'codex_session_files', return_value=[other]):
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 1000)
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 1000)
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            for _ in range(5):
                agentcat.codex_sessions_snapshot()
            rebuild.assert_not_called()
            self.clock += 86399
            agentcat.codex_sessions_snapshot()
            rebuild.assert_not_called()
            self.clock += 1
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 1000)
            self.assertEqual(rebuild.call_count, 1)

    def test_identity_moves_survive_restart_without_rebuild(self):
        for kind in ('filename', 'record', 'inode'):
            with self.subTest(kind=kind):
                if kind != 'filename':
                    renamed = self.path.with_name(kind + '.jsonl')
                    self.path.rename(renamed)
                    self.path = renamed
                    if kind == 'record':
                        self.path.write_text(json.dumps({'type': 'session_meta',
                            'payload': {'id': UUID_A}}) + '\n' + self.path.read_text())
                    else:
                        self.path.write_text('\n'.join(self.path.read_text().splitlines()[1:]) + '\n')
                before = agentcat.codex_sessions_snapshot(force_rebuild=True)
                agentcat.save_codex_sessions_cursor(agentcat.load_codex_sessions_cursor())
                moved = self.path.with_name('moved-' + self.path.name)
                self.path.rename(moved)
                self.path = moved
                agentcat._CODEX_CURSOR_CACHE = None
                agentcat.load_codex_sessions_cursor()
                with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
                    self.append()
                    snap = agentcat.codex_sessions_snapshot()
                    self.assertEqual(snap['tokens']['all'], before['tokens']['all'] + 200)
                    self.assertNotIn('pendingReconcile', snap)
                    rebuild.assert_not_called()

    def test_identity_cache_resolves_only_new_changed_and_moved_files(self):
        with patch.object(agentcat, '_codex_file_identity', wraps=agentcat._codex_file_identity) as resolve:
            agentcat.codex_sessions_snapshot()
            self.assertEqual(resolve.call_count, 1)
            resolve.reset_mock()
            for _ in range(3):
                agentcat.codex_sessions_snapshot()
            resolve.assert_not_called()
            self.append()
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
            self.assertEqual(resolve.call_count, 1)
            resolve.reset_mock()
            moved = self.path.with_name('moved-' + self.path.name)
            self.path.rename(moved)
            self.path = moved
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
            self.assertEqual(resolve.call_count, 1)
            resolve.reset_mock()
            self._codex_session(self.home, UUID_B)
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 600)
            self.assertEqual(resolve.call_count, 1)
            resolve.reset_mock()
            agentcat.codex_sessions_snapshot(force_rebuild=True)
            resolve.assert_not_called()
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 600)

    def test_identity_cache_invalidates_on_nanosecond_mtime_change(self):
        agentcat.codex_sessions_snapshot()
        stat = self.path.stat()
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        with patch.object(agentcat, '_codex_file_identity', wraps=agentcat._codex_file_identity) as resolve:
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 200)
            resolve.assert_called_once()
            resolve.reset_mock()
            agentcat.codex_sessions_snapshot()
            resolve.assert_not_called()

    def test_unnamed_hardlinks_count_once(self):
        unnamed = self.path.with_name('unnamed.jsonl')
        self.path.rename(unnamed)
        alias = unnamed.with_name('alias.jsonl')
        os.link(unnamed, alias)
        with patch.object(agentcat, 'codex_session_files', return_value=[unnamed, alias]):
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 200)
            self.assertEqual(len(agentcat.load_codex_sessions_cursor()['files']), 1)

    def test_duplicate_copies_and_new_session(self):
        copy = self.path.with_name('copy-' + self.path.name)
        copy.write_bytes(self.path.read_bytes())
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 200)
        self.append()
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
        # A shorter mirror appearing alone must not rewind the cursor.
        with patch.object(agentcat, 'codex_session_files', return_value=[copy]):
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
        self._codex_session(self.home, UUID_B)
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 600)

    def test_path_cursor_migration_preserves_rollups_and_offsets(self):
        for unnamed in (False, True):
            with self.subTest(unnamed=unnamed):
                if unnamed:
                    moved = self.path.with_name('unnamed.jsonl')
                    self.path.rename(moved)
                    self.path = moved
                before = agentcat.codex_sessions_snapshot(force_rebuild=True)
                raw = json.loads(agentcat.CODEX_SESSIONS_CURSOR_FILE.read_text())
                meta = next(iter(raw['files'].values()))
                meta.pop('inode', None)
                raw.pop('fileIdentityVersion')
                raw['files'] = {str(self.path): meta}
                raw['usagePaths'] = {str(self.path): next(iter(raw['usagePaths'].values()))}
                agentcat.CODEX_SESSIONS_CURSOR_FILE.write_text(json.dumps(raw))
                if not unnamed:
                    moved = self.path.with_name('archived-' + self.path.name)
                    self.path.rename(moved)
                    self.path = moved
                agentcat._CODEX_CURSOR_CACHE = None
                migrated = agentcat.load_codex_sessions_cursor()
                self.assertEqual(migrated['daily'], raw['daily'])
                self.assertEqual(next(iter(migrated['files'].values()))['offset'], meta['offset'])
                self.assertTrue(next(iter(migrated['files'])).startswith('inode:' if unnamed else 'session:'))
                with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
                    self.append()
                    snap = agentcat.codex_sessions_snapshot()
                    self.assertEqual(snap['tokens']['all'], before['tokens']['all'] + 200)
                    self.assertNotIn('pendingReconcile', snap)
                    rebuild.assert_not_called()

    def test_force_bypasses_window_and_append_is_not_double_counted(self):
        agentcat.codex_sessions_snapshot()
        self.append()
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            self.assertEqual(agentcat.codex_sessions_snapshot(force_rebuild=True)['tokens']['all'], 400)
            rebuild.assert_called_once()

    def test_discovered_home_add_remove_rebuilds_once_then_returns_to_budget(self):
        agentcat.codex_sessions_snapshot()
        other = agentcat.HOME / '.codex-2'
        self._codex_session(other, UUID_B)
        self._reset_discovery_cache()
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
            self.assertEqual(rebuild.call_count, 1)
            for _ in range(3):
                agentcat.codex_sessions_snapshot(reconcile=True)
            self.assertEqual(rebuild.call_count, 1)
            other.rename(self.root / 'retired-fixture')
            self._reset_discovery_cache()
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 200)
            self.assertEqual(rebuild.call_count, 2)
            for _ in range(3):
                agentcat.codex_sessions_snapshot(reconcile=True)
            self.assertEqual(rebuild.call_count, 2)

    def test_codex_settings_change_rebuilds_even_with_same_home_membership(self):
        agentcat.codex_sessions_snapshot()
        # External settings edits must work without write_agentcat_settings's
        # in-process invalidation. Adopting the default keeps membership equal.
        settings = {'homes': {'codex': {'adopted': [str(self.home)]}}}
        (agentcat.AGENTCAT_HOME / 'settings.json').write_text(json.dumps(settings))
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 200)
            self.assertEqual(rebuild.call_count, 1)
            agentcat.codex_sessions_snapshot(reconcile=True)
            self.assertEqual(rebuild.call_count, 1)
        agentcat._CODEX_CURSOR_CACHE = None
        # Loading the persisted configuration does not trigger another scan.
        agentcat.load_codex_sessions_cursor()
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            agentcat.codex_sessions_snapshot(reconcile=True)
            rebuild.assert_not_called()

    def test_unrelated_settings_and_equivalent_rewrite_do_not_rebuild(self):
        settings = {'homes': {'codex': {'adopted': [str(self.home)]}}}
        agentcat.write_agentcat_settings(settings)
        agentcat.codex_sessions_snapshot()
        settings.update(theme='dark')
        settings['homes']['claude'] = {'excluded': [str(agentcat.HOME / '.claude')]}
        agentcat.write_agentcat_settings(settings)
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            agentcat.codex_sessions_snapshot(reconcile=True)
            # An equivalent list with duplicates/ordering changes is not a new choice.
            settings['homes']['codex']['adopted'].append(str(self.home))
            agentcat.write_agentcat_settings(settings)
            agentcat.codex_sessions_snapshot(reconcile=True)
            rebuild.assert_not_called()

    def test_exclude_all_then_include_clears_old_usage_and_rebuilds_once(self):
        agentcat.codex_sessions_snapshot()
        agentcat.write_agentcat_settings({'homes': {'codex': {'excluded': [str(self.home)]}}})
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            self.assertEqual(agentcat.codex_sessions_snapshot()['status'], 'not_found')
            self.assertEqual(agentcat.load_codex_sessions_cursor()['daily'], {})
            self.assertEqual(agentcat._home_usage_periods('codex'), {})
            agentcat.codex_sessions_snapshot()
            self.assertEqual(rebuild.call_count, 1)
            agentcat.write_agentcat_settings({'homes': {'codex': {'excluded': []}}})
            self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 200)
            self.assertEqual(rebuild.call_count, 2)
            agentcat.codex_sessions_snapshot(reconcile=True)
            self.assertEqual(rebuild.call_count, 2)

    def test_dirty_tail_is_saved_on_later_idle_tick(self):
        # Use the real wall clock for the mtime-based write debounce.
        self.timer.stop()
        agentcat.codex_sessions_snapshot()
        self.append()
        with patch.object(agentcat, 'cursor_recently_saved', return_value=True):
            agentcat.codex_sessions_snapshot()
        with patch.object(agentcat, 'cursor_recently_saved', return_value=False):
            agentcat.codex_sessions_snapshot()
        agentcat._CODEX_CURSOR_CACHE = None
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
