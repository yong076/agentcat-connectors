"""Orca-aligned coding agent & model catalog for Agent Cat.

Ports Orca's (`agent-cat-studio-shell`) Available Agents catalog, CLI binary &
version-manager fallback directory resolution, Node/Python entrypoint process
signatures, and session-free model/effort catalogs into a pure read-only helper.

Privacy & OAuth invariants (R1-R10):
- Zero network calls, zero CLI subprocesses, zero session creation.
- Persisted Orca model listings (`agent-model-catalog.json`) are read strictly
  read-only; filesystem paths, account IDs, and fingerprints never leave this
  module.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class OrcaAgentModelSeed:
    id: str
    label: str
    is_default: bool = False


@dataclass(frozen=True)
class OrcaAgentSpec:
    id: str
    display_name: str
    vendor: str
    cli_binary: str
    aliases: Tuple[str, ...] = ()
    homepage_url: str = ""
    tier: str = "community"  # "primary" | "community"
    efforts: Tuple[str, ...] = ()
    seed_models: Tuple[OrcaAgentModelSeed, ...] = ()
    extra_bin_relpaths: Tuple[str, ...] = ()


def _m(model_id: str, label: str, default: bool = False) -> OrcaAgentModelSeed:
    return OrcaAgentModelSeed(model_id, label, default)


# Canonical 44-agent catalog ported from Orca's `studio-agent-catalog.ts`,
# `tui-agent-config.ts`, `agent-catalog-entries-{primary,secondary}.ts`, and
# `agent-session-option-catalog-*.ts`. IDs match Agent Cat's `ProviderRegistry`
# where Agent Cat already defines a canonical slug (`qwen`, `auggie`, `kilo-code`).
ORCA_AGENT_SPECS: Tuple[OrcaAgentSpec, ...] = (
    OrcaAgentSpec(
        "claude", "Claude Code", "Anthropic", "claude",
        aliases=("claude-code",),
        homepage_url="https://code.claude.com/docs",
        tier="primary",
        efforts=("low", "medium", "high", "xhigh", "max"),
        seed_models=(
            _m("claude-fable-5-1", "Fable 5.1"),
            _m("claude-opus-4-8", "Opus 4.8"),
            _m("claude-sonnet-4-6", "Sonnet 4.6", True),
            _m("claude-haiku-4-5", "Haiku 4.5"),
        ),
    ),
    OrcaAgentSpec(
        "codex", "Codex", "OpenAI", "codex",
        homepage_url="https://github.com/openai/codex",
        tier="primary",
        efforts=("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        seed_models=(
            _m("gpt-5.6-sol", "GPT-5.6 Sol", True),
            _m("gpt-5.6-terra", "GPT-5.6 Terra"),
            _m("gpt-5.6-luna", "GPT-5.6 Luna"),
            _m("gpt-5.5", "GPT-5.5"),
            _m("gpt-5.4", "GPT-5.4"),
            _m("gpt-5.4-mini", "GPT-5.4 Mini"),
            _m("gpt-5.3-codex", "GPT-5.3 Codex"),
            _m("gpt-5.2-codex", "GPT-5.2 Codex"),
        ),
    ),
    OrcaAgentSpec(
        "gemini", "Gemini CLI", "Google", "gemini",
        aliases=("gemini-cli",),
        homepage_url="https://github.com/google-gemini/gemini-cli",
        tier="primary",
        seed_models=(
            _m("gemini-3-pro-preview", "Gemini 3 Pro", True),
            _m("gemini-3-flash-preview", "Gemini 3 Flash"),
            _m("gemini-2.5-pro", "Gemini 2.5 Pro"),
            _m("gemini-2.5-flash", "Gemini 2.5 Flash"),
        ),
    ),
    OrcaAgentSpec(
        "antigravity", "Antigravity", "Google", "agy",
        aliases=("antigravity", "antigravity-cli"),
        homepage_url="https://antigravity.google/docs/cli-overview",
        tier="primary",
        efforts=("low", "medium", "high"),
        seed_models=(
            _m("gemini-3-pro-preview", "Gemini 3 Pro", True),
            _m("gemini-3-flash-preview", "Gemini 3 Flash"),
        ),
    ),
    OrcaAgentSpec(
        "opencode", "OpenCode", "Anomaly", "opencode",
        aliases=("opencode2",),
        homepage_url="https://opencode.ai/docs/cli/",
        tier="primary",
        efforts=("low", "medium", "high", "xhigh"),
        seed_models=(
            _m("opencode/gpt-5.4-mini", "GPT-5.4 Mini", True),
        ),
        extra_bin_relpaths=(".opencode/bin",),
    ),
    OrcaAgentSpec(
        "copilot", "GitHub Copilot", "GitHub", "copilot",
        homepage_url="https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli",
        tier="primary",
        efforts=("low", "medium", "high", "xhigh"),
        seed_models=(
            _m("gpt-5.4", "GPT-5.4", True),
            _m("gpt-5.5", "GPT-5.5"),
            _m("gpt-5.3-codex", "GPT-5.3 Codex"),
            _m("claude-opus-4.7", "Opus 4.7"),
            _m("claude-sonnet-4.6", "Sonnet 4.6"),
            _m("claude-haiku-4.5", "Haiku 4.5"),
        ),
    ),
    OrcaAgentSpec(
        "kimi", "Kimi Code", "Moonshot AI", "kimi",
        aliases=("kimi-code",),
        homepage_url="https://www.kimi.com/code/docs/en/kimi-code-cli/getting-started.html",
        tier="primary",
        efforts=("off", "on"),
        seed_models=(
            _m("kimi-code/kimi-for-coding", "Kimi K2.6", True),
        ),
    ),
    OrcaAgentSpec(
        "grok", "Grok", "xAI", "grok",
        aliases=("grok-code",),
        homepage_url="https://x.ai/cli",
        tier="primary",
        efforts=("low", "medium", "high", "xhigh"),
        seed_models=(
            _m("grok-4.7", "Grok 4.7", True),
            _m("grok-4.6", "Grok 4.6"),
            _m("grok-4.5", "Grok 4.5"),
        ),
        extra_bin_relpaths=(".grok/bin",),
    ),
    OrcaAgentSpec(
        "cursor", "Cursor", "Anysphere", "cursor-agent",
        aliases=("cursor-agent-cli",),
        homepage_url="https://cursor.com/cli",
        tier="primary",
        efforts=("low", "medium", "high"),
        seed_models=(
            _m("auto", "Auto", True),
            _m("gpt-5.3-codex", "GPT-5.3 Codex"),
            _m("claude-opus-4-8", "Opus 4.8"),
        ),
    ),
    OrcaAgentSpec(
        "hermes", "Hermes", "Nous Research", "hermes",
        homepage_url="https://hermes-agent.nousresearch.com/docs/",
        tier="primary",
    ),
    OrcaAgentSpec(
        "pi", "Pi", "Pi", "pi",
        homepage_url="https://pi.dev",
        tier="primary",
        efforts=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
        extra_bin_relpaths=(".vite-plus/bin",),
    ),
    OrcaAgentSpec(
        "omp", "OMP", "OMP", "omp",
        homepage_url="https://omp.sh",
        tier="primary",
        efforts=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
    ),
    OrcaAgentSpec(
        "droid", "Droid", "Factory", "droid",
        homepage_url="https://docs.factory.ai/cli/getting-started/quickstart",
        tier="primary",
    ),
    OrcaAgentSpec(
        "amp", "Amp", "Amp", "amp",
        homepage_url="https://ampcode.com/manual#install",
        tier="primary",
        efforts=("low", "medium", "high"),
        seed_models=(
            _m("smart", "Smart", True),
            _m("rush", "Rush"),
            _m("large", "Large"),
            _m("deep", "Deep"),
        ),
    ),
    OrcaAgentSpec(
        "kiro", "Kiro", "AWS", "kiro-cli",
        aliases=("kiro",),
        homepage_url="https://kiro.dev/docs/cli/",
        tier="primary",
    ),
    OrcaAgentSpec(
        "auggie", "Auggie", "Augment", "auggie",
        homepage_url="https://docs.augmentcode.com/cli/overview",
        tier="primary",
    ),
    OrcaAgentSpec(
        "goose", "Goose", "Block", "goose",
        homepage_url="https://block.github.io/goose/docs/quickstart/",
        tier="primary",
    ),
    OrcaAgentSpec(
        "qwen", "Qwen Code", "Alibaba", "qwen",
        aliases=("qwen-code",),
        homepage_url="https://github.com/QwenLM/qwen-code",
        tier="primary",
    ),
    OrcaAgentSpec(
        "aider", "Aider", "Aider", "aider",
        homepage_url="https://aider.chat/docs/",
        tier="primary",
    ),
    OrcaAgentSpec(
        "cline", "Cline", "Cline", "cline",
        homepage_url="https://docs.cline.bot/cline-cli/overview",
        tier="primary",
    ),
    OrcaAgentSpec(
        "crush", "Crush", "Charm", "crush",
        homepage_url="https://github.com/charmbracelet/crush",
        tier="primary",
    ),
    OrcaAgentSpec(
        "continue", "Continue", "Continue", "cn",
        homepage_url="https://docs.continue.dev/guides/cli",
    ),
    OrcaAgentSpec(
        "kilo-code", "Kilo Code", "Kilo", "kilo",
        aliases=("kilo-code",),
        homepage_url="https://kilo.ai/docs/cli",
    ),
    OrcaAgentSpec(
        "codebuddy", "CodeBuddy", "Tencent", "codebuddy",
        aliases=("cbc",),
        homepage_url="https://www.codebuddy.ai/cli",
        efforts=("minimal", "low", "medium", "high", "max"),
        seed_models=(
            _m("default-model", "Default", True),
            _m("fast-model", "Fast"),
            _m("deep-model", "Deep"),
        ),
    ),
    OrcaAgentSpec(
        "openclaude", "OpenClaude", "GitLaw", "openclaude",
        homepage_url="https://openclaude.gitlawb.com/",
    ),
    OrcaAgentSpec(
        "autohand", "Autohand Code", "Autohand", "autohand",
        homepage_url="https://github.com/autohandai/code-cli",
    ),
    OrcaAgentSpec(
        "opencode2", "OpenCode 2", "Anomaly", "opencode2",
        homepage_url="https://opencode.ai/v2/docs/",
    ),
    OrcaAgentSpec(
        "mimo-code", "MiMo Code", "Xiaomi", "mimo",
        homepage_url="https://mimo.xiaomi.com/coder",
    ),
    OrcaAgentSpec(
        "qoder", "Qoder CLI", "Qoder", "qodercli",
        aliases=("qoder",),
        homepage_url="https://docs.qoder.com/cli/overview",
    ),
    OrcaAgentSpec(
        "qoder-cn", "Qoder CLI China", "Qoder", "qoderclicn",
        aliases=("qodercn",),
        homepage_url="https://docs.qoder.cn/cli/overview",
    ),
    OrcaAgentSpec(
        "codebuff", "Codebuff", "Codebuff", "codebuff",
        homepage_url="https://www.codebuff.com/docs/help/quick-start",
    ),
    OrcaAgentSpec(
        "freebuff", "Freebuff", "Codebuff", "freebuff",
        homepage_url="https://freebuff.com/cli",
    ),
    OrcaAgentSpec(
        "command-code", "Command Code", "Command Code", "command-code",
        homepage_url="https://commandcode.ai/docs/quickstart",
    ),
    OrcaAgentSpec(
        "mistral-vibe", "Mistral Vibe", "Mistral AI", "vibe",
        aliases=("mistral-vibe",),
        homepage_url="https://github.com/mistralai/mistral-vibe",
    ),
    OrcaAgentSpec(
        "rovo", "Rovo Dev", "Atlassian", "acli",
        homepage_url="https://support.atlassian.com/rovo/docs/install-and-run-rovo-dev-cli-on-your-device/",
    ),
    OrcaAgentSpec(
        "openclaw", "OpenClaw", "OpenClaw", "openclaw",
        homepage_url="https://github.com/openclaw/openclaw",
    ),
    OrcaAgentSpec(
        "devin", "Devin", "Cognition", "devin",
        homepage_url="https://devin.ai/cli",
    ),
    OrcaAgentSpec(
        "ante", "Ante", "Antigma Labs", "ante",
        homepage_url="https://github.com/AntigmaLabs/ante-preview",
    ),
    OrcaAgentSpec(
        "trae", "Trae", "ByteDance", "traecli",
        homepage_url="https://docs.trae.cn/cli_get-started-with-trae-cli",
    ),
    OrcaAgentSpec(
        "muse", "Muse", "Meta", "muse",
        homepage_url="https://dev.meta.ai/docs/muse-code",
        efforts=("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    ),
    OrcaAgentSpec(
        "zcode", "ZCode", "Z.ai", "zcode",
        aliases=("zcode-cli",),
        homepage_url="https://zcode.z.ai/en/docs",
    ),
    OrcaAgentSpec(
        "prime-agent", "Prime Agent", "Prime Intellect", "prime-agent",
        homepage_url="https://github.com/PrimeIntellect-ai/prime-agent",
    ),
    OrcaAgentSpec(
        "dsh", "DeepSeek Harness", "DeepSeek", "dsh-tui",
        aliases=("dsh", "dst"),
        homepage_url="https://deepseek-harness.github.io/deepseek-harness/",
    ),
    OrcaAgentSpec(
        "jcode", "Jcode", "Jcode", "jcode",
        homepage_url="https://github.com/1jehuang/jcode",
    ),
)

_ORCA_ID_TO_SPEC_ID = {
    "aug": "auggie",
    "kilo": "kilo-code",
    "qwen-code": "qwen",
    "agy": "antigravity",
}

# Exact executable basenames (.exe/.cmd stripped by caller) -> canonical provider id.
# Excludes shell builtins (`continue`) and ambiguous system binaries.
PROCESS_EXECUTABLE_MAP: Dict[str, str] = {
    "codex": "codex",
    "claude": "claude",
    "claude-code": "claude",
    "agy": "antigravity",
    "antigravity": "antigravity",
    "antigravity-cli": "antigravity",
    "gemini": "gemini",
    "gemini-cli": "gemini",
    "kimi": "kimi",
    "kimi-code": "kimi",
    "grok": "grok",
    "grok-code": "grok",
    "grock": "grok",
    "cursor-agent": "cursor",
    "cursor-agent-cli": "cursor",
    "hermes": "hermes",
    "opencode": "opencode",
    "opencode2": "opencode",
    "copilot": "copilot",
    "pi": "pi",
    "omp": "omp",
    "droid": "droid",
    "amp": "amp",
    "kiro-cli": "kiro",
    "auggie": "auggie",
    "goose": "goose",
    "qwen": "qwen",
    "qwen-code": "qwen",
    "aider": "aider",
    "cline": "cline",
    "crush": "crush",
    "cn": "continue",
    "kilo": "kilo-code",
    "kilo-code": "kilo-code",
    "codebuddy": "codebuddy",
    "cbc": "codebuddy",
    "openclaude": "openclaude",
    "autohand": "autohand",
    "mimo": "mimo-code",
    "qodercli": "qoder",
    "qoder": "qoder",
    "qoderclicn": "qoder-cn",
    "qodercn": "qoder-cn",
    "codebuff": "codebuff",
    "freebuff": "freebuff",
    "command-code": "command-code",
    "vibe": "mistral-vibe",
    "mistral-vibe": "mistral-vibe",
    "openclaw": "openclaw",
    "devin": "devin",
    "ante": "ante",
    "traecli": "trae",
    "muse": "muse",
    "zcode": "zcode",
    "zcode-cli": "zcode",
    "prime-agent": "prime-agent",
    "dsh": "dsh",
    "dsh-tui": "dsh",
    "dst": "dsh",
    "jcode": "jcode",
}

_VERSIONED_EXEC_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^codex-(?:aarch64|x86_64|arm64|darwin|linux|win32|\d)[\w.-]*$"), "codex"),
    (re.compile(r"^grok-(?:aarch64|x86_64|arm64|darwin|linux|win32|\d)[\w.-]*$"), "grok"),
    (re.compile(r"^qodercli-\d[\w.-]*$"), "qoder"),
    (re.compile(r"^(?:qoderclicn|qodercn)-\d[\w.-]*$"), "qoder-cn"),
    (re.compile(r"^muse-bin-[\w.-]+$"), "muse"),
)

# Node/Python script path signatures ported from Orca's
# `agent-node-entrypoint-identities.ts` and `agent-node-package-entrypoints.ts`.
_SCRIPT_ENTRYPOINT_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:^|/)@openai/codex/bin/codex\.(?:m|c)?js$"), "codex"),
    (re.compile(r"(?:^|/)@anthropic-ai/claude-code/cli\.(?:m|c)?js$"), "claude"),
    (re.compile(r"(?:^|/)@google/gemini-cli/(?:index|dist/index|bundle/gemini)\.(?:m|c)?js$"), "gemini"),
    (re.compile(r"(?:^|/)@github/copilot/(?:index|dist/index|npm-loader)\.(?:m|c)?js$"), "copilot"),
    (re.compile(r"(?:^|/)@tencent-ai/codebuddy-code/(?:cli|bin/codebuddy)\.(?:m|c)?js$"), "codebuddy"),
    (re.compile(r"(?:^|/)@(?:earendil-works|mariozechner)/pi-coding-agent/dist/(?:bundle/)?cli\.(?:m|c)?js$"), "pi"),
    (re.compile(r"(?:^|/)cursor-agent/versions/[^/]+/index\.(?:m|c)?js$"), "cursor"),
    (re.compile(r"(?:^|/)@zcode/cli/(?:dist|bin)/[^/]+\.(?:m|c)?js$"), "zcode"),
    (re.compile(r"(?:^|/)prime-agent/dist/(?:bundle/)?cli\.(?:m|c)?js$"), "prime-agent"),
    (re.compile(r"(?:^|/)@deepseek-ai/dsh/lib/bin\.(?:m|c)?js$"), "dsh"),
    (re.compile(r"(?:^|/)@deepseek-harness-tui/dsh-tui/(?:dist|bin)/[^/]+\.(?:m|c)?js$"), "dsh"),
    (re.compile(r"(?:^|/)@qwen-code/qwen-code/(?:cli|dist/index|bin/qwen)\.(?:m|c)?js$"), "qwen"),
    (re.compile(r"(?:^|/)@augmentcode/auggie/(?:dist|bin)/[^/]+\.(?:m|c)?js$"), "auggie"),
)

_INTERPRETER_BASENAMES = frozenset({
    "node", "node.exe", "nodejs", "bun", "bun.exe",
    "python", "python3", "python.exe", "python3.exe",
})


def _strip_win_ext(name: str) -> str:
    lower = name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if lower.endswith(suffix):
            return lower[: -len(suffix)]
    return lower


def _extract_interpreter_script(command_normalized: str) -> Optional[str]:
    """If argv[0] is node/bun/python, find the first script path argument."""
    tokens = command_normalized.strip().split()
    if len(tokens) < 2:
        return None
    first_base = Path(tokens[0].strip("\"'")).name
    if first_base not in _INTERPRETER_BASENAMES and not re.match(r"^python3\.\d+$", first_base):
        return None
    skip_next = False
    for tok in tokens[1:]:
        cleaned = tok.strip("\"'")
        if skip_next:
            skip_next = False
            continue
        if cleaned in {"-e", "-c", "-m", "-r", "--require", "--import", "--loader"}:
            skip_next = True
            continue
        if cleaned.startswith("-"):
            continue
        return cleaned
    return None


def classify_orca_agent_process(executable_path_normalized: str, command_normalized: str) -> Optional[str]:
    """Match an executable path or Node/Python script entrypoint against Orca's catalog."""
    executable = _strip_win_ext(Path(executable_path_normalized).name)
    if executable:
        direct = PROCESS_EXECUTABLE_MAP.get(executable)
        if direct:
            return direct
        if executable == "acli" and "rovodev" in command_normalized:
            return "rovo"
        for pattern, provider in _VERSIONED_EXEC_PATTERNS:
            if pattern.match(executable):
                return provider

    for candidate in filter(None, (executable_path_normalized, _extract_interpreter_script(command_normalized))):
        if "/.gemini/antigravity-cli/" in candidate or "/com.google.antigravity/" in candidate:
            return "antigravity"
        if "/cursor-agent/" in candidate:
            return "cursor"
        for pattern, provider in _SCRIPT_ENTRYPOINT_PATTERNS:
            if pattern.search(candidate):
                return provider
        script_base = _strip_win_ext(Path(candidate).name)
        if script_base in {"aider", "hermes", "kimi", "kimi-code"}:
            return PROCESS_EXECUTABLE_MAP.get(script_base)
    return None


