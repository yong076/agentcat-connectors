"""Redirect every real filesystem path a loaded agentcat module holds.

Each test module loads its own copy of `bin/agentcat` via SourceFileLoader, and
that copy computes its paths from the *real* `$HOME` at import time. A test that
swaps only `HOME`/`AGENTCAT_HOME` leaves the rest — telemetry logs, usage
caches, cursors — pointing at the developer's live install, so anything that
walks the full snapshot touches real state.

That is not hypothetical: it truncated a real 8.9 GB telemetry log during
development of the rotation fix. `redirect_module_paths` swaps all of them and
`assert_sandboxed` fails loudly if a new module-level path is ever added without
being redirected here.
"""

from pathlib import Path


def _redirect(home: Path, agentcat_home: Path) -> dict:
    """Sandbox value for each module-level path constant."""
    return {
        "HOME": home,
        "AGENTCAT_HOME": agentcat_home,
        "EVENTS_DB": agentcat_home / "events.sqlite",
        "LATEST_SNAPSHOT": agentcat_home / "latest-snapshot.json",
        "LIMITS_FILE": agentcat_home / "limits.json",
        "AGENTCAT_KEYS_FILE": agentcat_home / "keys.json",
        "LIVE_LIMITS_CACHE": agentcat_home / "live-limits-cache.json",
        "PROVIDER_INSTANCE_SECRET": agentcat_home / "provider-instance.key",
        "JOURNAL_CURSOR_FILE": agentcat_home / "jsonl-cursor.json",
        "CODEX_SESSIONS_CURSOR_FILE": agentcat_home / "codex-sessions-cursor.json",
        "CODEX_USAGE_BREAKDOWN_CACHE": agentcat_home / "codex-usage-breakdown-cache.json",
        "CLAUDE_CONFIG_DIR": home / ".claude",
        "CLAUDE_PROJECTS_DIR": home / ".claude" / "projects",
        "GEMINI_TELEMETRY": agentcat_home / "gemini" / "telemetry.log",
        "GEMINI_USAGE_CACHE": agentcat_home / "gemini-usage-cache.json",
        "ANTIGRAVITY_CLI_DIR": home / ".gemini" / "antigravity-cli",
        "ANTIGRAVITY_TELEMETRY": agentcat_home / "gemini" / "antigravity-telemetry.log",
        "ANTIGRAVITY_USAGE_CACHE": agentcat_home / "antigravity-usage-cache.json",
        "ANTIGRAVITY_OAUTH_TOKEN": home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token",
        "ANTIGRAVITY_CLIENT_CACHE": agentcat_home / "antigravity-oauth-client.json",
        "PRICING_CACHE_FILE": agentcat_home / "pricing-cache.json",
        "LAUNCHD_AGENT_PLIST": home / "Library" / "LaunchAgents" / "agentcatd.plist",
        "WINDOWS_LEGACY_STARTUP_SCRIPT": home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AgentCatD.vbs",
    }


def redirect_module_paths(module, home: Path, agentcat_home: Path) -> dict:
    """Point every path constant at the sandbox. Returns the originals."""
    originals = {}
    # Snapshot tests must never call the developer's running Orca account API.
    if hasattr(module, "ORCA_ACCOUNTS_ENABLED"):
        originals["ORCA_ACCOUNTS_ENABLED"] = module.ORCA_ACCOUNTS_ENABLED
        module.ORCA_ACCOUNTS_ENABLED = False
    for name, sandboxed in _redirect(home, agentcat_home).items():
        if not hasattr(module, name):
            continue  # constant renamed or removed upstream; nothing to redirect
        originals[name] = getattr(module, name)
        setattr(module, name, sandboxed)
    assert_sandboxed(module, home, agentcat_home)
    return originals


def restore_module_paths(module, originals: dict) -> None:
    for name, value in originals.items():
        setattr(module, name, value)


def block_network(module):
    """Context manager that makes every outbound HTTP call fail immediately.

    Sandboxing the paths also removes the real pricing / live-limit caches, so
    code that would normally serve from cache starts reaching for the network
    and each provider burns its full timeout. The connector already degrades
    gracefully on a failed request, so failing fast is both faster and a
    truer unit test than depending on the developer's connectivity.
    """
    from unittest.mock import patch

    return patch.object(
        module.urllib.request,
        "urlopen",
        side_effect=OSError("network disabled in tests"),
    )


def assert_sandboxed(module, home: Path, agentcat_home: Path) -> None:
    """Fail if any module-level Path still points outside the sandbox.

    Catches the case this file exists to prevent: a new path constant lands
    upstream, no test redirects it, and the suite silently starts reading and
    writing the developer's live install.
    """
    allowed = (str(home), str(agentcat_home))
    leaked = []
    for name in dir(module):
        if name.startswith("__") or not name.isupper():
            continue
        value = getattr(module, name, None)
        if isinstance(value, Path) and not str(value).startswith(allowed):
            leaked.append(f"{name}={value}")
    if leaked:
        raise AssertionError(
            "module paths escape the test sandbox (add them to tests/sandbox.py): "
            + ", ".join(sorted(leaked))
        )
