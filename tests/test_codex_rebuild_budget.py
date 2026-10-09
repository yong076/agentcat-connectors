"""Codex polling uses a persisted daily rebuild budget and an in-memory cursor."""
import json
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
            self.assertTrue(snap['pendingReconcile'])
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

    def test_force_bypasses_window_and_append_is_not_double_counted(self):
        agentcat.codex_sessions_snapshot()
        self.append()
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
        self.assertEqual(agentcat.codex_sessions_snapshot()['tokens']['all'], 400)
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            self.assertEqual(agentcat.codex_sessions_snapshot(force_rebuild=True)['tokens']['all'], 400)
            rebuild.assert_called_once()

    def test_new_home_does_not_reset_existing_home_budget(self):
        agentcat.codex_sessions_snapshot()
        self._codex_session(agentcat.HOME / '.codex-2', UUID_B)
        self._reset_discovery_cache()
        with patch.object(agentcat, '_empty_codex_cursor', wraps=agentcat._empty_codex_cursor) as rebuild:
            self.assertEqual(agentcat.codex_sessions_snapshot(reconcile=True)['tokens']['all'], 400)
            rebuild.assert_not_called()

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
