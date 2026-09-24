"""Ask each installed agent CLI, per home, which account it is and how much
quota is left — the same answer its own /status or /usage prints.

Every probe is read-only toward the CLI's home:
- Codex: the official app-server (`account/read`, `account/rateLimits/read`)
  with CODEX_HOME set to the home.
- Claude: `claude -p /usage --no-session-persistence` with CLAUDE_CONFIG_DIR
  for non-default homes; identity from that home's own `oauthAccount`.
- Grok / Kimi: the CLI's own stored access token against the provider's usage
  endpoint. An expired token is reported, never refreshed: refreshing would
  rotate the credential the CLI itself relies on.

Results carry no filesystem paths; homes are keyed by an opaque hash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CLAUDE_USAGE_TIMEOUT_SECONDS = 60
_EMAIL_RE = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,253}$")
_CLAUDE_LINE_RE = re.compile(
    r"^\s*Current (session|week)(?: \(([^)]+)\))?:\s*(\d{1,3}(?:\.\d+)?)% used"
    r"(?:\s*·\s*resets\s+(.+?))?\s*$",
    re.IGNORECASE,
)
_CLAUDE_RESET_RE = re.compile(
    r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*(?:\(([^)]+)\))?$",
    re.IGNORECASE,
)


def home_key(home: Path) -> str:
    return hashlib.sha256(str(Path(home).expanduser()).encode("utf-8")).hexdigest()[:16]


def _email(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and _EMAIL_RE.match(value.strip()) else None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def parse_claude_reset(text: str, now: Optional[dt.datetime] = None) -> Optional[int]:
    """`Sep 24 at 11:10pm (Asia/Seoul)` -> epoch seconds. The year is implied."""
    match = _CLAUDE_RESET_RE.match(text.strip())
    if not match:
        return None
    month_name, day, hour, minute, meridiem, zone_name = match.groups()
    try:
        month = dt.datetime.strptime(month_name.title(), "%b").month
        hour24 = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
        tz: dt.tzinfo = dt.timezone.utc
        if zone_name:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(zone_name)
        now = now or dt.datetime.now(tz)
        now = now.astimezone(tz)
        candidate = dt.datetime(now.year, month, int(day), hour24, int(minute or 0), tzinfo=tz)
        # A reset is always ahead; a date that already passed belongs to next year.
        if candidate < now - dt.timedelta(days=1):
            candidate = candidate.replace(year=now.year + 1)
        return int(candidate.timestamp())
    except (ValueError, KeyError, OSError):
        return None


def parse_claude_usage(text: str, now: Optional[dt.datetime] = None) -> List[Dict[str, Any]]:
    windows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        match = _CLAUDE_LINE_RE.match(line)
        if not match:
            continue
        span, scope, percent, reset = match.groups()
        used = _clamp(float(percent))
        is_session = span.lower() == "session"
        scope_text = (scope or "").strip()
        all_models = not scope_text or scope_text.lower() == "all models"
        if is_session:
            window_id, label, mins = "claude:5h", "5h", 300
        elif all_models:
            window_id, label, mins = "claude:7d", "7d", 10080
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", scope_text.lower()).strip("-") or "model"
            window_id, label, mins = f"claude:7d:{slug}", f"{scope_text} 7d", 10080
        windows.append({
            "id": window_id,
            "label": label,
            "windowDurationMins": mins,
            "usedPercent": used,
            "remainingPercent": 100.0 - used,
            "resetsAt": parse_claude_reset(reset, now) if reset else None,
            "model": None if (is_session or all_models) else scope_text,
            "primary": not is_session and all_models,
        })
    return windows


def claude_identity(home: Path, default_home: Path) -> Dict[str, Optional[str]]:
    config = Path.home() / ".claude.json" if Path(home) == Path(default_home) else Path(home) / ".claude.json"
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"email": None, "accountID": None}
    account = raw.get("oauthAccount") if isinstance(raw, dict) else None
    if not isinstance(account, dict):
        return {"email": None, "accountID": None}
    account_id = account.get("accountUuid")
    return {
        "email": _email(account.get("emailAddress")),
        "accountID": account_id if isinstance(account_id, str) else None,
    }


def _result(provider: str, home: Path, **fields: Any) -> Dict[str, Any]:
    row = {
        "provider": provider,
        "homeKey": home_key(home),
        "email": None,
        "accountID": None,
        "plan": None,
        "status": "ok",
        "reason": None,
        "windows": [],
        "fetchedAt": int(time.time()),
    }
    row.update(fields)
    return row


def probe_claude_home(
    home: Path,
    default_home: Path,
    executable: Optional[str],
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    cwd: Optional[Path] = None,
    token_reader: Optional[Callable[[Path, Path], Optional[str]]] = None,
) -> Dict[str, Any]:
    identity = claude_identity(home, default_home)
    if not executable:
        return _result("claude", home, status="error", reason="cli_not_found", **identity)
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    if Path(home) == Path(default_home):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(home)
    try:
        completed = run(
            [executable, "-p", "/usage", "--no-session-persistence"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CLAUDE_USAGE_TIMEOUT_SECONDS,
            env=env,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        return _result("claude", home, status="error", reason="cli_timeout", **identity)
    except OSError:
        return _result("claude", home, status="error", reason="cli_failed", **identity)
    windows = parse_claude_usage(completed.stdout or "")
    details = claude_account_details(home, default_home)
    token = (token_reader or claude_access_token)(home, default_home)
    if token:
        try:
            passes = fetch_claude_passes(token)
        except Exception:
            passes = None
        if passes and passes["eligible"]:
            details["resetCreditsAvailable"] = passes["available"]
            details["resetCredits"] = passes["credits"]
    if not windows:
        return _result("claude", home, status="error", reason="usage_unparsed", **identity, **details)
    return _result("claude", home, windows=windows, **identity, **details)


def _codex_window(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    used = raw.get("usedPercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    mins = raw.get("windowDurationMins")
    label = "7d" if mins == 10080 else "5h" if mins == 300 else (raw.get("name") or "quota")
    return {
        "id": "codex:" + str(raw.get("id") or label),
        "label": label,
        "windowDurationMins": mins if isinstance(mins, int) else None,
        "usedPercent": _clamp(used),
        "remainingPercent": 100.0 - _clamp(used),
        "resetsAt": raw.get("resetsAt") if isinstance(raw.get("resetsAt"), int) else None,
        "model": raw.get("model") if isinstance(raw.get("model"), str) else None,
        "primary": bool(raw.get("primary")) and raw.get("limitID") in (None, "default", "codex"),
    }


def probe_codex_home(home: Path, server_factory: Callable[[Path], Any]) -> Dict[str, Any]:
    server = server_factory(Path(home))
    try:
        server.start()
        account = server.account() or {}
        usage = server.usage() or {}
    except Exception as exc:  # the app-server raises its own error type
        reason = "unauthorized" if "401" in str(exc) or "unauthorized" in str(exc).lower() else "cli_failed"
        return _result("codex", home, status="error", reason=reason)
    finally:
        try:
            server.close()
        except Exception:
            pass
    windows = [w for w in (_codex_window(r) for r in usage.get("windows") or [] if isinstance(r, dict)) if w]
    extra: Dict[str, Any] = {}
    reset = usage.get("resetCredits") if isinstance(usage.get("resetCredits"), dict) else None
    if reset and isinstance(reset.get("availableCount"), int):
        extra["resetCreditsAvailable"] = reset["availableCount"]
        extra["resetCredits"] = [
            {k: str(v) for k, v in item.items() if k in ("status", "resetType", "expiresAt", "grantedAt") and v is not None}
            for item in reset.get("details") or [] if isinstance(item, dict)
        ]
    billing = codex_billing(home)
    if billing:
        extra["billing"] = billing
    return _result(
        "codex",
        home,
        email=_email(account.get("email")),
        plan=account.get("planType"),
        windows=windows,
        rateLimitReached=bool(usage.get("rateLimitReachedType")),
        status="ok" if windows else "error",
        reason=None if windows else "usage_unavailable",
        **extra,
    )


def _jwt_claims(token: str) -> Dict[str, Any]:
    import base64

    if token.count(".") < 2:
        return {}
    try:
        part = token.split(".", 2)[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def probe_token_home(provider: str, home: Path, module: Any, token_key: str) -> Dict[str, Any]:
    """Grok/Kimi: use the CLI's current access token read-only."""
    now = int(time.time())
    candidates = module._credential_candidates(Path(home))
    if not candidates:
        return _result(provider, home, status="error", reason="not_signed_in")
    live = [c for c in candidates if c.get(token_key) and (c.get("expires") is None or c["expires"] > now)]
    if not live:
        # Refreshing here would rotate the CLI's own refresh token.
        return _result(provider, home, status="error", reason="cli_login_expired")
    token = live[0][token_key]
    claims = _jwt_claims(token)
    email = _email(claims.get("email")) or _email((live[0].get("raw") or {}).get("email"))
    if provider == "grok":
        try:
            grok = fetch_grok_billing(module, token)
        except Exception:
            return _result(provider, home, status="error", reason="usage_unavailable", email=email)
        return _result(
            provider, home, email=email, windows=grok["windows"], balances=grok.get("balances") or {},
            status="ok" if grok["windows"] else "error",
            reason=None if grok["windows"] else "usage_unavailable",
        )
    if provider == "kimi":
        try:
            kimi = fetch_kimi_usage(module, token)
        except Exception:
            return _result(provider, home, status="error", reason="usage_unavailable", email=email)
        return _result(
            provider, home, email=kimi["email"] or email, windows=kimi["windows"],
            status="ok" if kimi["windows"] else "error",
            reason=None if kimi["windows"] else "usage_unavailable",
        )
    try:
        usage = module._live_usage(token)
    except Exception:
        return _result(provider, home, status="error", reason="usage_unavailable", email=email)
    windows = []
    for raw in usage.get("windows") or []:
        used = raw.get("usedPercent")
        if not isinstance(used, (int, float)):
            continue
        window_id = str(raw.get("id") or f"{provider}:quota")
        is_week = window_id.endswith("7d")
        windows.append({
            "id": window_id,
            "label": "7d" if is_week else "5h" if window_id.endswith("5h") else "quota",
            "windowDurationMins": 10080 if is_week else 300 if window_id.endswith("5h") else None,
            "usedPercent": _clamp(used),
            "remainingPercent": 100.0 - _clamp(used),
            "resetsAt": raw.get("resetsAt") if isinstance(raw.get("resetsAt"), int) else None,
            "model": None,
            "primary": True,
        })
    return _result(
        provider,
        home,
        email=email,
        plan=usage.get("subscriptionTier"),
        windows=windows,
        status="ok" if windows else "error",
        reason=None if windows else "usage_unavailable",
    )


