"""Sandboxed tests for the Reflect (돌아보기) module — R1.

Synthetic transcripts only; the analyzer runner is stubbed; the network is
blocked; every path constant is redirected into a temp dir (tests/sandbox.py).
"""

import copy
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from tests.sandbox import assert_sandboxed, redirect_module_paths, restore_module_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
REFLECT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "reflect"
LOADER = SourceFileLoader("agentcat_module_reflect", str(REPO_ROOT / "bin" / "agentcat"))
SPEC = importlib.util.spec_from_loader("agentcat_module_reflect", LOADER)
agentcat = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agentcat)


CLAUDE_UUID = "11111111-2222-4333-8444-555555555555"
CODEX_UUID = "01a0700b-91e1-7333-a2dc-5e14ba57956b"
BASE_TS = dt.datetime(2026, 9, 2, 10, 0, 0, tzinfo=dt.timezone.utc)

SEEDED_SECRETS = [
    "sk-pro" + "j-ABCD" + "EFGHIJ" + "KLMNOP" + "QRSTUV" + "WXYZ01" + "234567" + "89",  # assembled at runtime so scanners never see a contiguous token
    "sk-ant" + "-api03" + "-abcde" + "fghijk" + "lmnopq" + "rstuvw" + "xyz",  # assembled at runtime so scanners never see a contiguous token
    "ghp_ab" + "cdefgh" + "ijklmn" + "opqrst" + "uvwxyz" + "012345" + "6789",  # assembled at runtime so scanners never see a contiguous token
    "github" + "_pat_1" + "1ABCDE" + "FG0123" + "456789" + "abcdef" + "ghij",  # assembled at runtime so scanners never see a contiguous token
    "xoxb-1" + "234567" + "890-ab" + "cdefgh" + "ijklmn" + "op",  # assembled at runtime so scanners never see a contiguous token
    "AKIAIO" + "SFODNN" + "7EXAMP" + "LE",  # assembled at runtime so scanners never see a contiguous token
    "eyJhbG" + "ciOiJI" + "UzI1Ni" + "IsInR5" + "cCI6Ik" + "pXVCJ9" + ".eyJzd" + "WIiOiI" + "xMjM0N" + "TY3ODk" + "wIn0.a" + "bcdefg" + "hijklm" + "nopqrs" + "tuvwxy" + "z",  # assembled at runtime so scanners never see a contiguous token
    "hunter" + "2hunte" + "r2",  # assembled at runtime so scanners never see a contiguous token
    "supers" + "ecretv" + "alue99",  # assembled at runtime so scanners never see a contiguous token
    "tok_ab" + "cdefgh" + "ijklmn" + "op1234" + "5",  # assembled at runtime so scanners never see a contiguous token
]
SEEDED_SECRET_TEXT = "\n".join(
    [
        "here is my openai key " + SEEDED_SECRETS[0] + " please use it",
        "and anthropic " + SEEDED_SECRETS[1],
        "gh token " + SEEDED_SECRETS[2] + " and " + SEEDED_SECRETS[3],
        "slack " + SEEDED_SECRETS[4] + " aws " + SEEDED_SECRETS[5],
        "Authorization: Bearer " + SEEDED_SECRETS[6],
        "DATABASE_PASSWORD=" + SEEDED_SECRETS[7],
        "export STRIPE_SECRET_KEY='" + SEEDED_SECRETS[8] + "'",
        "api_key: " + SEEDED_SECRETS[9],
    ]
)


def ts(offset_seconds):
    return (BASE_TS + dt.timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def claude_user(text, offset, cwd="/Users/alice/Code/my-repo", **extra):
    entry = {
        "type": "user",
        "cwd": cwd,
        "sessionId": CLAUDE_UUID,
        "timestamp": ts(offset),
        "uuid": "u-%d" % offset,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    entry.update(extra)
    return entry


def claude_tool_result(offset, cwd="/Users/alice/Code/my-repo"):
    return {
        "type": "user",
        "cwd": cwd,
        "sessionId": CLAUDE_UUID,
        "timestamp": ts(offset),
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
    }


def claude_assistant(text, offset, tools=(), request_id=None, usage=None, model="claude-sonnet-4-5", cwd="/Users/alice/Code/my-repo"):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for name in tools:
        content.append({"type": "tool_use", "id": "toolu_" + name, "name": name, "input": {}})
    entry = {
        "type": "assistant",
        "cwd": cwd,
        "sessionId": CLAUDE_UUID,
        "timestamp": ts(offset),
        "requestId": request_id or ("req-%d" % offset),
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0},
        },
    }
    return entry


def write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def codex_entries(cwd="/Users/alice/Code/codex-repo", user_texts=("fix the failing test",), model="gpt-5.6"):
    entries = [
        {"timestamp": ts(0), "type": "session_meta", "payload": {"id": CODEX_UUID, "cwd": cwd, "timestamp": ts(0), "originator": "codex-tui"}},
        {"timestamp": ts(1), "type": "turn_context", "payload": {"model": model, "cwd": cwd}},
        {"timestamp": ts(2), "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>/Users/alice</cwd>"}]}},
        {"timestamp": ts(3), "type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "developer instructions"}]}},
    ]
    offset = 10
    for text in user_texts:
        entries.append({"timestamp": ts(offset), "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        entries.append({"timestamp": ts(offset + 1), "type": "response_item", "payload": {"type": "custom_tool_call", "name": "shell", "input": "pytest", "call_id": "c1"}})
        entries.append({"timestamp": ts(offset + 2), "type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "arguments": "{}", "call_id": "c2"}})
        entries.append({"timestamp": ts(offset + 3), "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done: " + text}]}})
        entries.append({"timestamp": ts(offset + 4), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 2000, "cached_input_tokens": 500, "output_tokens": 300, "reasoning_output_tokens": 100, "total_tokens": 2300}}}})
        offset += 10
    return entries


def valid_analysis(rules=None, frictions=None, patterns=None, scores=None):
    scores = scores or {"clarity": 4, "context": 3, "constraints": 5, "success_criteria": 2, "scope": 4}
    return {
        "frictions": frictions if frictions is not None else [
            {"category": "missing_context", "attribution": "user_actionable", "evidence": "fix it", "turn": 0, "note": "no file named"},
            {"category": "wrong_or_buggy_output", "attribution": "ai_capability", "evidence": "that broke", "turn": 3},
        ],
        "patterns": patterns if patterns is not None else [
            {"category": "verification_requested", "evidence": "run the tests", "turn": 2},
        ],
        "prompt_quality": {name: {"score": score, "before": "fix it", "after": "fix the failing test in foo.py"} for name, score in scores.items()},
        "working_style": "Works in short bursts. Asks for tests. Corrects quickly.",
        "candidate_rules": rules if rules is not None else [
            "Always run the test suite before claiming done.",
            "Name the file and the symptom in the first message.",
            "Keep one logical change per commit.",
        ],
    }


class StubRunner(agentcat.ReflectRunner):
    name = "stub"
    model = "stub-model"

    def __init__(self, responses=None, cost=0.01):
        self.responses = list(responses) if responses is not None else [json.dumps(valid_analysis())]
        self.prompts = []
        self.cost = cost

    def available(self):
        return True

    def run(self, prompt):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("stub runner exhausted")
        text = self.responses.pop(0)
        return {"text": text, "cost_usd": self.cost, "session_id": "stub-session"}


