"""Every mutating daemon route rejects cross-site browser writes (CSRF).

A web page could otherwise send a text/plain POST to 127.0.0.1:8765 and switch
on Reflect auto-analysis (which spends Claude quota), rewrite /v1/config or
spam /v1/events. Native clients — the macOS app's URLSession, the Windows app's
Rust proxy, the CLI — send no Origin and must keep working, including the
macOS app's body-less POSTs without a Content-Type.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from sandbox import redirect_module_paths, restore_module_paths  # noqa: E402

LOADER = SourceFileLoader("mutation_origin_guard_agentcat", str(REPO / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("mutation_origin_guard_agentcat", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)

TAURI_ORIGINS = ("http://tauri.localhost", "https://tauri.localhost", "tauri://localhost")
CROSS_SITE_HEADER_SETS = (
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://evil.example"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "null"},
    {"Origin": "http://127.0.0.1.evil.example"},
    {"Origin": "http://tauri.localhost.evil.example"},
    {"Origin": "http://tauri.localhost:8080"},
)


class MutationOriginGuardUnitTests(unittest.TestCase):
    def test_native_clients_without_origin_pass(self):
        self.assertIsNone(agentcat.mutating_request_origin_error({}))
        self.assertIsNone(agentcat.mutating_request_origin_error({"Content-Type": "text/plain"}))
        self.assertIsNone(agentcat.mutating_request_origin_error({"Sec-Fetch-Site": "none"}))

    def test_loopback_pages_pass(self):
        for headers in (
            {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
            {"Origin": "http://localhost:1420", "Sec-Fetch-Site": "same-site"},
            {"Origin": "http://[::1]:8765"},
        ):
            self.assertIsNone(agentcat.mutating_request_origin_error(headers), headers)

    def test_tauri_webview_origins_get_no_exemption(self):
        # No shipped client needs one: the Windows app calls the daemon through
        # its native Rust proxy, which sends no Origin.
        self.assertFalse(hasattr(agentcat, "TRUSTED_APP_ORIGINS"))
        for origin in TAURI_ORIGINS:
            headers = {"Origin": origin, "Sec-Fetch-Site": "cross-site"}
            self.assertEqual(agentcat.mutating_request_origin_error(headers)[0], 403, origin)
            self.assertEqual(agentcat.mutating_request_origin_error({"Origin": origin})[0], 403, origin)

    def test_cross_site_and_foreign_origins_are_rejected(self):
        for headers in CROSS_SITE_HEADER_SETS:
            error = agentcat.mutating_request_origin_error(headers)
            self.assertIsNotNone(error, headers)
            self.assertEqual(error[0], 403, headers)


class MutationOriginGuardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.state)
        # A successful /v1/config write rebuilds the snapshot; keep that out of
        # the guard test (and away from any provider network call).
        self.build_patch = patch.object(agentcat, "build_snapshot", return_value={})
        self.build_patch.start()

    def tearDown(self):
        self.build_patch.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    @contextlib.contextmanager
    def server_url(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    @staticmethod
    def send(url, method, *, body=None, headers=None):
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            finally:
                exc.close()

    def event_count(self):
        if not agentcat.EVENTS_DB.exists():
            return 0
        with closing(sqlite3.connect(agentcat.EVENTS_DB)) as conn:
            return int(conn.execute("select count(*) from events").fetchone()[0])

    def test_cross_site_writes_are_rejected_before_any_route_runs(self):
        event_body = json.dumps({"provider": "codex", "eventType": "spam"}).encode()
        config_body = json.dumps({"providers": {"codex": {"enabled": False}}}).encode()
        automation_body = json.dumps({"autoAnalyze": True}).encode()
        with self.server_url() as url, \
            patch.object(agentcat, "merge_connector_config_payload", side_effect=AssertionError("config must not change")), \
            patch.object(agentcat, "reflect_http_post", side_effect=AssertionError("reflect must not run")), \
            patch.object(agentcat, "store_event", side_effect=AssertionError("event must not be stored")):
            for headers in CROSS_SITE_HEADER_SETS:
                simple = {"Content-Type": "text/plain", **headers}
                for method, path, body in (
                    ("POST", "/v1/events", event_body),
                    ("POST", "/v1/config", config_body),
                    ("POST", "/reflect/enable", None),
                    ("POST", "/reflect/automation", automation_body),
                    ("POST", "/reflect/analyze/some-session", None),
                    ("POST", "/v1/update/channel", b'{"channel":"public"}'),
                    ("PATCH", f"/v1/connections/{'a' * 32}", b'{"label":"x"}'),
                    ("DELETE", f"/v1/connections/{'a' * 32}", None),
                ):
                    status, payload = self.send(url + path, method, body=body, headers=simple)
                    self.assertEqual(status, 403, (method, path, headers))
                    self.assertEqual(payload["error"], "forbidden", (method, path, headers))
        self.assertEqual(self.event_count(), 0)
        self.assertFalse(agentcat.REFLECT_CONFIG_FILE.exists())

    def test_native_origin_less_requests_still_work(self):
        with self.server_url() as url:
            # The macOS app's Reflect toggle: POST, no body, no Content-Type.
            status, payload = self.send(url + "/reflect/enable", "POST")
            self.assertEqual(status, 200)
            self.assertTrue(payload["enabled"])
            self.assertTrue(agentcat.REFLECT_CONFIG_FILE.exists())

            status, payload = self.send(
                url + "/v1/events", "POST",
                body=json.dumps({"provider": "codex", "eventType": "native"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 201)
            self.assertEqual(payload["eventType"], "native")

            # The Windows app's native proxy: JSON POST to /v1/config, no Origin.
            status, payload = self.send(
                url + "/v1/config", "POST",
                body=json.dumps({"providers": {"codex": {"enabled": False}}}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
        self.assertEqual(self.event_count(), 1)

    def test_tauri_webview_origin_is_rejected(self):
        with self.server_url() as url:
            for origin in TAURI_ORIGINS:
                status, payload = self.send(
                    url + "/v1/events", "POST",
                    body=json.dumps({"provider": "codex", "eventType": "tauri"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": origin, "Sec-Fetch-Site": "cross-site"},
                )
                self.assertEqual(status, 403, origin)
                self.assertEqual(payload["error"], "forbidden")
        self.assertEqual(self.event_count(), 0)

    def test_forced_usage_fetch_rejects_cross_site_get(self):
        # GET /v1/usage forces a live fetch from every provider and skips their
        # backoffs. An <img src> on any page sends Sec-Fetch-Site: cross-site
        # (and no Origin) and must not reach it.
        with self.server_url() as url, \
            patch.object(agentcat, "fetch_llm_usage_fresh", side_effect=AssertionError("must not fetch")):
            for headers in (
                {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "image"},
                {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                {"Origin": "null"},
            ):
                status, payload = self.send(url + "/v1/usage", "GET", headers=headers)
                self.assertEqual(status, 403, headers)
                self.assertEqual(payload["error"], "forbidden")
        with self.server_url() as url, \
            patch.object(agentcat, "fetch_llm_usage_fresh", return_value={"providers": {}}) as fetch:
            # The native apps (URLSession, the Windows Rust proxy) send neither header.
            status, payload = self.send(url + "/v1/usage", "GET")
            self.assertEqual((status, payload), (200, {"providers": {}}))
            status, _payload = self.send(
                url + "/v1/usage", "GET",
                headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
            )
            self.assertEqual(status, 200)
        self.assertEqual(fetch.call_count, 2)

    def test_read_only_gets_stay_open_to_native_and_widgets(self):
        with self.server_url() as url:
            status, _payload = self.send(url + "/v1/version", "GET", headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(status, 200)

    def test_credential_routes_keep_their_stricter_guard(self):
        # The general guard admits a loopback page; /v1/connections/* still
        # requires a JSON body plus the control bearer token.
        with self.server_url() as url:
            status, payload = self.send(
                url + "/v1/connections/openrouter/oauth/start", "POST",
                body=b"{}",
                headers={
                    "Content-Type": "text/plain",
                    "Origin": "http://127.0.0.1:8765",
                    "Authorization": f"Bearer {agentcat.loopback_control_token()}",
                },
            )
            self.assertEqual(status, 415)
            self.assertEqual(payload["error"], "connection_write_forbidden")


if __name__ == "__main__":
    unittest.main()