def _semver_sort_key(version_dir_name: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", version_dir_name)
    return tuple(int(n) for n in nums) if nums else (0,)


def user_cli_fallback_dirs(home: Path, system_dirs: Sequence[str] = ()) -> List[Path]:
    """Return bounded user-local + configured system CLI install dirs (Orca parity).

    All user paths stay strictly inside ``home`` so test sandboxes that redirect
    ``HOME`` and ``HOME_DISCOVERY_LAUNCHER_DIRS`` never escape to the host FS.
    """
    dirs: List[Path] = [
        home / ".local" / "bin",
        home / ".opencode" / "bin",
        home / ".vite-plus" / "bin",
        home / ".grok" / "bin",
        home / ".volta" / "bin",
        home / ".asdf" / "shims",
        home / ".fnm" / "aliases" / "default" / "bin",
        home / ".local" / "share" / "mise" / "shims",
        home / "Library" / "pnpm",
        home / ".local" / "share" / "pnpm",
        home / ".yarn" / "bin",
        home / ".bun" / "bin",
        home / ".nix-profile" / "bin",
    ]
    nvm_nodes = home / ".nvm" / "versions" / "node"
    try:
        if nvm_nodes.is_dir():
            versions = sorted(
                (entry for entry in nvm_nodes.iterdir() if entry.is_dir()),
                key=lambda p: _semver_sort_key(p.name),
                reverse=True,
            )
            dirs.extend(v / "bin" for v in versions[:6])
    except OSError:
        pass
    if os.name == "nt":
        appdata = home / "AppData" / "Roaming"
        local_appdata = home / "AppData" / "Local"
        dirs.extend([
            appdata / "npm",
            local_appdata / "pnpm",
            local_appdata / "Yarn" / "bin",
        ])
    for raw in system_dirs:
        if raw:
            dirs.append(Path(raw))
    return dirs


def _orca_model_catalog_paths(home: Path) -> Tuple[Path, ...]:
    return (
        home / "Library" / "Application Support" / "orca" / "agent-model-catalog.json",
        home / ".config" / "orca" / "agent-model-catalog.json",
        home / "AppData" / "Roaming" / "orca" / "agent-model-catalog.json",
    )


def read_orca_persisted_models(home: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Read Orca's persisted `agent-model-catalog.json` read-only if present.

    Strips account fingerprints and workspace paths; returns only public model
    identifiers, labels, default flags, and effort levels per agent ID.
    """
    catalog_path: Optional[Path] = None
    for candidate in _orca_model_catalog_paths(home):
        try:
            if candidate.is_file():
                catalog_path = candidate
                break
        except OSError:
            continue
    if catalog_path is None:
        return {}
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        return {}

    by_agent: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for entry in raw["entries"]:
        if not isinstance(entry, dict):
            continue
        raw_agent = entry.get("agent")
        if not isinstance(raw_agent, str) or not raw_agent:
            continue
        agent_id = _ORCA_ID_TO_SPEC_ID.get(raw_agent, raw_agent)
        configured = entry.get("configured") if isinstance(entry.get("configured"), dict) else {}
        configured_id = configured.get("modelId") if isinstance(configured.get("modelId"), str) else None
        # Prefer live-session listings over probe listings when merging.
        for source_key in ("discovered", "live"):
            listing = entry.get(source_key)
            if not isinstance(listing, dict) or not isinstance(listing.get("models"), list):
                continue
            bucket = by_agent.setdefault(agent_id, {})
            for model in listing["models"]:
                if not isinstance(model, dict):
                    continue
                mid = model.get("id")
                label = model.get("label")
                if not isinstance(mid, str) or not mid.strip() or not isinstance(label, str) or not label.strip():
                    continue
                efforts = [
                    e["value"]
                    for e in (model.get("efforts") or [])
                    if isinstance(e, dict) and isinstance(e.get("value"), str)
                ]
                is_default = bool(model.get("isDefault")) or (configured_id == mid)
                row: Dict[str, Any] = {
                    "id": mid.strip(),
                    "label": label.strip(),
                    "isDefault": is_default,
                }
                if isinstance(model.get("defaultEffort"), str) and model["defaultEffort"]:
                    row["defaultEffort"] = model["defaultEffort"]
                if efforts:
                    row["efforts"] = efforts
                if isinstance(model.get("supportsFastMode"), bool):
                    row["supportsFastMode"] = model["supportsFastMode"]
                existing = bucket.get(mid)
                if existing is None or source_key == "live" or (is_default and not existing.get("isDefault")):
                    bucket[mid] = row
    return {agent: list(models.values()) for agent, models in by_agent.items() if models}


def build_available_agents_catalog(
    home: Path,
    *,
    resolve_binary: Callable[[Tuple[str, ...], Tuple[Path, ...]], Optional[str]],
) -> Dict[str, Any]:
    """Build the privacy-safe `availableAgents` snapshot payload (no local paths)."""
    persisted_models = read_orca_persisted_models(home)
    items: List[Dict[str, Any]] = []
    installed_count = 0

    for spec in ORCA_AGENT_SPECS:
        names = (spec.cli_binary,) + spec.aliases
        extra_dirs = tuple(home / rel for rel in spec.extra_bin_relpaths)
        resolved = resolve_binary(names, extra_dirs)
        installed = bool(resolved)
        detected_cmd: Optional[str] = None
        if resolved:
            installed_count += 1
            detected_cmd = _strip_win_ext(Path(resolved).name) or spec.cli_binary

        models: List[Dict[str, Any]] = []
        seen_ids = set()
        for live_model in persisted_models.get(spec.id, []):
            mid = live_model["id"]
            seen_ids.add(mid)
            models.append({**live_model, "source": "orca"})
        has_default = any(m.get("isDefault") for m in models)
        for seed in spec.seed_models:
            if seed.id in seen_ids:
                continue
            seen_ids.add(seed.id)
            models.append({
                "id": seed.id,
                "label": seed.label,
                "isDefault": bool(seed.is_default and not has_default),
                "source": "catalog",
            })

        entry: Dict[str, Any] = {
            "id": spec.id,
            "displayName": spec.display_name,
            "vendor": spec.vendor,
            "cliBinary": spec.cli_binary,
            "installed": installed,
            "tier": spec.tier,
            "homepageUrl": spec.homepage_url,
            "models": models,
        }
        if spec.aliases:
            entry["aliases"] = list(spec.aliases)
        if detected_cmd:
            entry["detectedCommand"] = detected_cmd
        if spec.efforts:
            entry["efforts"] = list(spec.efforts)
        items.append(entry)

    return {
        "totalCount": len(items),
        "installedCount": installed_count,
        "agents": items,
    }
