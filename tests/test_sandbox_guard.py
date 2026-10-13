"""The Keychain audit guard must leave unrelated subprocesses alone."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import sandbox


class KeychainAuditGuardTests(unittest.TestCase):
    command = ['/usr/bin/security', 'find-generic-password', '-s', 'gemini', '-a', 'antigravity', '-w']

    def setUp(self):
        counter = patch.object(sandbox, '_ANTIGRAVITY_KEYCHAIN_ATTEMPTS', 0)
        counter.start()
        self.addCleanup(counter.stop)
        platform = patch.object(sys, 'platform', 'darwin')
        platform.start()
        self.addCleanup(platform.stop)

    def audit(self, command, executable=None):
        sandbox._guard_antigravity_keychain('subprocess.Popen', (executable, command, None, None))

    def assert_no_violation(self):
        self.assertEqual(sandbox._ANTIGRAVITY_KEYCHAIN_ATTEMPTS, 0)
        sandbox.restore_module_paths(SimpleNamespace(), {})

    def test_unbalanced_windows_command_passes_without_latching(self):
        # Exercise parsing even on macOS, where the guard is active.
        command = r'C:\Python\python.exe -c "print(\"unfinished\path)'
        self.audit(command)
        self.audit(command, r'C:\Python\python.exe')
        self.assert_no_violation()

    def test_exact_keychain_read_is_blocked_in_list_and_string_forms(self):
        for command in (self.command, tuple(self.command), ' '.join(self.command),
                        ' '.join('"' + word + '"' for word in self.command)):
            with self.subTest(command_type=type(command).__name__):
                with self.assertRaisesRegex(AssertionError, 'inject the runner'):
                    self.audit(command)
        self.assertEqual(sandbox._ANTIGRAVITY_KEYCHAIN_ATTEMPTS, 4)
        # A confirmed forbidden read still fails teardown if a caller swallowed it.
        with self.assertRaisesRegex(AssertionError, 'real Antigravity Keychain read'):
            sandbox.restore_module_paths(SimpleNamespace(), {})

    def test_guard_is_inert_off_macos(self):
        for platform in ('win32', 'linux'):
            with patch.object(sys, 'platform', platform):
                self.audit(self.command)
                self.audit(' '.join(self.command))
                self.audit(r'C:\Python\python.exe -c "unbalanced\path')
        self.assert_no_violation()

    def test_other_reads_and_unrecognized_commands_do_not_latch(self):
        for command in (
            ['/usr/bin/security', 'find-generic-password', '-s', 'other', '-a', 'antigravity', '-w'],
            ['/usr/bin/security', 'find-generic-password', '-s', 'antigravity', '-a', 'gemini', '-w'],
            self.command[:-1],
            ' '.join(self.command) + ' "',
            [], None, [object()],
        ):
            self.audit(command)
        self.assert_no_violation()
