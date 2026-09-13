"""Credential-blind, isolated device login adapter for Kimi Code.

The Kimi CLI owns OAuth and writes credentials only below the supplied
``KIMI_CODE_HOME``.  This module neither reads that directory nor returns CLI
output; it keeps only a short-lived, allowlisted device-login surface in memory.
"""

from __future__ import annotations

import json
import base64
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen


_LOCK = threading.RLock()
_OPERATIONS: Dict[str, Dict[str, Any]] = {}
_PROBE: Optional[tuple[float, Optional[str]]] = None
_PROBE_TTL = 60.0
_OUTPUT_LIMIT = 8192
_AUTH_HOSTS = {"kimi.com", "www.kimi.com", "auth.kimi.com"}
_CODE_RE = re.compile(r"\b(?:code|user[ _-]?code)\s*[:=]\s*([A-Z0-9-]{4,64})\b", re.I)
_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_BINDING_NAME = ".agentcat-kimi-binding.json"


def _executable() -> Optional[str]:
    configured = os.environ.get("AGENTCAT_KIMI_CLI")
    candidates = [configured] if configured else []
    discovered = shutil.which("kimi")
    if discovered:
        candidates.append(discovered)
    home = os.environ.get("HOME")
    if home:
        candidates.append(str(Path(home) / ".local/bin/kimi"))
    candidates.extend(["/opt/homebrew/bin/kimi", "/usr/local/bin/kimi"])
    for candidate in candidates:
        if isinstance(candidate, str) and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _environment(profile_dir: Path) -> Dict[str, str]:
    env = dict(os.environ)
    for name in ("KIMI_CODE_HOME", "KIMI_HOME", "KIMI_CODE_DIR", "KIMI_DATA_DIR"):
        env.pop(name, None)
    env["KIMI_CODE_HOME"] = str(profile_dir)
    return env