class ReflectTestCase(unittest.TestCase):
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
        for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "AGENTCAT_HOME"):
            os.environ.pop(key, None)
        self.network = patch.object(
            agentcat.urllib.request,
            "urlopen",
            side_effect=AssertionError("network must never be used by reflect tests"),
        )
        self.network.start()
        # Reflect never spawns anything but the analyzer runner; every test that
        # stubs the runner therefore expects zero subprocess launches.
        self.subprocess_calls = []

        def _no_subprocess(*args, **kwargs):
            command = args[0] if args else kwargs.get("args")
            if command == ["defaults", "read", "-g", "AppleLocale"]:
                return subprocess.CompletedProcess(command, 0, stdout="en_US\n", stderr="")
            self.subprocess_calls.append((args, kwargs))
            raise AssertionError("subprocess.run must not be called with a stubbed runner")

        self.subprocess = patch.object(agentcat.subprocess, "run", side_effect=_no_subprocess)
        self.subprocess.start()
        self.claude_dir = self.home / ".claude" / "projects" / "-Users-alice-Code-my-repo"
        self.codex_dir = self.home / ".codex" / "sessions" / "2026" / "09" / "02"

    def tearDown(self):
        self.subprocess.stop()
        self.network.stop()
        self.env.stop()
        restore_module_paths(agentcat, self.old_paths)
        self.tmp.cleanup()

    # fixtures ---------------------------------------------------------------

    def claude_path(self, uuid=CLAUDE_UUID):
        return self.claude_dir / (uuid + ".jsonl")

    def codex_path(self, uuid=CODEX_UUID):
        return self.codex_dir / ("rollout-2026-09-02T10-00-00-" + uuid + ".jsonl")

    def write_claude_session(self, entries=None, uuid=CLAUDE_UUID):
        if entries is None:
            entries = [
                {"type": "attachment", "attachment": {}, "cwd": "/Users/alice/Code/my-repo", "timestamp": ts(0)},
                claude_user("<command-name>/clear</command-name>", 1),
                claude_user("fix the failing test in foo.py", 2),
                claude_assistant("Let me look.", 3, tools=("Read", "Bash")),
                claude_tool_result(4),
                claude_assistant("Found it. " + ("x" * 900), 5, tools=("Edit",), request_id="req-5"),
                claude_assistant("", 6, request_id="req-5"),  # streamed chunk of the same request: usage must not double count
                claude_user("now run the tests", 7),
                claude_assistant("All green.", 8, tools=("Bash",)),
                claude_user("hidden meta", 9, isMeta=True),
                claude_user("sidechain", 10, isSidechain=True),
            ]
        path = self.claude_path(uuid)
        write_jsonl(path, entries)
        return path

    def write_codex_session(self, entries=None, uuid=CODEX_UUID):
        path = self.codex_path(uuid)
        write_jsonl(path, entries if entries is not None else codex_entries())
        return path

    def write_opencode_session(self):
        storage = agentcat.REFLECT_OPENCODE_STORAGE
        session_id = "ses_opencode_fixture"
        session = storage / "session" / "project-1" / (session_id + ".json")
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(json.dumps({"id": session_id, "directory": "/Users/alice/Code/open-repo"}))
        messages = [
            {"id": "msg-u", "sessionID": session_id, "role": "user", "time": {"created": 1788343200000}},
            {"id": "msg-a", "sessionID": session_id, "role": "assistant", "modelID": "gpt-5.6",
             "time": {"created": 1788343201000}, "tokens": {"input": 100, "output": 50, "cache": {"read": 20}}, "cost": 0.02},
        ]
        for message in messages:
            target = storage / "message" / session_id / (message["id"] + ".json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(message))
        parts = {
            "msg-u": [{"id": "part-u", "type": "text", "text": "OpenCode prompt"}],
            "msg-a": [{"id": "part-a", "type": "text", "text": "OpenCode answer"},
                      {"id": "part-t", "type": "tool", "tool": "bash"}],
        }
        for message_id, rows in parts.items():
            for row in rows:
                target = storage / "part" / message_id / (row["id"] + ".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(row))
        return session

    def write_copilot_session(self):
        path = agentcat.REFLECT_COPILOT_SESSION_STATE / "33333333-3333-4333-8333-333333333333" / "events.jsonl"
        write_jsonl(path, [
            {"type": "session.start", "timestamp": ts(0), "data": {"context": {"cwd": "/Users/alice/Code/copilot-repo"}}},
            {"type": "user.message", "timestamp": ts(1), "data": {"content": "Copilot prompt"}},
            {"type": "tool.execution_start", "timestamp": ts(2), "data": {"toolName": "shell"}},
            {"type": "assistant.message", "timestamp": ts(3), "data": {"content": "Copilot answer"}},
            {"type": "assistant.usage", "timestamp": ts(4), "data": {"model": "gpt-5.6", "inputTokens": 120, "outputTokens": 30, "cacheReadTokens": 10}},
        ])
        return path

    def write_grok_session(self):
        path = agentcat.REFLECT_GROK_HOME / "sessions" / "%2FUsers%2Falice%2FCode%2Fgrok-repo" / "44444444-4444-4444-8444-444444444444" / "updates.jsonl"
        def update(kind, offset, **extra):
            return {"timestamp": ts(offset), "method": "session/update", "params": {"update": {"sessionUpdate": kind, **extra}}}
        write_jsonl(path, [
            update("user_message_chunk", 0, content={"type": "text", "text": "Grok prompt"}),
            update("tool_call", 1, name="read", toolCallId="tool-1"),
            update("agent_message_chunk", 2, content={"type": "text", "text": "Grok answer"}),
            update("turn_completed", 3, prompt_id="p1", usage={"inputTokens": 90, "outputTokens": 10, "totalTokens": 100, "modelUsage": {"grok-code": {}}}),
        ])
        return path

    def write_gemini_session(self):
        project = agentcat.REFLECT_GEMINI_TMP / "gemini-repo"
        project.mkdir(parents=True, exist_ok=True)
        (project / ".project_root").write_text("/Users/alice/Code/gemini-repo")
        path = project / "chats" / "session-2026-09-02-fixture.jsonl"
        write_jsonl(path, [
            {"sessionId": "gemini-fixture", "startTime": ts(0), "kind": "main"},
            {"id": "u1", "timestamp": ts(1), "type": "user", "content": [{"text": "Gemini prompt"}]},
            {"id": "a1", "timestamp": ts(2), "type": "gemini", "content": "Gemini answer", "model": "gemini-2.5-pro",
             "tokens": {"input": 80, "output": 20, "cached": 5, "total": 100},
             "toolCalls": [{"name": "read_file", "timestamp": ts(2)}]},
        ])
        return path

    def write_cursor_session(self):
        path = agentcat.REFLECT_CURSOR_WORKSPACE_STORAGE / "55555555-5555-4555-8555-555555555555" / "state.vscdb"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("create table ItemTable (key text primary key, value blob)")
            messages = [
                {"role": "user", "content": [{"type": "text", "text": "Cursor prompt"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Cursor answer"},
                                                     {"type": "tool-call", "toolName": "read"}]},
            ]
            conn.execute("insert into ItemTable (key, value) values (?, ?)", ("composerData:fixture", json.dumps({"messages": messages})))
            conn.commit()
        path.with_name("workspace.json").write_text(json.dumps({"folder": "file:///Users/alice/Code/cursor-repo"}))
        return path

    def write_kimi_session(self):
        path = agentcat.REFLECT_KIMI_HOME / "sessions" / "wd_kimi-repo_hash" / "session_66666666-6666-4666-8666-666666666666" / "agents" / "main" / "wire.jsonl"
        write_jsonl(path, [{"timestamp": ts(0), "type": "usage.record", "usageScope": "turn",
                            "usage": {"inputOther": 70, "output": 30, "inputCacheRead": 10}}])
        return path


# ---------------------------------------------------------------------------
# 1. indexer
# ---------------------------------------------------------------------------


class IndexerTests(ReflectTestCase):
    def test_claude_digest_shape(self):
        path = self.write_claude_session()
        digest = agentcat.reflect_parse_claude_session(path)
        self.assertIsNotNone(digest)
        self.assertEqual(digest["id"], "claude:" + CLAUDE_UUID)
        self.assertEqual(digest["tool"], "claude")
        self.assertEqual(digest["project"], "my-repo")  # basename only
        self.assertEqual(digest["path"], str(path))
        self.assertEqual(digest["started_at"], ts(1))  # attachments do not start a session; the first user entry does
        self.assertEqual(digest["ended_at"], ts(9))  # sidechain entries never move the clock
        self.assertEqual(digest["user_turns"], 2)  # command + meta + sidechain excluded
        self.assertEqual(digest["turns"], 4)  # user, assistant(merged), user, assistant
        self.assertEqual(digest["tool_call_counts"], {"Bash": 2, "Edit": 1, "Read": 1})
        self.assertEqual(digest["tool_calls"], 4)
        self.assertEqual(digest["model"], "claude-sonnet-4-5")
        # 4 assistant entries, two of them share req-5 → 3 counted requests × 1500 tokens
        self.assertEqual(digest["tokens"], 4500)
        expected_cost = agentcat.estimate_cost("claude-sonnet-4-5", 3000, 1500, 0, 0)["total"]
        self.assertAlmostEqual(digest["cost_usd"], round(expected_cost, 6))
        user_turns = [turn for turn in digest["turn_list"] if turn["role"] == "user"]
        self.assertEqual([turn["text"] for turn in user_turns], ["fix the failing test in foo.py", "now run the tests"])
        assistant = digest["turn_list"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertLessEqual(len(assistant["text"]), agentcat.REFLECT_TEXT_PER_TURN)
        self.assertTrue(assistant["text"].startswith("Let me look."))
        self.assertEqual(assistant["tools"], ["Read", "Bash", "Edit"])

    def test_claude_string_content_and_no_user_turns(self):
        path = self.write_claude_session([
            {"type": "user", "cwd": "/Users/alice/Code/my-repo", "timestamp": ts(0), "message": {"role": "user", "content": "plain string prompt"}},
            claude_assistant("ok", 1),
        ])
        digest = agentcat.reflect_parse_claude_session(path)
        self.assertEqual(digest["turn_list"][0]["text"], "plain string prompt")
        empty = self.write_claude_session([claude_assistant("only assistant", 1)], uuid="22222222-2222-4333-8444-555555555555")
        self.assertIsNone(agentcat.reflect_parse_claude_session(empty))

    def test_claude_analyzer_own_sessions_are_skipped(self):
        scratch = str(agentcat.REFLECT_SCRATCH_DIR)
        path = self.write_claude_session([
            claude_user(agentcat.REFLECT_MARKER + "\nanalyze this", 0, cwd=scratch),
            claude_assistant("{}", 1, cwd=scratch),
        ])
        self.assertIsNone(agentcat.reflect_parse_claude_session(path))

    def test_automated_markers_cover_claude_codex_and_orca_workers(self):
        sdk_path = self.write_claude_session([
            claude_user("automated sdk prompt", 0, entrypoint="sdk-cli", promptSource="sdk"),
            claude_assistant("done", 1),
        ])
        self.assertTrue(agentcat.reflect_parse_claude_session(sdk_path)["automated"])

        orca_path = self.write_claude_session([
            claude_user("worker prompt", 0, cwd="/Users/alice/orca/workspaces/repo"),
            claude_assistant("done", 1, cwd="/Users/alice/orca/workspaces/repo"),
        ], uuid="77777777-7777-4777-8777-777777777777")
        self.assertTrue(agentcat.reflect_parse_claude_session(orca_path)["automated"])

        entries = codex_entries()
        entries[0]["payload"]["originator"] = "codex_exec"
        codex_path = self.write_codex_session(entries)
        self.assertTrue(agentcat.reflect_parse_codex_session(codex_path)["automated"])
        agentcat.reflect_sync(files=[("codex", codex_path)])
        self.assertTrue(agentcat.reflect_get_session("codex:" + CODEX_UUID)["automated"])

    def test_claude_notifications_do_not_hide_human_session_or_count_as_prompts(self):
        for marker in ({"promptSource": "system"}, {"promptSource": "hook"},
                       {"origin": {"kind": "task-notification"}},
                       {"promptSource": "system", "origin": {"kind": "task-notification"}}):
            with self.subTest(marker=marker):
                path = self.write_claude_session([
                    claude_user("inspect the panel", 0, entrypoint="cli"),
                    claude_assistant("Checking.", 1, tools=("Read",)),
                    claude_user("Background task completed", 2, **marker),
                    claude_assistant("Task result checked.", 3, tools=("Bash",)),
                    claude_user("verify Escape closes it", 4),
                    claude_assistant("Verified.", 5),
                ])
                digest = agentcat.reflect_parse_claude_session(path)
                self.assertFalse(digest["automated"])
                self.assertEqual(digest["user_turns"], 2)
                self.assertEqual(digest["tool_call_counts"], {"Read": 1, "Bash": 1})
                self.assertEqual(digest["tokens"], 4500)
                self.assertNotIn("Background task completed", json.dumps(digest["turn_list"]))
                analysis, _ = agentcat.reflect_validate_analysis(valid_analysis())
                week = agentcat.reflect_synthesize_week("2026-W36", [{"session": digest, "analysis": analysis}])
                self.assertEqual(week["prompt_quality"]["mean"], 3.6)

    def test_claude_first_real_prompt_decides_worker_provenance(self):
        for origin, automated in (({"entrypoint": "cli"}, False),
                                  ({"entrypoint": "sdk-cli"}, True),
                                  ({"entrypoint": "sdk"}, True),
                                  ({"promptSource": "sdk"}, True),
                                  ({"promptSource": "agent"}, True)):
            with self.subTest(origin=origin):
                path = self.write_claude_session([
                    claude_user("Early task notification", 0, promptSource="system"),
                    claude_user("first real request", 1, **origin),
                    claude_assistant("done", 2),
                    claude_user("later request", 3, entrypoint="cli"),
                    claude_assistant("done again", 4),
                ])
                digest = agentcat.reflect_parse_claude_session(path)
                self.assertEqual(digest["automated"], automated)
                self.assertEqual(digest["user_turns"], 2)

    def test_claude_notification_only_journal_has_no_human_analysis(self):
        path = self.write_claude_session([
            claude_user("Worker complete", 0, origin={"kind": "task-notification"}),
            claude_assistant("Acknowledged.", 1),
        ])
        digest = agentcat.reflect_parse_claude_session(path)
        self.assertTrue(digest["counts_only"])
        self.assertTrue(digest["automated"])
        self.assertEqual(digest["user_turns"], 0)
        self.assertEqual(digest["turn_list"], [])

    def test_codex_digest_shape(self):
        path = self.write_codex_session()
        digest = agentcat.reflect_parse_codex_session(path)
        self.assertIsNotNone(digest)
        self.assertEqual(digest["id"], "codex:" + CODEX_UUID)
        self.assertEqual(digest["tool"], "codex")
        self.assertEqual(digest["project"], "codex-repo")
        self.assertEqual(digest["user_turns"], 1)  # environment_context + developer excluded
        self.assertEqual(digest["turns"], 2)
        self.assertEqual(digest["tool_call_counts"], {"apply_patch": 1, "shell": 1})
        self.assertEqual(digest["model"], "gpt-5.6")
        # uncached 1500 + cached 500 + output 300 (reasoning is inside output)
        self.assertEqual(digest["tokens"], 2300)
        expected = agentcat.estimate_cost("gpt-5.6", 1500, 300, 500, 0)["total"]
        self.assertAlmostEqual(digest["cost_usd"], round(expected, 6))
        self.assertEqual(digest["turn_list"][0]["text"], "fix the failing test")
        self.assertEqual(digest["turn_list"][1]["text"], "Done: fix the failing test")
        self.assertEqual(digest["turn_list"][1]["tools"], ["shell", "apply_patch"])
        self.assertEqual(digest["started_at"], ts(0))

    def test_opencode_reader_yields_shared_turns_and_digest(self):
        path = self.write_opencode_session()
        turns = list(agentcat.reflect_read_opencode_turns(path))
        self.assertTrue(turns)
        self.assertTrue(all(set(turn) == {"role", "text", "tool", "ts"} for turn in turns))
        digest = agentcat.reflect_parse_opencode_session(path)
        self.assertEqual((digest["tool"], digest["project"]), ("opencode", "open-repo"))
        self.assertEqual((digest["user_turns"], digest["tool_calls"], digest["tokens"]), (1, 1, 170))
        self.assertEqual(digest["cost_usd"], 0.02)
        self.assertFalse(digest["counts_only"])

    def test_copilot_reader_yields_shared_turns_and_digest(self):
        path = self.write_copilot_session()
        turns = list(agentcat.reflect_read_copilot_turns(path))
        self.assertTrue(all(set(turn) == {"role", "text", "tool", "ts"} for turn in turns))
        digest = agentcat.reflect_parse_copilot_session(path)
        self.assertEqual((digest["project"], digest["user_turns"]), ("copilot-repo", 1))
        self.assertEqual((digest["tool_call_counts"], digest["tokens"]), ({"shell": 1}, 160))
        self.assertFalse(digest["counts_only"])

    def test_grok_reader_yields_shared_turns_and_digest(self):
        path = self.write_grok_session()
        turns = list(agentcat.reflect_read_grok_turns(path))
        self.assertTrue(all(set(turn) == {"role", "text", "tool", "ts"} for turn in turns))
        digest = agentcat.reflect_parse_grok_session(path)
        self.assertEqual((digest["project"], digest["user_turns"]), ("grok-repo", 1))
        self.assertEqual((digest["tool_call_counts"], digest["tokens"]), ({"read": 1}, 100))
        self.assertFalse(digest["counts_only"])

    def test_gemini_reader_yields_shared_turns_and_digest(self):
        path = self.write_gemini_session()
        turns = list(agentcat.reflect_read_gemini_turns(path))
        self.assertTrue(all(set(turn) == {"role", "text", "tool", "ts"} for turn in turns))
        digest = agentcat.reflect_parse_gemini_session(path)
        self.assertEqual((digest["project"], digest["user_turns"]), ("gemini-repo", 1))
        self.assertEqual((digest["tool_call_counts"], digest["tokens"]), ({"read_file": 1}, 105))
        self.assertFalse(digest["counts_only"])

    def test_cursor_reader_copies_sqlite_and_yields_shared_turns(self):
        path = self.write_cursor_session()
        with patch.object(agentcat.shutil, "copy2", wraps=shutil.copy2) as copied:
            turns = list(agentcat.reflect_read_cursor_turns(path))
        self.assertEqual(copied.call_count, 1)
        self.assertNotEqual(Path(copied.call_args.args[1]), path)
        self.assertTrue(all(set(turn) == {"role", "text", "tool", "ts"} for turn in turns))
        digest = agentcat.reflect_parse_cursor_session(path)
        self.assertEqual((digest["project"], digest["user_turns"]), ("cursor-repo", 1))
        self.assertEqual(digest["tool_call_counts"], {"read": 1})
        self.assertFalse(digest["counts_only"])

    def test_kimi_wire_and_unknown_format_are_counts_only(self):
        kimi = agentcat.reflect_parse_kimi_session(self.write_kimi_session())
        self.assertTrue(kimi["counts_only"])
        self.assertEqual((kimi["user_turns"], kimi["tokens"]), (0, 110))
        unknown = agentcat.REFLECT_OPENCODE_STORAGE / "session" / "p" / "unknown.json"
        unknown.parent.mkdir(parents=True, exist_ok=True)
        unknown.write_text("not-json")
        result = agentcat.reflect_sync(files=[("opencode", unknown)])
        self.assertEqual(result["indexed"], 1)
        self.assertTrue(agentcat.reflect_get_session("opencode:unknown")["counts_only"])

    def test_session_files_cover_claude_and_codex_and_skip_subagents(self):
        claude = self.write_claude_session()
        codex = self.write_codex_session()
        subagent = self.claude_dir / CLAUDE_UUID / "subagents" / "agent-1.jsonl"
        write_jsonl(subagent, [claude_user("sub", 0)])
        files = agentcat.reflect_session_files()
        self.assertIn(("claude", claude), files)
        self.assertIn(("codex", codex), files)
        self.assertNotIn(("claude", subagent), files)

    def test_session_files_cover_all_multi_tool_readers(self):
        expected = {
            ("opencode", self.write_opencode_session()),
            ("copilot", self.write_copilot_session()),
            ("grok", self.write_grok_session()),
            ("gemini", self.write_gemini_session()),
            ("cursor", self.write_cursor_session()),
            ("kimi", self.write_kimi_session()),
        }
        self.assertTrue(expected.issubset(set(agentcat.reflect_session_files(max_age_days=0))))

    def test_session_files_skip_stale_files(self):
        claude = self.write_claude_session()
        codex = self.write_codex_session()
        old = 40 * 86400
        os.utime(codex, (codex.stat().st_atime - old, codex.stat().st_mtime - old))
        files = agentcat.reflect_session_files()  # default indexDays = 30
        self.assertEqual(files, [("claude", claude)])
        self.assertEqual(len(agentcat.reflect_session_files(max_age_days=0)), 2)  # 0 = no limit
        agentcat.REFLECT_CONFIG_FILE.write_text(json.dumps({"indexDays": 90}))
        self.assertEqual(len(agentcat.reflect_session_files()), 2)

    def test_prefilter_skips_lines_without_needles(self):
        path = self.write_claude_session()
        with patch.object(agentcat.json, "loads", wraps=json.loads) as loads:
            agentcat.reflect_parse_claude_session(path)
        decoded = [call.args[0] for call in loads.call_args_list]
        self.assertTrue(decoded)
        self.assertFalse(any('"type": "attachment"' in text for text in decoded))
        rows = list(agentcat._reflect_iter_jsonl(path, (b"nothing-matches",)))
        self.assertEqual(rows, [])

    def test_length_bucket(self):
        self.assertEqual(agentcat.reflect_length_bucket(0), "short")
        self.assertEqual(agentcat.reflect_length_bucket(9), "short")
        self.assertEqual(agentcat.reflect_length_bucket(10), "medium")
        self.assertEqual(agentcat.reflect_length_bucket(39), "medium")
        self.assertEqual(agentcat.reflect_length_bucket(40), "long")
        self.assertEqual(agentcat.reflect_length_bucket(500), "long")

    def test_project_name_is_basename_only(self):
        self.assertEqual(agentcat.reflect_project_name("/Users/alice/Code/my-repo/"), "my-repo")
        self.assertEqual(agentcat.reflect_project_name("C:\\Users\\alice\\proj"), "proj")
        self.assertEqual(agentcat.reflect_project_name(""), "unknown")
        self.assertEqual(agentcat.reflect_project_name(None), "unknown")

    def test_session_row_has_no_transcript(self):
        digest = agentcat.reflect_parse_claude_session(self.write_claude_session())
        row = agentcat.reflect_session_row(digest)
        self.assertNotIn("turn_list", row)
        self.assertEqual(
            set(row),
            {"id", "tool", "project", "path", "started_at", "ended_at", "turns", "user_turns",
             "tool_calls", "tokens", "cost_usd", "tool_call_counts", "model", "counts_only", "automated"},
        )


# ---------------------------------------------------------------------------
# 2. scrub + sampling
# ---------------------------------------------------------------------------


class ScrubTests(ReflectTestCase):
    def test_no_seeded_secret_survives(self):
        scrubbed = agentcat.reflect_scrub_text(SEEDED_SECRET_TEXT)
        for secret in SEEDED_SECRETS:
            self.assertNotIn(secret, scrubbed, secret)
        self.assertIn("[secret]", scrubbed)
        # the variable names stay so the analyzer still sees *what* was set
        self.assertIn("DATABASE_PASSWORD=[secret]", scrubbed)
        self.assertIn("STRIPE_SECRET_KEY='[secret]'", scrubbed)

    def test_scrub_keeps_ordinary_text(self):
        text = "Please rename foo_key_count to keyCount and keep the token budget at 4000"
        self.assertEqual(agentcat.reflect_scrub_secrets(text), text)

    def test_paths_become_basenames(self):
        text = (
            "edit /Users/alice/Code/my-repo/src/app.py then read ~/notes/todo.md and "
            "C:\\Users\\alice\\proj\\main.rs; log at -Users-alice-Code-my-repo"
        )
        out = agentcat.reflect_elide_paths(text)
        self.assertNotIn("/Users/alice", out)
        self.assertNotIn("alice", out)
        self.assertIn("edit app.py then read todo.md and main.rs", out)
        self.assertIn("<path>", out)

    def test_scrub_digest_removes_path_and_scrubs_turns(self):
        path = self.write_claude_session([
            claude_user("use " + SEEDED_SECRETS[0] + " in /Users/alice/Code/my-repo/.env", 0),
            claude_assistant("Set OPENAI_API_KEY=" + SEEDED_SECRETS[0] + " in .env", 1, tools=("Bash",)),
        ])
        digest = agentcat.reflect_parse_claude_session(path)
        scrubbed = agentcat.reflect_scrub_digest(digest)
        self.assertNotIn("path", scrubbed)
        dumped = json.dumps(scrubbed)
        self.assertNotIn(SEEDED_SECRETS[0], dumped)
        self.assertNotIn("/Users/alice", dumped)
        self.assertIn("path", digest)  # the original is untouched

    def _long_turns(self, user_count=80):
        turns = []
        for index in range(user_count):
            turns.append({"i": len(turns), "role": "user", "ts": ts(index * 10), "text": "u%d" % index, "tools": []})
            burst = 12 if index in (7, 33, 61) else 1
            for _ in range(burst):
                turns.append({"i": len(turns), "role": "assistant", "ts": None, "text": "a", "tools": ["Bash"]})
        return turns

    def test_sampling_keeps_first_last_and_largest_bursts(self):
        turns = self._long_turns()
        sampled, was_sampled = agentcat.reflect_sample_turns(turns)
        self.assertTrue(was_sampled)
        self.assertIs(sampled[0], turns[0])
        self.assertIs(sampled[-1], turns[-1])
        user_texts = [turn["text"] for turn in sampled if turn.get("role") == "user"]
        self.assertLessEqual(len(user_texts), agentcat.REFLECT_SAMPLE_USER_TURNS)
        for burst_turn in ("u7", "u33", "u61", "u0", "u79"):
            self.assertIn(burst_turn, user_texts)
        gaps = [turn for turn in sampled if turn.get("role") == "gap"]
        self.assertTrue(gaps)
        self.assertEqual(sum(gap["elided"] for gap in gaps) + len(sampled) - len(gaps), len(turns))
        # a 12-tool burst is capped to REFLECT_MAX_ASSISTANT_TURNS_PER_EXCHANGE assistant turns
        index_u7 = next(i for i, turn in enumerate(sampled) if turn.get("text") == "u7")
        index_u7_full = next(i for i, turn in enumerate(turns) if turn.get("text") == "u7")
        following = []
        for turn in sampled[index_u7 + 1:]:
            if turn.get("role") == "user":
                break
            if turn.get("role") == "assistant":
                following.append(turn)
        self.assertEqual(len(following), agentcat.REFLECT_MAX_ASSISTANT_TURNS_PER_EXCHANGE)
        self.assertEqual([turn["i"] for turn in following], [turns[i]["i"] for i in range(index_u7_full + 1, index_u7_full + 4)] + [turns[i]["i"] for i in range(index_u7_full + 10, index_u7_full + 13)])

    def test_short_sessions_are_not_sampled(self):
        turns = self._long_turns(user_count=5)
        sampled, was_sampled = agentcat.reflect_sample_turns(turns)
        self.assertFalse(was_sampled)
        self.assertEqual(sampled, turns)

    def test_fit_budget_shrinks_text(self):
        turns = [{"role": "user", "text": "x" * 400} for _ in range(10)]
        fitted = agentcat.reflect_fit_budget(turns, budget=1500)
        self.assertLessEqual(sum(len(t["text"]) for t in fitted), 1500)
        self.assertEqual(len(turns[0]["text"]), 400)  # input untouched



# ---------------------------------------------------------------------------
# 3. analyzer
# ---------------------------------------------------------------------------


class AnalyzerTests(ReflectTestCase):
    def test_prompt_carries_rubric_and_schema(self):
        prompt = agentcat.reflect_build_prompt({"turn_list": []})
        for category in agentcat.REFLECT_FRICTION_CATEGORIES + agentcat.REFLECT_PATTERN_CATEGORIES:
            self.assertIn(category, prompt)
        for attribution in agentcat.REFLECT_ATTRIBUTIONS:
            self.assertIn(attribution, prompt)
        for dimension in agentcat.REFLECT_PROMPT_DIMENSIONS:
            self.assertIn(dimension, prompt)
        self.assertIn('"AgentCatReflectAnalysis"', prompt)
        self.assertEqual(len(agentcat.REFLECT_FRICTION_CATEGORIES), 9)
        self.assertEqual(len(agentcat.REFLECT_PATTERN_CATEGORIES), 8)
        self.assertEqual(len(agentcat.REFLECT_PROMPT_DIMENSIONS), 5)
        nudged = agentcat.reflect_build_prompt({"turn_list": []}, nudge="Return only JSON.")
        self.assertEqual(nudged.splitlines()[1], "Return only JSON.")

    def test_prompt_names_reader_language_and_preserves_quotes(self):
        names = {
            "ko": "Korean",
            "en": "English",
            "ja": "Japanese",
            "zh-Hans": "Simplified Chinese",
        }
        for lang, name in names.items():
            prompt = agentcat.reflect_build_prompt({"turn_list": []}, lang=lang)
            self.assertIn(
                "Write `note`, `after`, `working_style`, and `candidate_rules` in {}; "
                "keep quoted `evidence` and `before` verbatim in their original language.".format(name),
                prompt,
            )

    def test_reader_language_maps_apple_locale_with_patched_runner(self):
        cases = {
            "ko_KR": "ko",
            "ja_JP": "ja",
            "zh-Hans_KR": "zh-Hans",
            "zh_CN": "zh-Hans",
            "fr_FR": "en",
        }
        with patch.object(agentcat.sys, "platform", "darwin"):
            for locale_name, expected in cases.items():
                completed = subprocess.CompletedProcess(
                    ["defaults", "read", "-g", "AppleLocale"], 0,
                    stdout=locale_name + "\n", stderr="",
                )
                with patch.object(agentcat.subprocess, "run", return_value=completed) as run:
                    self.assertEqual(agentcat.reflect_reader_lang({}), expected)
                run.assert_called_once_with(
                    ["defaults", "read", "-g", "AppleLocale"],
                    capture_output=True, text=True, timeout=2.0,
                    creationflags=agentcat.CREATE_NO_WINDOW,
                )
        with patch.object(agentcat.subprocess, "run", side_effect=AssertionError("must not run")):
            self.assertEqual(agentcat.reflect_reader_lang({"lang": "ja"}), "ja")
        with patch.object(agentcat.sys, "platform", "linux"):
            with patch.object(agentcat.subprocess, "run", side_effect=AssertionError("must not run")):
                self.assertEqual(agentcat.reflect_reader_lang({}), "en")
        with patch.object(agentcat.sys, "platform", "darwin"):
            with patch.object(agentcat.subprocess, "run", side_effect=OSError("synthetic failure")):
                self.assertEqual(agentcat.reflect_reader_lang({}), "en")

    def test_prompt_payload_is_clean(self):
        path = self.write_claude_session([
            claude_user("token " + SEEDED_SECRETS[2] + " lives in /Users/alice/Code/my-repo/.env", 0),
            claude_assistant("ok", 1),
        ])
        digest = agentcat.reflect_parse_claude_session(path)
        payload = agentcat.reflect_prompt_payload(digest)
        prompt = agentcat.reflect_build_prompt(payload)
        self.assertNotIn(SEEDED_SECRETS[2], prompt)
        self.assertNotIn("/Users/alice", prompt)
        self.assertNotIn(str(self.home), prompt)
        self.assertFalse(payload["sampled"])
        self.assertEqual(payload["sampled_from_turns"], 2)
        self.assertTrue(prompt.startswith(agentcat.REFLECT_MARKER))

    def test_extract_json_tolerates_fences_and_prose(self):
        self.assertEqual(agentcat.reflect_extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(agentcat.reflect_extract_json('Sure! Here it is: {"a": {"b": 2}} hope it helps'), {"a": {"b": 2}})
        with self.assertRaises(agentcat.ReflectAnalysisError):
            agentcat.reflect_extract_json("no json here")
        with self.assertRaises(agentcat.ReflectAnalysisError):
            agentcat.reflect_extract_json("[1, 2]")

    def test_validate_accepts_valid_and_computes_mean(self):
        analysis, errors = agentcat.reflect_validate_analysis(valid_analysis())
        self.assertEqual(errors, [])
        self.assertEqual(analysis["prompt_quality_mean"], 3.6)
        self.assertEqual(analysis["schema_version"], agentcat.REFLECT_SCHEMA_VERSION)
        self.assertEqual(len(analysis["candidate_rules"]), 3)
        self.assertEqual(analysis["frictions"][0]["attribution"], "user_actionable")

    def test_validate_rejects_bad_shapes(self):
        cases = {
            "bad category": {"frictions": [{"category": "nope", "attribution": "user_actionable", "evidence": "x"}]},
            "bad attribution": {"frictions": [{"category": "scope_creep", "attribution": "fate", "evidence": "x"}]},
            "score out of range": {"prompt_quality": {name: {"score": 9, "before": "", "after": ""} for name in agentcat.REFLECT_PROMPT_DIMENSIONS}},
            "missing dimension": {"prompt_quality": {"clarity": {"score": 3, "before": "", "after": ""}}},
            "empty rules": {"candidate_rules": []},
            "empty style": {"working_style": ""},
            "patterns not list": {"patterns": "many"},
        }
        for label, override in cases.items():
            raw = valid_analysis()
            raw.update(override)
            analysis, errors = agentcat.reflect_validate_analysis(raw)
            self.assertIsNone(analysis, label)
            self.assertTrue(errors, label)
        self.assertEqual(agentcat.reflect_validate_analysis("text")[1], ["analysis must be a JSON object"])

    def test_validate_trims_rules_and_evidence(self):
        raw = valid_analysis(rules=["a", "b", "c", "d", "e"])
        raw["frictions"][0]["evidence"] = "e" * 1000
        analysis, errors = agentcat.reflect_validate_analysis(raw)
        self.assertEqual(errors, [])
        self.assertEqual(analysis["candidate_rules"], ["a", "b", "c"])
        self.assertEqual(len(analysis["frictions"][0]["evidence"]), agentcat.REFLECT_EVIDENCE_MAX_CHARS)

    def test_duplicate_findings_merge_counts_and_keep_earliest_turn(self):
        raw = valid_analysis(
            frictions=[
                {"category": "missing_context", "attribution": "user_actionable", "evidence": "same quote", "turn": 8, "note": "first note"},
                {"category": "missing_context", "attribution": "ai_capability", "evidence": "same quote", "turn": 2, "note": "second note"},
                {"category": "missing_context", "attribution": "user_actionable", "evidence": "different quote", "turn": 3},
            ],
            patterns=[
                {"category": "context_provided", "evidence": "same pattern", "turn": 7},
                {"category": "context_provided", "evidence": "same pattern", "turn": 1},
            ],
        )
        analysis, errors = agentcat.reflect_validate_analysis(raw)
        self.assertEqual(errors, [])
        self.assertEqual(len(analysis["frictions"]), 2)
        self.assertEqual(analysis["frictions"][0]["count"], 2)
        self.assertEqual(analysis["frictions"][0]["turn"], 2)
        self.assertEqual(analysis["frictions"][0]["note"], "first note")
        self.assertEqual(analysis["frictions"][1]["count"], 1)
        self.assertEqual(len(analysis["patterns"]), 1)
        self.assertEqual(analysis["patterns"][0]["count"], 2)

        # Presentation repeats the merge for rows stored before WP37.
        legacy = copy.deepcopy(raw)
        legacy["frictions"] = legacy["frictions"][:2]
        presented = agentcat.reflect_present_analysis(
            legacy,
            {"id": "synthetic:session", "tokens": 100, "cost_usd": 1.0, "length_bucket": "short"},
            {"runner": "stub", "lang": "ko"},
        )
        self.assertEqual(len(presented["frictions"]), 1)
        self.assertEqual(presented["frictions"][0]["count"], 2)
        self.assertEqual(presented["frictions"][0]["turn"], 2)
        self.assertEqual(len(presented["patterns"]), 1)
        self.assertEqual(presented["patterns"][0]["count"], 2)
        self.assertEqual(presented["lang"], "ko")

    def test_retry_once_with_nudge_then_success(self):
        runner = StubRunner(responses=["I think the answer is: not json", json.dumps(valid_analysis())])
        analysis, meta = agentcat.reflect_analyze_payload({"turn_list": []}, runner)
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(meta["runner"], "stub")
        self.assertAlmostEqual(meta["cost_usd"], 0.02)
        self.assertEqual(len(runner.prompts), 2)
        self.assertIn("Return only the JSON object", runner.prompts[1])
        self.assertNotIn("Return only the JSON object", runner.prompts[0])
        self.assertEqual(analysis["prompt_quality_mean"], 3.6)

    def test_fails_after_two_invalid_replies(self):
        runner = StubRunner(responses=["{}", "{}"])
        with self.assertRaises(agentcat.ReflectAnalysisError) as ctx:
            agentcat.reflect_analyze_payload({"turn_list": []}, runner)
        self.assertTrue(ctx.exception.errors)
        self.assertEqual(len(runner.prompts), 2)

    def test_parse_claude_cli_output_object_and_array(self):
        obj = {"type": "result", "subtype": "success", "is_error": False, "result": "{\"ok\": true}", "session_id": "s1", "total_cost_usd": 0.05}
        parsed = agentcat.reflect_parse_claude_cli_output(json.dumps(obj))
        self.assertEqual(parsed, {"text": "{\"ok\": true}", "cost_usd": 0.05, "session_id": "s1"})
        events = [{"type": "system"}, {"type": "assistant", "message": {}}, obj]
        self.assertEqual(agentcat.reflect_parse_claude_cli_output(json.dumps(events))["text"], "{\"ok\": true}")
        # progress line before the JSON
        self.assertEqual(agentcat.reflect_parse_claude_cli_output("warming up\n" + json.dumps(obj))["session_id"], "s1")
        with self.assertRaises(agentcat.ReflectRunnerError):
            agentcat.reflect_parse_claude_cli_output(json.dumps({**obj, "is_error": True}))
        with self.assertRaises(agentcat.ReflectRunnerError):
            agentcat.reflect_parse_claude_cli_output("")
        with self.assertRaises(agentcat.ReflectRunnerError):
            agentcat.reflect_parse_claude_cli_output(json.dumps([{"type": "system"}]))

    def test_claude_native_runner_command_stdin_cwd_env_timeout(self):
        self.subprocess.stop()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"type": "result", "result": "{}", "session_id": "x", "total_cost_usd": 0.1}), stderr="")

        with patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}):
            with patch.object(agentcat.subprocess, "run", side_effect=fake_run):
                runner = agentcat.ClaudeNativeRunner(binary="/fake/claude")
                self.assertTrue(runner.available())
                result = runner.run("PROMPT")
        self.subprocess.start()
        self.assertEqual(captured["cmd"], ["/fake/claude", "-p", "--model", "sonnet", "--output-format", "json"])
        self.assertEqual(captured["kwargs"]["input"], "PROMPT")
        self.assertEqual(captured["kwargs"]["timeout"], agentcat.REFLECT_ANALYZER_TIMEOUT_SECONDS)
        self.assertEqual(captured["kwargs"]["cwd"], str(agentcat.REFLECT_SCRATCH_DIR))
        self.assertNotIn("CLAUDECODE", captured["kwargs"]["env"])
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", captured["kwargs"]["env"])
        self.assertEqual(result["text"], "{}")
        self.assertTrue(agentcat.REFLECT_SCRATCH_DIR.is_dir())

    def test_claude_native_runner_timeout_and_missing_binary(self):
        self.subprocess.stop()
        with patch.object(agentcat.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 120)):
            with self.assertRaises(agentcat.ReflectRunnerError):
                agentcat.ClaudeNativeRunner(binary="/fake/claude").run("p")
        self.subprocess.start()
        with patch.object(agentcat, "reflect_find_claude_binary", return_value=None):
            runner = agentcat.ClaudeNativeRunner()
            self.assertFalse(runner.available())
            with self.assertRaises(agentcat.ReflectRunnerError):
                runner.run("p")

    def test_runner_factory_applies_configured_timeout(self):
        runner = agentcat.reflect_runner({"runner": "claude-native", "analyzerTimeoutSeconds": 900})
        self.assertEqual(runner.timeout_seconds, 900.0)
        self.assertEqual(agentcat.reflect_runner({"runner": "claude-native"}).timeout_seconds, agentcat.REFLECT_ANALYZER_TIMEOUT_SECONDS)

    def test_analyzer_timeout_defaults_and_clamps(self):
        self.assertEqual(agentcat.reflect_analyzer_timeout(None), agentcat.REFLECT_ANALYZER_TIMEOUT_SECONDS)
        self.assertEqual(agentcat.reflect_analyzer_timeout("soon"), agentcat.REFLECT_ANALYZER_TIMEOUT_SECONDS)
        self.assertEqual(agentcat.reflect_analyzer_timeout(5), agentcat.REFLECT_ANALYZER_TIMEOUT_MIN_SECONDS)
        self.assertEqual(agentcat.reflect_analyzer_timeout(10_000), agentcat.REFLECT_ANALYZER_TIMEOUT_MAX_SECONDS)
        self.assertEqual(agentcat.reflect_analyzer_timeout("300"), 300.0)
        self.assertEqual(agentcat.reflect_config()["analyzerTimeoutSeconds"], agentcat.REFLECT_ANALYZER_TIMEOUT_SECONDS)

    def test_runner_factory(self):
        self.assertIsInstance(agentcat.reflect_runner({"runner": "claude-native"}), agentcat.ClaudeNativeRunner)
        with self.assertRaises(agentcat.ReflectRunnerError):
            agentcat.reflect_runner({"runner": "carrier-pigeon"})


