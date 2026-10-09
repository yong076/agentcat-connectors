"""Deterministic snapshot/worker regression coverage (no sleeps or network)."""
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from contextlib import closing
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from tests.sandbox import redirect_module_paths, restore_module_paths

loader = SourceFileLoader('agentcat_async_quota', str(Path(__file__).resolve().parents[1] / 'bin' / 'agentcat'))
spec = importlib.util.spec_from_loader(loader.name, loader)
a = importlib.util.module_from_spec(spec)
loader.exec_module(a)


class AsyncQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.originals = redirect_module_paths(a, root / 'home', root / 'state')
        a.schedule_quota_refresh = self.originals['schedule_quota_refresh']
        a._QUOTA_REFRESHING.clear()
        a._QUOTA_REFRESH_PENDING.clear()
        a._QUOTA_URL_FAILURES.clear()
        self.jobs = []
        self.thread_patch = patch.object(a.threading, 'Thread', side_effect=self.thread)
        self.thread_patch.start()

    def tearDown(self):
        self.thread_patch.stop()
        restore_module_paths(a, self.originals)
        self.tmp.cleanup()

    def thread(self, *, target, **kwargs):
        jobs = self.jobs
        class DeferredThread:
            def start(self):
                jobs.append(target)
        return DeferredThread()

    def test_snapshot_advances_while_breakdown_is_pending_for_twelve_seconds(self):
        # A deferred executor models a request blocked for 12 seconds. Snapshot
        # builds run normally throughout; the request cannot execute on the tick.
        home = a.HOME / '.codex'
        home.mkdir(parents=True)
        (home / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'fixture', 'account_id': 'fixture-account'}}))
        def hung_request(*args, **kwargs):
            clock[0] += 12
            raise TimeoutError('simulated 12 second hang')
        with patch.object(a.urllib.request, 'urlopen', side_effect=hung_request) as request, \
             patch.object(a, 'codex_usage_breakdown', side_effect=AssertionError('network on tick')), \
             patch.object(a, 'now_iso', side_effect=lambda: str(clock[0])), \
             patch.object(a.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            clock = [1800000000]
            for elapsed in (0, 6, 12):
                clock[0] = 1800000000 + elapsed
                before = clock[0]
                snapshot = a.build_snapshot()
                self.assertEqual(snapshot['generatedAt'], str(before))
                self.assertLessEqual(clock[0] - before, 2)
            self.assertEqual(len(self.jobs), 1)
            request.assert_not_called()

    def test_offline_breakdown_calls_url_once_in_fifteen_minutes(self):
        with patch.object(a, 'read_codex_auth', return_value={'tokens': {'access_token': 'fixture'}}), \
             patch.object(a.urllib.request, 'urlopen', side_effect=OSError('offline')) as request, \
             patch.object(a.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]):
            clock = [1800000000]
            for second in range(900):
                clock[0] = 1800000000 + second
                a.cached_codex_usage_breakdown()
                while self.jobs:
                    self.jobs.pop(0)()
            self.assertEqual(request.call_count, 1)

    def test_url_backoff_resets_after_success_and_caps_timeout(self):
        req = a.urllib.request.Request('https://example.invalid/usage')
        clock = [1000]
        with patch.object(a.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(a.urllib.request, 'urlopen', side_effect=OSError('offline')) as request:
            for second in (1000, 1299, 1300, 1899, 1900):
                clock[0] = second
                with self.assertRaises(OSError):
                    with a.quota_urlopen(req):
                        pass
            self.assertEqual(request.call_count, 3)
        clock[0] = 2800
        with patch.object(a.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(a.urllib.request, 'urlopen', return_value=closing(io.BytesIO(b'{}'))) as request:
            with a.quota_urlopen(req, timeout=100) as response:
                self.assertEqual(response.read(), b'{}')
            self.assertEqual(request.call_args.kwargs['timeout'], 8)
        with patch.object(a.time, 'time', return_value=2801), \
             patch.object(a.urllib.request, 'urlopen', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                with a.quota_urlopen(req):
                    pass
        self.assertEqual(next(iter(a._QUOTA_URL_FAILURES.values()))[:2], (3101, 1))

    def test_two_concurrent_ticks_start_one_worker_per_provider(self):
        # Real callers race; only the worker executor is deferred.
        self.thread_patch.stop()
        original_thread = threading.Thread
        barrier = threading.Barrier(2)
        def tick():
            barrier.wait()
            a.background_live_limits('claude', lambda: None)
        with patch.object(a.threading, 'Thread', side_effect=self.thread):
            callers = [original_thread(target=tick) for _ in range(2)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=2)
                self.assertFalse(caller.is_alive())
        self.assertEqual(len(self.jobs), 1)
        self.jobs.pop()()

    def test_cached_stale_limits_keep_original_timestamp(self):
        good = {'status': 'auto', 'updatedAt': 'original', 'quotas': [{'usedPercent': 10}]}
        with patch.object(a.time, 'time', return_value=1000):
            a.write_live_limits_cache('claude', good)
        with patch.object(a.time, 'time', return_value=3000):
            result = a.background_live_limits('claude', lambda: None)
        self.assertEqual(result['updatedAt'], 'original')
        self.assertTrue(result['stale'])
        self.assertEqual(len(self.jobs), 1)

    def test_healthy_refresh_is_visible_and_due_at_existing_interval(self):
        good = {'status': 'auto', 'updatedAt': 'fetch-time', 'quotas': [{'usedPercent': 10}]}
        with patch.object(a.time, 'time', side_effect=lambda: clock[0]):
            clock = [1000]
            refresh = lambda: a.write_live_limits_cache('claude', good)
            self.assertFalse(a.background_live_limits('claude', refresh)['quotas'])
            self.jobs.pop()()
            self.assertEqual(a.background_live_limits('claude', refresh)['updatedAt'], 'fetch-time')
            self.assertEqual(len(self.jobs), 0)
            clock[0] += a.LIVE_LIMITS_MAX_AGE_SECONDS + 1
            self.assertTrue(a.background_live_limits('claude', refresh)['stale'])
            self.assertEqual(len(self.jobs), 1)
            self.jobs.pop()()
            self.assertFalse(a.background_live_limits('claude', refresh).get('stale', False))

    def test_codex_accounts_and_breakdown_share_one_worker(self):
        calls = []
        a.schedule_quota_refresh('codex', lambda: calls.append('breakdown'), key='breakdown')
        a.schedule_quota_refresh('codex', lambda: calls.append('account-a'), key='account-a')
        a.schedule_quota_refresh('codex', lambda: calls.append('account-b'), key='account-b')
        self.assertEqual(len(self.jobs), 1)
        self.jobs.pop()()
        self.assertEqual(calls, ['breakdown', 'account-a', 'account-b'])

    def test_expired_codex_login_stays_stale_during_failure_backoff(self):
        good = {'status': 'auto', 'updatedAt': 'original', 'quotas': [{'id': 'codex:7d', 'usedPercent': 10}]}
        with patch.object(a.time, 'time', return_value=1000):
            a.write_live_limits_cache('codex-instance:fixture', good)
            a.serve_stale_live_limits('codex-instance:fixture', good, OSError('expired'), reason='cli_login_expired')
            result = a.background_live_limits('codex-instance:fixture', lambda: None)
        self.assertTrue(result['stale'])
        self.assertEqual(result['updatedAt'], 'original')
        self.assertEqual(result['reason'], 'cli_login_expired')
        self.assertEqual(self.jobs, [])
