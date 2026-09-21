"""Safe default-account switching: transaction, HTTP route, and redaction."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "tests"))

from sandbox import redirect_module_paths, restore_module_paths
import agentcat_account_switch as account_switch
import agentcat_managed_accounts as managed_accounts
from agentcat_account_switch import AccountSwitchError
from agentcat_managed_accounts import ManagedAccounts


LOADER = SourceFileLoader("account_switch_agentcat", str(REPO / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("account_switch_agentcat", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


CODEX_ACCESS = "codex-secret-access-token-LEAKME"
CODEX_REFRESH = "codex-secret-refresh-token-LEAKME"
DEFAULT_CODEX_ACCESS = "default-codex-access-token-LEAKME"
CLAUDE_ACCESS = "claude-secret-accessToken-LEAKME"
DEFAULT_CLAUDE_ACCESS = "default-claude-accessToken-LEAKME"
PLANTED_PATH = "/tmp/agentcat-secret-credential/auth.json"
FUTURE_UNIX = 4_102_444_800
PAST_UNIX = 1
CODEX_DEFAULT = "a" * 32
CODEX_TARGET = "b" * 32
CODEX_EXPIRED = "c" * 32
CODEX_MISSING = "d" * 32
CODEX_OTHER = "e" * 32
CLAUDE_DEFAULT = "f" * 32
CLAUDE_TARGET = "1" * 32
CLAUDE_EXPIRED = "2" * 32
CLAUDE_MISSING = "3" * 32
KIMI = "4" * 32
SECRETS = (
    CODEX_ACCESS, CODEX_REFRESH, DEFAULT_CODEX_ACCESS, CLAUDE_ACCESS,
    DEFAULT_CLAUDE_ACCESS, PLANTED_PATH, "Claude Code-credentials-",
)


class FakeAdapter:
    def adapter_capability(self):
        return {"provider": "fake", "supported": True, "available": True, "reason": None, "modes": ["device"]}

    def start(self, profile, mode):
        return {"operationID": "op", "status": "pending_device"}


class AccountSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cli_home = root / "cli-home"
        self.agentcat_home = root / "agentcat"
        self.cli_home.mkdir()
        self.agentcat_home.mkdir()
        self.accounts = ManagedAccounts(self.agentcat_home, {
            "codex": FakeAdapter(), "claude": FakeAdapter(), "kimi": FakeAdapter(),
        })
        self.home_patch = patch.object(managed_accounts, "_cli_home", return_value=self.cli_home)
        self.keychain_patch = patch.object(managed_accounts, "_read_claude_keychain", return_value=("skip", None))
        self.home_patch.start()
        self.keychain_patch.start()
        account_switch.HOOKS.clear()
        account_switch.PATH_RECORDER = None
        account_switch.KEYCHAIN_STORE = {}
        self.recorded_paths = []
        account_switch.PATH_RECORDER = self._record_path
        self._install()

    def tearDown(self):
        account_switch.HOOKS.clear()
        account_switch.PATH_RECORDER = None
        account_switch.KEYCHAIN_STORE = None
        self.keychain_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    def _record_path(self, path: Path) -> None:
        self.recorded_paths.append(Path(path))

    def _profile(self, provider, connection_id):
        path = self.accounts.profile_root / provider / connection_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _row(self, connection_id, provider, **extra):
        row = {
            "id": connection_id, "provider": provider, "label": extra.pop("label", "user@example.test"),
            "kind": "managed_native_auth", "scope": "managed_provider_profile",
            "status": extra.pop("status", "connected"), "createdAt": "2026-01-01T00:00:00Z",
            "identity": extra.pop("identity", {"email": "user@example.test", "verification": True, "source": "fixture"}),
            "usage": {"source": "managed-" + provider, "freshness": "unavailable", "windows": []},
        }
        row.update(extra)
        return row

    def _install(self):
        self._write_json(self.cli_home / ".codex" / "auth.json", {
            "tokens": {
                "account_id": "acct-default",
                "access_token": DEFAULT_CODEX_ACCESS,
                "refresh_token": "default-codex-refresh-token-LEAKME",
            },
            "source": PLANTED_PATH,
        })
        self._write_json(self.cli_home / ".claude.json", {
            "oauthAccount": {"accountUuid": "uuid-default"},
            "cachedUsageUtilization": {"accountUuid": "uuid-default"},
        })
        self._write_json(self.cli_home / ".claude" / ".credentials.json", {
            "claudeAiOauth": {"accessToken": DEFAULT_CLAUDE_ACCESS, "expiresAt": FUTURE_UNIX * 1000},
            "source": PLANTED_PATH,
        })
        self.accounts._write([
            self._row(CODEX_DEFAULT, "codex"),
            self._row(CODEX_TARGET, "codex"),
            self._row(CODEX_EXPIRED, "codex"),
            self._row(CODEX_MISSING, "codex"),
            self._row(CODEX_OTHER, "codex"),
            self._row(CLAUDE_DEFAULT, "claude"),
            self._row(CLAUDE_TARGET, "claude"),
            self._row(CLAUDE_EXPIRED, "claude"),
            self._row(CLAUDE_MISSING, "claude"),
            self._row(KIMI, "kimi", label="kimi@example.test"),
        ])
        self._write_json(self._profile("codex", CODEX_DEFAULT) / "auth.json", {
            "tokens": {"account_id": "acct-default", "access_token": DEFAULT_CODEX_ACCESS, "refresh_token": CODEX_REFRESH},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("codex", CODEX_TARGET) / "auth.json", {
            "tokens": {"account_id": "acct-target", "access_token": CODEX_ACCESS, "refresh_token": CODEX_REFRESH},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("codex", CODEX_OTHER) / "auth.json", {
            "tokens": {"account_id": "acct-other", "access_token": CODEX_ACCESS, "refresh_token": CODEX_REFRESH},
            "source": PLANTED_PATH,
        })
        self._write_json(self._profile("codex", CODEX_EXPIRED) / "auth.json", {
            "tokens": {"account_id": "acct-expired", "access_token": CODEX_ACCESS, "expires_at": PAST_UNIX},
            "source": PLANTED_PATH,
        })
        self._profile("codex", CODEX_MISSING)
        for connection_id, uuid_value, token in (
            (CLAUDE_DEFAULT, "uuid-default", DEFAULT_CLAUDE_ACCESS),
            (CLAUDE_TARGET, "uuid-target", CLAUDE_ACCESS),
            (CLAUDE_EXPIRED, "uuid-expired", CLAUDE_ACCESS),
        ):
            expires = PAST_UNIX * 1000 if connection_id == CLAUDE_EXPIRED else FUTURE_UNIX * 1000
            self._write_json(self._profile("claude", connection_id) / ".credentials.json", {
                "claudeAiOauth": {"accessToken": token, "expiresAt": expires},
                "source": PLANTED_PATH,
            })
            self._write_json(self._profile("claude", connection_id) / ".claude.json", {
                "oauthAccount": {"accountUuid": uuid_value},
            })
        self._profile("claude", CLAUDE_MISSING)
        self.original_codex = (self.cli_home / ".codex" / "auth.json").read_bytes()
        self.original_claude_json = (self.cli_home / ".claude.json").read_bytes()
        self.original_claude_cred = (self.cli_home / ".claude" / ".credentials.json").read_bytes()

    def _assert_original_codex(self):
        self.assertEqual((self.cli_home / ".codex" / "auth.json").read_bytes(), self.original_codex)

    def _assert_original_claude(self):
        self.assertEqual((self.cli_home / ".claude.json").read_bytes(), self.original_claude_json)
        self.assertEqual((self.cli_home / ".claude" / ".credentials.json").read_bytes(), self.original_claude_cred)

    def _audit_blob(self):
        path = self.agentcat_home / "account-manager" / "audit.jsonl"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _assert_redacted(self, blob):
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        self.assertNotIn(str(self.cli_home), blob)
        self.assertNotIn(str(self.accounts.profile_root), blob)
        self.assertNotIn("access_token", blob)
        self.assertNotIn("accessToken", blob)

    def _assert_no_orca(self):
        for path in self.recorded_paths:
            text = str(path).replace("\\", "/").lower()
            self.assertNotIn("library/application support/orca", text)
            self.assertNotIn("appdata/roaming/orca", text)
            self.assertNotIn("appdata/local/orca", text)

    def test_codex_success_switches_default_and_writes_redacted_audit(self):
        result = self.accounts.make_default(CODEX_TARGET, confirmed=True)
        self.assertTrue(result["ok"])
        self.assertEqual(managed_accounts._default_codex_account_id(), "acct-target")
        self.assertIs(result["connection"]["defaultActive"], True)
        self.assertEqual(result["connection"]["id"], CODEX_TARGET)
        snapshot = {row["id"]: row for row in self.accounts.snapshot()}
        self.assertIs(snapshot[CODEX_TARGET]["defaultActive"], True)
        self.assertIs(snapshot[CODEX_DEFAULT]["defaultActive"], False)
        audit = json.loads(self._audit_blob().splitlines()[-1])
        self.assertEqual(audit["provider"], "codex")
        self.assertEqual(audit["fromAccountId"], "acct-default")
        self.assertEqual(audit["toAccountId"], "acct-target")
        self.assertEqual(audit["outcome"], "success")
        self.assertIn("timestamp", audit)
        self._assert_redacted(self._audit_blob())
        self._assert_redacted(json.dumps(result))
        self._assert_no_orca()

    def test_claude_success_points_default_json_and_credential_at_target(self):
        result = self.accounts.make_default(CLAUDE_TARGET, confirmed=True)
        self.assertTrue(result["ok"])
        self.assertEqual(managed_accounts._default_claude_account_uuid(), "uuid-target")
        self.assertIs(result["connection"]["defaultActive"], True)
        cred = json.loads((self.cli_home / ".claude" / ".credentials.json").read_text(encoding="utf-8"))
        self.assertEqual(cred["claudeAiOauth"]["accessToken"], CLAUDE_ACCESS)
        audit = json.loads(self._audit_blob().splitlines()[-1])
        self.assertEqual(audit["provider"], "claude")
        self.assertEqual(audit["fromAccountId"], "uuid-default")
        self.assertEqual(audit["toAccountId"], "uuid-target")
        self.assertEqual(audit["outcome"], "success")
        self._assert_redacted(self._audit_blob())
        self._assert_redacted(json.dumps(result))
        self._assert_no_orca()

    def test_backup_failure_leaves_original_codex_default(self):
        account_switch.HOOKS["before_backup"] = lambda: (_ for _ in ()).throw(OSError("injected backup"))
        with self.assertRaises(AccountSwitchError) as ctx:
            self.accounts.make_default(CODEX_TARGET, confirmed=True)
        self.assertEqual(str(ctx.exception), "switch_backup_failed")
        self._assert_original_codex()
        self.assertEqual(json.loads(self._audit_blob().splitlines()[-1])["outcome"], "backup_failed")

    def test_backup_failure_leaves_original_claude_default(self):
        account_switch.HOOKS["before_backup"] = lambda: (_ for _ in ()).throw(OSError("injected backup"))
        with self.assertRaises(AccountSwitchError) as ctx:
            self.accounts.make_default(CLAUDE_TARGET, confirmed=True)
        self.assertEqual(str(ctx.exception), "switch_backup_failed")
        self._assert_original_claude()

    def test_write_failure_leaves_original_codex_default(self):
        account_switch.HOOKS["before_write"] = lambda: (_ for _ in ()).throw(OSError("injected write"))
        with self.assertRaises(AccountSwitchError) as ctx:
            self.accounts.make_default(CODEX_TARGET, confirmed=True)
        self.assertEqual(str(ctx.exception), "switch_write_failed")
        self._assert_original_codex()
        self.assertEqual(json.loads(self._audit_blob().splitlines()[-1])["outcome"], "write_failed")

    def test_write_failure_leaves_original_claude_default(self):
        account_switch.HOOKS["before_write"] = lambda: (_ for _ in ()).throw(OSError("injected write"))
        with self.assertRaises(AccountSwitchError) as ctx:
            self.accounts.make_default(CLAUDE_TARGET, confirmed=True)
        self.assertEqual(str(ctx.exception), "switch_write_failed")
        self._assert_original_claude()

    def test_verification_mismatch_restores_original_codex_default(self):
        def corrupt():
            (self.cli_home / ".codex" / "auth.json").write_text(json.dumps({
                "tokens": {"account_id": "acct-wrong", "access_token": CODEX_ACCESS},
            }), encoding="utf-8")
        account_switch.HOOKS["before_verify"] = corrupt
        with self.assertRaises(AccountSwitchError) as ctx:
            self.accounts.make_default(CODEX_TARGET, confirmed=True)
        self.assertEqual(str(ctx.exception), "switch_verification_failed")
        self._assert_original_codex()
        self.assertEqual(json.loads(self._audit_blob().splitlines()[-1])["outcome"], "verification_failed")

    def test_verification_mismatch_restores_original_claude_default(self):
        def corrupt():
            self._write_json(self.cli_home / ".claude.json", {
                "oauthAccount": {"accountUuid": "uuid-wrong"},
            })
        account_switch.HOOKS["before_verify"] = corrupt
        with self.assertRaises(AccountSwitchError) as ctx:
            self.accounts.make_default(CLAUDE_TARGET, confirmed=True)
        self.assertEqual(str(ctx.exception), "switch_verification_failed")
        self._assert_original_claude()

    def test_concurrent_calls_are_serialized(self):
        in_flight = []
        max_in_flight = []
        lock = threading.Lock()

        def during():
            with lock:
                in_flight.append(1)
                max_in_flight.append(len(in_flight))
            threading.Event().wait(0.05)
            with lock:
                in_flight.pop()

        account_switch.HOOKS["during_write"] = during
        errors = []
        results = []

        def worker(connection_id):
            try:
                results.append(self.accounts.make_default(connection_id, confirmed=True))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(CODEX_TARGET,)),
            threading.Thread(target=worker, args=(CODEX_OTHER,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(max(max_in_flight), 1)
        final = managed_accounts._default_codex_account_id()
        self.assertIn(final, {"acct-target", "acct-other"})

    def test_missing_and_expired_targets_are_refused(self):
        with self.assertRaises(AccountSwitchError) as missing:
            self.accounts.make_default(CODEX_MISSING, confirmed=True)
        self.assertEqual(str(missing.exception), "target_credential_missing")
        self._assert_original_codex()
        with self.assertRaises(AccountSwitchError) as expired:
            self.accounts.make_default(CODEX_EXPIRED, confirmed=True)
        self.assertEqual(str(expired.exception), "target_credential_expired")
        self._assert_original_codex()
        with self.assertRaises(AccountSwitchError) as claude_missing:
            self.accounts.make_default(CLAUDE_MISSING, confirmed=True)
        self.assertEqual(str(claude_missing.exception), "target_credential_missing")
        self._assert_original_claude()
        with self.assertRaises(AccountSwitchError) as claude_expired:
            self.accounts.make_default(CLAUDE_EXPIRED, confirmed=True)
        self.assertEqual(str(claude_expired.exception), "target_credential_expired")
        self._assert_original_claude()
        outcomes = [json.loads(line)["outcome"] for line in self._audit_blob().splitlines()]
        self.assertEqual(set(outcomes), {"refused"})

    def test_running_process_warning_does_not_block_switch(self):
        with patch.object(account_switch, "count_running_cli_sessions", return_value=2):
            result = self.accounts.make_default(CODEX_TARGET, confirmed=True)
        self.assertTrue(result["ok"])
        self.assertIn("warning", result)
        self.assertIn("2", result["warning"])
        self.assertIn("codex", result["warning"])
        self.assertIn("previous account until restarted", result["warning"])
        self.assertEqual(managed_accounts._default_codex_account_id(), "acct-target")
        self._assert_redacted(json.dumps(result))

    def test_unsupported_provider_and_unconfirmed_are_refused(self):
        with self.assertRaises(ValueError) as unconfirmed:
            self.accounts.make_default(CODEX_TARGET, confirmed=False)
        self.assertEqual(str(unconfirmed.exception), "confirmed_required")
        self._assert_original_codex()
        with self.assertRaises(ValueError) as kimi:
            self.accounts.make_default(KIMI, confirmed=True)
        self.assertEqual(str(kimi.exception), "provider_not_supported")
        with self.assertRaises(KeyError):
            self.accounts.make_default("0" * 32, confirmed=True)

    def test_refuses_orca_store_and_records_no_orca_filesystem_calls(self):
        orca = self.cli_home / "Library" / "Application Support" / "orca" / "codex"
        orca.mkdir(parents=True)
        self._write_json(orca / "auth.json", {
            "tokens": {"account_id": "acct-orca", "access_token": CODEX_ACCESS},
        })
        fs_targets = []
        real_replace = os.replace
        real_open = os.open

        def wrapped_replace(src, dst, *args, **kwargs):
            fs_targets.extend((src, dst))
            return real_replace(src, dst, *args, **kwargs)

        def wrapped_open(path, flags, *args, **kwargs):
            fs_targets.append(path)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(account_switch.os, "replace", wrapped_replace), patch.object(account_switch.os, "open", wrapped_open):
            with self.assertRaises(AccountSwitchError) as ctx:
                account_switch.make_default_account(
                    agentcat_home=self.agentcat_home,
                    provider="codex",
                    connection_id=CODEX_TARGET,
                    source_profile=orca,
                    confirmed=True,
                )
        self.assertEqual(str(ctx.exception), "orca_store_forbidden")
        self._assert_original_codex()
        self.assertEqual(fs_targets, [])


class AccountSwitchRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.agentcat_home = Path(self.tmp.name) / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        agentcat.LOOPBACK_CONTROL_TOKEN_FILE = self.agentcat_home / "loopback-control-token"
        self.home_patch = patch.object(managed_accounts, "_cli_home", return_value=self.home)
        self.keychain_patch = patch.object(managed_accounts, "_read_claude_keychain", return_value=("skip", None))
        self.home_patch.start()
        self.keychain_patch.start()
        account_switch.HOOKS.clear()
        account_switch.KEYCHAIN_STORE = {}
        account_switch.PATH_RECORDER = None
        self.accounts = ManagedAccounts(self.agentcat_home, {
            "codex": FakeAdapter(), "claude": FakeAdapter(), "kimi": FakeAdapter(),
        })
        agentcat._MANAGED_ACCOUNTS = self.accounts
        agentcat._MANAGED_ACCOUNTS_HOME = str(self.agentcat_home)
        self.token = agentcat.loopback_control_token()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._install()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        agentcat._MANAGED_ACCOUNTS = None
        agentcat._MANAGED_ACCOUNTS_HOME = None
        account_switch.HOOKS.clear()
        account_switch.KEYCHAIN_STORE = None
        self.keychain_patch.stop()
        self.home_patch.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def _write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _profile(self, provider, connection_id):
        path = self.accounts.profile_root / provider / connection_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _install(self):
        self._write_json(self.home / ".codex" / "auth.json", {
            "tokens": {"account_id": "acct-default", "access_token": DEFAULT_CODEX_ACCESS},
        })
        self._write_json(self.home / ".claude.json", {"oauthAccount": {"accountUuid": "uuid-default"}})
        self._write_json(self.home / ".claude" / ".credentials.json", {
            "claudeAiOauth": {"accessToken": DEFAULT_CLAUDE_ACCESS, "expiresAt": FUTURE_UNIX * 1000},
        })
        self.accounts._write([
            {"id": CODEX_TARGET, "provider": "codex", "label": "c@x.test", "kind": "managed_native_auth",
             "scope": "managed_provider_profile", "status": "connected", "createdAt": "2026-01-01T00:00:00Z",
             "identity": {"email": "c@x.test", "verification": True, "source": "fixture"},
             "usage": {"source": "managed-codex", "freshness": "unavailable", "windows": []}},
            {"id": CLAUDE_TARGET, "provider": "claude", "label": "c@x.test", "kind": "managed_native_auth",
             "scope": "managed_provider_profile", "status": "connected", "createdAt": "2026-01-01T00:00:00Z",
             "identity": {"email": "c@x.test", "verification": True, "source": "fixture"},
             "usage": {"source": "managed-claude", "freshness": "unavailable", "windows": []}},
            {"id": KIMI, "provider": "kimi", "label": "k@x.test", "kind": "managed_native_auth",
             "scope": "managed_provider_profile", "status": "connected", "createdAt": "2026-01-01T00:00:00Z",
             "usage": {"source": "managed-kimi", "freshness": "unavailable", "windows": []}},
        ])
        self._write_json(self._profile("codex", CODEX_TARGET) / "auth.json", {
            "tokens": {"account_id": "acct-target", "access_token": CODEX_ACCESS, "refresh_token": CODEX_REFRESH},
        })
        self._write_json(self._profile("claude", CLAUDE_TARGET) / ".credentials.json", {
            "claudeAiOauth": {"accessToken": CLAUDE_ACCESS, "expiresAt": FUTURE_UNIX * 1000},
        })
        self._write_json(self._profile("claude", CLAUDE_TARGET) / ".claude.json", {
            "oauthAccount": {"accountUuid": "uuid-target"},
        })
        self.original_codex = (self.home / ".codex" / "auth.json").read_bytes()

    def request(self, path, *, body=None, method="POST", token=None):
        headers = {}
        if token is not False:
            headers["Authorization"] = f"Bearer {token or self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        request = Request(self.base + path, data=body, method=method, headers=headers)
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())

    def test_http_make_default_requires_control_token_and_confirmed(self):
        path = f"/v1/connections/{CODEX_TARGET}/make-default"
        with self.assertRaises(HTTPError) as denied:
            self.request(path, body={"confirmed": True}, token=False)
        self.assertEqual(denied.exception.code, 401)
        with self.assertRaises(HTTPError) as unconfirmed:
            self.request(path, body={}, method="POST")
        self.assertEqual(unconfirmed.exception.code, 400)
        self.assertEqual(json.loads(unconfirmed.exception.read().decode())["error"], "confirmed_required")
        self.assertEqual((self.home / ".codex" / "auth.json").read_bytes(), self.original_codex)

    def test_http_make_default_success_for_codex_and_claude(self):
        with patch.object(account_switch, "count_running_cli_sessions", return_value=1):
            status, payload = self.request(
                f"/v1/connections/{CODEX_TARGET}/make-default",
                body={"confirmed": True},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("warning", payload)
        self.assertEqual(managed_accounts._default_codex_account_id(), "acct-target")
        blob = json.dumps(payload)
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        status, claude = self.request(
            f"/v1/connections/{CLAUDE_TARGET}/make-default",
            body={"confirmed": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(managed_accounts._default_claude_account_uuid(), "uuid-target")
        audit = (self.agentcat_home / "account-manager" / "audit.jsonl").read_text(encoding="utf-8")
        for secret in SECRETS:
            self.assertNotIn(secret, audit)

    def test_http_make_default_rejects_unsupported_provider(self):
        with self.assertRaises(HTTPError) as rejected:
            self.request(f"/v1/connections/{KIMI}/make-default", body={"confirmed": True})
        self.assertEqual(rejected.exception.code, 400)
        self.assertEqual(json.loads(rejected.exception.read().decode())["error"], "provider_not_supported")


if __name__ == "__main__":
    unittest.main()
