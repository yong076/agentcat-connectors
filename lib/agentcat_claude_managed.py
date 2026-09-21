"""Managed Claude Code subscription login in an isolated Claude profile."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from agentcat_managed_platform import augment_search_path, default_cli_fallback_path, resolve_cli, restrict_private

_LOCK = threading.RLock()
_OPERATIONS: Dict[str, Dict[str, Any]] = {}
_PROBE: Optional[tuple[float, Optional[str]]] = None
_PROBE_TTL = 60.0
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_ID_RE = re.compile(r"^[^\s\x00-\x1f]{1,255}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_AUTH_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_HOST_CREDS_FILE", "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
)
_FALLBACK_PATH = default_cli_fallback_path()

def _executable() -> Optional[str]:
    return resolve_cli("claude", "AGENTCAT_CLAUDE_CLI")

def _environment(profile_dir: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = augment_search_path(env.get("PATH"), _FALLBACK_PATH)
    for key in _AUTH_ENV:
        env.pop(key, None)
    # Official Claude Code storage resolves both values into a scoped Keychain
    # service. HOME remains untouched.
    env["CLAUDE_CONFIG_DIR"] = str(profile_dir)
    env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(profile_dir)
    return env

def _prepare_profile(profile_dir: Path) -> Path:
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    restrict_private(profile, directory=True)
    return profile

def _keychain_service(profile_dir: Path) -> str:
    value = str(Path(profile_dir).expanduser().resolve()).encode("utf-8")
    return "Claude Code-credentials-" + hashlib.sha256(value).hexdigest()[:8]

def _profile_oauth_record(profile_dir: Path) -> Optional[tuple[Dict[str, Any], Dict[str, Any], tuple[str, Any]]]:
    """Read only the controlled profile's scoped Keychain/files; never defaults."""
    profile = Path(profile_dir)
    records: list[tuple[Dict[str, Any], tuple[str, Any]]] = []
    if sys.platform == "darwin":
        try:
            raw = subprocess.check_output(
                ["security", "find-generic-password", "-s", _keychain_service(profile), "-w"],
                stderr=subprocess.DEVNULL, text=True, timeout=5,
            )
            value = json.loads(raw)
            if isinstance(value, dict):
                account = "agentcat-managed"
                try:
                    metadata = subprocess.run(
                        ["security", "find-generic-password", "-s", _keychain_service(profile)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, timeout=5, check=False,
                    )
                    match = re.search(r'"acct"<blob>="([^"]+)"', metadata.stdout + metadata.stderr)
                    if match:
                        account = match.group(1)
                except Exception:
                    pass
                records.append((value, ("keychain", account)))
        except Exception:
            pass
    for path in (profile / ".credentials.json", profile / "credentials.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append((value, ("file", path.name)))
    for value, storage in records:
        oauth = value.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            oauth = value.get("claude_ai_oauth")
        if not isinstance(oauth, dict):
            oauth = value
        token = oauth.get("accessToken") or oauth.get("access_token")
        if isinstance(token, str) and token:
            return value, dict(oauth), storage
    return None

def _profile_oauth(profile_dir: Path) -> Optional[Dict[str, Any]]:
    record = _profile_oauth_record(profile_dir)
    return record[1] if record is not None else None

def _expires_soon(oauth: Dict[str, Any]) -> bool:
    value = oauth.get("expiresAt") or oauth.get("expires_at") or oauth.get("expiry")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Native Claude records use milliseconds.  Support seconds defensively.
        expires = float(value) / (1000.0 if value > 10_000_000_000 else 1.0)
    elif isinstance(value, str):
        try:
            expires = time.mktime(time.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z"))
        except ValueError:
            return False
    else:
        return False
    return expires <= time.time() + 90

def _persist_profile_oauth(profile_dir: Path, outer: Dict[str, Any], oauth: Dict[str, Any], storage: tuple[str, Any]) -> None:
    updated = dict(outer)
    key = "claudeAiOauth" if isinstance(updated.get("claudeAiOauth"), dict) or "claude_ai_oauth" not in updated else "claude_ai_oauth"
    updated[key] = oauth
    kind, target = storage
    encoded = json.dumps(updated, separators=(",", ":"))
    if kind == "file":
        path = Path(profile_dir) / str(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".agentcat-refresh-" + uuid.uuid4().hex)
        try:
            temporary.write_text(encoded, encoding="utf-8")
            if not restrict_private(temporary):
                raise OSError("private_mode_failed")
            temporary.replace(path)
            if not restrict_private(path):
                raise OSError("private_mode_failed")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return
    # The service name is a SHA-256 derivation of the managed profile path.
    # Updating its existing account is a Keychain transaction scoped to this
    # connection; no global Claude service is queried or changed.
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", _keychain_service(profile_dir),
         "-a", str(target), "-w", encoded],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=8, check=False,
    )
    if result.returncode != 0:
        raise OSError("managed_keychain_write_failed")

def _remove_profile_oauth(profile_dir: Path, storage: tuple[str, Any]) -> None:
    kind, target = storage
    if kind == "file":
        (Path(profile_dir) / str(target)).unlink(missing_ok=True)
        return
    result = subprocess.run(
        ["security", "delete-generic-password", "-s", _keychain_service(profile_dir), "-a", str(target)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=8, check=False,
    )
    if result.returncode != 0:
        raise OSError("managed_keychain_delete_failed")

def _same_provider_identity(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    return expected == actual and all(isinstance(expected.get(key), str) and expected[key] for key in ("accountID", "tenantID"))

def promote_verified_profile(source: Path, destination: Path, identity: Dict[str, Any]) -> bool:
    """Atomically promote a proven Claude session into the canonical profile.

    Source and destination are both Agent Cat-managed profile roots.  Claude's
    one-shot login process has exited before this hook runs, so no native
    process retains either profile's credential cache.  The destination is
    backed up, written through the profile-derived Keychain service or its
    profile file, then revalidated through the official OAuth profile API.
    The source is deliberately retained for recovery.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or not isinstance(identity, dict):
        return False
    source_record = _profile_oauth_record(source)
    if source_record is None:
        return False
    source_outer, source_oauth, source_storage = source_record
    source_token = source_oauth.get("accessToken") or source_oauth.get("access_token")
    if not isinstance(source_token, str) or not source_token:
        return False
    source_proof = _profile_identity(source_token).get("providerIdentity")
    if not isinstance(source_proof, dict) or not _same_provider_identity(identity, source_proof):
        return False
    previous = _profile_oauth_record(destination)
    destination_storage: tuple[str, Any]
    if previous is not None:
        destination_storage = previous[2]
    else:
        # Native Claude reads the service derived from this exact managed
        # profile.  The account name does not enter identity matching.
        destination_storage = (source_storage[0], source_storage[1] if source_storage[0] == "file" else "agentcat-managed")
    replaced = False
    try:
        _prepare_profile(destination)
        _persist_profile_oauth(destination, source_outer, source_oauth, destination_storage)
        replaced = True
        candidate = _profile_oauth(destination)
        token = candidate.get("accessToken") or candidate.get("access_token") if isinstance(candidate, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("destination_credentials_unavailable")
        candidate_proof = _profile_identity(token).get("providerIdentity")
        if not isinstance(candidate_proof, dict) or not _same_provider_identity(identity, candidate_proof):
            raise ValueError("destination_identity_mismatch")
        return True
    except Exception:
        if replaced:
            try:
                if previous is None:
                    _remove_profile_oauth(destination, destination_storage)
                else:
                    _persist_profile_oauth(destination, previous[0], previous[1], previous[2])
            except Exception:
                pass
        return False

def _refresh_managed_oauth(profile_dir: Path) -> Optional[Dict[str, Any]]:
    record = _profile_oauth_record(profile_dir)
    if record is None:
        return None
    outer, oauth, storage = record
    if not _expires_soon(oauth):
        return oauth
    refresh_token = oauth.get("refreshToken") or oauth.get("refresh_token")
    client_id = oauth.get("clientId") or oauth.get("client_id") or _DEFAULT_CLIENT_ID
    scopes = oauth.get("scopes") or oauth.get("scope")
    if not isinstance(refresh_token, str) or not refresh_token or not isinstance(client_id, str) or not _CLIENT_ID_RE.fullmatch(client_id):
        return None
    if isinstance(scopes, list):
        scope_value = " ".join(item for item in scopes if isinstance(item, str) and item)
    else:
        scope_value = scopes if isinstance(scopes, str) else ""
    payload = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if scope_value:
        payload["scopes"] = scope_value
    request = urllib.request.Request(
        _TOKEN_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "claude-code/2.1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        refreshed = json.loads(response.read(1_000_000).decode("utf-8"))
    token = refreshed.get("access_token") if isinstance(refreshed, dict) else None
    expires_in = refreshed.get("expires_in") if isinstance(refreshed, dict) else None
    if not isinstance(token, str) or not token or not isinstance(expires_in, (int, float)):
        raise ValueError("oauth_refresh_invalid")
    updated = dict(oauth)
    updated["accessToken"] = token
    updated["refreshToken"] = refreshed.get("refresh_token") if isinstance(refreshed.get("refresh_token"), str) and refreshed.get("refresh_token") else refresh_token
    updated["expiresAt"] = int((time.time() + float(expires_in)) * 1000)
    updated["clientId"] = client_id
    if scope_value:
        updated["scopes"] = scope_value.split()
    _persist_profile_oauth(profile_dir, outer, updated, storage)
    return updated

def _unavailable_usage(reason: str) -> Dict[str, Any]:
    return {"source": "claude_oauth_usage", "freshness": "unavailable", "windows": [],
            "credits": None, "spendControl": None, "rateLimitReachedType": None,
            "tokenUsage": None, "tokenUsageAvailable": False,
            "scope": "claude_subscription_quota", "reason": reason}

def _percent(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, number)) if number == number else None

def _quota_usage(token: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        _USAGE_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/json",
        "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1"})
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("usage_shape_unknown")
    windows = []
    for name, identifier in (("five_hour", "claude:5h"), ("seven_day", "claude:7d"),
                             ("seven_day_opus", "claude:opus:7d"), ("seven_day_sonnet", "claude:sonnet:7d")):
        item = payload.get(name)
        used = _percent(item.get("utilization")) if isinstance(item, dict) else None
        if used is None:
            continue
        window: Dict[str, Any] = {"id": identifier, "usedPercent": used, "remainingPercent": 100.0 - used}
        if isinstance(item.get("resets_at"), str) and len(item["resets_at"]) <= 128:
            window["resetAt"] = item["resets_at"]
        windows.append(window)
    extra = payload.get("extra_usage")
    credits = None
    if isinstance(extra, dict):
        used, limit = _percent(extra.get("utilization")), extra.get("monthly_limit")
        if used is not None or isinstance(limit, (int, float)):
            credits = {key: value for key, value in {"usedPercent": used, "monthlyLimit": limit}.items() if value is not None}
    return {"source": "claude_oauth_usage", "freshness": "live", "windows": windows,
            "credits": credits, "spendControl": None, "rateLimitReachedType": None,
            "tokenUsage": None, "tokenUsageAvailable": False, "scope": "claude_subscription_quota"}

def _profile_identity(token: str) -> Dict[str, Any]:
    """Return only identity fields proven by Claude Code's OAuth profile API.

    Claude Code 2.1.220 calls ``/api/oauth/profile`` with the managed bearer
    token and validates ``account.uuid``, ``account.email``, and
    ``organization.uuid`` before using the result.  Configuration values and
    decoded token claims are deliberately not considered identity evidence.
    """
    request = urllib.request.Request(
        _PROFILE_URL,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                 "Content-Type": "application/json", "Cache-Control": "no-cache",
                 "User-Agent": "claude-code/2.1"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        payload = json.loads(response.read(1_000_000).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("oauth_profile_invalid")
    account = payload.get("account")
    organization = payload.get("organization")
    if not isinstance(account, dict) or not isinstance(organization, dict):
        raise ValueError("oauth_profile_invalid")
    account_id = account.get("uuid")
    organization_id = organization.get("uuid")
    email = account.get("email")
    if not isinstance(account_id, str) or not _ID_RE.fullmatch(account_id):
        raise ValueError("oauth_profile_invalid")
    if not isinstance(organization_id, str) or not _ID_RE.fullmatch(organization_id):
        raise ValueError("oauth_profile_invalid")
    if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email):
        raise ValueError("oauth_profile_invalid")
    return {
        "identity": {"email": email, "verification": True, "source": "claude_oauth_profile"},
        # This one-shot field never reaches the public registry/API.  The
        # generic registry HMACs both provider account and organization scope.
        "providerIdentity": {"accountID": account_id, "tenantID": organization_id},
    }

def _status(profile_dir: Path) -> Dict[str, Any]:
    executable = _executable()
    if executable is None:
        return {}
    try:
        result = subprocess.run([executable, "auth", "status", "--json"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env=_environment(_prepare_profile(profile_dir)), timeout=10, check=False)
        value = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}

def _verified_auth(profile_dir: Path) -> Dict[str, Any]:
    status = _status(profile_dir)
    if status.get("loggedIn") is not True or status.get("authMethod") != "claude.ai":
        return {"status": "needs_reconnect", "authenticated": False,
                "identityStatus": {"status": "unavailable", "reason": "sign_in_required"},
                "usage": _unavailable_usage("sign_in_required")}
    try:
        oauth = _refresh_managed_oauth(profile_dir)
    except urllib.error.HTTPError as error:
        if error.code in {400, 401, 403}:
            return {"status": "needs_reconnect", "authenticated": False,
                    "identityStatus": {"status": "unavailable", "reason": "sign_in_required"},
                    "usage": _unavailable_usage("sign_in_required")}
        return {"status": "error", "authenticated": False,
                "identityStatus": {"status": "unavailable", "reason": "oauth_refresh_unavailable"},
                "usage": _unavailable_usage("oauth_refresh_unavailable")}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "error", "authenticated": False,
                "identityStatus": {"status": "unavailable", "reason": "oauth_refresh_unavailable"},
                "usage": _unavailable_usage("oauth_refresh_unavailable")}
    token = oauth.get("accessToken") or oauth.get("access_token") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        return {"status": "needs_reconnect", "authenticated": False,
                "identityStatus": {"status": "unavailable", "reason": "managed_profile_credentials_unavailable"},
                "usage": _unavailable_usage("managed_profile_credentials_unavailable")}
    try:
        identity = _profile_identity(token)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            return {"status": "needs_reconnect", "authenticated": False,
                    "identityStatus": {"status": "unavailable", "reason": "sign_in_required"},
                    "usage": _unavailable_usage("sign_in_required")}
        return {"status": "error", "authenticated": False,
                "identityStatus": {"status": "unavailable", "reason": "oauth_profile_unavailable"},
                "usage": _unavailable_usage("oauth_profile_unavailable")}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "error", "authenticated": False,
                "identityStatus": {"status": "unavailable", "reason": "oauth_profile_unavailable"},
                "usage": _unavailable_usage("oauth_profile_unavailable")}
    try:
        usage = _quota_usage(token)
    except Exception:
        # The authenticated profile response has already proved the session.
        # A plan/quota endpoint can be unavailable without invalidating it.
        usage = _unavailable_usage("quota_unavailable")
    return {"status": "connected", "authenticated": True, **identity, "usage": usage}

def adapter_capability() -> Dict[str, Any]:
    global _PROBE
    now = time.monotonic()
    if _PROBE is not None and now - _PROBE[0] < _PROBE_TTL:
        reason = _PROBE[1]
    else:
        executable = _executable()
        if executable is None:
            reason = "claude_cli_not_installed"
        else:
            try:
                result = subprocess.run([executable, "auth", "login", "--help"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=_environment(Path.cwd() / ".agentcat-claude-probe"), timeout=3, check=False)
                reason = None if result.returncode == 0 else "claude_cli_unsupported"
            except (OSError, subprocess.TimeoutExpired):
                reason = "claude_cli_unsupported"
        _PROBE = (now, reason)
    return {"supported": True, "available": reason is None, "reason": reason,
            "modes": ["browser"] if reason is None else [],
            "browserLaunchMode": "provider" if reason is None else None}

def start(profile_dir: Path, mode: str) -> Dict[str, Any]:
    if mode != "browser":
        return {"status": "failed", "error": "claude_browser_only"}
    capability = adapter_capability()
    if not capability["available"]:
        return {"status": "failed", "error": capability["reason"]}
    profile = _prepare_profile(profile_dir)
    operation_id = uuid.uuid4().hex
    try:
        process = subprocess.Popen([str(_executable()), "auth", "login", "--claudeai"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_environment(profile))
    except OSError:
        return {"status": "failed", "error": "claude_login_start_failed"}
    with _LOCK:
        _OPERATIONS[operation_id] = {"process": process, "profile": profile}
    return {"operationID": operation_id, "status": "pending_browser", "browserLaunchMode": "provider"}

def poll(profile_dir: Path, operation_id: str) -> Dict[str, Any]:
    del profile_dir
    with _LOCK:
        operation = _OPERATIONS.get(operation_id)
    if operation is None:
        return {"status": "failed", "error": "oauth_operation_not_found"}
    if operation["process"].poll() is None:
        return {"status": "pending_browser", "browserLaunchMode": "provider"}
    with _LOCK:
        _OPERATIONS.pop(operation_id, None)
    proof = _verified_auth(operation["profile"])
    return proof if proof.get("authenticated") is True else {"status": "failed", "error": "claude_auth_state_not_updated"}

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
    return _verified_auth(Path(profile_dir))

def remove(profile_dir: Path) -> None:
    profile = Path(profile_dir)
    with _LOCK:
        operations = [key for key, value in _OPERATIONS.items() if value.get("profile") == profile]
    for operation in operations:
        cancel(profile, operation)

def build_adapter() -> Any:
    return sys.modules[__name__]
