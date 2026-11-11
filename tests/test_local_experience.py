import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_claude_statusline_windows as window_tests
from test_agentcat import agentcat
import agentcat_local_experience as experience


class GatewayProjectionTests(unittest.TestCase):
    def test_only_exact_loopback_origins(self):
        self.assertEqual(experience.loopback_origin("http://localhost:3425/v1"), "http://127.0.0.1:3425")
        for url in ("https://127.0.0.1:3425", "http://127.0.0.1.attacker:3425", "http://user:secret@127.0.0.1:3425",
                    "http://10.0.0.1:3425", "http://127.0.0.1:3425/?key=secret", "http://127.0.0.1:99999"):
            self.assertIsNone(experience.loopback_origin(url), url)

    def test_configuration_reads_only_active_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:3425/v1", "API_KEY": "secret"}}))
            (home / "config.toml").write_text('model_provider = "magpie"\n[model_providers.other]\nbase_url = "http://127.0.0.1:9999"\n[model_providers.magpie]\nbase_url = "http://127.0.0.1:3425/v1"\n')
            self.assertEqual(experience.configured_gateways(home, home), {"http://127.0.0.1:3425": ["claude", "codex"]})

    def test_identity_is_required_before_summary_and_no_quota_probe(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return {"name": "something-else"}
        reader = experience.GatewayReader(fetch=fetch, clock=lambda: 1000)
        result = reader.snapshot({"http://127.0.0.1:3425": ["claude"]})
        self.assertEqual(calls, ["http://127.0.0.1:3425/"])
        self.assertEqual(result[0]["status"], "unavailable")

    def test_projection_drops_identifiers_paths_and_raw_models(self):
        data = {"path": "/private/secret", "accounts": [{"email": "private@example.test"}],
                "models": [{"model": "kimi-k3", "provider": "moonshot", "input": 100, "output": 50,
                            "cache_read": 25, "providerKeyName": "secret", "account": "private-id"},
                           {"model": "private-project-model", "provider": "private-host", "input": 3, "output": -1}]}
        result = experience.usage_projection(data)
        self.assertEqual(result[0], {"vendor": "Moonshot", "modelFamily": "Kimi", "input": 100,
                                     "output": 50, "cacheRead": 25, "cacheWrite": 0})
        self.assertEqual(result[1]["modelFamily"], "Unknown")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_cached_failure_keeps_observation_time_and_removed_config_clears(self):
        now = [1000]
        calls = []
        def fetch(url):
            calls.append(url)
            if now[0] > 1000:
                raise OSError("secret must not appear")
            return {"name": "magpie"} if url.endswith("/") else {"models": []}
        reader = experience.GatewayReader(fetch=fetch, clock=lambda: now[0])
        config = {"http://127.0.0.1:3425": ["claude"]}
        first = reader.snapshot(config)[0]
        reader.snapshot(config)
        self.assertEqual(len(calls), 2)
        now[0] = 1061
        stale = reader.snapshot(config)[0]
        self.assertEqual(stale["updatedAt"], first["updatedAt"])
        self.assertTrue(stale["stale"])
        self.assertNotIn("secret", json.dumps(stale))
        self.assertEqual(reader.snapshot({}), [])
        self.assertEqual(reader.cache, {})

    def test_no_configuration_means_no_network(self):
        reader = experience.GatewayReader(fetch=lambda _: self.fail("unexpected network"))
        self.assertEqual(reader.snapshot({}), [])

    def test_gateway_client_has_no_inferred_burn_rate_or_quota(self):
        data = {"usageAttribution": "gateway_or_mixed", "hourlyTokens": {"anything": 100}, "dailyTokens": {}}
        self.assertIsNone(agentcat.provider_burn_rate(data))
        self.assertIsNone(agentcat.provider_auto_quota(data))


class SessionProjectionTests(unittest.TestCase):
    def test_wait_continue_stop_and_expiry(self):
        events = [experience.session_observation("PermissionRequest", {}, "opaque", 100)]
        self.assertEqual(experience.session_summary(events, 101)["waiting"], 1)
        events.append(experience.session_observation("PostToolUse", {}, "opaque", 102))
        result = experience.session_summary(events, 103)
        self.assertEqual((result["waiting"], result["running"]), (0, 1))
        events.append(experience.session_observation("Stop", {}, "opaque", 104))
        self.assertEqual(experience.session_summary(events, 104)["completed"], 1)
        self.assertEqual(experience.session_summary(events, 705)["completed"], 0)

    def test_concurrent_sessions_are_independent_and_future_events_rejected(self):
        events = [experience.session_observation("PermissionRequest", {}, "first", 100),
                  experience.session_observation("Stop", {}, "second", 100),
                  experience.session_observation("PermissionRequest", {}, "future", 200)]
        result = experience.session_summary(events, 101)
        self.assertEqual((result["waiting"], result["completed"]), (1, 1))
        self.assertNotIn("first", json.dumps(result))

    def test_only_known_notification_types_change_state(self):
        self.assertIsNone(experience.session_observation("Notification", {"message": "permission needed"}, "opaque", 100))
        self.assertEqual(experience.session_observation("Notification", {"notification_type": "permission_prompt"}, "opaque", 100)["state"], "waiting")


class HookStorageTests(unittest.TestCase):
    # Reuse the sandbox lifecycle without inheriting its test methods.
    setUp = window_tests.ClaudeStatuslineWindowTests.setUp
    account = window_tests.ClaudeStatuslineWindowTests.account

    def test_hook_projection_is_collector_owned_and_snapshot_has_no_session_id(self):
        with patch.object(agentcat.time, "time", return_value=1000):
            agentcat.store_event("claude", "claude-hook", "PermissionRequest", {
                "session_id": "private-session", "prompt": "private prompt", "cwd": "/private/repo",
                "_agentcat_session": {"session": "forged", "state": "completed", "observedAt": 1000},
            })
            result = agentcat.local_session_summary()
        self.assertEqual(result["waiting"], 1)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("forged", json.dumps(result))

class HTTPBoundaryTests(unittest.TestCase):
    def test_redirect_size_shape_and_valid_http(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                hits.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/secret')
                    self.end_headers()
                    return
                body = b'x' * (1024 * 1024 + 1) if self.path == '/large' else b'[]' if self.path == '/array' else b'{"name":"magpie"}'
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            origin = 'http://127.0.0.1:%d' % server.server_port
            for suffix in ('/redirect', '/large', '/array'):
                with self.assertRaises(Exception):
                    experience.fetch_local_json(origin + suffix)
            self.assertNotIn('/secret', hits)
            self.assertEqual(experience.fetch_local_json(origin + '/'), {'name': 'magpie'})
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_gateway_does_not_change_cli_totals_or_recommend_a_subscription(self):
        providers = {p: {'status': 'ok', 'tokens': {'today': 100}, 'limits': {'status': 'ok', 'shortUsedPercent': n, 'shortResetAt': 9999999999}} for p, n in [('claude', 1), ('codex', 50)]}
        providers['claude']['usageAttribution'] = 'gateway_or_mixed'
        self.assertIsNone(agentcat.snapshot_recommendation(providers))
        self.assertEqual(sum(p['tokens']['today'] for p in providers.values()), 200)
