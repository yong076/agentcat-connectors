import importlib.util
import datetime as dt
import json
import os
import tempfile
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
