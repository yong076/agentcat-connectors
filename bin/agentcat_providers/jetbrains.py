"""JetBrains AI monthly quota from the IDE's local XML cache."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

from ._base import HomeSpec, ProviderSpec, cost as _cost


SPEC = ProviderSpec(
    id="jetbrains", display_name="JetBrains AI", brand_color="#FF7A00", icon_hint="jetbrains",
    windows=("monthly",), source_hint_key="provider.source.jetbrains",
    homes=(
        HomeSpec("JETBRAINS_CONFIG_ROOT", "Library/Preferences", ("JetBrains",),
                 ("JetBrains*/options/AIAssistantQuotaManager2.xml",)),
        HomeSpec(None, "Library/Application Support/JetBrains", (),
                 ("*/options/AIAssistantQuotaManager2.xml",)),
        HomeSpec(None, ".config/JetBrains", (), ("*/options/AIAssistantQuotaManager2.xml",)),
        HomeSpec(None, "AppData/Roaming/JetBrains", (), ("*/options/AIAssistantQuotaManager2.xml",)),
    ),
    capabilities=("usage.jetbrains.localQuota",),
)


def _home_paths(ctx) -> List[Path]:
    raw = ctx.invoke("home_paths", SPEC)
    return [Path(path) for path in raw] if isinstance(raw, list) else []


def discover(ctx) -> List[Path]:
    candidates: List[Path] = []
    for root in _home_paths(ctx):
        patterns = (
            "JetBrains*/options/AIAssistantQuotaManager2.xml",
            "*/options/AIAssistantQuotaManager2.xml",
            "options/AIAssistantQuotaManager2.xml",
        )
        for pattern in patterns:
            try:
                candidates.extend(path for path in root.glob(pattern) if path.is_file())
            except OSError:
                continue
    unique = {str(path): path for path in candidates}
    return sorted(unique.values(), key=lambda path: path.stat().st_mtime, reverse=True)


def _read_option(root: ET.Element, name: str) -> Optional[Dict[str, Any]]:
    for option in root.iter("option"):
        if option.attrib.get("name") != name:
            continue
        try:
            value = json.loads(option.attrib.get("value") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def _epoch(raw: Any) -> Optional[int]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def _quota_from_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ET.ParseError):
        return None
    quota_info = _read_option(root, "quotaInfo")
    if quota_info is None:
        return None
    next_refill = _read_option(root, "nextRefill") or {}
    try:
        maximum = float(quota_info.get("maximum"))
        current = float(quota_info.get("current"))
    except (TypeError, ValueError):
        maximum = current = 0.0
    used_percent = min(max(current / maximum * 100.0, 0.0), 100.0) if maximum > 0 else None
    if used_percent is None and quota_info.get("type") not in {None, "Available"}:
        used_percent = 100.0
    remaining = max(maximum - current, 0.0) if maximum > 0 else None
    quota = {
        "id": "jetbrains:monthly",
        "label": "Monthly AI credits",
        "window": "monthly",
        "scope": "account",
        "aggregate": True,
        "unit": "credits",
        "used": current if maximum > 0 else None,
        "remaining": remaining,
        "limit": maximum if maximum > 0 else None,
        "usedPercent": used_percent,
        "remainingPercent": 100.0 - used_percent if used_percent is not None else None,
        "resetAt": _epoch(next_refill.get("next")),
        "model": None,
        "source": "jetbrains-ai-quota-xml",
    }
    return quota


def usage(ctx, home=None) -> Dict[str, Any]:
    files = discover(ctx)
    status = "ok" if files and _quota_from_file(files[0]) is not None else "not_found"
    return {
        "status": status,
        "source": "jetbrains-ai-quota-xml" if status == "ok" else None,
        "tokens": {"today": 0, "week": 0, "month": 0, "all": 0},
        "models": {}, "dailyTokens": {}, "hourlyTokens": {},
        "breakdown": {"status": "not_available"},
        "projects": {"status": "not_available", "items": []},
    }


def quota(ctx, home=None) -> Dict[str, Any]:
    files = discover(ctx)
    item = _quota_from_file(files[0]) if files else None
    if item is None:
        return {"status": "not_configured", "source": "jetbrains-ai-quota-xml", "quotas": []}
    return {
        "status": "auto", "source": "jetbrains-ai-quota-xml",
        "monthlyTokens": item.get("limit"), "quotas": [item],
    }


def cost(ctx, usage_slice): return _cost(ctx, SPEC, usage_slice)


def health(ctx) -> Dict[str, Any]:
    return {"status": "ok" if discover(ctx) else "not_found", "source": "jetbrains-ai-quota-xml"}
