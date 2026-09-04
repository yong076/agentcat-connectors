import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from tests.sandbox import block_network, redirect_module_paths, restore_module_paths


ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_wp10", str(ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_wp10", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


PUBLIC_CAPABILITIES = (
    "snapshot.metadata", "connector.contract.v1", "connector.autoUpdate", "connector.channel",
    "connector.daemon", "connector.daemon.status.v1", "activity.terminal", "activity.memory",
    "activity.runtimeModes", "connector.config", "desktopApps.localMetadata", "events.sqlite",
    "usage.dailyTokens", "usage.hourlyTokens", "usage.breakdown", "usage.projects",
    "usage.coverage", "projects.daily", "homes.discovery", "providerInstances.v1", "pricing.feed",
    "usage.opencode.sqlite", "usage.antigravity.sqlite", "usage.copilot.localLogs",
    "limits.liveCache", "limits.errorBackoff", "limits.quotaFallbackOn429", "limits.codex.oauthUsage",
    "limits.claude.oauthUsage", "limits.claude.statusline", "limits.claude.statuslineQuotas",
    "limits.gemini.codeAssist", "limits.grok.oauthUsage", "limits.kimi.oauthUsage",
    "usage.grok.sessions", "hooks.codexNotify", "hooks.claudeStatusline", "hooks.claudeHooks",
    "hooks.geminiTelemetry", "activity.windowsProcessScan",
)
WP10_CAPABILITIES = (
    "projects.dailyCost", "insights.periods", "insights.burnRate",
    "insights.autoQuota", "insights.recommendation",
)


class SandboxedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.state)
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "AGENTCAT_HOME": str(self.state)}, clear=False)
        self.env.start()
        os.environ.pop("AGENTCAT_OPENAI_KEY", None)

    def tearDown(self):
        self.env.stop()
        restore_module_paths(agentcat, self.old_paths)
        agentcat._PRICING_TABLE_MEMO = None
        self.tmp.cleanup()


class WP10CapabilityTests(SandboxedCase):
    def test_public_and_wp10_capabilities_are_declared_once(self):
        capabilities = agentcat.CONNECTOR_CAPABILITIES
        self.assertEqual(len(capabilities), 45)
        self.assertEqual(len(set(capabilities)), 45)
        for capability in PUBLIC_CAPABILITIES + WP10_CAPABILITIES:
            self.assertIn(capability, capabilities)
        self.assertEqual(capabilities[capabilities.index("projects.daily") + 1:capabilities.index("projects.daily") + 6], WP10_CAPABILITIES)

    def test_requested_engine_definitions_exist(self):
        names = (
            "provider_effective_usd_per_token", "provider_burn_rate", "provider_auto_quota",
            "provider_quota_headroom", "snapshot_recommendation", "codex_usage_breakdown",
            "antigravity_history_days", "antigravity_inference_days", "filter_google_usage_days",
            "load_project_daily_state", "project_daily_cost_snapshot_slice", "read_provider_key",
            "write_provider_key", "command_set_key", "openai_usage_live",
        )
        self.assertTrue(all(callable(getattr(agentcat, name, None)) for name in names))


