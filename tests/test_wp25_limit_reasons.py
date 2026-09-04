"""Sandboxed regression tests for WP25 live-limit reason fixes."""

import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

from tests.sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_module_wp25", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_wp25", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class WP25TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.agentcat_home = self.root / "agentcat"
        self.home.mkdir()
        self.agentcat_home.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.agentcat_home)
        assert_sandboxed(agentcat, self.home, self.agentcat_home)
        self.env = patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env.start()
        for key in (
            "KIMI_CODE_DIR",
            "KIMI_DATA_DIR",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_PROJECT_ID",
        ):
            os.environ.pop(key, None)
        self.network = patch.object(
            agentcat.urllib.request,
            "urlopen",
            side_effect=AssertionError("network must be mocked in WP25 tests"),
        )
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    def http_error(self, url, status, body=b""):
        return urllib.error.HTTPError(url, status, "failed", {}, io.BytesIO(body))


class KimiCredentialDiscoveryTests(WP25TestCase):
    def credentials_dir(self):
        path = agentcat.HOME / ".kimi-code" / "credentials"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_kimi_file(self, name, payload):
        path = self.credentials_dir() / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def stub_payload(self):
        return {
            "access_token": "",
            "refresh_token": "",
            "expires_at": 0,
            "expires_in": 0,
            "scope": "",
            "token_type": "Bearer",
        }

    def env_payload(self, access, refresh="refresh-token", expires_at=1_900_000_000):
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": expires_at,
            "expires_in": 3600,
            "scope": "openid",
            "token_type": "Bearer",
        }

    def test_stub_plus_env_file_finds_credentials(self):
        self.write_kimi_file("kimi-code.json", self.stub_payload())
        self.write_kimi_file(
            "kimi-code-env-abc123.json",
            self.env_payload("live-access", expires_at=1_900_000_000),
        )

        creds = agentcat.read_kimi_oauth_credentials()

        self.assertIsNone(creds.get("reason"))
        self.assertEqual(creds["access_token"], "live-access")
        self.assertEqual(creds["refresh_token"], "refresh-token")
        self.assertEqual(creds["expires_at"], 1_900_000_000)
        self.assertTrue(str(agentcat.kimi_credentials_path()).endswith("kimi-code-env-abc123.json"))

    def test_two_env_files_newest_expires_at_wins(self):
        self.write_kimi_file("kimi-code.json", self.stub_payload())
        self.write_kimi_file(
            "kimi-code-env-old.json",
            self.env_payload("old-access", refresh="old-refresh", expires_at=1_800_000_000),
        )
        self.write_kimi_file(
            "kimi-code-env-new.json",
            self.env_payload("new-access", refresh="new-refresh", expires_at=1_950_000_000),
        )

        creds = agentcat.read_kimi_oauth_credentials()

        self.assertEqual(creds["access_token"], "new-access")
        self.assertEqual(creds["refresh_token"], "new-refresh")
        self.assertEqual(creds["expires_at"], 1_950_000_000)

    def test_expires_at_seconds_versus_milliseconds(self):
        self.assertEqual(agentcat.kimi_expires_at_epoch_seconds(1_780_000_000), 1_780_000_000)
        self.assertEqual(agentcat.kimi_expires_at_epoch_seconds(1_780_000_000_000), 1_780_000_000)
        self.assertEqual(agentcat.kimi_expires_at_epoch_seconds(0), 0)
        self.assertEqual(agentcat.kimi_expires_at_epoch_seconds("1900000000000"), 1_900_000_000)
        self.assertIsNone(agentcat.kimi_expires_at_epoch_seconds(None))
        self.assertIsNone(agentcat.kimi_expires_at_epoch_seconds(-1))
        self.assertLess(agentcat.KIMI_EXPIRES_AT_MS_THRESHOLD, 1_000_000_000_000)
        self.assertGreater(agentcat.KIMI_EXPIRES_AT_MS_THRESHOLD, 10_000_000_000)

        self.write_kimi_file("kimi-code.json", self.stub_payload())
        self.write_kimi_file(
            "kimi-code-env-seconds.json",
            self.env_payload("seconds-access", expires_at=1_800_000_000),
        )
        self.write_kimi_file(
            "kimi-code-env-millis.json",
            self.env_payload("millis-access", refresh="millis-refresh", expires_at=1_950_000_000_000),
        )

        creds = agentcat.read_kimi_oauth_credentials()

        self.assertEqual(creds["access_token"], "millis-access")
        self.assertEqual(creds["expires_at"], 1_950_000_000)

    def test_stub_only_is_token_missing(self):
        self.write_kimi_file("kimi-code.json", self.stub_payload())

        creds = agentcat.read_kimi_oauth_credentials()
        limits = agentcat.kimi_live_limits(force=True)

        self.assertEqual(creds["reason"], "token_missing")
        self.assertEqual(limits["reason"], "token_missing")
        self.assertEqual(limits["status"], "not_configured")

    def test_expired_env_file_refresh_failure_is_token_expired(self):
        self.write_kimi_file("kimi-code.json", self.stub_payload())
        self.write_kimi_file(
            "kimi-code-env-dead.json",
            self.env_payload("stale-access", refresh="stale-refresh", expires_at=1_000),
        )

        def fake_urlopen(request, timeout=None):
            raise OSError("refresh failed")

        with patch.object(agentcat.urllib.request, "urlopen", side_effect=fake_urlopen):
            limits = agentcat.kimi_live_limits(force=True)

        self.assertEqual(limits["reason"], "token_expired")
        self.assertNotEqual(limits.get("reason"), "token_missing")
