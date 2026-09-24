"""Account switching ("make default") is gone (rule R1: never write a CLI login).

The connector used to copy a managed grant into ~/.codex/auth.json or the
Claude Keychain item. These tests pin the removal: the route answers 404, the
module is absent, and the CLI's own credential files are left untouched.
"""

from __future__ import annotations

import importlib.util
import json
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
import agentcat_managed_accounts as managed_accounts
from agentcat_managed_accounts import ManagedAccounts


LOADER = SourceFileLoader("account_switch_removed_agentcat", str(REPO / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("account_switch_removed_agentcat", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)

CODEX_TARGET = "b" * 32


class FakeAdapter:
    def adapter_capability(self):
        return {"provider": "fake", "supported": True, "available": True, "reason": None, "modes": ["device"]}

    def start(self, profile, mode):
        return {"operationID": "op", "status": "pending_device"}


class AccountSwitchRemovedTests(unittest.TestCase):
    def test_module_is_gone(self):
        self.assertFalse((REPO / "lib" / "agentcat_account_switch.py").exists())
        self.assertIsNone(importlib.util.find_spec("agentcat_account_switch"))
        self.assertFalse(hasattr(ManagedAccounts, "make_default"))
        self.assertFalse(hasattr(agentcat, "switch_default_connection"))

    def test_no_capability_advertises_switching(self):
        for capability in agentcat.CONNECTOR_CAPABILITIES:
            self.assertNotIn("default", capability.lower())
            self.assertNotIn("switch", capability.lower())


class MakeDefaultRouteRemovedTests(unittest.TestCase):
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
        self.accounts = ManagedAccounts(self.agentcat_home, {"codex": FakeAdapter(), "claude": FakeAdapter()})
        agentcat._MANAGED_ACCOUNTS = self.accounts
        agentcat._MANAGED_ACCOUNTS_HOME = str(self.agentcat_home)
        self.accounts._write([
            {"id": CODEX_TARGET, "provider": "codex", "label": "c@x.test", "kind": "managed_native_auth",
             "scope": "managed_provider_profile", "status": "connected", "createdAt": "2026-01-01T00:00:00Z",
             "identity": {"email": "c@x.test", "verification": True, "source": "fixture"},
             "usage": {"source": "managed-codex", "freshness": "unavailable", "windows": []}},
        ])
        auth = self.home / ".codex" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"tokens": {"account_id": "acct-default"}}), encoding="utf-8")
        self.original_codex = auth.read_bytes()
        self.token = agentcat.loopback_control_token()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        agentcat._MANAGED_ACCOUNTS = None
        agentcat._MANAGED_ACCOUNTS_HOME = None
        self.keychain_patch.stop()
        self.home_patch.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def test_make_default_route_returns_404(self):
        for connection_id in (CODEX_TARGET, "0123456789abcdef0123456789abcdef"):
            request = Request(
                f"{self.base}/v1/connections/{connection_id}/make-default",
                data=json.dumps({"confirmed": True}).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as ctx:
                urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 404)
        self.assertEqual((self.home / ".codex" / "auth.json").read_bytes(), self.original_codex)


if __name__ == "__main__":
    unittest.main()