def parse_antigravity_quota_summary(payload: Any) -> List[Dict[str, Any]]:
    """`retrieveUserQuotaSummary` groups -> windows, the same data agy shows."""
    windows: List[Dict[str, Any]] = []
    groups = payload.get("groups") if isinstance(payload, dict) else None
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("displayName") or "")
        short = "Gemini" if "gemini" in name.lower() else "Claude+GPT" if ("claude" in name.lower() or "gpt" in name.lower()) else (name or "Models")
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            fraction = bucket.get("remainingFraction")
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
                continue
            span = str(bucket.get("window") or bucket.get("bucketId") or "").lower()
            weekly = "week" in span or span.endswith("7d")
            short_window = "5h" in span or "hour" in span
            used = _clamp((1.0 - float(fraction)) * 100.0)
            reset = None
            if isinstance(bucket.get("resetTime"), str):
                try:
                    reset = int(dt.datetime.fromisoformat(bucket["resetTime"].replace("Z", "+00:00")).timestamp())
                except ValueError:
                    reset = None
            windows.append({
                "id": "antigravity:" + str(bucket.get("bucketId") or f"{short}:{span}"),
                "label": f"{short} {'7d' if weekly else '5h' if short_window else span}",
                "windowDurationMins": 10080 if weekly else 300 if short_window else None,
                "usedPercent": used,
                "remainingPercent": 100.0 - used,
                "resetsAt": reset,
                "model": None,
                "primary": weekly,
            })
    return windows


