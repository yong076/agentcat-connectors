"""Read-only, local-only projections for the desktop experience.

Never forward gateway account/key/session groups, URLs, paths or raw model IDs.
Gateway figures are a separate observation, never added to CLI usage totals.
"""
import copy
import datetime as dt
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


def stamp(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read_object(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = handle.read(1024 * 1024 + 1)
        value = json.loads(data) if len(data) <= 1024 * 1024 else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def loopback_origin(value: Any) -> Optional[str]:
    # Exact hosts avoid DNS, userinfo, redirects and disguised public endpoints.
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"http://(127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})(?:/v1)?/?", value)
    if not match or not 1 <= int(match[2]) <= 65535:
        return None
    host = "[::1]" if match[1] == "[::1]" else "127.0.0.1"
    return "http://%s:%s" % (host, match[2])


def configured_gateways(claude_home: Path, codex_home: Path) -> dict:
    found: Dict[str, list] = {}
    env = read_object(claude_home / "settings.json").get("env")
    origin = loopback_origin(env.get("ANTHROPIC_BASE_URL")) if isinstance(env, dict) else None
    if origin:
        found.setdefault(origin, []).append("claude")
    # Only the active named provider's exact TOML section; never arbitrary URLs
    # or credentials. Compatible with the connector's Python 3.9 floor.
    try:
        with (codex_home / "config.toml").open() as handle:
            content = handle.read(1024 * 1024)
        top = content.split("[", 1)[0]
        selected = re.search(r'^\s*model_provider\s*=\s*["\']([A-Za-z0-9_-]+)["\']\s*(?:#.*)?$', top, re.M)
        if selected:
            section = re.search(r'^\[model_providers\.' + re.escape(selected[1]) + r'\]\s*\n([^\[]*)', content, re.M)
            base = re.search(r'^\s*base_url\s*=\s*["\']([^"\']+)["\']\s*(?:#.*)?$', section[1], re.M) if section else None
            origin = loopback_origin(base[1]) if base else None
            if origin:
                found.setdefault(origin, []).append("codex")
    except OSError:
        pass
    return found


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_local_json(url: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=0.5) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("response_too_large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("invalid_response")
    return value


def model_family(value: Any) -> str:
    if not isinstance(value, str):
        return "Unknown"
    value = value.lower().rsplit("/", 1)[-1]
    for prefix, family in (("claude", "Claude"), ("gpt", "GPT"), ("kimi", "Kimi"),
                           ("deepseek", "DeepSeek"), ("glm", "GLM"), ("gemini", "Gemini"),
                           ("grok", "Grok"), ("llama", "Llama"), ("qwen", "Qwen")):
        if value == prefix or value.startswith(prefix + "-"):
            return family
    return "Unknown"


def usage_projection(raw: dict) -> list:
    vendors = {"anthropic": "Anthropic", "openai": "OpenAI", "kimi": "Moonshot",
               "moonshot": "Moonshot", "deepseek": "DeepSeek", "zai": "Z.ai",
               "google": "Google", "gemini": "Google", "xai": "xAI", "grok": "xAI"}
    grouped = {}
    rows = raw.get("models")
    for row in rows[:200] if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        key = (vendors.get(str(row.get("provider", "")).lower(), "Unknown"), model_family(row.get("model")))
        bucket = grouped.setdefault(key, {"vendor": key[0], "modelFamily": key[1], "input": 0, "output": 0,
                                          "cacheRead": 0, "cacheWrite": 0})
        for source, target in (("input", "input"), ("output", "output"), ("cache_read", "cacheRead"), ("cache_write", "cacheWrite")):
            value = row.get(source)
            if type(value) is int and 0 <= value <= 10**15:
                bucket[target] += value
    return sorted(grouped.values(), key=lambda row: -(row["input"] + row["output"]))[:12]


class GatewayReader:
    def __init__(self, fetch=fetch_local_json, clock=time.time):
        self.fetch, self.clock, self.cache = fetch, clock, {}

    def snapshot(self, configured: dict) -> list:
        now = self.clock()
        result = []
        # Do not retain removed configuration or another user's gateway.
        self.cache = {key: value for key, value in self.cache.items() if key in configured}
        for origin, clients in list(configured.items())[:2]:
            if loopback_origin(origin) != origin:
                continue
            previous = self.cache.get(origin)
            if previous and 0 <= now - previous[0] < 60:
                item = copy.deepcopy(previous[1])
            else:
                item = {"status": "unavailable", "stale": True, "models": []}
                try:
                    identity = self.fetch(origin + "/")
                    if identity.get("name") != "magpie":
                        raise ValueError("unrecognized_gateway")
                    usage = self.fetch(origin + "/v1/magpie/usage?period=today")
                    item = {"name": "magpie", "status": "ok", "updatedAt": stamp(now), "stale": False,
                            "models": usage_projection(usage)}
                except Exception:
                    if previous and previous[1].get("name") == "magpie":
                        item = {**copy.deepcopy(previous[1]), "status": "unavailable", "stale": True}
                self.cache[origin] = (now, copy.deepcopy(item))
            item["clients"] = sorted(set(clients) & {"claude", "codex"})
            result.append(item)
        return result


def session_observation(event: str, payload: Any, session_key: str, now: int) -> Optional[dict]:
    if not session_key or not isinstance(payload, dict):
        return None
    state = {"SessionStart": "running", "UserPromptSubmit": "running", "PostToolUse": "running",
             "Stop": "completed", "SessionEnd": "completed", "PermissionRequest": "waiting"}.get(event)
    if event == "Notification" and payload.get("notification_type") in ("permission_prompt", "idle_prompt"):
        state = "waiting"
    if not state:
        return None
    return {"session": session_key, "state": state, "observedAt": now}


def session_summary(observations: list, now: int) -> dict:
    latest = {}
    for observation in observations:
        key, when = observation.get("session"), observation.get("observedAt")
        if not isinstance(key, str) or type(when) is not int or not 0 <= now - when <= 600:
            continue
        if key not in latest or when >= latest[key]["observedAt"]:
            latest[key] = observation
    counts = {"waiting": 0, "running": 0, "completed": 0}
    for row in latest.values():
        if row.get("state") in counts:
            counts[row["state"]] += 1
    return {**counts, "provider": "claude", "updatedAt": stamp(now),
            "expiresAt": stamp(min((x["observedAt"] + 600 for x in latest.values()), default=now)),
            "source": "claude-hooks"}