# ---------------------------------------------------------------------------
# 4. store
# ---------------------------------------------------------------------------


class StoreTests(ReflectTestCase):
    def test_upgrade_reindexes_unchanged_claude_provenance_once_and_keeps_analysis(self):
        path = self.write_claude_session([
            claude_user("inspect the panel", 0, entrypoint="cli"),
            claude_assistant("Done.", 1),
            claude_user("Worker complete", 2, promptSource="system"),
        ])
        codex = self.write_codex_session()
        agentcat.reflect_sync(files=[("claude", path), ("codex", codex)])
        session_id = "claude:" + CLAUDE_UUID
        analysis, _ = agentcat.reflect_validate_analysis(valid_analysis())
        agentcat.reflect_store_analysis(session_id, "short", analysis, {"runner": "stub"})
        # Model a pre-upgrade index, including unchanged file metadata.
        with closing(sqlite3.connect(agentcat.REFLECT_DB)) as conn:
            conn.execute("delete from state where key = 'claude_prompt_provenance_v1'")
            conn.execute("update sessions set automated = 1, user_turns = 2 where id = ?", (session_id,))
            conn.commit()
        refreshed = agentcat.reflect_sync(files=[("claude", path), ("codex", codex)])
        self.assertEqual((refreshed["indexed"], refreshed["skipped"]), (1, 1))
        row = agentcat.reflect_get_session(session_id)
        self.assertFalse(row["automated"])
        self.assertEqual(row["user_turns"], 1)
        self.assertEqual(agentcat.reflect_get_analysis(session_id)["analysis"], analysis)
        repeated = agentcat.reflect_sync(files=[("claude", path), ("codex", codex)])
        self.assertEqual((repeated["indexed"], repeated["skipped"]), (0, 2))
        self.assertEqual(self.subprocess_calls, [])

    def test_upgrade_preserves_analysis_coverage_after_prompt_bucket_shrinks(self):
        path = self.write_claude_session()
        agentcat.reflect_sync(files=[("claude", path)])
        session_id = "claude:" + CLAUDE_UUID
        analysis, _ = agentcat.reflect_validate_analysis(valid_analysis())
        old_turns = 25
        old_bucket = agentcat.reflect_length_bucket(old_turns)
        agentcat.reflect_store_analysis(session_id, old_bucket, analysis, {"runner": "stub"})
        with closing(sqlite3.connect(agentcat.REFLECT_DB)) as conn:
            conn.execute("delete from state where key = 'claude_prompt_provenance_v1'")
            conn.execute("update sessions set automated = 1, user_turns = ? where id = ?", (old_turns, session_id))
            conn.commit()
        agentcat.reflect_sync(files=[("claude", path)])
        self.assertEqual(agentcat.reflect_pending_sessions(7, now=BASE_TS + dt.timedelta(days=1)), [])
        result = agentcat.reflect_analyze_session(session_id, runner=StubRunner(responses=[]))
        self.assertTrue(result["cached"])
        self.assertEqual(result["analysis"], analysis)
        self.assertEqual(self.subprocess_calls, [])

    def test_upgrade_notification_only_row_keeps_counts_without_coaching(self):
        path = self.write_claude_session([
            claude_user("Task completed", 0, promptSource="system"),
            claude_assistant("Acknowledged.", 1),
        ])
        agentcat.reflect_sync(files=[("claude", path)])
        session_id = "claude:" + CLAUDE_UUID
        analysis, _ = agentcat.reflect_validate_analysis(valid_analysis())
        agentcat.reflect_store_analysis(session_id, "short", analysis, {"runner": "stub"})
        with closing(sqlite3.connect(agentcat.REFLECT_DB)) as conn:
            conn.execute("delete from state where key = 'claude_prompt_provenance_v1'")
            conn.execute("update sessions set user_turns = 1, counts_only = 0 where id = ?", (session_id,))
            conn.commit()
        refreshed = agentcat.reflect_sync(files=[("claude", path)])
        self.assertEqual(refreshed["indexed"], 1)
        self.assertTrue(agentcat.reflect_get_session(session_id)["counts_only"])
        self.assertEqual(agentcat.reflect_all_analyses(), [])
        self.assertEqual(agentcat.reflect_get_analysis(session_id)["analysis"], analysis)
        self.assertEqual(agentcat.reflect_pending_sessions(7, now=BASE_TS + dt.timedelta(days=1)), [])
        self.assertEqual(agentcat.reflect_sync(files=[("claude", path)])["skipped"], 1)

    def test_init_db_migrates_analysis_language_column(self):
        with closing(sqlite3.connect(agentcat.REFLECT_DB)) as conn:
            conn.execute(
                """
                create table analyses (
                  id integer primary key autoincrement, session_id text not null,
                  length_bucket text not null, analysis_json text not null,
                  runner text, model text, prompt_quality real,
                  frictions integer not null default 0, patterns integer not null default 0,
                  created_at text not null, created_day text not null,
                  unique(session_id, length_bucket)
                )
                """
            )
            conn.commit()
        agentcat.reflect_init_db()
        with closing(sqlite3.connect(agentcat.REFLECT_DB)) as conn:
            columns = {row[1] for row in conn.execute("pragma table_info(analyses)")}
        self.assertIn("lang", columns)

    def test_sync_indexes_and_skips_unchanged(self):
        self.write_claude_session()
        self.write_codex_session()
        first = agentcat.reflect_sync()
        self.assertEqual((first["indexed"], first["skipped"], first["sessions"]), (2, 0, 2))
        second = agentcat.reflect_sync()
        self.assertEqual((second["indexed"], second["skipped"]), (0, 2))
        # append a turn → the file changes → re-indexed
        with self.claude_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(claude_user("one more", 20)) + "\n")
            fh.write(json.dumps(claude_assistant("sure", 21)) + "\n")
        third = agentcat.reflect_sync()
        self.assertEqual((third["indexed"], third["skipped"]), (1, 1))
        self.assertEqual(agentcat.reflect_get_session("claude:" + CLAUDE_UUID)["user_turns"], 3)
        self.assertTrue(agentcat.REFLECT_DB.exists())
        self.assertIsNone(agentcat.reflect_get_session("claude:nope"))

    def test_store_analysis_is_idempotent_per_bucket(self):
        analysis, _ = agentcat.reflect_validate_analysis(valid_analysis())
        meta = {"runner": "stub", "model": "m", "lang": "ko"}
        agentcat.reflect_store_analysis("claude:x", "short", analysis, meta)
        agentcat.reflect_store_analysis("claude:x", "short", analysis, meta)
        agentcat.reflect_store_analysis("claude:x", "long", analysis, meta)
        with agentcat.closing(agentcat._reflect_connect()) as conn:
            rows = conn.execute("select session_id, length_bucket from analyses order by id").fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [("claude:x", "short"), ("claude:x", "long")])
        record = agentcat.reflect_get_analysis("claude:x", "short")
        self.assertEqual(record["analysis"]["prompt_quality_mean"], 3.6)
        self.assertEqual(record["runner"], "stub")
        self.assertEqual(record["lang"], "ko")
        self.assertEqual(agentcat.reflect_get_analysis("claude:x")["length_bucket"], "long")
        self.assertIsNone(agentcat.reflect_get_analysis("claude:y"))

    def test_analyze_session_stores_then_caches(self):
        self.write_claude_session()
        runner = StubRunner()
        result = agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID, runner=runner)
        self.assertFalse(result["cached"])
        self.assertEqual(result["length_bucket"], "short")
        self.assertEqual(result["session"]["project"], "my-repo")
        self.assertEqual(result["analysis"]["prompt_quality_mean"], 3.6)
        self.assertEqual(result["meta"]["attempts"], 1)
        self.assertEqual(result["meta"]["lang"], "en")
        again = agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID, runner=StubRunner(responses=[]))
        self.assertTrue(again["cached"])
        localized_runner = StubRunner()
        localized = agentcat.reflect_analyze_session(
            "claude:" + CLAUDE_UUID, runner=localized_runner, lang="ko"
        )
        self.assertFalse(localized["cached"])
        self.assertEqual(localized["meta"]["lang"], "ko")
        self.assertIn("in Korean; keep quoted", localized_runner.prompts[0])
        forced = agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID, runner=StubRunner(), force=True)
        self.assertFalse(forced["cached"])
        self.assertEqual(self.subprocess_calls, [])

    def test_analyze_unknown_session_and_unavailable_runner(self):
        with self.assertRaises(agentcat.ReflectNotFound):
            agentcat.reflect_analyze_session("claude:missing", runner=StubRunner())
        self.write_claude_session()
        with patch.object(agentcat, "reflect_find_claude_binary", return_value=None):
            with self.assertRaises(agentcat.ReflectRunnerError):
                agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID)

    def test_list_sessions_window_and_flags(self):
        self.write_claude_session()
        self.write_codex_session()
        agentcat.reflect_sync()
        agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID, runner=StubRunner())
        now = BASE_TS + dt.timedelta(days=3)
        listed = agentcat.reflect_list_sessions(7, now=now)
        self.assertEqual({row["id"] for row in listed}, {"claude:" + CLAUDE_UUID, "codex:" + CODEX_UUID})
        by_id = {row["id"]: row for row in listed}
        self.assertTrue(by_id["claude:" + CLAUDE_UUID]["analyzed"])
        self.assertEqual(by_id["claude:" + CLAUDE_UUID]["prompt_quality"], 3.6)
        self.assertFalse(by_id["codex:" + CODEX_UUID]["analyzed"])
        self.assertIsNone(by_id["codex:" + CODEX_UUID]["prompt_quality"])
        self.assertEqual(agentcat.reflect_list_sessions(1, now=now), [])