def _iso_epoch(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def parse_kimi_usage(payload: Any) -> List[Dict[str, Any]]:
    """Kimi Code /usages, mapped as CodexBar does: `usage` is the weekly pool,
    each `limits[]` entry is a rate window of `window.duration` time units."""
    windows: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return windows
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def window(window_id: str, label: str, mins: Optional[int], detail: Any, primary: bool) -> None:
        if not isinstance(detail, dict):
            return
        limit = _number(detail.get("limit"))
        used = _number(detail.get("used"))
        remaining = _number(detail.get("remaining"))
        if used is None and limit is not None and remaining is not None:
            used = max(limit - remaining, 0.0)
        if used is None or not limit:
            return
        percent = _clamp(used * 100.0 / limit)
        windows.append({
            "id": window_id,
            "label": label,
            "windowDurationMins": mins,
            "usedPercent": percent,
            "remainingPercent": 100.0 - percent,
            "resetsAt": _iso_epoch(detail.get("resetTime") or detail.get("reset_time")),
            "model": None,
            "primary": primary,
        })

    window("kimi:7d", "7d", 10080, data.get("usage"), True)
    for index, item in enumerate(data.get("limits") or []):
        if not isinstance(item, dict):
            continue
        spec = item.get("window") if isinstance(item.get("window"), dict) else {}
        duration = _number(spec.get("duration"))
        unit = str(spec.get("timeUnit") or "").upper()
        factor = 1 if "MINUTE" in unit else 60 if "HOUR" in unit else 1440 if "DAY" in unit else None
        mins = int(duration * factor) if duration is not None and factor else None
        label = "5h" if mins == 300 else f"{mins // 60}h" if mins and mins % 60 == 0 else "quota"
        window(f"kimi:limit:{index}", label, mins, item.get("detail"), False)
    return windows


def fetch_kimi_usage(module: Any, token: str) -> Dict[str, Any]:
    from urllib.request import Request, urlopen

    request = Request(module._USAGE_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "kimi-code/1.0"})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8"))
    identity, _ = module._verified_email_identity(token)
    email = _email((identity.get("identity") or {}).get("email"))
    return {"windows": parse_kimi_usage(payload), "email": email}


