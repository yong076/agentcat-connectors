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


class PeriodInsightTests(SandboxedCase):
    def test_summary_uses_period_classes_and_costs_every_priced_provider(self) -> None:
        snapshot = {
            "providers": {
                "first": {
                    "tokens": {
                        "inputTokens": 90_000,
                        "outputTokens": 80_000,
                        "cacheReadInputTokens": 70_000,
                        "cacheCreationInputTokens": 60_000,
                    },
                    "models": {
                        "gpt-5": {
                            "week": {
                                "inputTokens": 10,
                                "outputTokens": 20,
                                "cacheReadInputTokens": 30,
                                "cacheCreationInputTokens": 40,
                            }
                        }
                    },
                },
                "second": {
                    "tokens": {
                        "inputTokens": 50_000,
                        "week_input": 7,
                        "week_output": 8,
                        "week_cacheRead": 9,
                        "week_cacheWrite_1h": 10,
                        "week_cacheWrite_5m": 11,
                    },
                    "models": {
                        "claude-sonnet-4-6": {
                            "week": {
                                "inputTokens": 1,
                                "outputTokens": 2,
                                "cacheReadInputTokens": 3,
                                "cacheCreationInputTokens": 4,
                            }
                        }
                    },
                },
            }
        }

        def priced(_model, inp, out, cache_read, cache_write):
            costs = {
                "input": float(inp),
                "output": float(out * 2),
                "cache_read": float(cache_read * 3),
                "cache_write": float(cache_write * 4),
            }
            return {**costs, "total": sum(costs.values())}

        with patch.object(agentcat, "estimate_cost", side_effect=priced):
            result = agentcat.derive_insights(snapshot, period="week")

        summary = result["summary"]
        self.assertEqual(summary["input_tokens"], 11)
        self.assertEqual(summary["output_tokens"], 22)
        self.assertEqual(summary["cache_read_tokens"], 33)
        self.assertEqual(summary["cache_write_tokens"], 44)
        # The exact provider period classes replace only that provider's
        # per-model cost contribution; the first provider remains included.
        self.assertEqual(summary["input_cost_usd"], 17.0)
        self.assertEqual(summary["output_cost_usd"], 56.0)
        self.assertEqual(summary["cache_read_cost_usd"], 117.0)
        self.assertEqual(summary["cache_write_cost_usd"], 244.0)
        self.assertEqual(
            summary["estimated_cost_usd"],
            sum(
                summary[key]
                for key in (
                    "input_cost_usd",
                    "output_cost_usd",
                    "cache_read_cost_usd",
                    "cache_write_cost_usd",
                )
            ),
        )


