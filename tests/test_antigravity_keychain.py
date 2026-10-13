"""Antigravity Keychain reads use injected synthetic responses only."""
import base64
import importlib.util
import json
import subprocess
import sys
import sandbox
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import Mock, patch

from sandbox import redirect_module_paths, restore_module_paths

LOADER = SourceFileLoader('antigravity_keychain_tests', str(Path(__file__).resolve().parents[1] / 'bin/agentcat'))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
app = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(app)


class AntigravityKeychainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        originals = redirect_module_paths(app, root, root / '.agentcat')
        self.addCleanup(restore_module_paths, app, originals)
        self.patch(app.sys, 'platform', 'darwin')
        self.patch(app.time, 'time', Mock(return_value=1000))
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 44, b''))
        self.patch(app, 'antigravity_keychain_run', self.runner)
        self.patch(app, '_ANTIGRAVITY_KEYCHAIN_CACHE', None)
        self.patch(app, '_ANTIGRAVITY_KEYCHAIN_WARNED', False)
        self.file_read = Mock(return_value=None)
        self.patch(app, 'read_json', self.file_read)
        self.refresh = Mock(side_effect=AssertionError('read-only grant must not refresh'))
        self.patch(app, 'refresh_antigravity_access_token', self.refresh)
        self.patch(app, 'antigravity_oauth_client_candidates', Mock(side_effect=AssertionError('must not read client cache')))
        self.patch(app, 'write_json_atomic', Mock(side_effect=AssertionError('must not persist credentials')))
        self.patch(app.urllib.request, 'urlopen', Mock(side_effect=AssertionError('provider calls forbidden in tests')))

    def patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def keychain(self, expiry=2000000):
        # Deliberately small fake values, never sourced from a real credential.
        return {'token': {'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh', 'expiry_date': expiry}}

    def respond(self, value):
        self.runner.return_value = subprocess.CompletedProcess([], 0, value)

    def test_three_encodings_and_current_login_precedence(self):
        raw = json.dumps(self.keychain()).encode()
        for encoded in (raw, b'go-keyring-base64:' + base64.b64encode(raw), b'go-keyring-encoded:' + raw.hex().encode()):
            with self.subTest(encoding=encoded[:1]):
                app._ANTIGRAVITY_KEYCHAIN_CACHE = None
                self.respond(encoded)
                self.file_read.return_value = {'access_token': 'fixture-old', 'expiry_date': 1}
                self.assertEqual(app.antigravity_access_token(), 'fixture-access')
                creds = app.read_antigravity_oauth_credentials()
                self.assertEqual(creds[app.ANTIGRAVITY_CREDS_SOURCE_KEY], app.ANTIGRAVITY_KEYCHAIN_SOURCE)
        self.file_read.assert_not_called()
        self.refresh.assert_not_called()
        args, kwargs = self.runner.call_args
        self.assertEqual(args[0], ['/usr/bin/security', 'find-generic-password', '-s', 'gemini', '-a', 'antigravity', '-w'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertLessEqual(kwargs['timeout'], 5)

    def test_expired_keychain_uses_refreshable_file(self):
        self.respond(json.dumps(self.keychain(1)).encode())
        self.file_read.return_value = {'access_token': 'fixture-file', 'refresh_token': 'fixture-file-refresh', 'expiry_date': 1}
        self.refresh.side_effect = None
        self.refresh.return_value = {'access_token': 'fixture-updated', 'expiry_date': 2000000}
        self.assertEqual(app.antigravity_access_token(), 'fixture-updated')
        passed = self.refresh.call_args.args[0]
        self.assertNotIn(app.ANTIGRAVITY_CREDS_SOURCE_KEY, passed)
        self.assertEqual(passed['access_token'], 'fixture-file')

    def test_expired_keychain_without_file_reports_expired_in_both_probes(self):
        self.respond(json.dumps(self.keychain(1)).encode())
        self.assertIsNone(app.antigravity_access_token())
        self.assertEqual(app.antigravity_live_limits(force=True)['reason'], 'cli_login_expired')
        self.assertEqual(app.probe_antigravity()['reason'], 'cli_login_expired')
        self.refresh.assert_not_called()

    def test_expired_keychain_survives_unusable_files(self):
        self.respond(json.dumps(self.keychain(1)).encode())
        self.file_read.return_value = {'access_token': 'fixture-expired-file', 'expiry_date': 1}
        creds = app.read_antigravity_oauth_credentials()
        self.assertEqual(creds[app.ANTIGRAVITY_CREDS_SOURCE_KEY], app.ANTIGRAVITY_KEYCHAIN_SOURCE)
        self.assertIsNone(app.antigravity_access_token())
        self.refresh.assert_not_called()

    def test_unknown_expiry_is_not_refreshed(self):
        self.respond(json.dumps({'access_token': 'fixture-access'}).encode())
        self.assertIsNone(app.antigravity_access_token())
        self.refresh.assert_not_called()

    def test_failures_fall_back_to_file(self):
        responses = [subprocess.CompletedProcess([], 44, b''), subprocess.CompletedProcess([], 45, b''),
                     subprocess.TimeoutExpired('security', 5), subprocess.CompletedProcess([], 0, b'garbage'),
                     subprocess.CompletedProcess([], 0, b'[]'), subprocess.CompletedProcess([], 0, b'{}'),
                     subprocess.CompletedProcess([], 0, b'x' * 65537),
                     subprocess.CompletedProcess([], 0, b'go-keyring-base64:!'),
                     subprocess.CompletedProcess([], 0, b'go-keyring-encoded:!')]
        self.file_read.return_value = {'access_token': 'fixture-file', 'expiry_date': 2000000}
        for response in responses:
            with self.subTest(response_type=type(response).__name__):
                app._ANTIGRAVITY_KEYCHAIN_CACHE = None
                if isinstance(response, Exception):
                    self.runner.side_effect = response
                else:
                    self.runner.side_effect = None
                    self.runner.return_value = response
                self.assertEqual(app.antigravity_access_token(), 'fixture-file')
        self.refresh.assert_not_called()

    def test_memoizes_success_and_failure_for_sixty_seconds(self):
        for code, value in [(0, json.dumps(self.keychain()).encode()), (44, b'')]:
            with self.subTest(code=code):
                app._ANTIGRAVITY_KEYCHAIN_CACHE = None
                self.runner.reset_mock()
                self.runner.return_value = subprocess.CompletedProcess([], code, value)
                with patch.object(app.time, 'monotonic', return_value=100):
                    app.read_antigravity_keychain_credentials()
                    app.read_antigravity_keychain_credentials()
                with patch.object(app.time, 'monotonic', return_value=159.9):
                    app.read_antigravity_keychain_credentials()
                self.assertEqual(self.runner.call_count, 1)
                with patch.object(app.time, 'monotonic', return_value=160):
                    app.read_antigravity_keychain_credentials()
                self.assertEqual(self.runner.call_count, 2)

    def test_non_darwin_never_spawns(self):
        for platform in ('linux', 'win32'):
            with patch.object(app.sys, 'platform', platform):
                self.assertIsNone(app.read_antigravity_keychain_credentials())
        self.runner.assert_not_called()

    def test_failed_read_logs_once_without_values(self):
        self.respond(b'not-json')
        with patch.object(app.logging, 'warning') as warning:
            app.read_antigravity_keychain_credentials()
            app._ANTIGRAVITY_KEYCHAIN_CACHE = None
            app.read_antigravity_keychain_credentials()
        warning.assert_called_once_with('agentcat: Antigravity Keychain read unavailable')

    def test_cache_returns_a_copy(self):
        self.respond(json.dumps(self.keychain()).encode())
        creds = app.read_antigravity_keychain_credentials()
        creds['access_token'] = 'changed'
        self.assertEqual(app.read_antigravity_keychain_credentials()['access_token'], 'fixture-access')

    def test_refresh_function_rejects_read_only_sources_before_client_lookup(self):
        # Exercise the actual refresh function, not the default forbidden-call mock.
        refresh = self.original_refresh
        for source in (app.ANTIGRAVITY_APP_SOURCE, app.ANTIGRAVITY_KEYCHAIN_SOURCE):
            with self.assertRaisesRegex(RuntimeError, 'read-only'):
                refresh({'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh', app.ANTIGRAVITY_CREDS_SOURCE_KEY: source})

    def test_sandbox_guard_blocks_security_before_execution(self):
        with patch.object(sandbox, '_ANTIGRAVITY_KEYCHAIN_ATTEMPTS', 0):
            with self.assertRaisesRegex(AssertionError, 'inject the runner'):
                subprocess.Popen(['/usr/bin/security', 'find-generic-password', '-s', 'gemini', '-a', 'antigravity', '-w'])
            self.assertEqual(sandbox._ANTIGRAVITY_KEYCHAIN_ATTEMPTS, 1)
            with self.assertRaisesRegex(AssertionError, 'inject the runner'):
                sandbox._guard_antigravity_keychain('subprocess.Popen', (
                    None, '/usr/bin/security find-generic-password -s gemini -a antigravity -w', None, None))
            self.assertEqual(sandbox._ANTIGRAVITY_KEYCHAIN_ATTEMPTS, 2)

    @unittest.skipIf(sys.platform == 'win32', 'POSIX pipe runner used only on macOS')
    def test_runner_bounds_output_and_time(self):
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1)
        with self.assertRaisesRegex(ValueError, 'size limit'):
            self.original_runner([sys.executable, '-c', 'print("x" * 70000)'], **kwargs)
        kwargs['timeout'] = 0.05
        with self.assertRaises(subprocess.TimeoutExpired):
            self.original_runner([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)

    original_refresh = staticmethod(app.refresh_antigravity_access_token)
    original_runner = staticmethod(app.antigravity_keychain_run)
