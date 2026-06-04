#!/usr/bin/env python3
"""End-to-end verification harness for the 12 local-usage providers.

This is NOT a unit test. It proves that each of the 12 newly-added
local-usage providers (cursor, goose, kiro, roo-code, kilo-code, cline,
qwen, crush, continue, pearai, llm, gptme) extracts tokens through the FULL
snapshot pipeline (build_snapshot()) when run against a realistic on-disk
fixture.

How it works
------------
1. Builds a throwaway temp directory used as a FAKE $HOME, and writes a
   realistic fixture for each provider at the EXACT path its parser reads
   (macOS paths), mirroring the formats from the parser functions in
   bin/agentcat and the unit tests in tests/test_agentcat.py.
   Every provider also gets at least one row the parser MUST skip
   (zero-token / wrong-type / child-session) so filtering is exercised.

2. bin/agentcat captures HOME and AGENTCAT_HOME at *module import time*, so
   the connector is run in a SUBPROCESS with HOME pointed at the fixture
   directory (and AGENTCAT_HOME at <fixture>/.agentcat so it never touches
   the real home). A tiny loader SourceFileLoader-loads bin/agentcat and
   prints json.dumps(build_snapshot()). QWEN_DATA_DIR and CRUSH_GLOBAL_DATA
   are also set explicitly because those two parsers honor env first.

3. The resulting snapshot JSON is parsed and, for each of the 8 providers,
   asserts: status == 'ok', aggregated tokens.all == the exact expected
   total from the fixture, and the expected model appears in .models.
   The mature five (codex/claude/gemini/opencode/copilot) are asserted to
   be present and not crashed (they will be not_found in the empty fake
   home, which is expected and fine).

4. Prints a PASS/FAIL matrix and exits non-zero if any of the 8 fail.

Re-runnable via a single:  python3 scripts/verify_providers_e2e.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTCAT_BIN = REPO_ROOT / "bin" / "agentcat"

# A recent timestamp so the rows land in today/week/month buckets too.
NOW = dt.datetime.now(dt.timezone.utc)
NOW_ISO = NOW.isoformat().replace("+00:00", "Z")
NOW_MS = int(NOW.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Token-math helpers (mirror bin/agentcat exactly so the expected totals in
# this script are derived, not hand-waved).
# ---------------------------------------------------------------------------
def char_token_estimate(text: str) -> int:
    if not isinstance(text, str) or not text:
        return 0
    return max(1, int((len(text) + 3) / 4))


def char_token_estimate_for_length(length: int) -> int:
    if length <= 0:
        return 0
    return max(1, int((length + 3) / 4))


# ---------------------------------------------------------------------------
# Fixture writers. Each returns (expected_total, expected_model).
# ---------------------------------------------------------------------------
def write_cursor(home: Path) -> Tuple[int, str]:
    """~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
    sqlite `cursorDiskKV`, key 'bubbleId:%', json value with
    $.tokenCount.inputTokens/outputTokens, $.modelInfo.modelName,
    $.createdAt, $.text, $.type."""
    db = (home / "Library" / "Application Support" / "Cursor"
          / "User" / "globalStorage" / "state.vscdb")
    db.parent.mkdir(parents=True, exist_ok=True)
    model = "cursor-claude-sonnet-4-6"
    with sqlite3.connect(db) as conn:
        conn.execute("create table cursorDiskKV (key text primary key, value text)")
        # Counted: explicit 100 in / 40 out (assistant bubble, type 2).
        conn.execute(
            "insert into cursorDiskKV(key, value) values (?, ?)",
            ("bubbleId:counted", json.dumps({
                "tokenCount": {"inputTokens": 100, "outputTokens": 40},
                "modelInfo": {"modelName": model},
                "createdAt": NOW_MS,
                "text": "hello world",
                "type": 2,
            })),
        )
        # IGNORED: zero-token bubble with empty text -> char estimate is 0 ->
        # add_usage_metrics returns False (proves zero-token filtering).
        conn.execute(
            "insert into cursorDiskKV(key, value) values (?, ?)",
            ("bubbleId:zero", json.dumps({
                "tokenCount": {"inputTokens": 0, "outputTokens": 0},
                "modelInfo": {"modelName": model},
                "createdAt": NOW_MS,
                "text": "",
                "type": 2,
            })),
        )
        # IGNORED: non-bubble key (wrong key prefix) must never be read.
        conn.execute(
            "insert into cursorDiskKV(key, value) values (?, ?)",
            ("composerData:zzz", json.dumps({
                "tokenCount": {"inputTokens": 9999, "outputTokens": 9999},
                "modelInfo": {"modelName": "should-never-appear"},
                "createdAt": NOW_MS,
                "text": "ignored",
                "type": 2,
            })),
        )
        conn.commit()
    return (100 + 40, model)


def write_goose(home: Path) -> Tuple[int, str]:
    """goose sessions.db -> <XDG_DATA_HOME>/goose/sessions/sessions.db.
    We set XDG_DATA_HOME under HOME so goose_db_path() resolves there."""
    db = (home / ".local" / "share" / "goose" / "sessions" / "sessions.db")
    db.parent.mkdir(parents=True, exist_ok=True)
    model = "goose-claude-sonnet-4-6"
    with sqlite3.connect(db) as conn:
        conn.execute(textwrap.dedent("""
            create table sessions (
              id text primary key,
              created_at text,
              updated_at text,
              accumulated_input_tokens integer,
              accumulated_output_tokens integer,
              model_config_json text
            )
        """))
        # Counted: 120 in / 40 out.
        conn.execute(
            "insert into sessions values (?, ?, ?, ?, ?, ?)",
            ("session-goose-1", NOW_ISO, NOW_ISO, 120, 40,
             json.dumps({"model_name": model})),
        )
        # IGNORED: zero-token session -> excluded by the WHERE filter.
        conn.execute(
            "insert into sessions values (?, ?, ?, ?, ?, ?)",
            ("session-goose-zero", NOW_ISO, NOW_ISO, 0, 0,
             json.dumps({"model_name": "goose-should-skip"})),
        )
        conn.commit()
    return (120 + 40, model)


def write_qwen(home: Path) -> Tuple[int, str]:
    """<QWEN_DATA_DIR>/<project>/chats/<session>.jsonl assistant entries with
    usageMetadata promptTokenCount/candidatesTokenCount/thoughtsTokenCount/
    cachedContentTokenCount. We pass QWEN_DATA_DIR explicitly."""
    chats = home / ".qwen" / "projects" / "proj-qwen-1" / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    model = "qwen-coder-plus"
    lines = [
        # Counted: assistant turn with explicit usageMetadata.
        json.dumps({
            "type": "assistant", "model": model, "timestamp": NOW_ISO,
            "sessionId": "sess-1",
            "usageMetadata": {
                "promptTokenCount": 200,
                "candidatesTokenCount": 50,
                "thoughtsTokenCount": 10,
                "cachedContentTokenCount": 30,
            },
        }),
        # IGNORED: user turn (wrong type).
        json.dumps({"type": "user", "timestamp": NOW_ISO, "text": "hello"}),
        # IGNORED: assistant turn without usageMetadata (must not fabricate).
        json.dumps({"type": "assistant", "model": model, "timestamp": NOW_ISO}),
    ]
    (chats / "chat-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (200 + 50 + 10 + 30, model)


def write_crush(home: Path) -> Tuple[int, str]:
    """<CRUSH_GLOBAL_DATA>/projects.json registry + per-project crush.db
    (sessions + messages). Child sessions (parent_session_id not null) and
    zero-token sessions must be ignored."""
    global_data = home / ".local" / "share" / "crush"
    global_data.mkdir(parents=True, exist_ok=True)
    project_dir = home / "work" / "crush-proj"
    data_dir = project_dir / ".crush"
    data_dir.mkdir(parents=True, exist_ok=True)
    model = "crush-claude-sonnet-4-6"
    (global_data / "projects.json").write_text(
        json.dumps({"p1": {"path": str(project_dir), "data_dir": ".crush"}}),
        encoding="utf-8",
    )
    db = data_dir / "crush.db"
    with sqlite3.connect(db) as conn:
        conn.execute(textwrap.dedent("""
            create table sessions (
              id text primary key,
              parent_session_id text,
              prompt_tokens integer,
              completion_tokens integer,
              created_at text,
              updated_at text
            )
        """))
        conn.execute("create table messages (id text primary key, session_id text, model text)")
        # Counted: root session 150 in / 60 out.
        conn.execute("insert into sessions values (?, ?, ?, ?, ?, ?)",
                     ("s1", None, 150, 60, NOW_ISO, NOW_ISO))
        # IGNORED: child session (parent_session_id not null).
        conn.execute("insert into sessions values (?, ?, ?, ?, ?, ?)",
                     ("s2", "s1", 999, 999, NOW_ISO, NOW_ISO))
        # IGNORED: zero-token root session.
        conn.execute("insert into sessions values (?, ?, ?, ?, ?, ?)",
                     ("s3", None, 0, 0, NOW_ISO, NOW_ISO))
        conn.execute("insert into messages values (?, ?, ?)", ("m1", "s1", model))
        conn.commit()
    return (150 + 60, model)


def _write_cline_family(home: Path, ext_id: str, model_tag: str, expected_model: str,
                        tokens_in: int, tokens_out: int, cache_reads: int,
                        cache_writes: int, task_name: str) -> Tuple[int, str]:
    """VS Code globalStorage/<ext-id>/tasks/<task>/ui_messages.json
    (+ api_conversation_history.json carrying a <model> tag)."""
    base = (home / "Library" / "Application Support" / "Code"
            / "User" / "globalStorage" / ext_id)
    task_dir = base / "tasks" / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "api_conversation_history.json").write_text(
        json.dumps([{
            "role": "user",
            "content": [{"type": "text", "text": f"ctx <model>{model_tag}</model> more"}],
        }]),
        encoding="utf-8",
    )
    (task_dir / "ui_messages.json").write_text(
        json.dumps([
            # Counted: an api_req_started say with token metrics.
            {
                "type": "say", "say": "api_req_started", "ts": NOW_MS,
                "text": json.dumps({
                    "tokensIn": tokens_in, "tokensOut": tokens_out,
                    "cacheReads": cache_reads, "cacheWrites": cache_writes,
                }),
            },
            # IGNORED: wrong say type (not api_req_started).
            {
                "type": "say", "say": "text", "ts": NOW_MS,
                "text": json.dumps({"tokensIn": 9999, "tokensOut": 9999}),
            },
            # IGNORED: api_req_started with zero tokens -> add_usage returns False.
            {
                "type": "say", "say": "api_req_started", "ts": NOW_MS,
                "text": json.dumps({"tokensIn": 0, "tokensOut": 0,
                                    "cacheReads": 0, "cacheWrites": 0}),
            },
        ]),
        encoding="utf-8",
    )
    total = tokens_in + tokens_out + cache_reads + cache_writes
    return (total, expected_model)


def write_roo_code(home: Path) -> Tuple[int, str]:
    return _write_cline_family(
        home, "rooveterinaryinc.roo-cline",
        model_tag="anthropic/claude-sonnet-4-6", expected_model="claude-sonnet-4-6",
        tokens_in=100, tokens_out=30, cache_reads=10, cache_writes=5,
        task_name="task-roo-1",
    )


def write_kilo_code(home: Path) -> Tuple[int, str]:
    return _write_cline_family(
        home, "kilocode.kilo-code",
        model_tag="anthropic/claude-opus-4-1", expected_model="claude-opus-4-1",
        tokens_in=110, tokens_out=25, cache_reads=0, cache_writes=0,
        task_name="task-kilo-1",
    )


def write_cline(home: Path) -> Tuple[int, str]:
    return _write_cline_family(
        home, "saoudrizwan.claude-dev",
        model_tag="claude-3-5-sonnet", expected_model="claude-3-5-sonnet",
        tokens_in=90, tokens_out=35, cache_reads=0, cache_writes=0,
        task_name="task-cline-1",
    )


def write_kiro(home: Path) -> Tuple[int, str]:
    """~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/
    <32-char-workspace>/<file>.chat. JSON: metadata{modelId,startTime} +
    chat[{role: human|bot, content}]. Tokens are char-estimated:
      input  = char_token_estimate(last human content[:500])
      output = char_token_estimate_for_length(sum len(bot content))
    Model: modelId with `\\d+.\\d+` rewritten to `\\d+-\\d+`."""
    agent_dir = (home / "Library" / "Application Support" / "Kiro"
                 / "User" / "globalStorage" / "kiro.kiroagent")
    workspace = agent_dir / ("a" * 32)  # workspace dirs are 32 chars
    workspace.mkdir(parents=True, exist_ok=True)

    human_text = "x" * 80          # 80 chars -> int((80+3)/4) = 20 input tokens
    bot_text = "y" * 200           # 200 chars -> int((200+3)/4) = 50 output tokens
    expected_in = char_token_estimate(human_text)            # 20
    expected_out = char_token_estimate_for_length(len(bot_text))  # 50
    model = "kiro-claude-sonnet-4-6"  # from modelId 'kiro-claude-sonnet-4.6'

    counted = {
        "metadata": {"modelId": "kiro-claude-sonnet-4.6", "startTime": NOW_ISO},
        "chat": [
            {"role": "human", "content": human_text},
            {"role": "bot", "content": bot_text},
        ],
    }
    (workspace / "counted.chat").write_text(json.dumps(counted), encoding="utf-8")

    # IGNORED: an <identity>-prefixed human (excluded) + no bot -> 0/0 -> skipped.
    ignored = {
        "metadata": {"modelId": "kiro-claude-sonnet-4.6", "startTime": NOW_ISO},
        "chat": [
            {"role": "human", "content": "<identity>system preamble only</identity>"},
        ],
    }
    (workspace / "ignored.chat").write_text(json.dumps(ignored), encoding="utf-8")

    return (expected_in + expected_out, model)


def write_continue(home: Path) -> Tuple[int, str]:
    """~/.continue/dev_data/<schema>/tokensGenerated.jsonl (versioned) plus a
    flat ~/.continue/dev_data/tokensGenerated.jsonl (legacy) — both globbed.
    Each line: promptTokens/generatedTokens (+ model, timestamp)."""
    dev_data = home / ".continue" / "dev_data"
    versioned = dev_data / "0.2.0"
    versioned.mkdir(parents=True, exist_ok=True)
    model = "continue-claude-sonnet-4-6"
    versioned_lines = [
        # Counted: 300 prompt / 70 generated.
        json.dumps({"model": model, "promptTokens": 300, "generatedTokens": 70,
                    "timestamp": NOW_ISO}),
        # IGNORED: neither token field > 0.
        json.dumps({"model": model, "promptTokens": 0, "generatedTokens": 0,
                    "timestamp": NOW_ISO}),
    ]
    (versioned / "tokensGenerated.jsonl").write_text(
        "\n".join(versioned_lines) + "\n", encoding="utf-8")
    # Legacy flat layout in the same dev_data dir: 5 prompt / 25 generated.
    (dev_data / "tokensGenerated.jsonl").write_text(
        json.dumps({"model": model, "promptTokens": 5, "generatedTokens": 25,
                    "timestamp": NOW_ISO}) + "\n",
        encoding="utf-8",
    )
    return (300 + 70 + 5 + 25, model)


def write_pearai(home: Path) -> Tuple[int, str]:
    """PearAI is a Continue fork: same dev_data schema under ~/.pearai."""
    versioned = home / ".pearai" / "dev_data" / "0.1.0"
    versioned.mkdir(parents=True, exist_ok=True)
    model = "pearai-gpt-4o"
    (versioned / "tokensGenerated.jsonl").write_text(
        json.dumps({"model": model, "promptTokens": 180, "generatedTokens": 45,
                    "timestamp": NOW_ISO}) + "\n",
        encoding="utf-8",
    )
    return (180 + 45, model)


def write_llm(home: Path) -> Tuple[int, str]:
    """simonw `llm` logs.db `responses` table. We set LLM_USER_PATH to
    <home>/.llm in run_pipeline so llm_db_path() resolves <LLM_USER_PATH>/logs.db."""
    user_path = home / ".llm"
    user_path.mkdir(parents=True, exist_ok=True)
    db = user_path / "logs.db"
    model = "llm-gpt-4o-mini"
    with sqlite3.connect(db) as conn:
        conn.execute(textwrap.dedent("""
            create table responses (
              id text primary key,
              model text,
              input_tokens integer,
              output_tokens integer,
              datetime_utc text,
              conversation_id text
            )
        """))
        # Counted: 220 in / 80 out.
        conn.execute(
            "insert into responses(id, model, input_tokens, output_tokens, datetime_utc, conversation_id) values (?, ?, ?, ?, ?, ?)",
            ("r1", model, 220, 80, NOW_ISO, "c1"),
        )
        # IGNORED: zero-token row (excluded by WHERE filter).
        conn.execute(
            "insert into responses(id, model, input_tokens, output_tokens, datetime_utc, conversation_id) values (?, ?, ?, ?, ?, ?)",
            ("r2", "llm-should-skip", 0, 0, NOW_ISO, "c2"),
        )
        conn.commit()
    return (220 + 80, model)


def write_gptme(home: Path) -> Tuple[int, str]:
    """gptme ~/.local/share/gptme/logs/<conv>/conversation.jsonl. Only
    assistant messages with token fields under metadata.usage are counted."""
    conv = home / ".local" / "share" / "gptme" / "logs" / "2026-01-01-conv"
    conv.mkdir(parents=True, exist_ok=True)
    model = "gptme-claude-sonnet-4-6"
    lines = [
        # Counted: assistant with metadata.usage (prompt/completion).
        json.dumps({
            "role": "assistant", "content": "hi", "timestamp": NOW_ISO,
            "metadata": {"model": model,
                         "usage": {"prompt_tokens": 240, "completion_tokens": 60}},
        }),
        # IGNORED: user turn.
        json.dumps({"role": "user", "content": "hello", "timestamp": NOW_ISO}),
        # IGNORED: assistant turn with no token fields anywhere (no fabrication).
        json.dumps({"role": "assistant", "content": "no usage", "timestamp": NOW_ISO,
                    "metadata": {"model": model}}),
    ]
    (conv / "conversation.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (240 + 60, model)


# Order matters only for display.
PROVIDERS: List[Tuple[str, Any]] = [
    ("cursor", write_cursor),
    ("goose", write_goose),
    ("kiro", write_kiro),
    ("roo-code", write_roo_code),
    ("kilo-code", write_kilo_code),
    ("cline", write_cline),
    ("qwen", write_qwen),
    ("crush", write_crush),
    ("continue", write_continue),
    ("pearai", write_pearai),
    ("llm", write_llm),
    ("gptme", write_gptme),
]

MATURE_FIVE = ("codex", "claude", "gemini", "opencode", "copilot")


# ---------------------------------------------------------------------------
# Subprocess loader: SourceFileLoader-load bin/agentcat (it has no .py suffix)
# and print build_snapshot() as JSON. HOME/AGENTCAT_HOME are read at import.
# ---------------------------------------------------------------------------
LOADER = textwrap.dedent("""
    import json, sys
    from importlib.machinery import SourceFileLoader
    mod = SourceFileLoader("agentcat", sys.argv[1]).load_module()
    print(json.dumps(mod.build_snapshot()))
