import importlib.util
import datetime as dt
import json
import os
import tempfile
import time
import unittest
import urllib.parse
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from tests.sandbox import redirect_module_paths, restore_module_paths


ROOT = Path(__file__).resolve().parents[1]
LOADER = SourceFileLoader("agentcat_wp18", str(ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_wp18", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


class SandboxedCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.old_paths = redirect_module_paths(agentcat, self.home, self.state)
        self.env = patch.dict(
            os.environ,
            {"HOME": str(self.home), "AGENTCAT_HOME": str(self.state)},
            clear=False,
        )
        self.env.start()
        for key in ("GROK_HOME", "XAI_HOME", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        self.env.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()


class GrokAccountingTests(SandboxedCase):
    def test_total_tokens_is_authoritative_and_breakdowns_are_informational(self) -> None:
        metrics = agentcat.grok_usage_metrics(
            {
                "inputTokens": 100,
                "outputTokens": 20,
                "cachedReadTokens": 90,
                "reasoningTokens": 15,
                "totalTokens": 120,
            }
        )

        self.assertEqual(metrics["totalTokens"], 120)
        self.assertEqual(agentcat.usage_token_total(metrics), 120)
        self.assertEqual(metrics["cacheReadInputTokens"], 90)
        self.assertEqual(metrics["reasoningTokens"], 15)

    def test_missing_total_falls_back_to_input_plus_output_only(self) -> None:
        metrics = agentcat.grok_usage_metrics(
            {
                "input_tokens": 70,
                "output_tokens": 30,
                "cached_read_tokens": 50,
                "reasoning_tokens": 10,
            }
        )

        self.assertEqual(metrics["totalTokens"], 100)
        self.assertEqual(agentcat.usage_token_total(metrics), 100)

    def test_explicit_zero_total_is_authoritative(self) -> None:
        metrics = agentcat.grok_usage_metrics(
            {"inputTokens": 70, "cachedReadTokens": 50, "totalTokens": 0}
        )

        self.assertEqual(metrics, {})

    def test_grok_accounting_revision_reseeds_only_grok_baselines(self) -> None:
        for old_revision in (None, agentcat.GROK_ACCOUNTING_STATE_VERSION - 1):
            with self.subTest(old_revision=old_revision):
                state = {
                    # A shared schema revision must not decide which provider
                    # baselines survive a Grok-only accounting correction.
                    "version": agentcat.PROJECT_DAILY_STATE_VERSION + 1,
                    "baselines": {
                        "grok|/tmp/grok-project": 983_087_448,
                        "claude|/tmp/claude-project": 700,
                        "codex|/tmp/codex-project": 800,
                    },
                    "modelBaselines": {
                        "grok|/tmp/grok-project": {
                            "grok-fixture": {"tokens": 983_087_448, "classes": None}
                        },
                        "claude|/tmp/claude-project": {
                            "claude-fixture": {"tokens": 700, "classes": None}
                        },
                    },
                }
                if old_revision is not None:
                    state["grokAccountingVersion"] = old_revision
                agentcat.project_daily_state_file().write_text(
                    json.dumps(state), encoding="utf-8"
                )

                loaded = agentcat.load_project_daily_state()

                self.assertNotIn("grok|/tmp/grok-project", loaded["baselines"])
                self.assertNotIn("grok|/tmp/grok-project", loaded["modelBaselines"])
                self.assertEqual(loaded["baselines"]["claude|/tmp/claude-project"], 700)
                self.assertEqual(loaded["baselines"]["codex|/tmp/codex-project"], 800)
                self.assertIn("claude|/tmp/claude-project", loaded["modelBaselines"])

                agentcat.update_project_daily(
                    {
                        "grok": {
                            "projects": {
                                "status": "ok",
                                "items": [{"id": "/tmp/grok-project", "tokens": 220}],
                            }
                        }
                    }
                )
                persisted = json.loads(
                    agentcat.project_daily_state_file().read_text(encoding="utf-8")
                )
                self.assertEqual(
                    persisted["grokAccountingVersion"],
                    agentcat.GROK_ACCOUNTING_STATE_VERSION,
                )
                self.assertEqual(persisted["baselines"]["grok|/tmp/grok-project"], 220)
                self.assertEqual(persisted["baselines"]["claude|/tmp/claude-project"], 700)
                self.assertEqual(persisted["baselines"]["codex|/tmp/codex-project"], 800)
                self.assertIn(
                    "claude|/tmp/claude-project", persisted["modelBaselines"]
                )

    def test_grok_snapshot_rebuilds_corrected_history_from_raw_sessions(self) -> None:
        project_path = "/tmp/Grok Project"
        encoded_project = urllib.parse.quote(project_path, safe="")
        session_dir = self.home / ".grok" / "sessions" / encoded_project / "session-a"
        session_dir.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc)
        events = [
            {
                "params": {
                    "_meta": {
                        "agentTimestampMs": int(now.timestamp() * 1000),
                        "promptId": "p1",
                    },
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": 100,
                            "outputTokens": 20,
                            "cachedReadTokens": 90,
                            "reasoningTokens": 15,
                            "totalTokens": 120,
                            "modelUsage": {"grok-fixture": {"totalTokens": 120}},
                        },
                    },
                }
            },
            {
                "params": {
                    "_meta": {
                        "agentTimestampMs": int(now.timestamp() * 1000),
                        "promptId": "p2",
                    },
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": 70,
                            "outputTokens": 30,
                            "cachedReadTokens": 50,
                            "reasoningTokens": 10,
                            "modelUsage": {"grok-fixture": {"totalTokens": 100}},
                        },
                    },
                }
            },
        ]
        (session_dir / "updates.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        snapshot = agentcat.grok_snapshot()

        today = agentcat.day_key_for_timestamp(now)
        self.assertEqual(snapshot["tokens"]["all"], 220)
        self.assertEqual(snapshot["tokens"]["totalTokens"], 220)
        self.assertEqual(
            snapshot["tokens"]["inputTokens"] + snapshot["tokens"]["outputTokens"],
            220,
        )
        self.assertEqual(snapshot["dailyTokens"], {today: 220})
        self.assertEqual(snapshot["tokens"]["cacheReadInputTokens"], 140)
        self.assertEqual(snapshot["tokens"]["reasoningTokens"], 25)
        self.assertEqual(snapshot["projects"]["items"][0]["path"], project_path)
        self.assertEqual(snapshot["projects"]["items"][0]["tokens"], 220)
        self.assertEqual(snapshot["models"]["grok-fixture"]["totalTokens"], 220)


class PeriodWindowTests(SandboxedCase):
    def test_windows_are_exact_inclusive_local_calendar_days(self) -> None:
        fixed_now = dt.datetime(2026, 9, 4, 15, 30, tzinfo=dt.timezone.utc)
        today = fixed_now.astimezone().date()

        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        daily = {
            today.isoformat(): 1,
            (today + dt.timedelta(days=1)).isoformat(): 100_000,
            (today - dt.timedelta(days=6)).isoformat(): 10,
            (today - dt.timedelta(days=7)).isoformat(): 100,
            (today - dt.timedelta(days=29)).isoformat(): 1_000,
            (today - dt.timedelta(days=30)).isoformat(): 10_000,
        }
        with patch.object(agentcat.dt, "datetime", FixedDateTime):
            periods = agentcat.periods_from_daily_tokens(daily)

        self.assertEqual(
            periods,
            {"today": 1, "week": 11, "month": 1_111, "all": 111_111},
        )

    def test_event_periods_use_the_same_calendar_boundaries(self) -> None:
        fixed_now = dt.datetime(2026, 9, 4, 23, 59, tzinfo=dt.timezone.utc)
        week_floor = fixed_now.astimezone().date() - dt.timedelta(days=6)

        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is None else fixed_now.astimezone(tz)

        periods = agentcat.empty_periods()
        with patch.object(agentcat.dt, "datetime", FixedDateTime):
            agentcat.add_to_periods(
                periods,
                7,
                dt.datetime.combine(week_floor, dt.time(12), tzinfo=fixed_now.astimezone().tzinfo),
            )

        self.assertEqual(periods, {"today": 0, "week": 7, "month": 7, "all": 7})

    def test_usage_floor_recomputes_windows_from_merged_daily_tokens(self) -> None:
        today = dt.datetime.now().astimezone().date()
        yesterday = today - dt.timedelta(days=1)
        primary = {
            "status": "ok",
            "source": "primary",
            "tokens": {"today": 10, "week": 10, "month": 10, "all": 10, "totalTokens": 10},
            "dailyTokens": {today.isoformat(): 10},
            "models": {},
        }
        floor = {
            "status": "ok",
            "source": "floor",
            "tokens": {"today": 0, "week": 20, "month": 20, "all": 20, "totalTokens": 20},
            "dailyTokens": {yesterday.isoformat(): 20},
            "models": {},
        }

        merged = agentcat.merge_provider_with_usage_floor(
            primary,
            floor,
            strategy="fixture_floor",
        )

        self.assertEqual(merged["dailyTokens"], {
            today.isoformat(): 10,
            yesterday.isoformat(): 20,
        })
        self.assertEqual(merged["tokens"]["today"], 10)
        self.assertEqual(merged["tokens"]["week"], 30)
        self.assertEqual(merged["tokens"]["month"], 30)
        self.assertEqual(merged["tokens"]["all"], 20)
        self.assertEqual(merged["tokens"]["totalTokens"], 20)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone control")
    def test_claude_stats_model_uses_raw_local_date_bucket(self) -> None:
        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            fixed_now = dt.datetime(2026, 9, 4, 19, 0, tzinfo=dt.timezone.utc)

            class FixedDateTime(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed_now if tz is None else fixed_now.astimezone(tz)

            claude_dir = self.home / ".claude"
            claude_dir.mkdir(parents=True)
            (claude_dir / "stats-cache.json").write_text(
                json.dumps(
                    {
                        "dailyModelTokens": [
                            {
                                "date": "2026-09-04",
                                "tokensByModel": {"claude-sonnet-fixture": 25},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(agentcat.dt, "datetime", FixedDateTime),
                patch.object(agentcat, "claude_usage_by_source", return_value={"status": "not_available"}),
                patch.object(
                    agentcat,
                    "codexbar_cost_cache_snapshot",
                    return_value=agentcat.empty_provider_usage(),
                ),
            ):
                snapshot = agentcat.claude_snapshot()

            self.assertEqual(snapshot["dailyTokens"], {"2026-09-04": 25})
            self.assertEqual(snapshot["tokens"]["today"], 25)
            self.assertEqual(snapshot["models"]["claude-sonnet-fixture"]["today"], 25)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

