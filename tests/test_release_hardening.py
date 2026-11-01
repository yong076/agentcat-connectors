"""Isolated regressions for local health, accounting and lifecycle tails."""
import datetime as dt
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from sandbox import redirect_module_paths, restore_module_paths

loader = SourceFileLoader("release_hardening_agentcat", str(Path(__file__).resolve().parents[1] / "bin/agentcat"))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
loader.exec_module(app)


class ReleaseHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        old = redirect_module_paths(app, self.root, self.root / ".agentcat")
        self.addCleanup(restore_module_paths, app, old)

    def test_health_keeps_credential_failure_separate_from_healthy_usage(self):
        health = app.connector_health_snapshot({
            "claude": {"status": "ok", "error": "private arbitrary payload", "limits": {
                "status": "error", "reason": "keychain_denied", "stale": True, "access_token": "never-copy"}},
            "codex": {"status": "not_found", "limits": {"status": "error", "reason": "/private/path"}},
        })
        rows = {x["id"]: x for x in health["sources"]}
        self.assertEqual(rows["claude.usage"]["state"], "ok")
        self.assertEqual(rows["claude.limits"]["reason"], "keychain_denied")
        self.assertEqual(rows["claude.limits"]["fix"], "grant_keychain_access")
        self.assertEqual(rows["codex.limits"]["reason"], "health_unknown")
        self.assertNotIn("private", json.dumps(health))
        self.assertNotIn("never-copy", json.dumps(health))

    def test_usage_floor_lifetime_covers_the_merged_dated_buckets(self):
        primary = {"status": "ok", "tokens": {"all": 100, "totalTokens": 100}, "dailyTokens": {"2026-10-30": 100}}
        floor = {"status": "ok", "tokens": {"all": 120, "totalTokens": 120}, "dailyTokens": {"2026-10-31": 120}}
        merged = app.merge_provider_with_usage_floor(primary, floor, strategy="test")
        self.assertEqual(merged["tokens"]["all"], 220)
        self.assertEqual(merged["tokens"]["totalTokens"], 220)
        self.assertEqual(primary["tokens"]["all"], 100)

    def test_project_top_list_reentry_reseeds_after_a_gap(self):
        def providers(amount):
            return {"claude": {"projects": {"status": "ok", "items": [
                {"id": "project-id", "name": "display", "tokens": amount}]}}}
        when = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        app.update_project_daily(providers(100), when)
        app.update_project_daily(providers(110), when)
        app.update_project_daily({}, when)
        after_gap = app.update_project_daily(providers(200), when)
        self.assertEqual(sum(x.get("display", 0) for x in after_gap.values()), 10)
        after_growth = app.update_project_daily(providers(205), when)
        self.assertEqual(sum(x.get("display", 0) for x in after_growth.values()), 15)

    def test_daemon_port_override_is_explicitly_overridable(self):
        with patch.dict(os.environ, {"AGENTCAT_PORT": "9876"}):
            self.assertEqual(app.build_parser().parse_args(["daemon"]).port, 9876)
            self.assertEqual(app.build_parser().parse_args(["daemon", "--port", "8765"]).port, 8765)

    def test_gemini_unterminated_record_has_a_bounded_failure(self):
        path = self.root / "telemetry"
        path.write_text('{"unclosed":"' + 'x' * 2000000)
        with patch.object(app, "GEMINI_TELEMETRY_MAX_RECORD_CHARS", 1024 * 1024), patch.object(
            app, "GEMINI_TELEMETRY_STREAM_CHUNK_BYTES", 1024 * 1024
        ):
            with self.assertRaisesRegex(ValueError, "record_too_large"):
                list(app.iter_gemini_telemetry_payloads(telemetry_path=path))

    def test_pricing_does_not_invent_prices_for_air_or_nested_mini_variants(self):
        table = {"model": {"input": 1.0}}
        self.assertIsNone(app._lookup_pricing("model-air", table))
        self.assertIsNone(app._lookup_pricing("model-2026-mini", table))
        self.assertEqual(app._lookup_pricing("model-20261101", table), table["model"])

    def test_gemini_delta_points_and_sticky_family_do_not_double_count(self):
        def payload(family, amount, end, delta=False):
            attributes = {"session.id": "synthetic", "model": "gemini-test", "type": "input",
                          "gen_ai.response.model": "gemini-test", "gen_ai.token.type": "input"}
            return {"scopeMetrics": [{"metrics": [{"descriptor": {"name": family},
                "aggregationTemporality": 1 if delta else 2,
                "dataPoints": [{"attributes": attributes, "value": amount, "startTime": 1, "endTime": end}]}]}]}
        state = app.empty_gemini_usage_state()
        for value in [payload("gen_ai.client.token.usage", 10, 2, True),
                      payload("gen_ai.client.token.usage", 10, 3, True),
                      payload("gen_ai.client.token.usage", 10, 3, True),
                      payload("gemini_cli.token.usage", 30, 4, False)]:
            app.apply_gemini_records_to_state(state, app.gemini_stream_records(state, value))
        self.assertEqual(state["tokenClasses"]["inputTokens"], 20)

    def test_snapshot_is_compact_but_other_state_stays_readable(self):
        payload = {"x": {"y": 1}}
        app.write_json_atomic(app.LATEST_SNAPSHOT, payload)
        self.assertEqual(app.LATEST_SNAPSHOT.read_text(), '{"x":{"y":1}}\n')
        state = app.AGENTCAT_HOME / "other.json"
        app.write_json_atomic(state, payload)
        self.assertIn('  "x"', state.read_text())


if __name__ == "__main__":
    unittest.main()