class ClaudeWindowTests(SandboxedCase):
    def _write_usage(self, when: dt.datetime) -> Path:
        project = agentcat.CLAUDE_PROJECTS_DIR / "fixture"
        project.mkdir(parents=True, exist_ok=True)
        journal = project / "session.jsonl"
        event = {
            "timestamp": when.isoformat(),
            "cwd": str(self.home / "project"),
            "requestId": f"request-{when.date().isoformat()}",
            "message": {
                "model": "claude-sonnet-fixture",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
        with journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        return journal

    def test_cursor_omits_rolling_fields_and_snapshot_recomputes_them(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self._write_usage(now)
        self._write_usage(now - dt.timedelta(days=7))

        with (
            patch.object(agentcat, "claude_usage_by_source", return_value={"status": "not_available"}),
            patch.object(
                agentcat,
                "codexbar_cost_cache_snapshot",
                return_value=agentcat.empty_provider_usage(),
            ),
        ):
            snapshot = agentcat.claude_snapshot()

        cursor = json.loads(agentcat.JOURNAL_CURSOR_FILE.read_text(encoding="utf-8"))
        self.assertEqual(set(cursor["totals"]), set(agentcat._JOURNAL_ALL_KEYS))
        self.assertFalse(
            any(
                key.startswith(("today_", "week_", "month_"))
                for key in cursor["totals"]
            )
        )
        model_state = cursor["models"]["claude-sonnet-fixture"]
        self.assertNotIn("today", model_state)
        self.assertNotIn("week", model_state)
        self.assertNotIn("month", model_state)
        self.assertIn("dailyTokens", model_state)

        expected = agentcat.periods_from_daily_tokens(snapshot["dailyTokens"])
        self.assertEqual(snapshot["tokens"]["today"], expected["today"])
        self.assertEqual(snapshot["tokens"]["week"], expected["week"])
        self.assertEqual(snapshot["tokens"]["month"], expected["month"])
        self.assertEqual(snapshot["tokens"]["all"], 30)

    def test_v3_cursor_rebuilds_raw_journal_and_persists_v4(self) -> None:
        journal = self._write_usage(dt.datetime.now(dt.timezone.utc))
        agentcat.JOURNAL_CURSOR_FILE.write_text(
            json.dumps(
                {
                    "version": agentcat.CLAUDE_JOURNAL_CURSOR_VERSION - 1,
                    "offsets": {str(journal): journal.stat().st_size},
                    "totals": {"all_input": 999, "today_input": 999},
                    "projects": {},
                    "dailyTokens": {"2020-01-01": 999},
                    "hourlyTokens": {},
                    "models": {
                        "claude-fixture": {
                            "inputTokens": 999,
                            "totalTokens": 999,
                            "today": 999,
                        }
                    },
                    "usageRecords": {},
                    "totals_as_of": "2020-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        cursor = agentcat.load_journal_cursor()
        self.assertTrue(cursor["usage_needs_rebuild"])
        self.assertEqual(cursor["offsets"], {})
        with (
            patch.object(agentcat, "claude_usage_by_source", return_value={"status": "not_available"}),
            patch.object(
                agentcat,
                "codexbar_cost_cache_snapshot",
                return_value=agentcat.empty_provider_usage(),
            ),
        ):
            snapshot = agentcat.claude_snapshot()

        persisted = json.loads(agentcat.JOURNAL_CURSOR_FILE.read_text(encoding="utf-8"))
        self.assertEqual(persisted["version"], agentcat.CLAUDE_JOURNAL_CURSOR_VERSION)
        self.assertEqual(persisted["offsets"][str(journal)], journal.stat().st_size)
        self.assertEqual(persisted["totals"]["all_input"], 10)
        self.assertEqual(persisted["totals"]["all_output"], 5)
        self.assertFalse(
            any(
                key.startswith(("today_", "week_", "month_"))
                for key in persisted["totals"]
            )
        )
        model_state = persisted["models"]["claude-sonnet-fixture"]
        self.assertIn("dailyTokens", model_state)
        self.assertNotIn("today", model_state)
        self.assertNotIn("week", model_state)
        self.assertNotIn("month", model_state)
        self.assertNotIn("totals_as_of", persisted)
        self.assertEqual(snapshot["tokens"]["all"], 15)
        self.assertEqual(snapshot["tokens"]["today"], 15)

    def test_windows_age_without_append_or_cursor_rebuild(self) -> None:
        first_now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
        clock = {"now": first_now}

        class FixedDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                current = clock["now"]
                return current if tz is None else current.astimezone(tz)

        journal = self._write_usage(first_now)
        with (
            patch.object(agentcat.dt, "datetime", FixedDateTime),
            patch.object(agentcat, "claude_usage_by_source", return_value={"status": "not_available"}),
            patch.object(
                agentcat,
                "codexbar_cost_cache_snapshot",
                return_value=agentcat.empty_provider_usage(),
            ),
        ):
            first = agentcat.claude_snapshot()
            persisted_before = agentcat.JOURNAL_CURSOR_FILE.read_bytes()
            mtime_before = agentcat.JOURNAL_CURSOR_FILE.stat().st_mtime_ns
            offset_before = json.loads(persisted_before.decode("utf-8"))["offsets"][str(journal)]

            clock["now"] = first_now + dt.timedelta(days=1)
            second = agentcat.claude_snapshot()

        self.assertEqual(first["tokens"]["today"], 15)
        self.assertEqual(second["tokens"]["today"], 0)
        self.assertEqual(second["tokens"]["week"], 15)
        self.assertEqual(second["tokens"]["all"], 15)
        self.assertEqual(agentcat.JOURNAL_CURSOR_FILE.read_bytes(), persisted_before)
        self.assertEqual(agentcat.JOURNAL_CURSOR_FILE.stat().st_mtime_ns, mtime_before)
        persisted_after = json.loads(agentcat.JOURNAL_CURSOR_FILE.read_text(encoding="utf-8"))
        self.assertEqual(persisted_after["offsets"][str(journal)], offset_before)


class ClaudeDailyFloorTests(SandboxedCase):
    def test_merged_daily_sum_floors_lifetime_headline(self) -> None:
        claude_dir = self.home / ".claude"
        claude_dir.mkdir(parents=True)
        old_day = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).date().isoformat()
        (claude_dir / "stats-cache.json").write_text(
            json.dumps(
                {
                    "dailyModelTokens": [
                        {
                            "date": old_day,
                            "tokensByModel": {"claude-sonnet-4-6": 999},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        now = dt.datetime.now(dt.timezone.utc)
        project = agentcat.CLAUDE_PROJECTS_DIR / "fixture"
        project.mkdir(parents=True, exist_ok=True)
        (project / "session.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": now.isoformat(),
                    "cwd": str(self.home / "project"),
                    "requestId": "request-current",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with (
            patch.object(agentcat, "claude_usage_by_source", return_value={"status": "not_available"}),
            patch.object(
                agentcat,
                "codexbar_cost_cache_snapshot",
                return_value=agentcat.empty_provider_usage(),
            ),
        ):
            snapshot = agentcat.claude_snapshot()

        daily_sum = sum(snapshot["dailyTokens"].values())
        self.assertEqual(daily_sum, 1_014)
        self.assertEqual(snapshot["tokens"]["all"], daily_sum)
        self.assertEqual(snapshot["tokens"]["totalTokens"], daily_sum)

