"""Credential-blind, isolated device login adapter for the Grok CLI."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from agentcat_managed_platform import augment_search_path, default_cli_fallback_path, resolve_cli, restrict_private


_LOCK = threading.RLock()
_OPERATIONS: Dict[str, Dict[str, Any]] = {}
_PROBE: Optional[tuple[float, Optional[str], bool]] = None
_PROBE_TTL = 60.0
_OUTPUT_LIMIT = 8192
_AUTH_SUFFIXES = ("x.ai", "grok.com")
_CODE_RE = re.compile(r"\b(?:code|user[ _-]?code)\s*[:=]\s*([A-Z0-9-]{4,64})\b", re.I)
_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
_FALLBACK_PATH = default_cli_fallback_path()


def _executable() -> Optional[str]:
    return resolve_cli("grok", "AGENTCAT_GROK_CLI")


def _environment(profile_dir: Path) -> Dict[str, str]:
    env = dict(os.environ)
    # Native CLI wrappers may use ``/usr/bin/env``.  Daemon launches with an
    # empty or system-only PATH must still start an already trusted CLI.
    # Windows uses ``os.pathsep`` (``;``).
    env["PATH"] = augment_search_path(env.get("PATH"), _FALLBACK_PATH)
    for name in ("GROK_HOME", "XAI_HOME"):
        env.pop(name, None)
    env["GROK_HOME"] = str(profile_dir)
    return env


def _credential_fingerprint(path: Path) -> Optional[str]:
    """Private state fingerprint; never sent to the account registry or caller."""
    try:
        data = path.read_bytes()
        stat = path.stat()
    except OSError:
        return None
    return hashlib.sha256(data + str(stat.st_mtime_ns).encode("ascii") + str(stat.st_size).encode("ascii")).hexdigest()


def _epoch(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return result // 1000 if result >= 100_000_000_000 else result


def _credential_candidates(profile_dir: Path) -> list[Dict[str, Any]]:
    path = Path(profile_dir) / "auth.json"
    fingerprint = _credential_fingerprint(path)
    if fingerprint is None:
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        modified = path.stat().st_mtime_ns
    except (OSError, json.JSONDecodeError):
        return []
    records = list(raw.values()) if isinstance(raw, dict) else []
    if isinstance(raw, dict):
        records.append(raw)
    result = []
    for record in records:
        if not isinstance(record, dict):
            continue
        token = record.get("key") or record.get("access_token") or record.get("accessToken")
        refresh = record.get("refresh_token") or record.get("refreshToken")
        if not isinstance(token, str) or not token.strip():
            token = None
        if not isinstance(refresh, str) or not refresh.strip():
            refresh = None
        if token or refresh:
            stable = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            result.append({"raw": record, "token": token, "refresh": refresh, "expires": _epoch(record.get("expires_at") or record.get("expiresAt") or record.get("expiry")), "modified": modified, "stable": stable, "fingerprint": fingerprint})
    return result


def _select_credential(profile_dir: Path) -> Optional[Dict[str, Any]]:
    now = int(time.time())
    candidates = _credential_candidates(profile_dir)
    def score(item: Dict[str, Any]) -> tuple[int, int, int, str]:
        expiry = item.get("expires")
        current = isinstance(item.get("token"), str) and (expiry is None or expiry > now)
        renewable = isinstance(item.get("refresh"), str)
        # JSON insertion order is not an account selection policy; use stable claim text as tie-breaker.
        return (2 if current else 1 if renewable else 0, expiry if isinstance(expiry, int) else -1, int(item["modified"]), str(item["stable"]))
    return max(candidates, key=score) if candidates and score(max(candidates, key=score))[0] else None


def _native_identity(raw: Dict[str, Any], token: str) -> Dict[str, str]:
    claims: Dict[str, Any] = dict(raw)
    if token.count(".") >= 2:
        try:
            part = token.split(".", 2)[1]
            decoded = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            payload = json.loads(decoded.decode("utf-8"))
            if isinstance(payload, dict):
                claims.update(payload)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    for key in ("account_id", "accountId", "user_id", "userId", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and 1 <= len(value.strip()) <= 256:
            return {"accountID": value.strip()}
    return {"status": "unavailable"}


def _verified_email_identity(raw: Dict[str, Any], token: str) -> Dict[str, Any]:
    """Fail closed: Grok exposes no verified email source in its installed CLI."""
    del raw, token
    return {"identityStatus": {"status": "unavailable", "reason": "native_email_not_exposed"}}


def _subscription_tier(config: Dict[str, Any]) -> Optional[str]:
    """Allowlist the provider-reported billing tier for display only."""
    value = config.get("subscriptionTier")
    if not isinstance(value, str):
        return None
    tier = value.strip()
    if not 1 <= len(tier) <= 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+/-]*", tier):
        return None
    return tier


def _live_usage(token: str) -> Dict[str, Any]:
    request = Request(_BILLING_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8"))
    config = payload.get("config") if isinstance(payload, dict) and isinstance(payload.get("config"), dict) else None
    percent = config.get("creditUsagePercent") if config else None
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        raise ValueError("usage_shape_unknown")
    period_raw = config.get("currentPeriod")
    period = str(period_raw.get("type") if isinstance(period_raw, dict) else period_raw or "weekly").lower()
    window_id = "grok:5h" if "hour" in period or "5h" in period or "short" in period else "grok:7d"
    products = config.get("productUsage")
    credits = {"products": products} if isinstance(products, (dict, list)) else None
    tier = _subscription_tier(config)
    return {"source": "grok-live", "freshness": "live", "windows": [{"id": window_id, "usedPercent": max(0.0, min(100.0, float(percent))), "remainingPercent": max(0.0, min(100.0, 100.0 - float(percent)))}], "credits": credits, "spendControl": None, "rateLimitReachedType": None, "tokenUsage": None, "tokenUsageAvailable": False, **({"subscriptionTier": tier} if tier else {})}


def _verified_credential(profile_dir: Path, before: Optional[set[str]] = None) -> Optional[Dict[str, Any]]:
    selected = _select_credential(profile_dir)
    if selected is None or (before is not None and selected["fingerprint"] in before):
        return None
    token = selected.get("token")
    if not isinstance(token, str) or not token:
        return None
    return {**_verified_email_identity(selected["raw"], token), "usage": _live_usage(token)}


def _safe_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _AUTH_SUFFIXES):
        return None
    return value


def _surface(line: str) -> Dict[str, str]:
    try:
        value: Any = json.loads(line)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        url = _safe_url(value.get("verification_uri_complete") or value.get("verification_uri") or value.get("verificationUrl"))
        code = value.get("user_code") or value.get("userCode")
        return {**({"verificationURL": url} if url else {}), **({"userCode": code} if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9-]{4,64}", code) else {})}
    url_match = re.search(r"https://[^\s\"']+", line)
    code_match = _CODE_RE.search(line)
    url = _safe_url(url_match.group(0).rstrip(".,)")) if url_match else None
    code = code_match.group(1) if code_match else None
    return {**({"verificationURL": url} if url else {}), **({"userCode": code} if code else {})}


def _read_output(operation_id: str, stream: Any) -> None:
    size = 0
    try:
        for line in iter(stream.readline, ""):
            size += len(line.encode("utf-8", errors="ignore"))
            if size > _OUTPUT_LIMIT:
                break
            surface = _surface(line)
            if surface:
                with _LOCK:
                    operation = _OPERATIONS.get(operation_id)
                    if operation is not None:
                        operation["surface"].update(surface)
                        operation["surface_ready"].set()
    finally:
        try:
            stream.close()
        except OSError:
            pass


def adapter_capability() -> Dict[str, Any]:
    global _PROBE
    now = time.monotonic()
    if _PROBE is not None and now - _PROBE[0] < _PROBE_TTL:
        reason, browser = _PROBE[1], _PROBE[2]
    else:
        executable = _executable()
        if executable is None:
            reason, browser = "grok_cli_not_installed", False
        else:
            try:
                result = subprocess.run([executable, "login", "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_environment(Path.cwd() / ".agentcat-grok-probe"), timeout=3, check=False)
                reason = None if result.returncode == 0 else "grok_cli_unsupported"
                help_text = result.stdout if isinstance(result.stdout, str) else ""
                browser = reason is None and "--oauth" in help_text
            except (OSError, subprocess.TimeoutExpired):
                reason, browser = "grok_cli_unsupported", False
        _PROBE = (now, reason, browser)
    modes = (["browser"] if browser else []) + (["device"] if reason is None else [])
    return {"supported": True, "available": reason is None, "reason": reason, "modes": modes}


def start(profile_dir: Path, mode: str) -> Dict[str, Any]:
    if mode not in {"browser", "device"}:
        return {"status": "failed", "error": "grok_login_mode_unsupported"}
    capability = adapter_capability()
    if not capability["available"]:
        return {"status": "failed", "error": capability["reason"]}
    if mode not in capability["modes"]:
        return {"status": "failed", "error": "grok_browser_not_supported"}
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    restrict_private(profile_dir, directory=True)
    operation_id = uuid.uuid4().hex
    before = {item["fingerprint"] for item in _credential_candidates(profile_dir)}
    try:
        command = [str(_executable()), "login", "--oauth"] if mode == "browser" else [str(_executable()), "login", "--device-auth"]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=_environment(profile_dir))
    except OSError:
        return {"status": "failed", "error": "grok_login_start_failed"}
    with _LOCK:
        _OPERATIONS[operation_id] = {"process": process, "surface": {}, "surface_ready": threading.Event(), "profile": profile_dir, "mode": mode, "before": before}
    assert process.stdout is not None
    threading.Thread(target=_read_output, args=(operation_id, process.stdout), daemon=True).start()
    operation = _OPERATIONS[operation_id]
    operation["surface_ready"].wait(timeout=5)
    surface = dict(operation["surface"])
    if mode == "browser" and "verificationURL" in surface:
        surface["authorizationURL"] = surface.pop("verificationURL")
    if not surface:
        cancel(profile_dir, operation_id)
        return {"status": "failed", "error": "grok_login_surface_unavailable"}
    return {"operationID": operation_id, "status": "pending_browser" if mode == "browser" else "pending_device", **surface}


def poll(profile_dir: Path, operation_id: str) -> Dict[str, Any]:
    del profile_dir
    with _LOCK:
        operation = _OPERATIONS.get(operation_id)
        if operation is None:
            return {"status": "failed", "error": "oauth_operation_not_found"}
        process = operation["process"]
        surface = dict(operation["surface"])
        mode = operation["mode"]
    code = process.poll()
    if code is None:
        if mode == "browser" and "verificationURL" in surface:
            surface["authorizationURL"] = surface.pop("verificationURL")
        return {"status": "pending_browser" if mode == "browser" else "pending_device", **surface}
    with _LOCK:
        _OPERATIONS.pop(operation_id, None)
    if code != 0:
        return {"status": "failed", "error": "grok_login_failed"}
    try:
        verified = _verified_credential(operation["profile"], operation["before"])
    except Exception:
        verified = None
    if verified is None:
        return {"status": "failed", "error": "grok_auth_state_not_updated"}
    return {"status": "connected", "authenticated": True, **verified}


def cancel(profile_dir: Path, operation_id: str) -> str:
    del profile_dir
    with _LOCK:
        operation = _OPERATIONS.pop(operation_id, None)
    if operation is None:
        return "notFound"
    process = operation["process"]
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    return "canceled"


def refresh(profile_dir: Path) -> Dict[str, Any]:
    try:
        verified = _verified_credential(profile_dir)
        if verified is None:
            raise ValueError("token_missing")
        return {"status": "connected", "authenticated": True, **verified}
    except Exception:
        return {"status": "connected", "authenticated": False, "identityStatus": {"status": "unavailable", "reason": "sign_in_required"}, "usage": {"source": "grok-live", "freshness": "unavailable", "windows": [], "credits": None, "spendControl": None, "rateLimitReachedType": None, "tokenUsage": None, "tokenUsageAvailable": False}}


def remove(profile_dir: Path) -> None:
    profile = Path(profile_dir)
    with _LOCK:
        identifiers = [key for key, value in _OPERATIONS.items() if value.get("profile") == profile]
    for operation_id in identifiers:
        cancel(profile, operation_id)


def build_adapter() -> Any:
    """Return the stateless module adapter without probing at import time."""
    import sys
    return sys.modules[__name__]
    return None