""")


def run_pipeline(home: Path) -> Dict[str, Any]:
    agentcat_home = home / ".agentcat"
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["AGENTCAT_HOME"] = str(agentcat_home)
    # goose_db_path() / crush_registry_path() honor XDG_DATA_HOME first; pin it
    # under the fake home so neither escapes to the real machine.
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    # qwen + crush parsers read their own env vars first — point them at fixtures.
    env["QWEN_DATA_DIR"] = str(home / ".qwen" / "projects")
    env["CRUSH_GLOBAL_DATA"] = str(home / ".local" / "share" / "crush")
    # llm honors LLM_USER_PATH first — point it at the fixture logs.db dir.
    env["LLM_USER_PATH"] = str(home / ".llm")
    # Keep the connector from reaching out anywhere it might otherwise.
    env.pop("GOOSE_PATH_ROOT", None)
    env.pop("LOCALAPPDATA", None)

    proc = subprocess.run(
        [sys.executable, "-c", LOADER, str(AGENTCAT_BIN)],
        env=env, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        sys.stderr.write("--- connector subprocess STDERR ---\n")
        sys.stderr.write(proc.stderr + "\n")
        raise SystemExit(f"connector subprocess exited {proc.returncode}")
    # The connector may print warnings to stderr; the snapshot is on stdout.
    stdout = proc.stdout.strip()
    # Be defensive: take the last line that parses as a JSON object.
    snapshot = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            snapshot = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if snapshot is None:
        sys.stderr.write("--- connector STDOUT ---\n" + proc.stdout + "\n")
        sys.stderr.write("--- connector STDERR ---\n" + proc.stderr + "\n")
        raise SystemExit("could not parse snapshot JSON from connector stdout")
    return snapshot


# ---------------------------------------------------------------------------
def main() -> int:
    if not AGENTCAT_BIN.is_file():
        raise SystemExit(f"connector not found at {AGENTCAT_BIN}")

    home = Path(tempfile.mkdtemp(prefix="agentcat-e2e-"))
    try:
        expected: Dict[str, Tuple[int, str]] = {}
        for name, writer in PROVIDERS:
            expected[name] = writer(home)

        snapshot = run_pipeline(home)
        providers = snapshot.get("providers", {})

        # Collect results for the 8 target providers.
        rows: List[Tuple[str, str, int, int, str, bool]] = []
        all_pass = True
        for name, _ in PROVIDERS:
            exp_total, exp_model = expected[name]
            data = providers.get(name) or {}
            status = data.get("status", "MISSING")
            actual_total = int((data.get("tokens") or {}).get("all", 0))
            models = data.get("models") or {}

            status_ok = status == "ok"
            total_ok = actual_total == exp_total
            model_ok = exp_model in models
            ok = status_ok and total_ok and model_ok
            all_pass = all_pass and ok

            reason = ""
            if not status_ok:
                reason = f"status={status}"
            elif not total_ok:
                reason = f"total {actual_total}!={exp_total}"
            elif not model_ok:
                reason = f"model {exp_model!r} not in {list(models)}"
            # Informational only (not a pass/fail gate): cursor/kiro fixtures
            # char-estimate their tokens, so their snapshots now carry
            # estimated:true. Surface it next to the model so the matrix stays
            # consistent with the app's provider.estimated contract.
            detail = exp_model
            if ok and data.get("estimated") is True:
                detail = f"{exp_model} (estimated)"
            rows.append((name, status, exp_total, actual_total, detail, ok))
            if not ok and reason:
                rows[-1] = (name, status, exp_total, actual_total, reason, ok)

        # Sanity-check the mature five: present and not crashed.
        mature_problems: List[str] = []
        for name in MATURE_FIVE:
            data = providers.get(name)
            if data is None:
                mature_problems.append(f"{name}: absent from snapshot")
                continue
            st = data.get("status", "MISSING")
            if st in ("error", "MISSING", "unsupported_schema"):
                mature_problems.append(f"{name}: status={st} ({data.get('error', '')})")

        # ----- print the matrix -----
        print()
        print("=" * 78)
        print("AGENT CAT — LOCAL-USAGE PROVIDER E2E VERIFICATION")
        print(f"connector: {AGENTCAT_BIN}")
        print(f"connectorVersion: {snapshot.get('connectorVersion')}  schemaVersion: {snapshot.get('schemaVersion')}")
        print(f"fake HOME: {home}")
        print("=" * 78)
        header = f"{'provider':<12} {'status':<10} {'expected':>9} {'actual':>9}  result  detail/model"
        print(header)
        print("-" * 78)
        for name, status, exp_total, actual_total, detail, ok in rows:
            verdict = "PASS" if ok else "FAIL"
            print(f"{name:<12} {status:<10} {exp_total:>9} {actual_total:>9}  "
                  f"{verdict:<6}  {detail}")
        print("-" * 78)

        passed = sum(1 for r in rows if r[5])
        total = len(rows)
        print(f"local-provider result: {passed}/{total} PASS")

        print()
        print("mature five (expected not_found in empty fake home):")
        for name in MATURE_FIVE:
            data = providers.get(name) or {}
            print(f"  {name:<10} status={data.get('status', 'MISSING')}")
        if mature_problems:
            all_pass = False
            print("MATURE-FIVE PROBLEMS:")
            for p in mature_problems:
                print(f"  ! {p}")

        print("=" * 78)
        overall = "ALL PASS" if all_pass else "FAILURES PRESENT"
        print(f"OVERALL: {overall}")
        print("=" * 78)

        return 0 if all_pass else 1
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