class WP10ProjectDailyTests(SandboxedCase):
    @staticmethod
    def provider(tokens, model_tokens, path="/work/repo"):
        return {"claude": {"status": "ok", "projects": {"status": "ok", "items": [{
            "id": path, "tokens": tokens, "models": {"claude-sonnet-4-5": {
                "inputTokens": model_tokens, "outputTokens": 0,
                "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                "totalTokens": model_tokens,
            }},
        }]}}}

    def test_v1_ledger_migrates_and_legacy_shape_remains(self):
        today = dt.datetime.now().astimezone().date()
        day = (today - dt.timedelta(days=1)).isoformat()
        agentcat.write_json_atomic(agentcat.project_daily_file(), {day: {"repo": 7}})
        rich = agentcat.load_project_daily()
        self.assertEqual(rich[day]["repo"], {"total": 7, "providers": {}})
        self.assertEqual(agentcat.project_daily_snapshot_slice(rich, today), {day: {"repo": 7}})

    def test_growth_writes_flat_public_and_rich_cost_ledgers(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.assertEqual(agentcat.update_project_daily(self.provider(100, 100), now), {})
        legacy = agentcat.update_project_daily(self.provider(160, 160), now)
        day = agentcat.day_key_for_timestamp(now)
        self.assertEqual(legacy, {day: {"repo": 60}})
        flat = json.loads(agentcat.project_daily_file().read_text())
        self.assertEqual(flat, legacy)
        rich = json.loads(agentcat.project_daily_cost_file().read_text())
        entry = rich["days"][day]["repo"]
        self.assertEqual(entry["total"], 60)
        self.assertEqual(entry["providers"]["claude"]["tokens"], 60)
        self.assertEqual(entry["providers"]["claude"]["topModel"], "claude-sonnet-4-5")
        self.assertIsInstance(entry["providers"]["claude"]["estCostUSD"], float)

    def test_cost_slice_is_thirty_days_and_nullable(self):
        today = dt.date(2026, 9, 4)
        inside = (today - dt.timedelta(days=29)).isoformat()
        outside = (today - dt.timedelta(days=30)).isoformat()
        entry = {"total": 3, "providers": {"codex": {"tokens": 3, "estCostUSD": None, "topModel": "gpt-5", "models": {"gpt-5": 3}}}}
        result = agentcat.project_daily_cost_snapshot_slice({inside: {"x": entry}, outside: {"x": entry}}, today)
        self.assertIn(inside, result)
        self.assertNotIn(outside, result)
        self.assertIsNone(result[inside]["x"]["providers"]["codex"]["estCostUSD"])


class WP10InsightsTests(SandboxedCase):
    def test_burn_auto_quota_and_pricing(self):
        now = dt.datetime.now(dt.timezone.utc).astimezone().replace(minute=30, second=0, microsecond=0)
        provider = {
            "hourlyTokens": {agentcat.hour_key_for_timestamp(now): 3000, agentcat.hour_key_for_timestamp(now - dt.timedelta(hours=1)): 6000},
            "dailyTokens": {(now.date() - dt.timedelta(days=i + 1)).isoformat(): (i + 1) * 1000 for i in range(14)},
            "models": {"claude-sonnet-4-5": {"today": {"inputTokens": 9000, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}}},
        }
        burn = agentcat.provider_burn_rate(provider, now)
        self.assertEqual(burn["basisMinutes"], 90.0)
        self.assertEqual(burn["tokensPerMin"], 100.0)
        self.assertGreater(burn["usdPerHour"], 0)
        self.assertEqual(agentcat.provider_auto_quota(provider, now), {"p90DailyTokens": 13000, "sampleDays": 14, "basis": "p90"})

    def test_binding_quota_and_recommendation_tiebreak(self):
        providers = {
            "alpha": {"status": "ok", "limits": {"quotas": [{"remainingPercent": 80, "resetAt": 20}, {"remainingPercent": 30, "resetAt": 10}]}},
            "beta": {"status": "ok", "limits": {"weeklyUsedPercent": 60, "weeklyResetAt": 30}},
        }
        self.assertEqual(agentcat.provider_quota_headroom(providers["alpha"]["limits"]), (30.0, 10))
        self.assertEqual(agentcat.snapshot_recommendation(providers)["providerId"], "beta")
        self.assertIsNone(agentcat.snapshot_recommendation({"alpha": providers["alpha"]}))

    def test_period_derivation_does_not_mutate_provider(self):
        provider = {"status": "ok", "tokens": {}, "models": {}, "limits": {}}
        snapshot = {"providers": {"claude": provider}}
        for period in ("today", "week", "month", "all"):
            self.assertIn("summary", agentcat.derive_insights(snapshot, period))
        self.assertNotIn("_pc_cost", provider)


class WP10OpenAIKeyTests(SandboxedCase):
    def test_key_roundtrip_permissions_and_cli(self):
        args = argparse.Namespace(provider="openai", key="secret-value", clear=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(agentcat.command_set_key(args), 0)
        self.assertEqual(agentcat.read_provider_key("openai"), "secret-value")
        self.assertEqual(agentcat.AGENTCAT_KEYS_FILE.stat().st_mode & 0o777, 0o600)
        parser_args = agentcat.build_parser().parse_args(["set-key", "--provider", "openai", "--clear"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(parser_args.func(parser_args), 0)
        self.assertIsNone(agentcat.read_provider_key("openai"))

    def test_openai_usage_and_bounded_pagination(self):
        pages = []
        def fake_get(_url, _key, params):
            pages.append(dict(params))
            n = len(pages)
            return {"data": [{"results": [{"input_tokens": 10, "output_tokens": 5, "amount": {"value": .01}}]}], "has_more": True, "next_page": str(n)}
        with patch.object(agentcat, "_openai_get", side_effect=fake_get):
            self.assertEqual(len(list(agentcat._openai_paginate("u", "k", {}, max_pages=3))), 3)
        self.assertEqual(len(pages), 3)

    def test_openai_usage_parses_token_and_cost_buckets(self):
        responses = [
            {"data": [{"results": [{"input_tokens": 100, "output_tokens": 25, "input_cached_tokens": 40}]}]},
            {"data": [{"results": [{"amount": {"value": 0.125}}]}]},
        ]
        with patch.dict(os.environ, {"AGENTCAT_OPENAI_KEY": "fixture-key"}), patch.object(
            agentcat, "_openai_get", side_effect=responses
        ):
            result = agentcat.openai_usage_live()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["tokens"], {"total": 125, "input": 100, "output": 25, "cache_read": 40, "cache_write": 0})
        self.assertEqual(result["cost_usd"], 0.125)

    def test_codex_breakdown_parser_and_no_auth_network_skip(self):
        parsed = agentcat.codex_usage_breakdown_from_response({"units": "credits", "data": [
            {"date": "2026-09-03", "product_surface_usage_values": {"cli": 2, "web": "3"}, "models": [{"model": "gpt-5", "credits": 4}]},
            {"date": "2026-09-04", "product_surface_usage_values": {"cli": 5}},
        ]})
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["bySurface"], {"cli": 7.0, "web": 3.0})
        with patch.object(agentcat, "read_codex_auth", return_value=None), block_network(agentcat):
            self.assertEqual(agentcat.codex_usage_breakdown()["status"], "not_available")

    def test_local_keys_endpoint_never_echoes_secret(self):
        server = agentcat.ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps({"provider": "openai", "key": "secret-http-value"}).encode()
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/keys", data=payload, headers={"Content-Type": "application/json"}, method="POST")
            response = urllib.request.urlopen(request, timeout=2).read().decode()
            self.assertNotIn("secret-http-value", response)
            status = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/keys", timeout=2).read().decode()
            self.assertNotIn("secret-http-value", status)
            self.assertTrue(json.loads(status)["openai"]["configured"])
        finally:
            server.shutdown()
            server.server_close()


class WP10HistoryBaselineTests(SandboxedCase):
    def test_antigravity_history_inference_and_filter(self):
        agentcat.ANTIGRAVITY_CLI_DIR.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        day = agentcat.day_key_for_timestamp(now)
        (agentcat.ANTIGRAVITY_CLI_DIR / "history.jsonl").write_text(json.dumps({"timestamp": now.isoformat()}) + "\nnot-json\n")
        self.assertEqual(agentcat.antigravity_history_days(), {day})
        inferred = agentcat.antigravity_inference_days({"countsByProvider": {"antigravity": 1, "gemini": 0}})
        self.assertIn(day, inferred)
        other = (now.date() - dt.timedelta(days=1)).isoformat()
        usage = {"tokens": {"totalTokens": 30, "inputTokens": 30}, "dailyTokens": {day: 20, other: 10}, "hourlyTokens": {f"{day}T01": 20, f"{other}T01": 10}, "models": {}, "modelDailyTokens": {}}
        filtered = agentcat.filter_google_usage_days(usage, {day}, include=True)
        self.assertEqual(filtered["dailyTokens"], {day: 20})
        self.assertEqual(agentcat._hour_day_key(f"{day}T01"), day)

    def test_model_baselines_reseed_and_reset_without_false_usage(self):
        baseline = {}
        observed = {"m": {"tokens": 100, "classes": {"input": 100, "output": 0, "cacheRead": 0, "cacheWrite": 0}}}
        agentcat._reseed_model_baselines(baseline, observed)
        self.assertEqual(baseline["m"], agentcat._model_baseline_value(observed["m"]))
        reset = {"m": {"tokens": 50, "classes": {"input": 50, "output": 0, "cacheRead": 0, "cacheWrite": 0}}}
        deltas, cost, changed = agentcat._project_model_deltas(baseline, reset)
        self.assertEqual(deltas, {})
        self.assertIsNone(cost)
        self.assertTrue(changed)

    def test_claude_project_model_classes_are_normalized(self):
        models = agentcat._journal_project_item_models({"claude-sonnet-4-5": {"input": 3, "output": 2, "cacheRead": 1, "cacheWrite": 4}})
        self.assertEqual(models["claude-sonnet-4-5"]["totalTokens"], 10)


class WP10SnapshotContractTests(SandboxedCase):
    def test_fixture_snapshot_has_swift_contract_shapes(self):
        now = dt.datetime.now(dt.timezone.utc).astimezone().replace(minute=30, second=0, microsecond=0)
        days = {(now.date() - dt.timedelta(days=i + 1)).isoformat(): 1000 + i for i in range(14)}
        provider = {
            "status": "ok", "tokens": {}, "models": {}, "dailyTokens": days,
            "hourlyTokens": {agentcat.hour_key_for_timestamp(now): 90},
            "projects": {"status": "ok", "items": []}, "limits": {"weeklyUsedPercent": 20},
        }
        providers = {name: dict(provider) for name in ("codex", "claude", "gemini", "antigravity", "opencode", "copilot", "kimi", "grok")}
        snapshot = {"schemaVersion": 4, "connectorVersion": agentcat.CONNECTOR_VERSION, "capabilities": list(agentcat.CONNECTOR_CAPABILITIES), "generatedAt": agentcat.now_iso(), "providers": providers}
        for data in providers.values():
            data["burnRate"] = agentcat.provider_burn_rate(data, now)
            data["autoQuota"] = agentcat.provider_auto_quota(data, now)
        snapshot["insightsByPeriod"] = {period: {"status": "ok", **agentcat.derive_insights(snapshot, period)} for period in ("today", "week", "month", "all")}
        snapshot["insights"] = snapshot["insightsByPeriod"]["week"]
        snapshot["projectsDaily"] = {}
        snapshot["projectsDailyCost"] = {"2026-09-04": {"repo": {"total": 3, "providers": {"codex": {"tokens": 3, "estCostUSD": None, "topModel": "gpt-5"}}}}}
        snapshot["recommendation"] = agentcat.snapshot_recommendation(providers)
        self.assertEqual(set(snapshot["insightsByPeriod"]), {"today", "week", "month", "all"})
        self.assertIsInstance(snapshot["providers"]["codex"]["burnRate"]["tokensPerMin"], float)
        self.assertIsInstance(snapshot["providers"]["codex"]["autoQuota"]["p90DailyTokens"], int)
        self.assertIsNone(snapshot["projectsDailyCost"]["2026-09-04"]["repo"]["providers"]["codex"]["estCostUSD"])
        self.assertIn(snapshot["recommendation"]["providerId"], providers)

    def test_build_snapshot_wires_legacy_and_wp10_top_level_fields(self):
        now = dt.datetime.now(dt.timezone.utc)
        daily = {(now.astimezone().date() - dt.timedelta(days=i + 1)).isoformat(): 100 + i for i in range(14)}
        provider = {
            "status": "ok", "tokens": {}, "models": {}, "dailyTokens": daily,
            "hourlyTokens": {agentcat.hour_key_for_timestamp(now): 60},
            "projects": {"status": "ok", "items": []},
        }
        limits = {name: {"status": "ok", "weeklyUsedPercent": 10 + i} for i, name in enumerate(("codex", "claude", "gemini", "antigravity", "opencode", "copilot", "kimi", "grok"))}
        rich_days = {agentcat.day_key_for_timestamp(now): {"repo": {"total": 2, "providers": {"codex": {"tokens": 2, "estCostUSD": None, "topModel": "gpt-5", "models": {"gpt-5": 2}}}}}}
        with contextlib.ExitStack() as stack:
            replacements = {
                "agentcat_settings": {}, "configured_provider_entries": {}, "latest_events_by_provider": {},
                "configured_limits": {}, "runtime_limits": limits, "terminal_activity_snapshot": {"status": "ok"},
                "desktop_app_sources_snapshot": {}, "auto_update_status_snapshot": {}, "daemon_status_snapshot": {},
                "provider_instances_snapshot": [], "home_discovery_snapshot": {}, "pricing_status_snapshot": {},
                "update_project_daily": {}, "load_project_daily": rich_days,
            }
            for name, value in replacements.items():
                stack.enter_context(patch.object(agentcat, name, return_value=value))
            for name in ("codex_snapshot", "claude_snapshot", "opencode_snapshot", "copilot_snapshot", "kimi_snapshot", "grok_snapshot"):
                stack.enter_context(patch.object(agentcat, name, return_value=dict(provider)))
            stack.enter_context(patch.object(agentcat, "gemini_snapshot", return_value={}))
            stack.enter_context(patch.object(agentcat, "split_google_cli_snapshots", return_value=(dict(provider), dict(provider))))
            stack.enter_context(patch.object(agentcat, "attach_local_usage_coverage"))
            stack.enter_context(patch.object(agentcat, "attach_desktop_app_sources"))
            snapshot = agentcat._build_snapshot_impl()
        self.assertEqual(snapshot["projectsDaily"], {})
        self.assertIn("projectsDailyCost", snapshot)
        self.assertEqual(set(snapshot["insightsByPeriod"]), {"today", "week", "month", "all"})
        self.assertEqual(snapshot["insights"], snapshot["insightsByPeriod"]["week"])
        self.assertIn("recommendation", snapshot)
        self.assertTrue(any("burnRate" in value for value in snapshot["providers"].values()))
        self.assertTrue(any("autoQuota" in value for value in snapshot["providers"].values()))


class WP10LegacyChannelTests(SandboxedCase):
    def test_legacy_pro_state_normalizes_without_private_targets(self):
        agentcat.write_json_atomic(agentcat.update_channel_state_file(), {"channel": "pro", "manifest": {"version": "99.0.0", "downloadUrl": "https://private.invalid/x"}, "targetVersion": "99.0.0"})
        status = agentcat.update_channel_status_snapshot()
        self.assertEqual(status["channel"], "public")
        self.assertEqual(status["status"], "public")
        self.assertEqual(status["installStatus"], "current")
        self.assertNotIn("targetVersion", status)
        self.assertNotIn("manifest", status)

    def test_new_pro_channel_write_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "channel must be public"):
            agentcat.write_update_channel_state("pro", {})


if __name__ == "__main__":
    unittest.main()
