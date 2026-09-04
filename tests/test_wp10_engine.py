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
import urllib.error
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
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "AGENTCAT_HOME": str(self.state),
            "CODEX_HOME": "", "CLAUDE_CONFIG_DIR": "",
        }, clear=False)
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
        self.assertEqual(len(capabilities), 47)
        self.assertEqual(len(set(capabilities)), 47)
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


class WP10CodexBreakdownHardeningTests(SandboxedCase):
    RAW = {
        "units": "credits",
        "data": [
            {
                "date": "2026-09-03",
                "product_surface_usage_values": {"cli": 2, "web": "3.5"},
                "models": [{"model": "gpt-5", "speed": "standard", "credits": 4}],
            },
            {
                "date": "2026-09-04",
                "product_surface_usage_values": {"cli": 5},
                "models": [{"model": "gpt-5", "speed": "standard", "credits": 6}],
            },
        ],
    }

    @classmethod
    def section(cls):
        return agentcat.codex_usage_breakdown_from_response(cls.RAW)

    @staticmethod
    def auth():
        return {"tokens": {"access_token": "access", "refresh_token": "refresh", "account_id": "acct"}}

    def test_parser_handles_garbage_multiple_days_models_and_nonfinite_values(self):
        for raw in (None, "bad", [], {"units": "credits"}):
            self.assertEqual(agentcat.codex_usage_breakdown_from_response(raw)["status"], "not_available")
        empty = agentcat.codex_usage_breakdown_from_response({"data": []})
        self.assertEqual((empty["status"], empty["daily"], empty["byModel"]), ("ok", [], []))
        parsed = self.section()
        self.assertEqual(parsed["bySurface"], {"cli": 7.0, "web": 3.5})
        self.assertEqual(parsed["byModel"], [{"model": "gpt-5", "speed": "standard", "credits": 10.0}])
        self.assertEqual([entry["date"] for entry in parsed["daily"]], ["2026-09-03", "2026-09-04"])
        malformed = agentcat.codex_usage_breakdown_from_response({"data": [{
            "date": "2026-09-05",
            "product_surface_usage_values": {"good": "1.25", "nan": float("nan"), "inf": "Infinity", "huge": 10 ** 1000, "junk": "no"},
            "models": [
                {"model": "good", "credits": "2.5"},
                {"model": "nan", "credits": "NaN"},
                {"model": "inf", "credits": float("inf")},
                "junk",
            ],
        }, "junk"]})
        self.assertEqual(malformed["bySurface"], {"good": 1.25})
        self.assertEqual(malformed["byModel"], [{"model": "good", "speed": None, "credits": 2.5}])
        json.dumps(malformed, allow_nan=False)

    def test_fresh_cache_skips_auth_and_stale_cache_survives_refresh_failure(self):
        section = self.section()
        agentcat.write_json_atomic(agentcat.CODEX_USAGE_BREAKDOWN_CACHE, {
            "fetched_at": int(agentcat.time.time()), "data": section,
        })
        with patch.object(agentcat, "read_codex_auth", side_effect=AssertionError("fresh cache must skip auth")):
            self.assertEqual(agentcat.codex_usage_breakdown(), section)
        agentcat.write_json_atomic(agentcat.CODEX_USAGE_BREAKDOWN_CACHE, {"fetched_at": 1, "data": section})
        with patch.object(agentcat, "read_codex_auth", return_value=None), block_network(agentcat):
            stale = agentcat.codex_usage_breakdown()
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["bySurface"], section["bySurface"])

    def test_successful_refresh_writes_strict_cache(self):
        with patch.object(agentcat, "read_codex_auth", return_value=self.auth()), patch.object(
            agentcat, "codex_usage_breakdown_request", return_value=self.RAW
        ):
            result = agentcat.codex_usage_breakdown()
        cached = json.loads(agentcat.CODEX_USAGE_BREAKDOWN_CACHE.read_text())
        self.assertEqual(result["status"], "ok")
        self.assertIsInstance(cached["fetched_at"], int)
        self.assertEqual(cached["data"], result)
        json.dumps(cached, allow_nan=False)

    def test_schema_drift_preserves_stale_good_cache_and_empty_data_is_cacheable(self):
        section = self.section()
        original = {"fetched_at": 1, "data": section}
        agentcat.write_json_atomic(agentcat.CODEX_USAGE_BREAKDOWN_CACHE, original)
        with patch.object(agentcat, "read_codex_auth", return_value=self.auth()), patch.object(
            agentcat, "codex_usage_breakdown_request", return_value={"unexpected": "shape"}
        ):
            result = agentcat.codex_usage_breakdown()
        self.assertTrue(result["stale"])
        self.assertEqual(result["bySurface"], section["bySurface"])
        self.assertEqual(json.loads(agentcat.CODEX_USAGE_BREAKDOWN_CACHE.read_text()), original)

        agentcat.CODEX_USAGE_BREAKDOWN_CACHE.unlink()
        with patch.object(agentcat, "read_codex_auth", return_value=self.auth()), patch.object(
            agentcat, "codex_usage_breakdown_request", return_value={"units": "credits", "data": []}
        ):
            empty = agentcat.codex_usage_breakdown()
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["daily"], [])
        self.assertEqual(json.loads(agentcat.CODEX_USAGE_BREAKDOWN_CACHE.read_text())["data"], empty)

    def test_malformed_cache_timestamp_and_data_never_raise_or_leak_nonfinite(self):
        section = self.section()
        for malformed_timestamp in ("not-a-time", None, True, float("nan"), float("inf")):
            agentcat.write_json_atomic(agentcat.CODEX_USAGE_BREAKDOWN_CACHE, {
                "fetched_at": malformed_timestamp, "data": section,
            })
            with patch.object(agentcat, "read_codex_auth", return_value=None):
                result = agentcat.codex_usage_breakdown()
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["stale"])
        for malformed_data in (
            "bad", {"status": "ok"},
            {**section, "bySurface": {"cli": float("nan")}},
            {**section, "bySurface": {"cli": 10 ** 1000}},
            {**section, "daily": [{"date": "d", "surfaces": {"cli": float("inf")}}]},
        ):
            agentcat.write_json_atomic(agentcat.CODEX_USAGE_BREAKDOWN_CACHE, {
                "fetched_at": int(agentcat.time.time()), "data": malformed_data,
            })
            with patch.object(agentcat, "read_codex_auth", return_value=None):
                result = agentcat.codex_usage_breakdown()
            self.assertEqual(result, agentcat.empty_codex_usage_breakdown())
            json.dumps(result, allow_nan=False)

    def test_401_refreshes_exactly_once_and_second_401_fails_soft(self):
        error = urllib.error.HTTPError("u", 401, "Unauthorized", None, None)
        with patch.object(agentcat, "read_codex_auth", return_value=self.auth()), patch.object(
            agentcat, "codex_usage_breakdown_request", side_effect=[error, self.RAW]
        ) as request, patch.object(agentcat, "refresh_codex_access_token", return_value="fresh") as refresh:
            result = agentcat.codex_usage_breakdown()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.call_count, 2)
        refresh.assert_called_once()
        error.close()

        agentcat.CODEX_USAGE_BREAKDOWN_CACHE.unlink()
        error_one = urllib.error.HTTPError("u", 401, "Unauthorized", None, None)
        error_two = urllib.error.HTTPError("u", 401, "Unauthorized", None, None)
        with patch.object(agentcat, "read_codex_auth", return_value=self.auth()), patch.object(
            agentcat, "codex_usage_breakdown_request", side_effect=[error_one, error_two]
        ) as request, patch.object(agentcat, "refresh_codex_access_token", return_value="fresh") as refresh:
            result = agentcat.codex_usage_breakdown()
        self.assertEqual(result["status"], "not_available")
        self.assertEqual(request.call_count, 2)
        refresh.assert_called_once()
        error_one.close()
        error_two.close()

    def test_403_timeout_and_no_auth_fail_soft_without_unwanted_refresh(self):
        forbidden = urllib.error.HTTPError("u", 403, "Forbidden", None, None)
        for failure in (forbidden, TimeoutError("timed out")):
            with patch.object(agentcat, "read_codex_auth", return_value=self.auth()), patch.object(
                agentcat, "codex_usage_breakdown_request", side_effect=failure
            ), patch.object(agentcat, "refresh_codex_access_token", side_effect=AssertionError("must not refresh")):
                self.assertEqual(agentcat.codex_usage_breakdown()["status"], "not_available")
        forbidden.close()
        with patch.object(agentcat, "read_codex_auth", return_value=None), patch.object(
            agentcat, "codex_usage_breakdown_request", side_effect=AssertionError("no auth must skip network")
        ):
            self.assertEqual(agentcat.codex_usage_breakdown()["status"], "not_available")

    def test_sqlite_and_jsonl_attachment_preserve_parent_fields_when_breakdown_raises(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir()
        database = codex_dir / "state_fixture.sqlite"
        import sqlite3
        connection = sqlite3.connect(database)
        try:
            connection.execute("create table threads(tokens_used integer, model text, updated_at text, cwd text)")
            connection.execute("insert into threads values (10, 'gpt-test', ?, '/work/repo')", (agentcat.now_iso(),))
            connection.commit()
        finally:
            connection.close()
        with patch.object(agentcat, "codex_state_sqlite_paths", return_value=[database]), patch.object(
            agentcat, "codex_usage_breakdown", side_effect=RuntimeError("optional failed")
        ):
            sqlite_snapshot = agentcat.codex_sqlite_snapshot()
        self.assertEqual(sqlite_snapshot["status"], "ok")
        self.assertEqual(sqlite_snapshot["tokens"]["all"], 10)
        self.assertEqual(sqlite_snapshot["codexUsageBreakdown"]["status"], "not_available")

        sessions = {
            "status": "ok", "source": "fixture-jsonl", "tokens": {"all": 7},
            "models": {"gpt-test": {"all": 7}}, "breakdown": {"status": "ok", "chat": 1},
        }
        with patch.object(agentcat, "codex_sessions_snapshot", return_value=sessions), patch.object(
            agentcat, "codex_sqlite_snapshot", return_value={"status": "not_found", "tokens": {"all": 0}}
        ), patch.object(agentcat, "_codex_sessions_need_rebuild", return_value=False), patch.object(
            agentcat, "codexbar_cost_cache_snapshot", return_value={"status": "not_found", "tokens": {"all": 0}}
        ), patch.object(agentcat, "merge_provider_with_usage_floor", side_effect=lambda primary, *_args, **_kwargs: primary), patch.object(
            agentcat, "attach_codex_usage_coverage", side_effect=lambda result, *_args: result
        ), patch.object(agentcat, "codex_usage_breakdown", side_effect=RuntimeError("optional failed")):
            jsonl_snapshot = agentcat.codex_snapshot()
        self.assertEqual(jsonl_snapshot["status"], "ok")
        self.assertEqual(jsonl_snapshot["tokens"], {"all": 7})
        self.assertEqual(jsonl_snapshot["models"], {"gpt-test": {"all": 7}})
        self.assertEqual(jsonl_snapshot["breakdown"], {"status": "ok", "chat": 1})
        self.assertEqual(jsonl_snapshot["codexUsageBreakdown"]["status"], "not_available")


class WP10KeyHTTPTests(SandboxedCase):
    @contextlib.contextmanager
    def server_url(self):
        server = agentcat.ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    @staticmethod
    def request(request):
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def test_text_plain_and_foreign_browser_posts_are_rejected_without_mutation(self):
        with self.server_url() as url:
            body = json.dumps({"provider": "openai", "key": "attacker-secret"}).encode()
            text_request = urllib.request.Request(
                f"{url}/v1/keys", data=body, method="POST", headers={"Content-Type": "text/plain"},
            )
            status, response = self.request(text_request)
            self.assertEqual(status, 415)
            self.assertNotIn("attacker-secret", json.dumps(response))
            self.assertIsNone(agentcat.read_provider_key("openai"))

            for headers in (
                {"Content-Type": "application/json", "Origin": "https://attacker.example"},
                {"Content-Type": "application/json", "Origin": "http://localhost:not-a-port"},
                {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
            ):
                request = urllib.request.Request(f"{url}/v1/keys", data=body, method="POST", headers=headers)
                status, response = self.request(request)
                self.assertEqual(status, 403)
                self.assertNotIn("attacker-secret", json.dumps(response))
                self.assertIsNone(agentcat.read_provider_key("openai"))

    def test_native_and_local_origin_json_can_set_clear_and_status_never_echoes(self):
        secret = "native-secret-value"
        with self.server_url() as url:
            set_request = urllib.request.Request(
                f"{url}/v1/keys",
                data=json.dumps({"provider": "openai", "key": secret}).encode(),
                method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            status, set_response = self.request(set_request)
            self.assertEqual(status, 200)
            self.assertTrue(set_response["openai"]["configured"])
            self.assertNotIn(secret, json.dumps(set_response))
            self.assertEqual(agentcat.read_provider_key("openai"), secret)

            status, get_response = self.request(urllib.request.Request(f"{url}/v1/keys"))
            self.assertEqual(status, 200)
            self.assertEqual(get_response, {"openai": {"configured": True}})
            self.assertNotIn(secret, json.dumps(get_response))

            clear_request = urllib.request.Request(
                f"{url}/v1/keys",
                data=json.dumps({"provider": "openai", "clear": True}).encode(),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://localhost:{url.rsplit(':', 1)[1]}",
                    "Sec-Fetch-Site": "same-site",
                },
            )
            status, clear_response = self.request(clear_request)
            self.assertEqual(status, 200)
            self.assertFalse(clear_response["openai"]["configured"])
            self.assertIsNone(agentcat.read_provider_key("openai"))


class WP10ProjectDailyEdgeTests(SandboxedCase):
    @staticmethod
    def providers(model, total, input_tokens, output_tokens, path="/work/repo"):
        models = {model: {
            "inputTokens": input_tokens, "outputTokens": output_tokens,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
            "totalTokens": total,
        }}
        return {"claude": {"status": "ok", "projects": {"status": "ok", "items": [{
            "id": path, "path": path, "tokens": total, "models": models,
        }]}}}

    def test_v1_migration_books_growth_end_to_end_without_losing_history(self):
        now = dt.datetime.now(dt.timezone.utc)
        today = agentcat.day_key_for_timestamp(now)
        yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
        key = "claude|/work/repo"
        agentcat.write_json_atomic(agentcat.project_daily_file(), {yesterday: {"repo": 41}})
        agentcat.write_json_atomic(agentcat.project_daily_state_file(), {"baselines": {key: 100}})
        legacy = agentcat.update_project_daily(self.providers("claude-sonnet-4-5", 130, 130, 0), now)
        self.assertEqual(legacy, {yesterday: {"repo": 41}, today: {"repo": 30}})
        rich = agentcat.load_project_daily()
        self.assertEqual(rich[yesterday]["repo"], {"total": 41, "providers": {}})
        self.assertEqual(rich[today]["repo"]["total"], 30)

    def test_class_split_is_priced_but_classless_model_remains_nullable(self):
        now = dt.datetime.now(dt.timezone.utc)
        day = agentcat.day_key_for_timestamp(now)
        agentcat.update_project_daily(self.providers("claude-sonnet-4-5", 2_000_000, 1_000_000, 1_000_000), now)
        agentcat.update_project_daily(self.providers("claude-sonnet-4-5", 4_000_000, 2_000_000, 2_000_000), now)
        rich = agentcat.load_project_daily()[day]["repo"]["providers"]["claude"]
        self.assertGreater(rich["estCostUSD"], 0)
        self.assertEqual(rich["tokens"], 2_000_000)

        other_path = "/work/classless"
        classless = {"codex": {"status": "ok", "projects": {"status": "ok", "items": [{
            "id": other_path, "tokens": 100, "models": {"gpt-5": 100},
        }]}}}
        agentcat.update_project_daily(classless, now)
        classless["codex"]["projects"]["items"][0].update(tokens=160, models={"gpt-5": 160})
        agentcat.update_project_daily(classless, now)
        codex = agentcat.load_project_daily()[day]["classless"]["providers"]["codex"]
        self.assertIsNone(codex["estCostUSD"])
        self.assertEqual(codex["topModel"], "gpt-5")

    def test_class_counter_reset_rebases_model_then_resumes_without_false_mix_or_cost(self):
        now = dt.datetime.now(dt.timezone.utc)
        day = agentcat.day_key_for_timestamp(now)
        agentcat.update_project_daily(self.providers("claude-sonnet-4-5", 100, 90, 10), now)
        # Aggregate grows, but input shrinks: provider growth may book while
        # model attribution must rebase and remain absent.
        agentcat.update_project_daily(self.providers("claude-sonnet-4-5", 110, 80, 30), now)
        reset_entry = agentcat.load_project_daily()[day]["repo"]["providers"]["claude"]
        self.assertEqual(reset_entry["tokens"], 10)
        self.assertEqual(reset_entry["models"], {})
        self.assertIsNone(reset_entry["topModel"])
        self.assertIsNone(reset_entry["estCostUSD"])
        agentcat.update_project_daily(self.providers("claude-sonnet-4-5", 130, 100, 30), now)
        resumed = agentcat.load_project_daily()[day]["repo"]["providers"]["claude"]
        self.assertEqual(resumed["tokens"], 30)
        self.assertEqual(resumed["models"], {"claude-sonnet-4-5": 20})
        self.assertEqual(resumed["topModel"], "claude-sonnet-4-5")
        self.assertGreater(resumed["estCostUSD"], 0)


class WP10InsightEdgeTests(SandboxedCase):
    @staticmethod
    def quota(remaining, reset, status="ok"):
        return {"status": status, "limits": {"quotas": [{"remainingPercent": remaining, "resetAt": reset}]}}

    def test_burn_absent_without_activity_and_omits_unreliable_pricing(self):
        now = dt.datetime.now(dt.timezone.utc).astimezone().replace(minute=20, second=0, microsecond=0)
        self.assertIsNone(agentcat.provider_burn_rate({"hourlyTokens": {}}, now))
        old = {agentcat.hour_key_for_timestamp(now - dt.timedelta(hours=4)): 100}
        self.assertIsNone(agentcat.provider_burn_rate({"hourlyTokens": old}, now))
        classless = {
            "hourlyTokens": {agentcat.hour_key_for_timestamp(now): 100},
            "models": {"gpt-5": {"today": 100, "all": 100}},
        }
        self.assertNotIn("usdPerHour", agentcat.provider_burn_rate(classless, now))
        unpriced = {
            "hourlyTokens": {agentcat.hour_key_for_timestamp(now): 100},
            "models": {"unknown-model": {"today": {"inputTokens": 100, "outputTokens": 0}}},
        }
        self.assertNotIn("usdPerHour", agentcat.provider_burn_rate(unpriced, now))

    def test_auto_quota_requires_fourteen_complete_days(self):
        now = dt.datetime.now(dt.timezone.utc)
        history = {(now.astimezone().date() - dt.timedelta(days=i + 1)).isoformat(): i + 1 for i in range(13)}
        self.assertIsNone(agentcat.provider_auto_quota({"dailyTokens": history}, now))

    def test_equal_headroom_uses_real_reset_epoch_and_recommendation_can_be_absent(self):
        providers = {
            "later": self.quota(50, 2_000_000_000),
            "sooner": self.quota(50, 1_900_000_000),
        }
        self.assertEqual(agentcat.snapshot_recommendation(providers), {
            "providerId": "sooner", "reason": "resets_soonest", "confidence": "heuristic",
        })
        self.assertIsNone(agentcat.snapshot_recommendation({"only": providers["later"]}))
        self.assertIsNone(agentcat.snapshot_recommendation({
            "a": {"status": "not_found", "limits": {}}, "b": {"status": "ok", "limits": {}},
        }))

    def test_all_periods_are_attempted_and_one_error_is_isolated(self):
        provider = {"status": "ok", "tokens": {}, "models": {}, "projects": {"status": "ok", "items": []}}
        replacements = {
            "agentcat_settings": {}, "configured_provider_entries": {}, "latest_events_by_provider": {},
            "configured_limits": {}, "runtime_limits": {}, "terminal_activity_snapshot": {"status": "ok"},
            "desktop_app_sources_snapshot": {}, "auto_update_status_snapshot": {}, "daemon_status_snapshot": {},
            "provider_instances_snapshot": [], "home_discovery_snapshot": {}, "pricing_status_snapshot": {},
            "update_project_daily": {}, "load_project_daily": {},
        }
        periods = []
        def derive(_snapshot, period):
            periods.append(period)
            if period == "month":
                raise RuntimeError("month fixture failed")
            return {"summary": {"period": period}, "providers": [], "models": [], "findings": [], "pricing_status": "unknown"}
        with contextlib.ExitStack() as stack:
            for name, value in replacements.items():
                stack.enter_context(patch.object(agentcat, name, return_value=value))
            for name in ("codex_snapshot", "claude_snapshot", "opencode_snapshot", "copilot_snapshot", "kimi_snapshot", "grok_snapshot"):
                stack.enter_context(patch.object(agentcat, name, return_value=dict(provider)))
            stack.enter_context(patch.object(agentcat, "gemini_snapshot", return_value={}))
            stack.enter_context(patch.object(agentcat, "split_google_cli_snapshots", return_value=(dict(provider), dict(provider))))
            stack.enter_context(patch.object(agentcat, "attach_local_usage_coverage"))
            stack.enter_context(patch.object(agentcat, "attach_desktop_app_sources"))
            stack.enter_context(patch.object(agentcat, "derive_insights", side_effect=derive))
            snapshot = agentcat._build_snapshot_impl()
        self.assertEqual(periods, ["today", "week", "month", "all"])
        self.assertEqual(snapshot["insightsByPeriod"]["month"]["status"], "error")
        for period in ("today", "week", "all"):
            self.assertEqual(snapshot["insightsByPeriod"][period]["status"], "ok")
        self.assertEqual(snapshot["insights"], snapshot["insightsByPeriod"]["week"])


class WP10OpenAIEdgeTests(SandboxedCase):
    def test_no_key_reports_reason_and_fetch_includes_openai_only_when_configured(self):
        with block_network(agentcat):
            result = agentcat.openai_usage_live()
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["reason"], "no_api_key")
        limit_functions = (
            "claude_live_limits", "codex_live_limits", "gemini_live_limits", "antigravity_live_limits",
            "grok_live_limits", "kimi_live_limits",
        )
        with contextlib.ExitStack() as stack:
            for name in limit_functions:
                stack.enter_context(patch.object(agentcat, name, return_value={"status": "fixture"}))
            stack.enter_context(patch.object(agentcat, "read_provider_key", return_value=None))
            without = agentcat.fetch_llm_usage_fresh()
        self.assertNotIn("openai", without["providers"])
        with contextlib.ExitStack() as stack:
            for name in limit_functions:
                stack.enter_context(patch.object(agentcat, name, return_value={"status": "fixture"}))
            stack.enter_context(patch.object(agentcat, "read_provider_key", return_value="key"))
            stack.enter_context(patch.object(agentcat, "openai_usage_live", return_value={"status": "ok", "source": "fixture"}))
            with_key = agentcat.fetch_llm_usage_fresh()
        self.assertEqual(with_key["providers"]["openai"], {"status": "ok", "source": "fixture"})
        self.assertIn("grok", with_key["providers"])
        self.assertIn("kimi", with_key["providers"])

    def test_pagination_stops_on_server_end_and_forwards_next_page(self):
        responses = [
            {"data": [{"results": [{"id": 1}]}], "has_more": True, "next_page": "next"},
            {"data": [{"results": [{"id": 2}]}], "has_more": False},
        ]
        with patch.object(agentcat, "_openai_get", side_effect=responses) as get:
            self.assertEqual(list(agentcat._openai_paginate("u", "k", {"limit": 1}, max_pages=6)), [{"id": 1}, {"id": 2}])
        self.assertEqual(get.call_args_list[1].args[2]["page"], "next")
        self.assertEqual(get.call_count, 2)


class WP10AntigravityAttributionTests(SandboxedCase):
    @staticmethod
    def usage(by_day, model="gemini-test"):
        total = sum(by_day.values())
        return {
            "tokens": {"totalTokens": total, "inputTokens": total, "all": total},
            "models": {model: {"totalTokens": total, "inputTokens": total, "all": total}},
            "dailyTokens": dict(by_day),
            "hourlyTokens": {f"{day}T01": amount for day, amount in by_day.items()},
            "modelDailyTokens": {model: dict(by_day)},
            "events": len(by_day), "cache": {"source": "fixture"},
        }

    @staticmethod
    def snapshot(common, dedicated=None):
        return {
            "status": "ok", "source": "combined", "tokens": dict(common["tokens"]),
            "models": dict(common["models"]), "dailyTokens": dict(common["dailyTokens"]),
            "hourlyTokens": dict(common["hourlyTokens"]), "events": common["events"],
            "sources": {
                "geminiCli": {"installed": True, "telemetryConfigured": False, "source": "/fixture/gemini.log"},
                "antigravityCli": {"installed": True, "source": "/fixture/antigravity.log"},
            },
            "_sourceUsages": {"geminiCli": common, "antigravityCli": dedicated},
        }

    def test_history_days_split_common_usage_once_and_mark_inference(self):
        ag_day, gemini_day = "2026-09-03", "2026-09-04"
        common = self.usage({ag_day: 100, gemini_day: 200})
        with patch.object(agentcat, "antigravity_sqlite_usage", return_value=None), patch.object(
            agentcat, "antigravity_inference_days", return_value={ag_day}
        ):
            gemini, antigravity = agentcat.split_google_cli_snapshots(self.snapshot(common), {"countsByProvider": {}})
        self.assertEqual(gemini["tokens"]["totalTokens"], 200)
        self.assertEqual(gemini["dailyTokens"], {gemini_day: 200})
        self.assertEqual(antigravity["tokens"]["totalTokens"], 100)
        self.assertEqual(antigravity["dailyTokens"], {ag_day: 100})
        self.assertEqual(antigravity["sourceAttribution"], "history-day-inference")
        source = antigravity["sources"]["antigravityCli"]
        self.assertEqual(source["status"], "inferred_from_history")
        self.assertEqual(source["collectionMethod"], "history_day_inference")
        self.assertEqual(source["source"], "/fixture/gemini.log")
        self.assertEqual(gemini["tokens"]["totalTokens"] + antigravity["tokens"]["totalTokens"], 300)

    def test_dedicated_telemetry_wins_without_filtering_common_usage(self):
        common = self.usage({"2026-09-03": 300})
        dedicated = self.usage({"2026-09-03": 50}, model="antigravity-model")
        with patch.object(agentcat, "antigravity_sqlite_usage", side_effect=AssertionError("dedicated must win")), patch.object(
            agentcat, "antigravity_inference_days", side_effect=AssertionError("dedicated must skip inference")
        ):
            gemini, antigravity = agentcat.split_google_cli_snapshots(self.snapshot(common, dedicated), None)
        self.assertEqual(gemini["tokens"]["totalTokens"], 300)
        self.assertEqual(antigravity["tokens"]["totalTokens"], 50)
        self.assertEqual(antigravity["sourceAttribution"], "dedicated-telemetry")
        self.assertEqual(antigravity["sources"]["antigravityCli"]["collectionMethod"], "dedicated_otel_token_metrics")

    def test_configured_gemini_stream_is_never_relabelled_from_history(self):
        common = self.usage({"2026-09-03": 300})
        snapshot = self.snapshot(common)
        snapshot["sources"]["geminiCli"]["telemetryConfigured"] = True
        with patch.object(agentcat, "antigravity_sqlite_usage", return_value=None), patch.object(
            agentcat, "antigravity_inference_days", return_value={"2026-09-03"}
        ):
            gemini, antigravity = agentcat.split_google_cli_snapshots(snapshot, None)
        self.assertEqual(gemini["tokens"]["totalTokens"], 300)
        self.assertEqual(antigravity["tokens"], {})
        self.assertEqual(antigravity["sourceAttribution"], "no-antigravity-telemetry")

    def test_sqlite_wins_and_uncorroborated_common_usage_stays_gemini(self):
        common = self.usage({"2026-09-03": 300})
        sqlite_usage = self.usage({"2026-09-02": 40}, model="antigravity-model")
        with patch.object(agentcat, "antigravity_sqlite_usage", return_value=sqlite_usage), patch.object(
            agentcat, "antigravity_inference_days", side_effect=AssertionError("sqlite must skip inference")
        ):
            gemini, antigravity = agentcat.split_google_cli_snapshots(self.snapshot(common), None)
        self.assertEqual(gemini["tokens"]["totalTokens"], 300)
        self.assertEqual(antigravity["tokens"]["totalTokens"], 40)
        self.assertEqual(antigravity["sourceAttribution"], "local-sqlite-trajectory")

        with patch.object(agentcat, "antigravity_sqlite_usage", return_value=None), patch.object(
            agentcat, "antigravity_inference_days", return_value=set()
        ):
            gemini, antigravity = agentcat.split_google_cli_snapshots(self.snapshot(common), None)
        self.assertEqual(gemini["tokens"]["totalTokens"], 300)
        self.assertEqual(antigravity["tokens"], {})
        self.assertEqual(antigravity["sourceAttribution"], "no-antigravity-telemetry")


class WP10SwiftTypeTests(SandboxedCase):
    def test_emitted_optional_fields_match_required_swift_names_and_types(self):
        now = dt.datetime.now(dt.timezone.utc).astimezone().replace(minute=30, second=0, microsecond=0)
        daily = {(now.date() - dt.timedelta(days=i + 1)).isoformat(): 1000 + i for i in range(14)}
        provider = {
            "hourlyTokens": {agentcat.hour_key_for_timestamp(now): 9000}, "dailyTokens": daily,
            "models": {"claude-sonnet-4-5": {"today": {"inputTokens": 9000, "outputTokens": 0}}},
        }
        burn = agentcat.provider_burn_rate(provider, now)
        auto = agentcat.provider_auto_quota(provider, now)
        recommendation = agentcat.snapshot_recommendation({
            "codex": {"status": "ok", "limits": {"weeklyUsedPercent": 10}},
            "claude": {"status": "ok", "limits": {"weeklyUsedPercent": 20}},
        })
        self.assertEqual(set(burn), {"tokensPerMin", "basisMinutes", "usdPerHour"})
        self.assertIsInstance(burn["tokensPerMin"], float)
        self.assertIsInstance(burn["basisMinutes"], float)
        self.assertIsInstance(burn["usdPerHour"], float)
        self.assertEqual(set(auto), {"p90DailyTokens", "sampleDays", "basis"})
        self.assertIs(type(auto["p90DailyTokens"]), int)
        self.assertIs(type(auto["sampleDays"]), int)
        self.assertIs(type(auto["basis"]), str)
        self.assertEqual(set(recommendation), {"providerId", "reason", "confidence"})
        self.assertTrue(all(isinstance(value, str) for value in recommendation.values()))

        breakdown = agentcat.codex_usage_breakdown_from_response({"data": [{
            "date": "2026-09-04", "product_surface_usage_values": {"cli": 1},
        }]})
        self.assertIsInstance(breakdown["bySurface"], dict)
        self.assertTrue(all(isinstance(value, float) for value in breakdown["bySurface"].values()))
        period = {"status": "ok", **agentcat.derive_insights({"providers": {}}, "week")}
        self.assertIsInstance(period["status"], str)
        for key in ("summary", "providers", "models", "findings", "pricing_status"):
            self.assertIn(key, period)
        project = agentcat.project_daily_cost_snapshot_slice({"2026-09-04": {"repo": {
            "total": 3, "providers": {"codex": {
                "tokens": 3, "estCostUSD": None, "topModel": "gpt-5", "models": {"gpt-5": 3},
            }},
        }}}, dt.date(2026, 9, 4))
        entry = project["2026-09-04"]["repo"]
        self.assertIs(type(entry["total"]), int)
        self.assertEqual(set(entry["providers"]["codex"]), {"tokens", "estCostUSD", "topModel"})
        self.assertIs(type(entry["providers"]["codex"]["tokens"]), int)
        self.assertIsNone(entry["providers"]["codex"]["estCostUSD"])
        required_snapshot = {"generatedAt": agentcat.now_iso(), "providers": {}}
        self.assertIsInstance(required_snapshot["generatedAt"], str)
        self.assertIsInstance(required_snapshot["providers"], dict)


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