# ---------------------------------------------------------------------------
# 5. weekly synthesis (pure)
# ---------------------------------------------------------------------------


class SynthesisTests(unittest.TestCase):
    def items(self):
        a1, _ = agentcat.reflect_validate_analysis(valid_analysis())
        a2, _ = agentcat.reflect_validate_analysis(valid_analysis(
            rules=["always run the test suite before claiming done!", "Ask before deleting files.", "Keep one logical change per commit."],
            frictions=[{"category": "missing_context", "attribution": "user_actionable", "evidence": "x"}],
            patterns=[{"category": "verification_requested", "evidence": "y"}, {"category": "clear_goal_upfront", "evidence": "z"}],
            scores={"clarity": 5, "context": 5, "constraints": 5, "success_criteria": 5, "scope": 5},
        ))
        return [
            {"session": {"id": "claude:a", "tool": "claude", "user_turns": 10, "tokens": 2000,
                         "cost_usd": 2.0, "started_at": ts(0)}, "analysis": a1},
            {"session": {"id": "codex:b", "tool": "codex", "user_turns": 10, "tokens": 1000,
                         "cost_usd": 1.0, "started_at": ts(3600)}, "analysis": a2},
        ]

    def test_counts_costs_rates_and_fluency(self):
        synthesis = agentcat.reflect_synthesize_week("2026-W36", self.items())
        self.assertEqual(synthesis["week"], "2026-W36")
        self.assertEqual(synthesis["sessions_analyzed"], 2)
        self.assertEqual(synthesis["user_turns"], 20)
        self.assertEqual(synthesis["cost_usd_total"], 3.0)
        top = synthesis["frictions_top"][0]
        self.assertEqual(top["category"], "missing_context")
        self.assertEqual(top["count"], 2)
        self.assertEqual(top["sessions"], 2)
        self.assertEqual(top["cost_usd"], 3.0)  # summed once per session
        self.assertEqual(top["attribution"]["user_actionable"], 2)
        self.assertEqual(synthesis["frictions_top"][1], {
            "category": "wrong_or_buggy_output", "count": 1, "sessions": 1, "cost_usd": 2.0,
            "attribution": {"user_actionable": 0, "ai_capability": 1, "environmental": 0},
        })
        self.assertEqual(synthesis["patterns_top"][0]["category"], "verification_requested")
        self.assertEqual(synthesis["patterns_top"][0]["count"], 2)
        self.assertEqual(synthesis["frictions_total"], 3)
        self.assertEqual(synthesis["patterns_total"], 3)
        self.assertEqual(synthesis["friction_rate_per_10_turns"], 1.5)
        self.assertEqual(synthesis["pattern_rate_per_10_turns"], 1.5)
        self.assertEqual(synthesis["prompt_quality"]["mean"], 4.3)  # (3.6 + 5) / 2
        self.assertEqual(synthesis["prompt_quality"]["by_dimension"]["clarity"], 4.5)
        self.assertEqual([row["prompt_quality"] for row in synthesis["prompt_quality"]["trend"]], [3.6, 5.0])
        expected = round(0.5 * ((4.3 - 1) / 4) + 0.3 * (1 - 1.0) + 0.2 * 1.0, 3)
        self.assertEqual(synthesis["fluency_score"], expected)
        self.assertEqual(synthesis["byTool"], [
            {"tool": "claude", "sessions": 1, "tokens": 2000, "costUSD": 2.0,
             "reworkRate": 1.0, "promptQuality": 3.6},
            {"tool": "codex", "sessions": 1, "tokens": 1000, "costUSD": 1.0,
             "reworkRate": 0.0, "promptQuality": 5.0},
        ])
        self.assertEqual(
            synthesis["toolComparison"],
            "Claude의 재작업 세션 비율은 100%로 Codex의 0%보다 높아요.",
        )
        self.assertEqual(len(synthesis["nextChecks"]), 3)
        self.assertEqual([check["id"] for check in synthesis["nextChecks"]], ["c1", "c2", "c3"])
        self.assertEqual(synthesis["nextChecks"][0]["derivedFrom"], "missing_context")
        self.assertEqual(synthesis["nextChecks"][0]["evidenceSessionIds"], ["claude:a", "codex:b"])
        self.assertTrue(synthesis["nextChecks"][0]["text"].endswith("요."))

    def test_next_checks_and_comparison_are_localized(self):
        expected_fragments = {
            "ko": "첫 문장",
            "en": "first sentence",
            "ja": "最初の一文",
            "zh-Hans": "第一句话",
        }
        for lang, fragment in expected_fragments.items():
            synthesis = agentcat.reflect_synthesize_week("2026-W36", self.items(), lang=lang)
            self.assertIn(fragment, synthesis["nextChecks"][0]["text"])
            self.assertIsInstance(synthesis["toolComparison"], str)
        with self.assertRaises(agentcat.ReflectError):
            agentcat.reflect_synthesize_week("2026-W36", self.items(), lang="fr")

    def test_automated_sessions_are_listed_but_excluded_from_quality_averages(self):
        items = self.items()
        items[0]["session"]["automated"] = True
        synthesis = agentcat.reflect_synthesize_week("2026-W36", items)
        self.assertEqual(synthesis["sessions_analyzed"], 2)
        self.assertEqual(synthesis["prompt_quality"]["mean"], 5.0)
        self.assertEqual([row["automated"] for row in synthesis["prompt_quality"]["trend"]], [True, False])
        by_tool = {row["tool"]: row for row in synthesis["byTool"]}
        self.assertIsNone(by_tool["claude"]["promptQuality"])
        self.assertEqual(by_tool["codex"]["promptQuality"], 5.0)

        included = agentcat.reflect_synthesize_week(
            "2026-W36", items, include_automated_prompt_quality=True
        )
        self.assertEqual(included["prompt_quality"]["mean"], 4.3)
        self.assertEqual({row["tool"]: row for row in included["byTool"]}["claude"]["promptQuality"], 3.6)

    def test_fluency_formula(self):
        self.assertEqual(agentcat.reflect_fluency_score(5, 0, 1), 1.0)
        self.assertEqual(agentcat.reflect_fluency_score(1, 1, 0), 0.0)
        self.assertEqual(agentcat.reflect_fluency_score(3, 0.5, 0.5), round(0.5 * 0.5 + 0.3 * 0.5 + 0.2 * 0.5, 3))
        # clamped: a friction rate above 1 cannot go negative, a pattern rate above 1 cannot exceed 1
        self.assertEqual(agentcat.reflect_fluency_score(3, 7, 9), agentcat.reflect_fluency_score(3, 1, 1))

    def test_rules_dedupe_by_normalized_text(self):
        synthesis = agentcat.reflect_synthesize_week("2026-W36", self.items())
        texts = [rule["text"] for rule in synthesis["rules"]]
        self.assertEqual(len(texts), 4)
        self.assertEqual(synthesis["rules"][0]["count"], 2)
        self.assertEqual(synthesis["rules"][0]["text"], "Always run the test suite before claiming done.")
        self.assertEqual(synthesis["rules"][1]["count"], 2)
        self.assertEqual(sorted(synthesis["rules"][0]["sessions"]), ["claude:a", "codex:b"])
        self.assertNotIn("always run the test suite before claiming done!", texts)
        self.assertLessEqual(len(texts), 5)
        self.assertEqual(agentcat.reflect_normalize_rule("  Always, RUN tests!! "), "always run tests")

    def test_empty_week(self):
        synthesis = agentcat.reflect_synthesize_week("2026-W01", [])
        self.assertEqual(synthesis["sessions_analyzed"], 0)
        self.assertIsNone(synthesis["fluency_score"])
        self.assertIsNone(synthesis["prompt_quality"]["mean"])
        self.assertEqual(synthesis["frictions_top"], [])
        self.assertEqual(synthesis["byTool"], [])
        self.assertIsNone(synthesis["toolComparison"])
        self.assertEqual(synthesis["nextChecks"], [])
        self.assertEqual(synthesis["rules"], [])

    def test_iso_week_helpers(self):
        self.assertEqual(agentcat.reflect_iso_week_key(dt.datetime(2026, 9, 2, 12, 0)), "2026-W36")
        self.assertEqual(agentcat.reflect_iso_week_key(dt.datetime(2026, 1, 1, 12, 0)), "2026-W01")
        self.assertEqual(agentcat.reflect_week_of(ts(0)), agentcat.reflect_iso_week_key(BASE_TS.astimezone()))
        self.assertIsNone(agentcat.reflect_week_of("garbage"))


