"""Read-only CLI home discovery. Registry data owns all product fingerprints.

No shell, credential refresh, network, logging, or persistent state. Filesystem
and clock operations are injectable; results contain evidence codes, not content.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import sqlite3
import time
try:
    import tomllib
except ImportError:  # The supported Windows Python 3.9/3.10 installs.
    tomllib = None
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Fingerprint:
    code: str
    operation: str  # exists, json_key, json_prefix, jsonl_prefix, filename, toml_key
    file: str = ""
    key: str = ""
    value: str = ""
    weight: int = 6
    default: str | None = None
    default_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityExtractor:
    file: str
    account_paths: tuple[str, ...] = ()
    email_paths: tuple[str, ...] = ()
    plan_paths: tuple[str, ...] = ()
    default_file: str | None = None


@dataclass(frozen=True)
class HomeSignature:
    provider: str
    kind: str
    layout: str
    env_vars: tuple[str, ...] = ()
    default_names: tuple[str, ...] = ()
    structure_all: tuple[str, ...] = ()
    structure_any: tuple[str, ...] = ()
    fingerprints: tuple[Fingerprint, ...] = ()
    negative_fingerprints: tuple[Fingerprint, ...] = ()
    usage_globs: tuple[str, ...] = ()
    identity: IdentityExtractor | None = None
    runtime_globs: tuple[str, ...] = ()


CLAUDE_GLOBS = ("projects/**/*.jsonl",)
CODEX_GLOBS = ("sessions/**/*.jsonl", "archived_sessions/**/*.jsonl")
CODEBUDDY = Fingerprint("brand.codebuddy", "exists", ".codebuddy.json", weight=12)
CODEBUDDY_CONFIG = Fingerprint("config.codebuddy", "exists", "codebuddy.json", weight=12)
GROK = Fingerprint("originator.grok", "jsonl_prefix", key="payload.originator", value="grok", weight=12)
KIMI = Fingerprint("originator.kimi", "jsonl_prefix", key="payload.originator", value="kimi", weight=12)
GROK_PROVIDER = Fingerprint("provider.grok", "toml_key", "config.toml", "model_provider", "grok", 12)
KIMI_PROVIDER = Fingerprint("provider.kimi", "toml_key", "config.toml", "model_provider", "kimi", 12)
GROK_XAI = Fingerprint("provider.xai", "toml_key", "config.toml", "model_provider", "xai", 12)
CODEBUDDY_NAME = Fingerprint("home.codebuddy", "filename", ".", value=r"^\.codebuddy$", weight=12)
GROK_NAME = Fingerprint("home.grok", "filename", ".", value=r"^\.(?:trappist-)?grok(?:-.*)?$", weight=12)
KIMI_NAME = Fingerprint("home.kimi_code", "filename", ".", value=r"^\.kimi-code$", weight=12)
GROK_MODEL = Fingerprint("model.grok", "jsonl_prefix", key="payload.model", value="grok", weight=12)
KIMI_MODEL = Fingerprint("model.kimi", "jsonl_prefix", key="payload.model", value="kimi", weight=12)
KIMI_CONFIG = Fingerprint("config.kimi", "exists", "kimi.json", weight=12)

REGISTRY = (
    HomeSignature(
        "claude", "provider", "claude", ("CLAUDE_CONFIG_DIR",), (".claude",),
        ("projects",), ("settings.json", ".claude.json"),
        (Fingerprint("identity.claude_oauth", "json_key", ".claude.json", "oauthAccount"),
         Fingerprint("identity.claude_user", "json_key", ".claude.json", "userID"),
         Fingerprint("model.claude", "jsonl_prefix", key="message.model", value="claude-")),
        (CODEBUDDY, CODEBUDDY_NAME, CODEBUDDY_CONFIG), CLAUDE_GLOBS,
        IdentityExtractor(".claude.json", ("oauthAccount.accountUuid", "oauthAccount.accountId"),
                          ("oauthAccount.emailAddress",), ("oauthAccount.organizationRateLimitTier",), "../.claude.json"),
    ),
    HomeSignature(
        "codex", "provider", "codex", ("CODEX_HOME",), (".codex",),
        (), ("sessions", "archived_sessions", "auth.json", "config.toml", "session_index.jsonl", "state_*.sqlite"),
        (Fingerprint("originator.codex", "jsonl_prefix", key="payload.originator", value="codex"),
         Fingerprint("identity.openai", "json_key", "auth.json", "tokens.id_token"),
         Fingerprint("provider.openai", "toml_key", "config.toml", "model_provider", "openai", default="openai",
                     default_keys=("model", "model_reasoning_effort", "approval_policy", "sandbox_mode")),
         Fingerprint("index.codex", "exists", "session_index.jsonl"),
         Fingerprint("state.codex", "exists", "state_*.sqlite"),
         Fingerprint("filename.codex_rollout", "filename", value=r"^rollout-.*[0-9a-f-]{36}\.jsonl$", weight=2)),
        (GROK, KIMI, GROK_PROVIDER, KIMI_PROVIDER, GROK_XAI, GROK_NAME, KIMI_NAME, GROK_MODEL, KIMI_MODEL, KIMI_CONFIG), CODEX_GLOBS,
        IdentityExtractor("auth.json", ("tokens.account_id",)),
        ("Library/Application Support/orca/codex-runtime-home/home",
         "Library/Application Support/orca/codex-accounts/*/home"),
    ),
    HomeSignature("codebuddy", "foreign", "claude", default_names=(".codebuddy",),
                  structure_any=("projects", ".codebuddy.json", "codebuddy.json"),
                  fingerprints=(CODEBUDDY, CODEBUDDY_NAME, CODEBUDDY_CONFIG),
                  usage_globs=CLAUDE_GLOBS),
    HomeSignature("grok", "foreign", "codex", default_names=(".grok",),
                  structure_any=("sessions", "archived_sessions", "config.toml", "auth.json", "session_index.jsonl", "state_*.sqlite"),
                  fingerprints=(GROK, GROK_PROVIDER, GROK_XAI, GROK_NAME, GROK_MODEL),
                  usage_globs=CODEX_GLOBS),
    HomeSignature("kimi-code", "foreign", "codex", default_names=(".kimi-code",),
                  structure_any=("sessions", "archived_sessions", "config.toml", "auth.json", "session_index.jsonl", "state_*.sqlite", "kimi.json"),
                  fingerprints=(KIMI, KIMI_PROVIDER, KIMI_NAME, KIMI_CONFIG, KIMI_MODEL),
                  usage_globs=CODEX_GLOBS),
)

DENYLIST = frozenset((
    "Library", "Applications", "Desktop", "Documents", "Downloads", "Movies", "Music", "Pictures", "Public",
    ".Trash", ".cache", ".npm", ".git", ".cargo", ".rustup", ".gradle", ".android", ".docker",
    "node_modules", ".local", "bin", ".agentcat", "AppData", ".venv", "venv", ".m2", ".vscode",
))


@dataclass(frozen=True)
class ScanLimits:
    bytes_per_file: int = 65536
    newest_jsonl: int = 4
    first_lines: int = 24
    entries: int = 50000
    seconds: float = 0.25


class LocalFS:
    now = staticmethod(time.monotonic)

    def children(self, path: Path):
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    yield Path(entry.path)
        except OSError:
            return

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def stat(self, path: Path):
        return path.stat()

    def realpath(self, path: Path) -> Path:
        return path.resolve()

    def read(self, path: Path, limit: int) -> bytes:
        with path.open("rb") as handle:
            return handle.read(limit)


def _key(obj: Any, key: str) -> Any:
    for part in key.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def account_key(provider: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return hashlib.sha256(f"{provider}:{value.strip()}".encode()).hexdigest()


def home_id(path: Path, fs=None) -> str:
    fs = fs or LocalFS()
    return hashlib.sha256(str(fs.realpath(path)).encode()).hexdigest()[:12]


def usage_files(path: Path, patterns: Sequence[str], *, fs=None, deadline=None, limit=50000, status=None):
    """Bounded traversal, including files directly under a ** root; no symlinks."""
    fs = fs or LocalFS()
    deadline = fs.now() + 1.0 if deadline is None else deadline
    stack = [path / root for root in dict.fromkeys(p.split("/")[0] for p in patterns)]
    visited = 0
    while stack and visited < limit and fs.now() < deadline:
        root = stack.pop()
        if fs.is_symlink(root) or not fs.is_dir(root):
            continue
        for child in fs.children(root):
            visited += 1
            if visited > limit or fs.now() >= deadline:
                if status is not None:
                    status["capped"] = True
                return
            if fs.is_symlink(child):
                continue
            if fs.is_dir(child):
                stack.append(child)
            elif fs.is_file(child):
                relative = child.relative_to(path).as_posix()
                if any(fnmatch.fnmatchcase(relative, p) or fnmatch.fnmatchcase(relative, p.replace("**/", "")) for p in patterns):
                    yield child
    if status is not None:
        status["capped"] = bool(stack or visited >= limit or fs.now() >= deadline)


def sample_usage_files(path: Path, patterns: Sequence[str], *, fs=None, deadline=None,
                       count=4, limit=50000, status=None):
    """Visit newest date/project directories, stopping after K matching files.

    Sorting happens only at directory levels, never over the rollout corpus.
    Within a leaf directory, scandir order suffices for a bounded sample.
    """
    fs = fs or LocalFS()
    deadline = fs.now() + ScanLimits().seconds if deadline is None else deadline
    status = {} if status is None else status
    status.update(capped=False, partial=False)
    visited = 0
    found = []

    def visit(root):
        nonlocal visited
        if fs.now() >= deadline or visited >= limit:
            status["capped"] = True
            return
        if fs.is_symlink(root) or not fs.is_dir(root):
            return
        directories = []
        for child in fs.children(root):
            visited += 1
            if fs.now() >= deadline or visited > limit:
                status["capped"] = True
                break
            if fs.is_symlink(child):
                continue
            if fs.is_dir(child):
                directories.append(child)
            elif child.suffix == ".jsonl" and fs.is_file(child):
                relative = child.relative_to(path).as_posix()
                if any(fnmatch.fnmatchcase(relative, p) or fnmatch.fnmatchcase(relative, p.replace("**/", "")) for p in patterns):
                    found.append(child)
                    if len(found) >= count:
                        status["partial"] = True
                        return
        def newest(directory):
            # ISO date components sort chronologically; projects sort by mtime.
            if directory.name.isdigit() and directory.relative_to(path).parts[0] in ("sessions", "archived_sessions"):
                return (1, int(directory.name))
            try:
                return (0, fs.stat(directory).st_mtime)
            except OSError:
                return (0, 0)
        directories.sort(key=newest, reverse=True)
        for directory in directories:
            if len(found) >= count or status["capped"]:
                break
            visit(directory)

    for name in dict.fromkeys(p.split("/")[0] for p in patterns):
        if len(found) >= count or status["capped"]:
            break
        visit(path / name)
    return found


def classify_home(path: Path, registry=REGISTRY, *, fs=None, limits=ScanLimits(), threshold=6, margin=3,
                  deadline=None) -> dict:
    fs = fs or LocalFS()
    deadline = min(deadline, fs.now() + limits.seconds) if deadline is not None else fs.now() + limits.seconds
    contents: dict[str, Any] = {}
    failed_reads = set()
    samples: dict[tuple[str, ...], list[tuple[Path, list[Any]]]] = {}
    timed_out = False
    partial = False

    def target(name):
        # Some standard homes store metadata beside the directory. The
        # alternate location, like every product detail, belongs to the registry.
        direct = path / name
        for signature in registry:
            extractor = signature.identity
            if extractor and extractor.file == name and extractor.default_file and path.name in signature.default_names:
                default = path / extractor.default_file
                if not fs.is_file(direct) and fs.is_file(default):
                    return default
        return direct

    def exists(name):
        resolved = target(name)
        if "*" in resolved.name:
            return any(fnmatch.fnmatchcase(child.name, resolved.name) and not fs.is_symlink(child) and fs.is_file(child)
                       for child in fs.children(resolved.parent))
        return not fs.is_symlink(resolved) and (fs.is_dir(resolved) or fs.is_file(resolved))

    def read(name):
        nonlocal timed_out
        if name not in contents:
            if fs.now() >= deadline:
                timed_out = True
                return ""
            try:
                resolved = target(name)
                # Brand/config probes never follow links into another home.
                raw = b"" if fs.is_symlink(resolved) or not fs.is_file(resolved) else fs.read(resolved, limits.bytes_per_file)
                contents[name] = raw.decode("utf-8", errors="replace")
            except (OSError, UnicodeError):
                contents[name] = ""
                failed_reads.add(name)
        return contents[name]

    def json_file(name):
        try:
            return json.loads(read(name))
        except (ValueError, TypeError):
            return {}

    def sample(signature):
        nonlocal timed_out, partial
        patterns = signature.usage_globs
        if patterns not in samples:
            status = {}
            files = sample_usage_files(path, patterns, fs=fs, deadline=deadline,
                                       count=limits.newest_jsonl, limit=limits.entries, status=status)
            timed_out |= status["capped"]
            partial |= status["partial"]
            rows = []
            for file in files[:limits.newest_jsonl]:
                objects = []
                for line in read(str(file.relative_to(path))).splitlines()[:limits.first_lines]:
                    try:
                        objects.append(json.loads(line))
                    except ValueError:
                        continue
                rows.append((file, objects))
            samples[patterns] = rows
        return samples[patterns]

    def matches(fp, signature):
        if fp.operation == "exists":
            return exists(fp.file)
        if fp.operation.startswith("jsonl"):
            return any(isinstance(value := _key(obj, fp.key), str) and value.lower().startswith(fp.value.lower())
                       for _, objects in sample(signature) for obj in objects)
        if fp.operation == "filename":
            if fp.file:
                return bool(re.search(fp.value, (path if fp.file == "." else path / fp.file).name, re.IGNORECASE))
            return any(re.search(fp.value, file.name, re.IGNORECASE) for file, _ in sample(signature))
        if fp.operation.startswith("json"):
            value = _key(json_file(fp.file), fp.key)
            return value is not None if fp.operation == "json_key" else isinstance(value, str) and value.startswith(fp.value)
        if fp.operation == "toml_key":
            # Only the root key belongs to this CLI, not a nested provider definition.
            content = read(fp.file)
            if not exists(fp.file) or fp.file in failed_reads:
                return False
            if tomllib is not None:
                try:
                    obj = tomllib.loads(content)
                except ValueError:
                    return False
                if fp.key not in obj and fp.default_keys and not any(key in obj for key in fp.default_keys):
                    return False
                value = obj.get(fp.key, fp.default)
                return isinstance(value, str) and value.lower().startswith(fp.value.lower())
            root = content.split("[", 1)[0]
            for line in root.splitlines():
                if line.strip() and not line.lstrip().startswith("#") and not re.match(r"\s*[A-Za-z0-9_.-]+\s*=\s*\S", line):
                    return False
            match = re.search(r"(?m)^\s*" + re.escape(fp.key) + r"\s*=\s*['\"]([^'\"]+)['\"]", root)
            if match:
                return match.group(1).lower().startswith(fp.value.lower())
            defaults_match = not fp.default_keys or any(re.search(r"(?m)^\s*" + re.escape(key) + r"\s*=", root) for key in fp.default_keys)
            return bool(defaults_match and fp.default == fp.value and not re.search(r"(?m)^\s*" + re.escape(fp.key) + r"\s*=", root))
        raise ValueError("unknown fingerprint operation")

    ranked = []
    layouts = set()
    eligible = []
    def cheap(fp):
        return fp.operation != "jsonl_prefix" and not (fp.operation == "filename" and not fp.file)

    # Read every cheap identity/reject before any signature requests JSONL IO.
    for signature in registry:
        if fs.now() >= deadline:
            timed_out = True
            break
        if not all(exists(m) for m in signature.structure_all):
            continue
        if signature.structure_any and not any(exists(m) for m in signature.structure_any):
            continue
        layouts.add(signature.layout)
        positive = [fp for fp in signature.fingerprints if cheap(fp) and matches(fp, signature)]
        negative = [fp for fp in signature.negative_fingerprints if cheap(fp) and matches(fp, signature)]
        eligible.append((signature, positive, negative))
    for signature, positive, negative in eligible:
        positive += [fp for fp in signature.fingerprints if not cheap(fp) and matches(fp, signature)]
        negative += [fp for fp in signature.negative_fingerprints if not cheap(fp) and matches(fp, signature)]
        score = sum(fp.weight for fp in positive) - sum(fp.weight for fp in negative)
        # Rejects are ownership decisions, not a score that extra positives undo.
        if negative:
            score = min(score, 0)
        ranked.append((score, signature, [fp.code for fp in positive] + ["reject." + fp.code for fp in negative]))
    ranked.sort(key=lambda row: row[0], reverse=True)
    result = {"provider": None, "kind": "ambiguous", "score": ranked[0][0] if ranked else 0,
              "evidence": [], "identity": {}, "layouts": sorted(layouts)}
    if not ranked:
        result["kind"] = "ambiguous" if timed_out else "unknown"
        if timed_out:
            result["evidence"] = ["scan.budget"]
        return result
    score, winner, evidence = ranked[0]
    result["evidence"] = evidence
    if timed_out or partial or fs.now() >= deadline:
        result["evidence"].append("scan.partial")
    product_conflict = sum(s >= threshold and sig.kind == "provider" for s, sig, _ in ranked) > 1
    if product_conflict or score < threshold or (len(ranked) > 1 and score - ranked[1][0] < margin):
        result["evidence"].append("classification.ambiguous")
        return result
    result.update(provider=winner.provider, kind=winner.kind)
    result["layouts"] = [winner.layout]
    if winner.identity:
        obj = json_file(winner.identity.file)
        native = next((v for key in winner.identity.account_paths if (v := _key(obj, key))), None)
        email = next((v for key in winner.identity.email_paths if (v := _key(obj, key))), None)
        # Email/plan paths are declarative; only the account key leaves this module.
        hashed = account_key(winner.provider, native or (email.lower() if isinstance(email, str) else None))
        if hashed:
            result["identity"] = {"accountKey": hashed}
    return result


SESSION_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def session_inventory(path: Path, patterns: Sequence[str], *, fs=None, deadline=None,
                      sample_count=256, index_limit=50000) -> dict:
    """Bounded filename sample plus local ID indexes; never parse transcripts.

    Index files and SQLite are read-only. IDs and paths stay inside discovery;
    only counts, ages and evidence codes reach the snapshot.
    """
    fs = fs or LocalFS()
    deadline = fs.now() + ScanLimits().seconds if deadline is None else deadline
    status = {}
    files = sample_usage_files(path, patterns, fs=fs, deadline=deadline,
                               count=sample_count, status=status)
    sessions = {}
    newest = None
    for file in files:
        match = SESSION_UUID.search(file.name)
        if match:
            sessions[match.group().lower()] = file.relative_to(path)
        if fs.now() >= deadline:
            status["capped"] = True
            break
        try:
            mtime = fs.stat(file).st_mtime
            newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            continue
    indexed = set()
    index_partial = False
    index = path / "session_index.jsonl"
    if fs.now() < deadline and not fs.is_symlink(index) and fs.is_file(index):
        try:
            byte_limit = 8 * 1024 * 1024
            raw = fs.read(index, byte_limit + 1)
            index_partial = len(raw) > byte_limit
            # A truncated final row cannot count as a complete index entry.
            lines = raw[:byte_limit].splitlines()
            if index_partial:
                lines = lines[:-1]
            for line in lines:
                if fs.now() >= deadline or len(indexed) >= index_limit:
                    index_partial = True
                    break
                try:
                    obj = json.loads(line)
                    value = obj.get("id", obj.get("session_id"))
                    if isinstance(value, str) and SESSION_UUID.fullmatch(value):
                        indexed.add(value.lower())
                except (ValueError, AttributeError):
                    continue
        except OSError:
            index_partial = True
    # State-only homes and incomplete JSONL indexes still have cheap ID metadata.
    for database in fs.children(path):
        if fs.now() >= deadline or len(indexed) >= index_limit:
            index_partial = True
            break
        if not fnmatch.fnmatchcase(database.name, "state_*.sqlite") or fs.is_symlink(database):
            continue
        try:
            # Immutable mode avoids creating WAL/SHM sidecars in a CLI home.
            # Filename samples cover live IDs not yet checkpointed in the DB.
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)) as conn:
                conn.set_progress_handler(lambda: int(fs.now() >= deadline), 1000)
                for (value,) in conn.execute("SELECT id FROM threads LIMIT ?", (index_limit + 1,)):
                    if fs.now() >= deadline or len(indexed) >= index_limit:
                        index_partial = True
                        break
                    if isinstance(value, str) and SESSION_UUID.fullmatch(value):
                        indexed.add(value.lower())
        except (OSError, sqlite3.Error):
            index_partial = True
    return {"sessions": set(sessions) | indexed, "paths": sessions,
            "stats": {"files": max(len(files), len(indexed)), "newestMtime": newest,
                      "capped": bool(status["capped"] or status["partial"] or index_partial)},
            "partial": bool(status["capped"] or index_partial)}


def _expand(value: str, home: Path) -> Path | None:
    value = re.sub(r"\$(?:\{HOME\}|HOME(?![A-Za-z0-9_]))", lambda _: str(home), value)
    if value == "~" or value.startswith("~/"):
        value = str(home) + value[1:]
    if any(c in value for c in ("$", "`", "\n", "\x00")):
        return None
    path = Path(value)
    return path if path.is_absolute() else None


def collect_candidates(home: Path, launcher_dirs, runtime_homes, env: Mapping[str, str], *, registry=REGISTRY,
                       fs=None, seconds=2.0) -> list[dict]:
    fs = fs or LocalFS()
    deadline = fs.now() + seconds
    candidates: dict[str, dict] = {}
    variables = {var for sig in registry if sig.kind == "provider" for var in sig.env_vars}

    def add(path, source, launcher=None):
        if path is None or fs.is_symlink(path):
            return
        key = str(fs.realpath(path))
        row = candidates.setdefault(key, {"path": path, "sources": [], "launchers": []})
        if source not in row["sources"]:
            row["sources"].append(source)
        if launcher and launcher not in row["launchers"]:
            row["launchers"].append(launcher)

    for var in sorted(variables):
        if env.get(var):
            add(_expand(env[var], home), "env")
    for path in runtime_homes:
        add(Path(path), "known_runtime")
    for directory in launcher_dirs:
        for index, script in enumerate(fs.children(Path(directory))):
            if index >= 4096 or fs.now() >= deadline:
                break
            try:
                if fs.is_symlink(script) or not fs.is_file(script) or fs.stat(script).st_size > 65536:
                    continue
                raw = fs.read(script, 65537)
                if len(raw) > 65536 or b"\x00" in raw:
                    continue
                source = raw.decode("utf-8")
                if not (fs.stat(script).st_mode & 0o111 or source.startswith("#!") or script.suffix in (".sh", ".cmd", ".bat", ".ps1")):
                    continue
                if not any(var in source for var in variables):
                    continue
                for line in source.splitlines():
                    try:
                        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
                        lexer.whitespace_split = True
                        lexer.commenters = "#"
                        tokens = list(lexer)
                    except ValueError:
                        continue
                    # Assignments must precede the command (or follow export/env),
                    # never be echo arguments or shell text to be evaluated.
                    commands = [[]]
                    for token in tokens:
                        if token in (";", "&&", "||", "&", "|"):
                            commands.append([])
                        else:
                            commands[-1].append(token)
                    for command in commands:
                        if command and command[0] in ("export", "env"):
                            command = command[1:]
                        for token in command:
                            name, separator, value = token.partition("=")
                            if separator and name in variables:
                                add(_expand(value, home), "launcher", script.name)
                            elif not separator and not token.startswith("-"):
                                break
            except (OSError, UnicodeError):
                continue
    for index, path in enumerate(fs.children(home)):
        if index >= 4096 or fs.now() >= deadline:
            break
        if path.name not in DENYLIST and not fs.is_symlink(path) and fs.is_dir(path):
            add(path, "auto")
    return list(candidates.values())