def next_monthly_renewal(anchor: Any, now: Optional[dt.datetime] = None) -> Optional[Dict[str, Any]]:
    """Next monthly renewal from a subscription date. A date already past is
    rolled forward month by month and marked estimated: the CLI only records
    when the subscription started or was last confirmed."""
    if not isinstance(anchor, str) or not anchor:
        return None
    try:
        when = dt.datetime.fromisoformat(anchor.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    if when > now:
        return {"renewsAt": when.isoformat().replace("+00:00", "Z"), "estimated": False}
    months = 0
    candidate = when
    while candidate <= now and months < 600:
        months += 1
        year = when.year + (when.month - 1 + months) // 12
        month = (when.month - 1 + months) % 12 + 1
        day = min(when.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        candidate = when.replace(year=year, month=month, day=day)
    return {"renewsAt": candidate.isoformat().replace("+00:00", "Z"), "estimated": True}


def codex_billing(home: Path, now: Optional[dt.datetime] = None) -> Optional[Dict[str, Any]]:
    """Subscription window from the CLI's own id_token claims (dates only)."""
    try:
        raw = json.loads((Path(home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tokens = raw.get("tokens") if isinstance(raw, dict) else None
    token = tokens.get("id_token") if isinstance(tokens, dict) else None
    claims = _jwt_claims(token) if isinstance(token, str) else {}
    auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    if not isinstance(auth, dict):
        return None
    return next_monthly_renewal(auth.get("chatgpt_subscription_active_until"), now)


_CLAUDE_TIERS = {"max_5x": "Max 5x", "max_20x": "Max 20x", "pro": "Pro", "team": "Team", "enterprise": "Enterprise"}


def claude_account_details(home: Path, default_home: Path, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    config = Path.home() / ".claude.json" if Path(home) == Path(default_home) else Path(home) / ".claude.json"
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    account = raw.get("oauthAccount") if isinstance(raw, dict) else None
    if not isinstance(account, dict):
        return {}
    tier = str(account.get("organizationRateLimitTier") or account.get("userRateLimitTier") or "")
    plan = next((label for key, label in _CLAUDE_TIERS.items() if key in tier), None)
    details: Dict[str, Any] = {"plan": plan}
    billing = next_monthly_renewal(account.get("subscriptionCreatedAt"), now)
    if billing:
        details["billing"] = billing
    if isinstance(account.get("hasExtraUsageEnabled"), bool):
        details["extraUsageEnabled"] = account["hasExtraUsageEnabled"]
    return details


CLAUDE_PASSES_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"


def claude_keychain_service(home: Path, default_home: Path) -> str:
    """Claude Code keeps each config dir's login under its own Keychain item."""
    if Path(home) == Path(default_home):
        return "Claude Code-credentials"
    scope = hashlib.sha256(os.path.realpath(str(home)).encode("utf-8")).hexdigest()[:8]
    return f"Claude Code-credentials-{scope}"


def claude_access_token(home: Path, default_home: Path, run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """The home's current Claude Code access token, read-only; None if expired."""
    try:
        completed = run(
            ["security", "find-generic-password", "-s", claude_keychain_service(home, default_home), "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if completed.returncode != 0:
            return None
        oauth = json.loads(completed.stdout).get("claudeAiOauth") or {}
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
        return None
    token = oauth.get("accessToken")
    expires = oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        return None
    if isinstance(expires, (int, float)) and expires / 1000 <= time.time() + 60:
        return None  # never refresh: that would rotate Claude Code's own login
    return token


def parse_claude_passes(payload: Any) -> Optional[Dict[str, Any]]:
    """`cedar_ember` from /api/oauth/usage: usage-limit reset grants."""
    block = payload.get("cedar_ember") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return None
    passes = []
    for grant in block.get("grants") or []:
        if not isinstance(grant, dict):
            continue
        left = grant.get("resets_left")
        if not isinstance(left, int):
            continue
        for _ in range(max(0, left)):
            passes.append({
                "status": "available" if grant.get("usable_now") else "unavailable",
                "resetType": "claudeUsageLimits",
                "title": str(grant.get("label") or "")[:120],
                "expiresAt": grant.get("ends_at"),
            })
    return {
        "available": len(passes),
        "credits": passes,
        "atLimit": bool(block.get("at_limit")),
        "eligible": bool(block.get("eligible")),
    }


def fetch_claude_passes(token: str, version: str = "2.1.280") -> Optional[Dict[str, Any]]:
    from urllib.request import Request, urlopen

    # Read-only. Eligibility is per client surface, so identify as the CLI the
    # token was issued to; claiming a reset is never sent from here.
    request = Request(CLAUDE_PASSES_URL, headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": f"claude-cli/{version} (external, cli)", "x-app": "cli",
    })
    with urlopen(request, timeout=15) as response:
        return parse_claude_passes(json.loads(response.read(2_000_000).decode("utf-8")))


def parse_grok_billing(payload: Any) -> Dict[str, Any]:
    """Grok CLI billing: the weekly credit window plus balances (no renewal date:
    billingPeriod* mirrors the weekly usage period, not the subscription)."""
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        return {"windows": []}
    percent = config.get("creditUsagePercent")
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    kind = str(period.get("type") or "").upper()
    weekly = "WEEK" in kind or not kind
    windows = []
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        used = _clamp(percent)
        windows.append({
            "id": "grok:7d" if weekly else "grok:period",
            "label": "7d" if weekly else "period",
            "windowDurationMins": 10080 if weekly else None,
            "usedPercent": used,
            "remainingPercent": 100.0 - used,
            "resetsAt": _iso_epoch(period.get("end")),
            "model": None,
            "primary": True,
        })

    def val(key: str) -> Optional[float]:
        raw = config.get(key)
        return _number(raw.get("val")) if isinstance(raw, dict) else None

    return {
        "windows": windows,
        "balances": {k: v for k, v in (("prepaid", val("prepaidBalance")), ("onDemandCap", val("onDemandCap")), ("onDemandUsed", val("onDemandUsed"))) if v is not None},
    }


def fetch_grok_billing(module: Any, token: str) -> Dict[str, Any]:
    from urllib.request import Request, urlopen

    request = Request(module._BILLING_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with urlopen(request, timeout=12) as response:
        return parse_grok_billing(json.loads(response.read(1_000_000).decode("utf-8")))


COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"


def parse_copilot_user(payload: Any) -> Dict[str, Any]:
    """copilot_internal/user: premium requests and chat as monthly windows."""
    if not isinstance(payload, dict):
        return {"windows": [], "subscribed": False}
    sku = str(payload.get("access_type_sku") or "")
    snapshots = payload.get("quota_snapshots") if isinstance(payload.get("quota_snapshots"), dict) else {}
    reset = _iso_epoch(payload.get("quota_reset_date_utc") or payload.get("quota_reset_date"))
    windows = []
    for key, label in (("premium_interactions", "Premium"), ("chat", "Chat"), ("completions", "Completions")):
        snap = snapshots.get(key)
        if not isinstance(snap, dict) or snap.get("unlimited"):
            continue
        entitlement = _number(snap.get("entitlement"))
        remaining = _number(snap.get("remaining"))
        percent_left = _number(snap.get("percent_remaining"))
        if percent_left is None and entitlement and remaining is not None:
            percent_left = remaining * 100.0 / entitlement
        if percent_left is None or (not entitlement and not remaining):
            continue  # placeholder snapshot: no usable signal
        used = _clamp(100.0 - percent_left)
        windows.append({
            "id": f"copilot:{key}", "label": label, "windowDurationMins": None,
            "usedPercent": used, "remainingPercent": 100.0 - used, "resetsAt": reset,
            "model": None, "primary": key == "premium_interactions",
            "remainingCount": remaining, "entitlement": entitlement,
        })
    return {
        "windows": windows,
        "subscribed": bool(windows) or (sku not in ("", "no_access")),
        "plan": payload.get("copilot_plan") if sku not in ("", "no_access") else None,
        "login": payload.get("login") if isinstance(payload.get("login"), str) else None,
        "canSignupFree": bool(payload.get("can_signup_for_limited")),
    }


def gh_accounts(run: Callable[..., Any] = subprocess.run) -> List[Dict[str, str]]:
    """Every github.com account the gh CLI is logged into: (login, token)."""
    try:
        status = run(["gh", "auth", "status", "--hostname", "github.com", "--json", "hosts"],
                     capture_output=True, text=True, timeout=10)
        hosts = json.loads(status.stdout).get("hosts", {}).get("github.com", []) if status.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
        hosts = []
    logins = [h.get("login") for h in hosts if isinstance(h, dict) and isinstance(h.get("login"), str)]
    accounts = []
    for login in logins or [None]:
        args = ["gh", "auth", "token", "--hostname", "github.com"] + (["--user", login] if login else [])
        try:
            out = run(args, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        token = (out.stdout or "").strip()
        if out.returncode == 0 and token:
            accounts.append({"login": login or "", "token": token})
    return accounts


def probe_copilot(run: Callable[..., Any] = subprocess.run) -> List[Dict[str, Any]]:
    from urllib.request import Request, urlopen

    rows = []
    for account in gh_accounts(run):
        home = Path("gh") / (account["login"] or "default")
        try:
            request = Request(COPILOT_USER_URL, headers={
                "Authorization": "token " + account["token"], "Accept": "application/json",
                "Editor-Version": "vscode/1.99.0", "User-Agent": "AgentCat",
            })
            with urlopen(request, timeout=15) as response:
                parsed = parse_copilot_user(json.loads(response.read(1_000_000).decode("utf-8")))
        except Exception:
            rows.append(_result("copilot", home, status="error", reason="usage_unavailable"))
            continue
        if not parsed["subscribed"]:
            rows.append(_result("copilot", home, status="error", reason="not_subscribed", accountID=parsed["login"]))
            continue
        rows.append(_result("copilot", home, windows=parsed["windows"], plan=parsed["plan"], accountID=parsed["login"],
                            status="ok" if parsed["windows"] else "error",
                            reason=None if parsed["windows"] else "usage_unavailable"))
    return rows