class WeekStoreTests(ReflectTestCase):
    def test_synthesis_and_rules_persist(self):
        synthesis = agentcat.reflect_synthesize_week("2026-W36", [])
        synthesis["rules"] = [{"text": "Always run tests.", "count": 2, "sessions": []}]
        agentcat.reflect_store_synthesis(synthesis)
        self.assertEqual(agentcat.reflect_get_synthesis("2026-W36")["week"], "2026-W36")
        with agentcat.closing(agentcat._reflect_connect()) as conn:
            rules = conn.execute("select week, text, normalized, count from rules").fetchall()
        self.assertEqual([tuple(row) for row in rules], [("2026-W36", "Always run tests.", "always run tests", 2)])
        self.assertIsNone(agentcat.reflect_get_synthesis("2020-W01"))


# ---------------------------------------------------------------------------
# 6. HTTP + CLI
# ---------------------------------------------------------------------------


class HttpTests(ReflectTestCase):
    def setUp(self):
        super().setUp()
        self.server = agentcat.ThreadingHTTPServer(("127.0.0.1", 0), agentcat.AgentCatHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2.0)
        self.server.server_close()
        super().tearDown()

    def request(self, path, method="GET", body=None, host=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if host is not None:
            req.add_header("Host", host)
        self.network.stop()
        try:
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            self.network.start()

    def request_text(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        self.network.stop()
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read().decode("utf-8")
        finally:
            self.network.start()

    def test_host_guard_applies_to_reflect_routes(self):
        status, payload = self.request("/reflect/week", host="evil.example.com")
        self.assertEqual((status, payload["error"]), (403, "forbidden"))
        status, _ = self.request("/reflect/analyze/x", method="POST", body={}, host="evil.example.com")
        self.assertEqual(status, 403)

    def test_explicit_disabled_config_returns_app_error(self):
        agentcat.REFLECT_CONFIG_FILE.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        for path in ("/reflect/week", "/reflect/sessions", "/reflect/rules", "/reflect/session/s-001", "/reflect/report.html"):
            status, payload = self.request(path)
            self.assertEqual((status, payload["error"]), (403, "reflect_disabled"), path)
        status, payload = self.request("/reflect/analyze/s-001", method="POST", body={})
        self.assertEqual((status, payload["error"]), (403, "reflect_disabled"))

    def test_week_sessions_session_rules_endpoints(self):
        self.write_claude_session()
        agentcat.reflect_sync()
        session_id = "claude:" + CLAUDE_UUID
        agentcat.reflect_analyze_session(session_id, runner=StubRunner())
        week = agentcat.reflect_week_of(ts(0))

        status, payload = self.request("/reflect/week?iso=" + week)
        self.assertEqual(status, 200)
        self.assertEqual(payload["week"], week)
        self.assertEqual(payload["sessions_analyzed"], 1)
        self.assertFalse(payload["stored"])
        self.assertIsNotNone(payload["fluency_score"])
        self.assertEqual(payload["byTool"][0]["tool"], "claude-code")
        self.assertEqual(payload["sessionsAnalyzed"], 1)
        self.assertEqual(len(payload["trend"]), 8)
        self.assertEqual(set(payload["promptQuality"]), {"clarity", "context", "constraints", "successCriteria", "scope"})
        self.assertEqual(len(payload["nextChecks"]), 3)

        status, payload = self.request("/reflect/week?iso=" + week + "&lang=en")
        self.assertEqual(status, 200)
        self.assertIn("first sentence", payload["nextChecks"][0]["text"])
        status, payload = self.request("/reflect/week?iso=" + week + "&lang=fr")
        self.assertEqual((status, payload["error"]), (400, "reflect_bad_request"))

        status, payload = self.request("/reflect/week?iso=bogus")
        self.assertEqual((status, payload["error"]), (400, "reflect_bad_request"))

        with patch.object(agentcat, "reflect_iso_week_key", return_value=week):
            status, payload = self.request("/reflect/week")
        self.assertEqual((status, payload["week"]), (200, week))

        now = BASE_TS + dt.timedelta(days=1)
        with patch.object(agentcat.dt, "datetime", wraps=dt.datetime) as fake_dt:
            fake_dt.now.return_value = now
            status, payload = self.request("/reflect/sessions?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(payload["days"], 7)
        self.assertEqual([row["id"] for row in payload["sessions"]], [session_id])
        self.assertTrue(payload["sessions"][0]["analyzed"])
        self.assertEqual(payload["sessions"][0]["project"], "my-repo")
        status, payload = self.request("/reflect/sessions?days=abc")
        self.assertEqual(status, 400)

        status, payload = self.request("/reflect/session/" + session_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], session_id)
        self.assertEqual(payload["tool"], "claude-code")
        self.assertEqual(payload["session"]["id"], session_id)
        self.assertEqual(payload["analysis"]["prompt_quality_mean"], 3.6)
        self.assertEqual(payload["analysis"]["promptQuality"]["successCriteria"]["score"], 2)
        self.assertEqual(payload["analysis_meta"]["runner"], "stub")
        self.assertNotIn("turn_list", json.dumps(payload))
        status, payload = self.request("/reflect/session/claude:missing")
        self.assertEqual((status, payload["error"]), (404, "session_not_found"))
        self.assertEqual(payload["legacy_error"], "not_found")

        status, payload = self.request("/reflect/rules?week=" + week)
        self.assertEqual(status, 200)
        self.assertEqual(payload["week"], week)
        self.assertEqual([rule["text"] for rule in payload["rules"]], valid_analysis()["candidate_rules"])

        status, payload = self.request("/reflect/nope")
        self.assertEqual(status, 404)

    def test_post_analyze_uses_configured_runner(self):
        self.write_codex_session()
        session_id = "codex:" + CODEX_UUID
        runner = StubRunner()
        with patch.object(agentcat, "reflect_runner", return_value=runner):
            status, payload = self.request("/reflect/analyze/" + session_id + "?lang=ko", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["cached"])
        self.assertEqual(payload["session"]["project"], "codex-repo")
        self.assertEqual(payload["analysis_meta"]["lang"], "ko")
        self.assertEqual(payload["analysis"]["lang"], "ko")
        self.assertEqual(payload["meta"]["lang"], "ko")
        self.assertIn("in Korean; keep quoted", runner.prompts[0])
        with patch.object(agentcat, "reflect_runner", return_value=StubRunner(responses=[])):
            status, payload = self.request("/reflect/analyze/" + session_id + "?lang=ko", method="POST", body={})
        self.assertTrue(payload["cached"])
        with patch.object(agentcat, "reflect_runner", return_value=StubRunner()):
            status, payload = self.request("/reflect/analyze/" + session_id, method="POST", body={"force": True})
        self.assertFalse(payload["cached"])
        status, payload = self.request("/reflect/analyze/claude:missing", method="POST", body={})
        self.assertEqual((status, payload["error"]), (404, "session_not_found"))
        with patch.object(agentcat, "reflect_find_claude_binary", return_value=None):
            status, payload = self.request("/reflect/analyze/" + session_id, method="POST", body={"force": True})
        self.assertEqual((status, payload["error"]), (503, "runner_unavailable"))
        with patch.object(agentcat, "reflect_runner", return_value=StubRunner(responses=["{}", "{}"])):
            status, payload = self.request("/reflect/analyze/" + session_id, method="POST", body={"force": True})
        self.assertEqual((status, payload["error"]), (502, "analysis_failed"))
        status, payload = self.request("/reflect/analyze/" + session_id + "?lang=xx", method="POST", body={})
        self.assertEqual((status, payload["error"]), (400, "reflect_bad_request"))
        self.assertEqual(self.subprocess_calls, [])

    def test_post_sync(self):
        self.write_claude_session()
        status, payload = self.request("/reflect/sync", method="POST", body={})
        self.assertEqual((status, payload["indexed"]), (200, 1))

    def test_self_contained_html_report_has_localized_four_parts(self):
        self.write_claude_session()
        self.write_codex_session()
        agentcat.reflect_sync()
        agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID, runner=StubRunner())
        agentcat.reflect_analyze_session("codex:" + CODEX_UUID, runner=StubRunner())
        week = agentcat.reflect_week_of(ts(0))
        headings = {
            "ko": ("한눈에", "잘한 점", "개선점", "앞으로의 체크 방식"),
            "en": ("At a glance", "What worked", "What to improve", "Checks for next time"),
            "ja": ("一目で", "良かった点", "改善点", "今後のチェック方法"),
            "zh-Hans": ("一览", "做得好的地方", "改进点", "今后的检查方式"),
        }
        for lang, expected in headings.items():
            status, content_type, document = self.request_text(
                "/reflect/report.html?week={}&lang={}".format(week, urllib.parse.quote(lang))
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "text/html; charset=utf-8")
            for heading in expected:
                self.assertIn("<h2>{}</h2>".format(heading), document)
            self.assertIn('class="comparison"', document)
            self.assertNotIn("http://", document)
            self.assertNotIn("https://", document)
            self.assertNotIn("<link", document)
            self.assertNotIn("src=", document)


class CliTests(ReflectTestCase):
    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = agentcat.main(argv)
        return code, buf.getvalue()

    def test_reflect_subcommands(self):
        self.write_claude_session()
        code, out = self.run_cli(["reflect", "sync"])
        self.assertEqual(code, 0)
        self.assertIn("indexed 1", out)
        code, out = self.run_cli(["reflect", "sync", "--json"])
        self.assertEqual(json.loads(out)["skipped"], 1)

        runner = StubRunner()
        with patch.object(agentcat, "reflect_runner", return_value=runner):
            code, out = self.run_cli(["reflect", "analyze", "claude:" + CLAUDE_UUID, "--lang", "zh-Hans"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["cached"])
        self.assertEqual(payload["analysis"]["candidate_rules"], valid_analysis()["candidate_rules"])
        self.assertEqual(payload["tool"], "claude-code")
        self.assertEqual(payload["analysis"]["runner"], "stub")
        self.assertEqual(payload["analysis_meta"]["lang"], "zh-Hans")
        self.assertIn("in Simplified Chinese; keep quoted", runner.prompts[0])

        week = agentcat.reflect_week_of(ts(0))
        code, out = self.run_cli(["reflect", "week", week])
        self.assertEqual(code, 0)
        week_payload = json.loads(out)
        self.assertEqual(week_payload["sessions_analyzed"], 1)
        self.assertEqual(week_payload["sessionsAnalyzed"], 1)
        code, out = self.run_cli(["reflect", "week", week, "--store"])
        self.assertTrue(json.loads(out)["stored"])
        self.assertIsNotNone(agentcat.reflect_get_synthesis(week))

        code, _ = self.run_cli(["reflect", "analyze", "claude:missing"])
        self.assertEqual(code, 1)
        parser = agentcat.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["reflect", "dance"])
        self.assertEqual(self.subprocess_calls, [])


# ---------------------------------------------------------------------------
# 6b. daemon -> app fixture contract
# ---------------------------------------------------------------------------


class ReflectContractTests(ReflectTestCase):
    """The app fixtures are the executable TED §4.1 presentation contract."""

    def fixture(self, name):
        return json.loads((REFLECT_FIXTURES / name).read_text(encoding="utf-8"))

    def assert_fixture_shape(self, expected, actual, path="$"):
        if isinstance(expected, dict):
            self.assertIsInstance(actual, dict, path)
            for key, value in expected.items():
                self.assertIn(key, actual, path + "." + key)
                self.assert_fixture_shape(value, actual[key], path + "." + key)
            return
        if isinstance(expected, list):
            self.assertIsInstance(actual, list, path)
            for index, (value, actual_value) in enumerate(zip(expected, actual)):
                self.assert_fixture_shape(value, actual_value, "{}[{}]".format(path, index))
            return
        self.assertIs(type(actual), type(expected), path)  # bool and int must not be conflated

    def contract_analysis(self, score=4.0, friction="missing_context", pattern="explicit_constraints"):
        return {
            "schema_version": 1,
            "frictions": [{
                "category": friction,
                "attribution": "user_actionable",
                "evidence": "name the affected surface before changing it",
                "turn": 1,
                "note": "Name the surface and gesture first.",
            }],
            "patterns": [{
                "category": pattern,
                "evidence": "do not touch the adjacent API",
                "turn": 2,
                "note": "",
            }],
            "prompt_quality": {
                name: {"score": score, "before": "fix it", "after": "Fix the popover overlap and run tests."}
                for name in agentcat.REFLECT_PROMPT_DIMENSIONS
            },
            "prompt_quality_mean": score,
            "working_style": "Opens with constraints. Verifies the result. Corrects scope quickly.",
            "candidate_rules": [
                "Name the surface and gesture before describing a UX bug.",
                "Quote globs and use absolute paths.",
                "Stay on the reported bug.",
            ],
        }

    def session(self, session_id, tool="claude", started=None, automated=False):
        return {
            "id": session_id,
            "tool": tool,
            "project": "agent-cat",
            "path": "/private/synthetic/{}.jsonl".format(session_id),
            "started_at": started or "2026-09-05T01:10:00Z",
            "ended_at": "2026-09-05T03:42:00Z",
            "turns": 41,
            "user_turns": 12,
            "tool_calls": 31,
            "tokens": 1832000,
            "cost_usd": 3.1,
            "tool_call_counts": {"Read": 1},
            "model": "synthetic",
            "counts_only": False,
            "automated": automated,
            "length_bucket": "long",
        }

    def test_week_fixture_keys_and_types(self):
        week = "2026-W36"
        week_keys = agentcat.reflect_recent_week_keys(week)
        items = []
        for index, historical_week in enumerate(week_keys[:-1]):
            monday = dt.date.fromisocalendar(int(historical_week[:4]), int(historical_week[-2:]), 1)
            started = dt.datetime.combine(monday, dt.time(10), tzinfo=dt.timezone.utc).isoformat().replace("+00:00", "Z")
            items.append({
                "session": self.session("history-{}".format(index), "codex", started),
                "analysis": self.contract_analysis(score=3.0 + index / 10.0),
            })

        current_specs = [
            ("s-001", "claude", "missing_context", "clear_goal_upfront"),
            ("s-002", "codex", "scope_creep", "delegation_to_tools"),
            ("s-003", "gemini", "tool_or_environment_failure", "explicit_constraints"),
            ("s-004", "kimi", "wrong_or_buggy_output", "verification_requested"),
        ]
        current_items = []
        for index, (session_id, tool, friction, pattern) in enumerate(current_specs):
            analysis = self.contract_analysis(score=3.5 + index / 10.0, friction=friction, pattern=pattern)
            analysis["patterns"][0].update({
                "tool": tool,
                "savedTurns": index + 1,
                "savedTokens": 1000 * (index + 1),
            })
            current_items.append({"session": self.session(session_id, tool), "analysis": analysis})
        items.extend(current_items)
        synthesis = agentcat.reflect_synthesize_week(week, current_items, lang="en")
        actual = agentcat.reflect_present_week(synthesis, items, sessions_total=19, lang="en")
        self.assert_fixture_shape(self.fixture("reflect-week.json"), actual)
        self.assertEqual(len(actual["trend"]), 8)
        self.assertEqual(actual["byTool"][0]["tool"], "claude-code")
        self.assertLessEqual(len(actual["frictions"][0]["example"]["quote"].split()), 20)
        for legacy_key in (
            "fluency_score", "sessions_analyzed", "frictions_top", "patterns_top",
            "prompt_quality", "generated_at", "friction_rate_per_10_turns",
        ):
            self.assertIn(legacy_key, actual)

    def test_sessions_fixture_keys_and_types(self):
        rows = []
        fixture = self.fixture("reflect-sessions.json")
        tools = ("claude", "codex", "gemini", "kimi")
        for index, expected in enumerate(fixture["sessions"]):
            row = self.session("s-00{}".format(index + 1), tools[index], automated=expected.get("automated", False))
            if "project" not in expected:
                row.pop("project")
            record = None
            if expected.get("analysis") is not None and "analysis" in expected:
                score = float(expected["analysis"]["promptQuality"])
                analysis = self.contract_analysis(score=score)
                # Match fixture cardinality while exercising computed summaries.
                analysis["frictions"] *= int(expected["analysis"]["frictionCount"])
                analysis["patterns"] *= int(expected["analysis"]["patternCount"])
                record = {"analysis": analysis, "runner": "stub", "model": "stub", "created_at": ts(0), "length_bucket": "long"}
            rows.append(agentcat.reflect_present_session(row, record))
        actual = {"sessions": rows}
        self.assert_fixture_shape(fixture, actual)
        self.assertIsNone(actual["sessions"][2]["analysis"])
        self.assertIsNone(actual["sessions"][3]["analysis"])
        self.assertIn("user_turns", actual["sessions"][0])
        self.assertIn("cost_usd", actual["sessions"][0])

    def test_session_detail_fixture_keys_and_types(self):
        session = self.session("s-001")
        analysis = self.contract_analysis(score=4)
        analysis["prompt_quality"]["context"]["score"] = 3.5
        analysis["frictions"] *= 2
        analysis["patterns"] *= 3
        record = {
            "analysis": analysis,
            "runner": "claude-native",
            "model": "sonnet",
            "created_at": "2026-09-05T04:00:12.001Z",
            "length_bucket": "long",
        }
        actual = agentcat.reflect_present_detail(session, record)
        self.assert_fixture_shape(self.fixture("reflect-session-detail.json"), actual)
        self.assertEqual(actual["id"], "s-001")
        self.assertIn("session", actual)
        self.assertIn("analysis_meta", actual)

    def test_rules_error_and_queued_fixture_keys_and_types(self):
        rules = [
            agentcat._reflect_present_rule("Name the surface and the gesture before describing a UX bug.", 0),
            agentcat._reflect_present_rule("Quote globs and use absolute paths.", 1),
        ]
        self.assert_fixture_shape(self.fixture("reflect-rules.json"), {"rules": rules})

        agentcat.REFLECT_CONFIG_FILE.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        status, disabled = agentcat.reflect_http_get("/reflect/week", {})
        self.assertEqual(status, 403)
        self.assert_fixture_shape(self.fixture("reflect-error-disabled.json"), disabled)

        agentcat.REFLECT_CONFIG_FILE.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        with patch.object(agentcat, "reflect_analyze_session", return_value={"queued": True}):
            status, queued = agentcat.reflect_http_post("/reflect/analyze/s-001", {})
        self.assertEqual(status, 200)
        self.assert_fixture_shape(self.fixture("reflect-analyze-queued.json"), queued)

    def test_all_tool_values_use_app_ids(self):
        expected = {"claude-code", "codex", "gemini", "kimi", "cursor", "opencode", "copilot", "grok"}
        actual = {agentcat.reflect_app_tool_id(tool) for tool in agentcat.REFLECT_TOOLS}
        self.assertEqual(actual, expected)


# ---------------------------------------------------------------------------
# 7. scheduler + config
# ---------------------------------------------------------------------------


class SchedulerTests(ReflectTestCase):
    def test_enable_preserves_existing_config_and_refuses_malformed_file(self):
        original = {"enabled": False, "runner": "codex-exec", "unknown": {"keep": True}}
        agentcat.REFLECT_CONFIG_FILE.write_text(json.dumps(original), encoding="utf-8")

        status, payload = agentcat.reflect_http_post("/reflect/enable", {})

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"enabled": True})
        saved = json.loads(agentcat.REFLECT_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["runner"], "codex-exec")
        self.assertEqual(saved["unknown"], {"keep": True})

        agentcat.REFLECT_CONFIG_FILE.write_text("{not json", encoding="utf-8")
        status, payload = agentcat.reflect_http_post("/reflect/enable", {})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "reflect_bad_request")
        self.assertEqual(agentcat.REFLECT_CONFIG_FILE.read_text(encoding="utf-8"), "{not json")

    def test_default_config_written_once_and_disabled(self):
        self.assertTrue(agentcat.reflect_write_default_config())
        self.assertFalse(agentcat.reflect_write_default_config())
        raw = json.loads(agentcat.REFLECT_CONFIG_FILE.read_text())
        self.assertFalse(raw["enabled"])
        self.assertEqual(raw["runner"], "claude-native")
        self.assertEqual(raw["dailyCap"], 20)
        self.assertEqual(raw["lang"], "en")
        config = agentcat.reflect_config()
        self.assertFalse(config["enabled"])
        agentcat.REFLECT_CONFIG_FILE.write_text(json.dumps({"enabled": True, "dailyCap": "3", "unknown": 1}))
        self.assertFalse(agentcat.reflect_write_default_config())  # never overwrites the user's file
        config = agentcat.reflect_config()
        self.assertTrue(config["enabled"])
        self.assertEqual(config["dailyCap"], 3)
        self.assertNotIn("unknown", config)
        agentcat.REFLECT_CONFIG_FILE.write_text("{not json")
        self.assertFalse(agentcat.reflect_config()["enabled"])

    def test_due_logic_nightly_and_weekly(self):
        config = agentcat.reflect_config()
        sunday_18 = dt.datetime(2026, 9, 6, 18, 5)  # Sunday
        self.assertEqual(agentcat.reflect_scheduler_due(config, sunday_18, {}), ["weekly"])
        monday_03 = dt.datetime(2026, 9, 7, 3, 30)
        self.assertEqual(agentcat.reflect_scheduler_due(config, monday_03, {}), ["nightly"])
        self.assertEqual(agentcat.reflect_scheduler_due(config, monday_03, {"last_nightly_date": "2026-09-07"}), [])
        self.assertEqual(agentcat.reflect_scheduler_due(config, sunday_18, {"last_weekly_week": "2026-W36"}), [])
        self.assertEqual(agentcat.reflect_scheduler_due(config, dt.datetime(2026, 9, 7, 4, 0), {}), [])
        self.assertEqual(agentcat.reflect_scheduler_due(config, dt.datetime(2026, 9, 5, 18, 0), {}), [])  # Saturday
        both = dt.datetime(2026, 9, 6, 3, 0)
        self.assertEqual(agentcat.reflect_scheduler_due({**config, "weeklyHour": 3}, both, {}), ["nightly", "weekly"])

    def test_tick_is_off_unless_enabled(self):
        self.write_claude_session()
        result = agentcat.reflect_scheduler_tick(now=dt.datetime(2026, 9, 7, 3, 0), runner=StubRunner())
        self.assertEqual(result, {"ran": [], "skipped": "disabled"})
        self.assertFalse(agentcat.REFLECT_DB.exists())

    def test_nightly_waits_for_idle_then_runs_once(self):
        self.write_claude_session()
        config = {**agentcat.reflect_config(), "enabled": True, "lang": "ja"}
        now = dt.datetime(2026, 9, 7, 3, 0)
        busy = agentcat.reflect_scheduler_tick(now=now, config=config, runner=StubRunner(), idle=False)
        self.assertEqual(busy["ran"], [])
        self.assertEqual(busy["nightly"], {"skipped": "busy"})
        self.assertIsNone(agentcat.reflect_state_get("last_nightly_date"))
        runner = StubRunner()
        ran = agentcat.reflect_scheduler_tick(now=now + dt.timedelta(minutes=5), config=config, runner=runner, idle=True)
        self.assertEqual(ran["ran"], ["nightly"])
        self.assertEqual(ran["nightly"]["analyzed"], ["claude:" + CLAUDE_UUID])
        self.assertEqual(agentcat.reflect_state_get("last_nightly_date"), "2026-09-07")
        self.assertIn("in Japanese; keep quoted", runner.prompts[0])
        again = agentcat.reflect_scheduler_tick(now=now + dt.timedelta(minutes=10), config=config, runner=StubRunner(responses=[]), idle=True)
        self.assertEqual(again["ran"], [])
        self.assertEqual(len(runner.prompts), 1)
        # idle probe is consulted only when a nightly is due and no override is given
        with patch.object(agentcat, "reflect_daemon_idle", return_value=False) as probe:
            agentcat.reflect_scheduler_tick(now=dt.datetime(2026, 9, 8, 3, 0), config=config, runner=StubRunner())
        probe.assert_called_once()

    def test_nightly_cap_analyzes_twenty_and_queues_the_rest(self):
        for index in range(50):
            uuid = "%08x-0000-4000-8000-%012x" % (index, index)
            write_jsonl(self.claude_path(uuid), [
                claude_user("task %d" % index, index),
                claude_assistant("done", index + 1),
            ])
        config = {**agentcat.reflect_config(), "enabled": True}
        runner = StubRunner(responses=[json.dumps(valid_analysis())] * 50)
        now = dt.datetime(2026, 9, 7, 3, 0)
        result = agentcat.reflect_nightly(config, runner=runner, now=now)
        self.assertEqual(result["cap"], 20)
        self.assertEqual(len(result["analyzed"]), 20)
        self.assertEqual(result["queued"], 30)
        self.assertEqual(len(runner.prompts), 20)
        self.assertEqual(agentcat.reflect_analyses_created_on("2026-09-07"), 20)
        # a second run the same day analyzes nothing more
        second = agentcat.reflect_nightly(config, runner=runner, now=now + dt.timedelta(hours=1))
        self.assertEqual(second["analyzed"], [])
        self.assertEqual(second["already_today"], 20)
        self.assertEqual(second["queued"], 30)
        # next day: 20 more, 10 left
        third = agentcat.reflect_nightly(config, runner=runner, now=now + dt.timedelta(days=1))
        self.assertEqual(len(third["analyzed"]), 20)
        self.assertEqual(third["queued"], 10)

    def test_nightly_one_call_per_session_per_bucket(self):
        self.write_claude_session()
        config = {**agentcat.reflect_config(), "enabled": True}
        runner = StubRunner(responses=[json.dumps(valid_analysis())] * 3)
        now = dt.datetime(2026, 9, 7, 3, 0)
        agentcat.reflect_nightly(config, runner=runner, now=now)
        agentcat.reflect_nightly(config, runner=runner, now=now + dt.timedelta(days=1))
        self.assertEqual(len(runner.prompts), 1)
        # the session grows into the next bucket → exactly one more call
        extra = []
        for index in range(12):
            extra.append(claude_user("more %d" % index, 100 + index * 2))
            extra.append(claude_assistant("ok", 101 + index * 2))
        with self.claude_path().open("a", encoding="utf-8") as fh:
            for entry in extra:
                fh.write(json.dumps(entry) + "\n")
        agentcat.reflect_nightly(config, runner=runner, now=now + dt.timedelta(days=2))
        self.assertEqual(len(runner.prompts), 2)
        with agentcat.closing(agentcat._reflect_connect()) as conn:
            buckets = [row[0] for row in conn.execute("select length_bucket from analyses order by id")]
        self.assertEqual(buckets, ["short", "medium"])

    def test_nightly_stops_when_runner_unavailable(self):
        self.write_claude_session()
        self.write_codex_session()
        config = {**agentcat.reflect_config(), "enabled": True}

        class Broken(agentcat.ReflectRunner):
            name = "broken"

            def available(self):
                return True

            def run(self, prompt):
                raise agentcat.ReflectRunnerError("claude timed out")

        result = agentcat.reflect_nightly(config, runner=Broken(), now=dt.datetime(2026, 9, 7, 3, 0))
        self.assertEqual(result["analyzed"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["queued"], 1)

    def test_weekly_synthesis_stored_on_sunday(self):
        self.write_claude_session()
        agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID, runner=StubRunner())
        config = {**agentcat.reflect_config(), "enabled": True}
        sunday = dt.datetime(2026, 9, 6, 18, 0)
        result = agentcat.reflect_scheduler_tick(now=sunday, config=config, runner=StubRunner(responses=[]))
        self.assertEqual(result["ran"], ["weekly"])
        self.assertEqual(result["weekly"]["week"], "2026-W36")
        self.assertTrue(result["weekly"]["stored"])
        self.assertEqual(agentcat.reflect_state_get("last_weekly_week"), "2026-W36")
        stored = agentcat.reflect_get_synthesis("2026-W36")
        self.assertEqual(stored["sessions_analyzed"], 1 if agentcat.reflect_week_of(ts(0)) == "2026-W36" else 0)

    def test_daemon_idle_uses_motion_stage(self):
        with patch.object(agentcat, "terminal_activity_snapshot", return_value={"motionStage": "sleeping"}):
            self.assertTrue(agentcat.reflect_daemon_idle())
        with patch.object(agentcat, "terminal_activity_snapshot", return_value={"motionStage": "running"}):
            self.assertFalse(agentcat.reflect_daemon_idle())
        with patch.object(agentcat, "terminal_activity_snapshot", side_effect=RuntimeError("ps failed")):
            self.assertFalse(agentcat.reflect_daemon_idle())

    def test_run_daemon_writes_default_config_and_starts_scheduler(self):
        started = []

        class FakeServer:
            def __init__(self, *args, **kwargs):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        def fake_thread(*args, **kwargs):
            started.append(kwargs.get("target"))

            class T:
                def start(self):
                    pass

            return T()

        with patch.object(agentcat, "ThreadingHTTPServer", FakeServer), \
                patch.object(agentcat.threading, "Thread", side_effect=fake_thread), \
                patch.object(agentcat, "prune_events"), \
                patch.object(agentcat, "auto_update_enabled_status", return_value=(False, "off")), \
                patch.object(agentcat, "PRICING_FETCH_INTERVAL_SECONDS", 0), \
                patch.object(agentcat, "resolve_bind_host", return_value="127.0.0.1"), \
                redirect_stdout(io.StringIO()):
            code = agentcat.run_daemon(agentcat.argparse.Namespace(host="127.0.0.1", port=0))
        self.assertEqual(code, 0)
        self.assertIn(agentcat.reflect_scheduler_loop, started)
        self.assertFalse(json.loads(agentcat.REFLECT_CONFIG_FILE.read_text())["enabled"])


