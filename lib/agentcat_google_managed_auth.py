"""Managed Gemini CLI authentication without touching the owner's CLI profile.

Gemini CLI documents both ``GEMINI_CLI_HOME`` for isolated user state and ACP
(``gemini --acp``) for programmatic authentication.  This adapter combines
those two official surfaces: each connection receives an Agent Cat-owned
profile root and the installed CLI performs the browser OAuth flow itself.

Antigravity deliberately is not represented here.  Its published CLI surface
has interactive authentication, but no documented profile-root override or
login RPC.  Pretending that a process-wide ``HOME`` override is an Antigravity
profile contract would risk mixing accounts.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


GEMINI_CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal"
GEMINI_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_OAUTH_ID_RE = re.compile(r"OAUTH_CLIENT_ID\s*[:=]\s*[\"']([^\"']+)[\"']")
_OAUTH_SECRET_RE = re.compile(r"OAUTH_CLIENT_SECRET\s*[:=]\s*[\"']([^\"']+)[\"']")
_AUTH_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID",
    "GEMINI_OAUTH_CLIENT_ID", "GEMINI_OAUTH_CLIENT_SECRET",
)


class GeminiManagedAuthError(RuntimeError):
    pass


class _AcpSession:
    """Small ACP client used only for Gemini's official authenticate method."""

    def __init__(self, profile_dir: Path, executable: str):
        self.profile_dir = profile_dir
        self.executable = executable
        self.process: Optional[subprocess.Popen[str]] = None
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._response_lock = threading.Condition()
        self._next_id = 0
        self.auth_request_id: Optional[int] = None

    def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.profile_dir.chmod(0o700)
        except OSError:
            pass
        self.process = subprocess.Popen(
            [self.executable, "--acp", "--skip-trust", "--approval-mode", "plan"],
            cwd=self.profile_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=child_environment(self.profile_dir),
        )
        assert self.process.stdout is not None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self.request("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "Agent Cat", "version": "1"},
            "clientCapabilities": {},
        }, timeout=12)

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or not isinstance(message.get("id"), int):
                continue
            with self._response_lock:
                self._responses[message["id"]] = message
                self._response_lock.notify_all()

    def _send(self, method: str, params: Dict[str, Any]) -> int:
        if self.process is None or self.process.poll() is not None or self.process.stdin is None:
            raise GeminiManagedAuthError("Gemini CLI stopped before sign-in completed")
        with self._response_lock:
            self._next_id += 1
            request_id = self._next_id
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        return request_id

    def request(self, method: str, params: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
        request_id = self._send(method, params)
        deadline = time.monotonic() + timeout
        with self._response_lock:
            while request_id not in self._responses and time.monotonic() < deadline:
                self._response_lock.wait(max(0.01, deadline - time.monotonic()))
            response = self._responses.pop(request_id, None)
        if not isinstance(response, dict):
            raise GeminiManagedAuthError("Gemini CLI did not acknowledge the managed sign-in request")
        if isinstance(response.get("error"), dict):
            raise GeminiManagedAuthError("Gemini CLI rejected the managed sign-in request")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def begin_authentication(self) -> None:
        self.auth_request_id = self._send("authenticate", {"methodId": "oauth-personal"})

    def authentication_result(self) -> Optional[bool]:
        request_id = self.auth_request_id
        if request_id is None:
            return False
        with self._response_lock:
            response = self._responses.pop(request_id, None)
        if response is None:
            return None
        return not isinstance(response.get("error"), dict)

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


_SESSIONS: Dict[Tuple[str, str], _AcpSession] = {}
_SESSIONS_LOCK = threading.Lock()


def _profile_key(profile_dir: Path, operation_id: str) -> Tuple[str, str]:
    return str(profile_dir.resolve()), operation_id


def _gemini_executable() -> Optional[str]:
    explicit = os.environ.get("AGENTCAT_GEMINI_CLI")
    if explicit and _is_executable_file(Path(explicit)):
        return explicit
    executable = shutil.which("gemini")
    if executable:
        return executable
    for candidate in (
        Path.home() / ".local" / "bin" / "gemini",
        Path("/opt/homebrew/bin/gemini"),
        Path("/usr/local/bin/gemini"),
    ):
        if _is_executable_file(candidate):
            return str(candidate)
    return None


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def child_environment(profile_dir: Path) -> Dict[str, str]:
    """A child-only environment that cannot inherit another Google identity.

    Gemini CLI documents ``GEMINI_CLI_HOME`` as the root for its user-level
    configuration and storage.  Do not replace ``HOME``: it is not Gemini's
    profile contract and could alter unrelated child-process behavior.
    """
    env = dict(os.environ)
    for name in _AUTH_ENV:
        env.pop(name, None)
    env["GEMINI_CLI_HOME"] = str(profile_dir)
    env["GEMINI_CLI_NO_RELAUNCH"] = "1"
    return env


def adapter_capability() -> Dict[str, Any]:
    executable = _gemini_executable()
    return {
        "provider": "gemini",
        "supported": True,
        "available": executable is not None,
        "reason": None if executable else "gemini_cli_not_installed",
        "modes": ["browser"] if executable else [],
        # ACP launches Google's approved browser flow itself.  It does not
        # expose an authorization URL which Agent Cat may safely recreate.
        "browserLaunchMode": "provider" if executable else None,
    }


def start(profile_dir: Path, mode: str) -> Dict[str, Any]:
    if mode != "browser":
        raise ValueError("gemini_managed_auth_browser_only")
    executable = _gemini_executable()
    if executable is None:
        raise GeminiManagedAuthError("gemini_cli_not_installed")
    operation_id = secrets.token_hex(16)
    session = _AcpSession(Path(profile_dir), executable)
    try:
        session.start()
        session.begin_authentication()
    except Exception:
        session.close()
        raise
    with _SESSIONS_LOCK:
        _SESSIONS[_profile_key(Path(profile_dir), operation_id)] = session
    return {
        "operationID": operation_id,
        "status": "pending_browser",
        "browserLaunchMode": "provider",
    }


def poll(profile_dir: Path, operation_id: str) -> Dict[str, Any]:
    key = _profile_key(Path(profile_dir), operation_id)
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
    if session is None:
        return {"status": "canceled"}
    result = session.authentication_result()
    if result is None:
        if session.process is not None and session.process.poll() is None:
            return {"status": "pending_browser"}
        with _SESSIONS_LOCK:
            _SESSIONS.pop(key, None)
        session.close()
        return {"status": "failed", "error": "Gemini sign-in ended before it completed."}
    with _SESSIONS_LOCK:
        _SESSIONS.pop(key, None)
    session.close()
    if not result:
        return {"status": "failed", "error": "Gemini did not accept this sign-in."}
    managed_profile = Path(profile_dir)
    if not _has_managed_authentication(managed_profile):
        return {"status": "failed", "error": "Gemini sign-in did not create managed credentials."}
    refreshed = refresh(managed_profile)
    usage = refreshed.get("usage") if isinstance(refreshed, dict) else None
    return {
        "status": "connected",
        "authenticated": True,
        **({"usage": usage} if isinstance(usage, dict) else {}),
    }


def cancel(profile_dir: Path, operation_id: str) -> str:
    key = _profile_key(Path(profile_dir), operation_id)
    with _SESSIONS_LOCK:
        session = _SESSIONS.pop(key, None)
    if session is None:
        return "notFound"
    session.close()
    return "canceled"


def _managed_credentials(profile_dir: Path) -> Optional[Dict[str, Any]]:
    path = profile_dir / ".gemini" / "oauth_creds.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _has_managed_authentication(profile_dir: Path) -> bool:
    """Confirm that ACP persisted usable auth in this profile only."""
    creds = _managed_credentials(profile_dir)
    if not creds:
        return False
    return any(
        isinstance(creds.get(field), str) and bool(creds[field])
        for field in ("access_token", "refresh_token")
    )


def _oauth_client_credentials(creds: Dict[str, Any]) -> Tuple[str, str]:
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    if isinstance(client_id, str) and client_id and isinstance(client_secret, str) and client_secret:
        return client_id, client_secret
    executable = _gemini_executable()
    if executable:
        resolved = Path(executable).resolve()
        candidates = (
            resolved.parent.parent / "libexec" / "lib" / "node_modules" / "@google" / "gemini-cli" / "bundle",
            resolved.parent.parent / "lib" / "node_modules" / "@google" / "gemini-cli" / "bundle",
        )
        for bundle_dir in candidates:
            if not bundle_dir.is_dir():
                continue
            for source in sorted(bundle_dir.glob("*.js")):
                try:
                    contents = source.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                found_id = _OAUTH_ID_RE.search(contents)
                found_secret = _OAUTH_SECRET_RE.search(contents)
                if found_id and found_secret:
                    return found_id.group(1), found_secret.group(1)
    raise GeminiManagedAuthError("Gemini CLI OAuth metadata is unavailable")


def _access_token(creds: Dict[str, Any]) -> str:
    expiry = creds.get("expiry_date")
    expired = not isinstance(expiry, (int, float)) or time.time() * 1000 >= float(expiry) - 60_000
    token = creds.get("access_token")
    if not expired and isinstance(token, str) and token:
        return token
    refresh_token = creds.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GeminiManagedAuthError("Gemini sign-in needs to be renewed")
    client_id, client_secret = _oauth_client_credentials(creds)
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": client_secret,
    }).encode("utf-8")
    request = urllib.request.Request(GEMINI_OAUTH_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    updated = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(updated, str) or not updated:
        raise GeminiManagedAuthError("Gemini sign-in needs to be renewed")
    return updated


def _code_assist_post(method: str, payload: Dict[str, Any], access_token: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"{GEMINI_CODE_ASSIST_URL}:{method}", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "AgentCat/1.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.loads(response.read().decode("utf-8"))
    return value if isinstance(value, dict) else {}


def _usage_from_profile(profile_dir: Path) -> Dict[str, Any]:
    creds = _managed_credentials(profile_dir)
    if not creds:
        return {"status": "unavailable", "reason": "sign_in_required", "scope": "gemini_code_assist_request_quota", "quotas": []}
    token = _access_token(creds)
    metadata = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
    try:
        tier = _code_assist_post("loadCodeAssist", {"metadata": metadata}, token)
    except Exception:
        tier = {}
    project = tier.get("cloudaicompanionProject") if isinstance(tier.get("cloudaicompanionProject"), str) else ""
    quota = _code_assist_post("retrieveUserQuota", {"project": project} if project else {}, token)
    buckets = quota.get("buckets") if isinstance(quota.get("buckets"), list) else []
    normalized = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        fraction = bucket.get("remainingFraction")
        remaining_percent = max(0.0, min(100.0, float(fraction) * 100.0)) if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) else None
        normalized.append({
            "id": "gemini:" + str(bucket.get("modelId") or len(normalized)),
            "model": bucket.get("modelId") if isinstance(bucket.get("modelId"), str) else None,
            "remainingPercent": remaining_percent,
            "usedPercent": (100.0 - remaining_percent) if remaining_percent is not None else None,
            "resetAt": bucket.get("resetTime"),
            "unit": "requests" if str(bucket.get("tokenType") or "REQUESTS").lower() == "requests" else str(bucket.get("tokenType")).lower(),
        })
    paid = tier.get("paidTier") if isinstance(tier.get("paidTier"), dict) else {}
    current = tier.get("currentTier") if isinstance(tier.get("currentTier"), dict) else {}
    plan = paid.get("name") or current.get("name") or paid.get("id") or current.get("id")
    return {
        "status": "available" if normalized else "unavailable",
        "reason": None if normalized else "quota_not_exposed",
        "source": "gemini_code_assist",
        "scope": "gemini_code_assist_request_quota",
        "planType": str(plan) if plan else None,
        "quotas": normalized,
    }


def refresh(profile_dir: Path) -> Dict[str, Any]:
    profile_dir = Path(profile_dir)
    if not _has_managed_authentication(profile_dir):
        return {
            "status": "needs_reconnect",
            "usage": {
                "status": "unavailable",
                "reason": "sign_in_required",
                "scope": "gemini_code_assist_request_quota",
                "quotas": [],
            },
        }
    try:
        usage = _usage_from_profile(profile_dir)
    except GeminiManagedAuthError:
        return {"status": "needs_reconnect", "usage": {"status": "unavailable", "reason": "sign_in_required", "scope": "gemini_code_assist_request_quota", "quotas": []}}
    except Exception:
        return {"status": "error", "usage": {"status": "unavailable", "reason": "usage_unavailable", "scope": "gemini_code_assist_request_quota", "quotas": []}}
    return {"status": "connected", "authenticated": True, "usage": usage}


def remove(profile_dir: Path) -> None:
    """Stop live auth and remove only known managed Gemini credential files."""
    resolved = Path(profile_dir).resolve()
    with _SESSIONS_LOCK:
        sessions = [key for key in _SESSIONS if key[0] == str(resolved)]
        live = [_SESSIONS.pop(key) for key in sessions]
    for session in live:
        session.close()
    for filename in ("oauth_creds.json", "google_accounts.json"):
        path = resolved / ".gemini" / filename
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def build_adapter() -> Any:
    """Return the lazy adapter surface expected by the managed registry."""
    return sys.modules[__name__]