def _epoch(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return result // 1000 if result >= 100_000_000_000 else result


def _credential_fingerprint(path: Path) -> Optional[str]:
    """Private state fingerprint; it is never returned or persisted."""
    try:
        data = path.read_bytes()
        stat = path.stat()
    except OSError:
        return None
    return hashlib.sha256(data + str(stat.st_mtime_ns).encode("ascii") + str(stat.st_size).encode("ascii")).hexdigest()


def _credential_candidates(profile_dir: Path) -> list[Dict[str, Any]]:
    directory = Path(profile_dir) / "credentials"
    result: list[Dict[str, Any]] = []
    try:
        paths = list(directory.glob("kimi-code*.json"))
    except OSError:
        paths = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            modified = path.stat().st_mtime_ns
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        access = raw.get("access_token") or raw.get("accessToken")
        refresh = raw.get("refresh_token") or raw.get("refreshToken")
        if not isinstance(access, str) or not access.strip():
            access = None
        if not isinstance(refresh, str) or not refresh.strip():
            refresh = None
        fingerprint = _credential_fingerprint(path)
        if not fingerprint or not (access or refresh):
            continue
        result.append({"raw": raw, "access": access, "refresh": refresh, "expires": _epoch(raw.get("expires_at") or raw.get("expiresAt")), "modified": modified, "name": path.name, "fingerprint": fingerprint})
    return result


def _account_id(candidate: Dict[str, Any]) -> Optional[str]:
    identity = _native_identity(candidate["raw"], candidate.get("access"))
    value = identity.get("accountID")
    return value if isinstance(value, str) else None


def _select_credential(
    profile_dir: Path,
    *,
    changed_since: Optional[set[str]] = None,
    account_id: Optional[str] = None,
    credential_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Choose a usable credential deterministically within the requested binding."""
    now = int(time.time())
    candidates = _credential_candidates(profile_dir)
    if changed_since is not None:
        candidates = [item for item in candidates if item["fingerprint"] not in changed_since]
    if account_id is not None:
        candidates = [item for item in candidates if _account_id(item) == account_id]
    if credential_name is not None:
        candidates = [item for item in candidates if item["name"] == credential_name]
    if not candidates:
        return None
    def score(item: Dict[str, Any]) -> tuple[int, int, int, str]:
        expiry = item.get("expires")
        current = isinstance(item.get("access"), str) and (expiry is None or expiry > now)
        renewable = isinstance(item.get("refresh"), str)
        return (2 if current else 1 if renewable else 0, expiry if isinstance(expiry, int) else -1, int(item["modified"]), str(item["name"]))
    selected = max(candidates, key=score)
    return selected if score(selected)[0] else None


def _binding_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / _BINDING_NAME


def _binding(profile_dir: Path) -> Dict[str, str]:
    """Read the private, non-secret selected-credential binding for this profile."""
    try:
        raw = json.loads(_binding_path(profile_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, str] = {}
    account_id = raw.get("accountID")
    if isinstance(account_id, str) and 1 <= len(account_id.strip()) <= 256:
        result["accountID"] = account_id.strip()
    credential_name = raw.get("credentialName")
    if isinstance(credential_name, str) and credential_name.startswith("kimi-code") and credential_name.endswith(".json") and Path(credential_name).name == credential_name:
        result["credentialName"] = credential_name
    return result


def _bind_credential(profile_dir: Path, identity: Dict[str, Any], credential_name: str) -> None:
    """Persist only a credential basename and provider-issued account ID, never secrets."""
    if not (credential_name.startswith("kimi-code") and credential_name.endswith(".json") and Path(credential_name).name == credential_name):
        return
    binding: Dict[str, Any] = {"version": 1, "credentialName": credential_name}
    account_id = identity.get("accountID")
    if isinstance(account_id, str) and 1 <= len(account_id.strip()) <= 256:
        binding["accountID"] = account_id.strip()
    path = _binding_path(profile_dir)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(binding) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass


def _native_identity(raw: Dict[str, Any], token: Optional[str]) -> Dict[str, str]:
    """Expose only a provider-issued subject/account claim, never a token derivative."""
    claims: Dict[str, Any] = dict(raw)
    if isinstance(token, str) and token.count(".") >= 2:
        try:
            segment = token.split(".", 2)[1]
            decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
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


def _live_usage(token: str) -> Dict[str, Any]:
    request = Request(_USAGE_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "kimi-code/1.0"})
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    rows = data.get("limits") if isinstance(data, dict) and isinstance(data.get("limits"), list) else []
    windows = []
    for item in rows:
        detail = item.get("detail") if isinstance(item, dict) and isinstance(item.get("detail"), dict) else item
        if not isinstance(detail, dict):
            continue
        used, limit = detail.get("used"), detail.get("limit")
        if not isinstance(used, (int, float)) or isinstance(used, bool) or not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
            continue
        percent = max(0.0, min(100.0, float(used) * 100.0 / float(limit)))
        windows.append({"id": "kimi:" + str(len(windows)), "usedPercent": percent, "remainingPercent": 100.0 - percent})
    if not windows:
        raise ValueError("usage_shape_unknown")
    return {"source": "kimi-live", "freshness": "live", "windows": windows, "credits": None, "spendControl": None, "rateLimitReachedType": None, "tokenUsage": None, "tokenUsageAvailable": False}


def _verified_credential(
    profile_dir: Path,
    before: Optional[set[str]] = None,
    account_id: Optional[str] = None,
    credential_name: Optional[str] = None,
    bind: bool = False,
) -> Optional[Dict[str, Any]]:
    selected = _select_credential(profile_dir, changed_since=before, account_id=account_id, credential_name=credential_name)
    if selected is None:
        return None
    token = selected.get("access")
    if not isinstance(token, str) or not token:
        refresh = selected.get("refresh")
        if not isinstance(refresh, str) or not refresh:
            return None
        body = urllib.parse.urlencode({"client_id": _CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh}).encode("utf-8")
        with urlopen(Request(_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}), timeout=12) as response:
            refreshed = json.loads(response.read(1_000_000).decode("utf-8"))
        token = refreshed.get("access_token") if isinstance(refreshed, dict) else None
    if not isinstance(token, str) or not token:
        return None
    # This provider request proves the selected credential is accepted before its claim is used.
    identity = _native_identity(selected["raw"], token)
    result = {"identity": identity, "usage": _live_usage(token)}
    if bind:
        _bind_credential(profile_dir, identity, selected["name"])
    return result


def _safe_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _AUTH_HOSTS:
        return None
    return value


def _surface(line: str) -> Dict[str, str]:
    """Extract only display-safe device fields; never retain the original line."""
    value: Any = None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        pass
    if isinstance(value, dict):
        url = _safe_url(value.get("verification_uri_complete") or value.get("verification_uri"))
        code = value.get("user_code")
        return {
            **({"verificationURL": url} if url else {}),
            **({"userCode": code} if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9-]{4,64}", code) else {}),
        }
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
        reason = _PROBE[1]
    else:
        executable = _executable()
        if executable is None:
            reason = "kimi_cli_not_installed"
        else:
            try:
                result = subprocess.run([executable, "login", "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_environment(Path.cwd() / ".agentcat-kimi-probe"), timeout=3, check=False)
                reason = None if result.returncode == 0 else "kimi_cli_unsupported"
            except (OSError, subprocess.TimeoutExpired):
                reason = "kimi_cli_unsupported"
        _PROBE = (now, reason)
    return {"supported": True, "available": reason is None, "reason": reason, "modes": ["device"] if reason is None else []}


def start(profile_dir: Path, mode: str) -> Dict[str, Any]:
    if mode != "device":
        return {"status": "failed", "error": "kimi_browser_not_supported"}
    capability = adapter_capability()
    if not capability["available"]:
        return {"status": "failed", "error": capability["reason"]}
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_dir.chmod(0o700)
    except OSError:
        pass
    operation_id = uuid.uuid4().hex
    # Snapshot before spawning: a zero exit alone never proves this login changed auth.
    before = {item["fingerprint"] for item in _credential_candidates(profile_dir)}
    try:
        process = subprocess.Popen([str(_executable()), "login"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=_environment(profile_dir))
    except OSError:
        return {"status": "failed", "error": "kimi_login_start_failed"}
    operation = {"process": process, "surface": {}, "surface_ready": threading.Event(), "profile": profile_dir, "before": before}
    with _LOCK:
        _OPERATIONS[operation_id] = operation
    assert process.stdout is not None
    threading.Thread(target=_read_output, args=(operation_id, process.stdout), daemon=True).start()
    operation["surface_ready"].wait(timeout=5)
    surface = dict(operation["surface"])
    if not surface:
        cancel(profile_dir, operation_id)
        return {"status": "failed", "error": "kimi_login_surface_unavailable"}
    return {"operationID": operation_id, "status": "pending_device", **surface}


def poll(profile_dir: Path, operation_id: str) -> Dict[str, Any]:
    del profile_dir
    with _LOCK:
        operation = _OPERATIONS.get(operation_id)
        if operation is None:
            return {"status": "failed", "error": "oauth_operation_not_found"}
        process = operation["process"]
        surface = dict(operation["surface"])
    code = process.poll()
    if code is None:
        return {"status": "pending_device", **surface}
    with _LOCK:
        _OPERATIONS.pop(operation_id, None)
    if code != 0:
        return {"status": "failed", "error": "kimi_login_failed"}
    try:
        verified = _verified_credential(operation["profile"], operation["before"], bind=True)
    except Exception:
        verified = None
    if verified is None:
        return {"status": "failed", "error": "kimi_auth_state_not_updated"}
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
    """Fetch normalized live usage using credentials only in this profile."""
    try:
        binding = _binding(profile_dir)
        verified = _verified_credential(
            profile_dir,
            account_id=binding.get("accountID"),
            credential_name=binding.get("credentialName"),
        )
        if verified is None:
            raise ValueError("token_missing")
        return {"status": "connected", "authenticated": True, **verified}
    except Exception:
        return {"status": "connected", "authenticated": False, "identity": {"status": "unavailable"}, "usage": {"source": "kimi-live", "freshness": "unavailable", "windows": [], "credits": None, "spendControl": None, "rateLimitReachedType": None, "tokenUsage": None, "tokenUsageAvailable": False}}


def remove(profile_dir: Path) -> None:
    """Stop live login operations for a profile; credential deletion is CLI-owned."""
    profile = Path(profile_dir)
    with _LOCK:
        identifiers = [key for key, value in _OPERATIONS.items() if value.get("profile") == profile]
    for operation_id in identifiers:
        cancel(profile, operation_id)
    try:
        _binding_path(profile).unlink()
    except OSError:
        pass


def build_adapter() -> Any:
    """Return the stateless module adapter without probing at import time."""
    import sys
    return sys.modules[__name__]
    return None