# ---------------------------------------------------------------------------
# 8. privacy boundary
# ---------------------------------------------------------------------------


class PrivacyTests(ReflectTestCase):
    def test_telemetry_summary_has_exactly_five_fields(self):
        synthesis = agentcat.reflect_synthesize_week("2026-W36", SynthesisTests().items())
        summary = agentcat.reflect_telemetry_summary(synthesis)
        self.assertEqual(
            set(summary),
            {"week", "fluency_score", "friction_top_category", "pattern_top_category", "sessions_analyzed"},
        )
        self.assertEqual(set(summary), set(agentcat.REFLECT_TELEMETRY_FIELDS))
        self.assertEqual(summary["week"], "2026-W36")
        self.assertEqual(summary["friction_top_category"], "missing_context")
        self.assertEqual(summary["pattern_top_category"], "verification_requested")
        self.assertEqual(summary["sessions_analyzed"], 2)
        self.assertIsInstance(summary["fluency_score"], float)
        for value in summary.values():
            self.assertNotIsInstance(value, (list, dict))
        empty = agentcat.reflect_telemetry_summary(agentcat.reflect_synthesize_week("2026-W01", []))
        self.assertEqual(empty, {"week": "2026-W01", "fluency_score": None, "friction_top_category": None,
                                 "pattern_top_category": None, "sessions_analyzed": 0})

    def test_only_the_runner_leaves_the_process(self):
        # network is blocked and subprocess.run asserts in setUp; a full
        # sync → analyze → week round-trip with a stubbed runner must touch neither.
        self.write_claude_session([
            claude_user("deploy with " + SEEDED_SECRETS[0] + " from /Users/alice/Code/my-repo", 0),
            claude_assistant("Deploying.", 1, tools=("Bash",)),
        ])
        self.write_codex_session()
        agentcat.reflect_sync()
        runner = StubRunner(responses=[json.dumps(valid_analysis())] * 2)
        agentcat.reflect_analyze_session("claude:" + CLAUDE_UUID, runner=runner)
        agentcat.reflect_analyze_session("codex:" + CODEX_UUID, runner=runner)
        agentcat.reflect_week(agentcat.reflect_week_of(ts(0)), store=True)
        self.assertEqual(self.subprocess_calls, [])
        for prompt in runner.prompts:
            self.assertNotIn(SEEDED_SECRETS[0], prompt)
            self.assertNotIn("/Users/alice", prompt)
        # nothing in the store carries transcript text or absolute project paths besides the file path column
        with agentcat.closing(agentcat._reflect_connect()) as conn:
            analyses = [row[0] for row in conn.execute("select analysis_json from analyses")]
            projects = [row[0] for row in conn.execute("select project from sessions")]
        for blob in analyses:
            self.assertNotIn(SEEDED_SECRETS[0], blob)
        self.assertEqual(sorted(projects), ["codex-repo", "my-repo"])

    def test_reflect_db_is_owner_only(self):
        agentcat.reflect_init_db()
        if os.name != "nt":
            self.assertEqual(agentcat.REFLECT_DB.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
