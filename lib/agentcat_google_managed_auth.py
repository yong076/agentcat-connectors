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
import urllib.error
import time
import urllib.parse
import urllib.request
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple


GEMINI_CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal"
GEMINI_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
_OAUTH_REFRESH_ERROR_REASONS = {
    "invalid_grant": "token_refresh_rejected",
    "invalid_client": "oauth_client_invalid",
    "unauthorized_client": "oauth_client_unauthorized",
    "invalid_request": "oauth_refresh_request_invalid",
}
_OAUTH_ID_RE = re.compile(r"OAUTH_CLIENT_ID\s*[:=]\s*[\"']([^\"']+)[\"']")
_OAUTH_SECRET_RE = re.compile(r"OAUTH_CLIENT_SECRET\s*[:=]\s*[\"']([^\"']+)[\"']")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_GOOGLE_ACCOUNT_ID_RE = re.compile(r"^[^\s\x00-\x1f]{1,255}$")
_AUTH_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID",
    "GEMINI_OAUTH_CLIENT_ID", "GEMINI_OAUTH_CLIENT_SECRET",
)
_REFRESH_DIAGNOSTIC: ContextVar[Optional[list[Dict[str, Any]]]] = ContextVar(
    "gemini_refresh_diagnostic", default=None
)


class GeminiManagedAuthError(RuntimeError):
    def __init__(self, message: str, reason: str = "sign_in_required"):
        super().__init__(message)
        self.reason = reason


def _record_refresh_diagnostic(stage: str, **details: Any) -> None:
    """Record only allowlisted local diagnostics during an explicit probe."""
    events = _REFRESH_DIAGNOSTIC.get()
    if events is None:
        return
    event: Dict[str, Any] = {"stage": stage}
    for key, value in details.items():
        if key == "httpStatus" and isinstance(value, int):
            event[key] = value
        elif key in {"outcome", "reason"} and isinstance(value, str):
            event[key] = value
    events.append(event)


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
        # Gemini ACP runs the OAuth exchange and then Code Assist setup in one
        # `authenticate` request.  The latter may reject an account that has
        # not completed Code Assist setup *after* the OAuth token has been
        # persisted.  A JSON-RPC error alone therefore is not proof that the
        # managed Google sign-in failed.  Accept it only when a fresh check of
        # this exact profile proves the credentials are usable.
        refreshed = refresh(Path(profile_dir))
        if refreshed.get("status") == "connected" and refreshed.get("authenticated") is True:
            return refreshed
        return {"status": "failed", "error": "Gemini did not accept this sign-in."}
    managed_profile = Path(profile_dir)
    if not _has_managed_authentication(managed_profile):
        return {"status": "failed", "error": "Gemini sign-in did not create managed credentials."}
    refreshed = refresh(managed_profile)
    usage = refreshed.get("usage") if isinstance(refreshed, dict) else None
    identity = refreshed.get("identity") if isinstance(refreshed, dict) else None
    identity_status = refreshed.get("identityStatus") if isinstance(refreshed, dict) else None
    provider_identity = refreshed.get("providerIdentity") if isinstance(refreshed, dict) else None
    return {
        "status": "connected",
        "authenticated": True,
        **({"usage": usage} if isinstance(usage, dict) else {}),
        **({"identity": identity} if isinstance(identity, dict) else {}),
        **({"identityStatus": identity_status} if isinstance(identity_status, dict) else {}),
        **({"providerIdentity": provider_identity} if isinstance(provider_identity, dict) else {}),
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
    path = _managed_credentials_path(profile_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _managed_credentials_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / ".gemini" / "oauth_creds.json"


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
        _record_refresh_diagnostic("oauth_metadata", outcome="credential_file")
        return client_id, client_secret
    executable = _gemini_executable()
    if executable:
        resolved = Path(executable).resolve()
        candidates = []
        # Homebrew's gemini executable resolves directly to
        # ``.../@google/gemini-cli/bundle/gemini.js``.  Prefer that exact
        # installed bundle before considering wrapper-oriented layouts.
        if resolved.parent.name == "bundle":
            candidates.append(resolved.parent)
        candidates.extend((
            resolved.parent / "bundle",
            resolved.parent.parent / "bundle",
            resolved.parent.parent / "libexec" / "lib" / "node_modules" / "@google" / "gemini-cli" / "bundle",
            resolved.parent.parent / "lib" / "node_modules" / "@google" / "gemini-cli" / "bundle",
        ))
        for bundle_dir in dict.fromkeys(candidates):
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
                    _record_refresh_diagnostic("oauth_metadata", outcome="installed_cli")
                    return found_id.group(1), found_secret.group(1)
    _record_refresh_diagnostic("oauth_metadata", outcome="unavailable", reason="oauth_metadata_unavailable")
    raise GeminiManagedAuthError("Gemini CLI OAuth metadata is unavailable", "oauth_metadata_unavailable")


def _persist_refreshed_credentials(profile_dir: Path, credentials: Dict[str, Any]) -> None:
    """Mirror Gemini CLI's managed oauth_creds.json update after refresh."""
    path = _managed_credentials_path(profile_dir)
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(8))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise GeminiManagedAuthError("Gemini managed credentials could not be updated") from error


def _oauth_refresh_error_reason(error: urllib.error.HTTPError) -> str:
    """Classify OAuth's standard error enum without retaining its response body."""
    if error.code != 400:
        return "token_refresh_unavailable"
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except Exception:
        return "token_refresh_rejected"
    code = payload.get("error") if isinstance(payload, dict) else None
    return _OAUTH_REFRESH_ERROR_REASONS.get(code, "token_refresh_rejected")


def _access_token(creds: Dict[str, Any], profile_dir: Optional[Path] = None) -> str:
    expiry = creds.get("expiry_date")
    expired = not isinstance(expiry, (int, float)) or time.time() * 1000 >= float(expiry) - 60_000
    token = creds.get("access_token")
    if not expired and isinstance(token, str) and token:
        _record_refresh_diagnostic("access_token", outcome="cached")
        return token
    refresh_token = creds.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        _record_refresh_diagnostic("access_token", outcome="unavailable", reason="sign_in_required")
        raise GeminiManagedAuthError("Gemini sign-in needs to be renewed", "sign_in_required")
    client_id, client_secret = _oauth_client_credentials(creds)
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": client_secret,
    }).encode("utf-8")
    request = urllib.request.Request(GEMINI_OAUTH_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        reason = _oauth_refresh_error_reason(error)
        _record_refresh_diagnostic("token_exchange", outcome="failed", httpStatus=error.code, reason=reason)
        raise GeminiManagedAuthError("Gemini sign-in needs to be renewed", reason) from error
    except OSError as error:
        _record_refresh_diagnostic("token_exchange", outcome="failed", reason="token_refresh_unavailable")
        raise GeminiManagedAuthError("Gemini sign-in needs to be renewed", "token_refresh_unavailable") from error
    if not isinstance(payload, dict):
        _record_refresh_diagnostic("token_exchange", outcome="invalid_response", reason="token_refresh_invalid_response")
        raise GeminiManagedAuthError("Gemini sign-in needs to be renewed", "token_refresh_invalid_response")
    updated = payload.get("access_token")
    if not isinstance(updated, str) or not updated:
        _record_refresh_diagnostic("token_exchange", outcome="invalid_response", reason="token_refresh_invalid_response")
        raise GeminiManagedAuthError("Gemini sign-in needs to be renewed", "token_refresh_invalid_response")
    _record_refresh_diagnostic("token_exchange", outcome="refreshed")
    if profile_dir is not None:
        refreshed = dict(creds)
        refreshed.update(payload)
        # Google refresh responses commonly omit the durable refresh token;
        # the Gemini CLI's OAuth client retains it before writing credentials.
        refreshed["refresh_token"] = payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) and payload.get("refresh_token") else refresh_token
        expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            refreshed["expiry_date"] = int(time.time() * 1000 + float(expires_in) * 1000)
            refreshed.pop("expires_in", None)
        _persist_refreshed_credentials(Path(profile_dir), refreshed)
    return updated


def _identity_unavailable(reason: str) -> Dict[str, Any]:
    return {
        "identityStatus": {"status": "unavailable", "reason": reason},
    }


def _identity_from_managed_profile(profile_dir: Path) -> Dict[str, Any]:
    """Resolve email from Google's authenticated userinfo endpoint only.

    Gemini CLI itself requests this endpoint after OAuth and caches the result.
    We query it with the managed profile's token so a stale local cache cannot
    label a newly authorized account.
    """
    creds = _managed_credentials(profile_dir)
    if not creds:
        _record_refresh_diagnostic("managed_credentials", outcome="missing", reason="sign_in_required")
        return _identity_unavailable("sign_in_required")
    _record_refresh_diagnostic("managed_credentials", outcome="present")
    try:
        token = _access_token(creds, profile_dir)
        request = urllib.request.Request(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except GeminiManagedAuthError as error:
        _record_refresh_diagnostic("userinfo", outcome="unavailable", reason=error.reason)
        return _identity_unavailable(error.reason)
    except urllib.error.HTTPError as error:
        _record_refresh_diagnostic("userinfo", outcome="failed", httpStatus=error.code)
        return _identity_unavailable(
            "sign_in_required" if error.code == 401 else "userinfo_unavailable"
        )
    except Exception:
        _record_refresh_diagnostic("userinfo", outcome="failed")
        return _identity_unavailable("userinfo_unavailable")
    _record_refresh_diagnostic("userinfo", outcome="received")
    email = payload.get("email") if isinstance(payload, dict) else None
    verified = payload.get("verified_email") if isinstance(payload, dict) else None
    if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email):
        return _identity_unavailable("email_not_available")
    if verified is not True:
        return _identity_unavailable("email_not_verified")
    account_id = payload.get("id") if isinstance(payload, dict) else None
    identity = {
        "email": email,
        "verification": True,
        "source": "google_userinfo",
    }
    # OAuth v2 userinfo's `id` is Google's provider-issued account identifier.
    # It is deliberately separate from the display email: Google documents that
    # an email address may change and must not be used as a primary key.
    if isinstance(account_id, str) and _GOOGLE_ACCOUNT_ID_RE.fullmatch(account_id):
        identity["accountID"] = account_id
        return {
            "identity": identity,
            # The managed-account registry consumes this private handoff and
            # HMACs it before persistence; never expose it through the app API.
            "providerIdentity": {"accountID": account_id},
        }
    return {
        "identity": identity,
    }


def _checked_provider_account_id(identity: Mapping[str, Any]) -> str:
    account_id = identity.get("accountID")
    if not isinstance(account_id, str) or not _GOOGLE_ACCOUNT_ID_RE.fullmatch(account_id):
        raise GeminiManagedAuthError("Gemini verified account identity is unavailable")
    return account_id


def _copy_0600(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def promote_verified_profile(source: Path, destination: Path, identity: Mapping[str, Any]) -> None:
    """Promote verified Gemini credentials without losing either profile on error.

    The registry calls this only after it has matched the private provider
    identity.  This function copies just Gemini's native OAuth credential file,
    validates the staged copy against Google userinfo, atomically replaces the
    destination credential, and retains the source for supersession recovery.
    """
    expected_account_id = _checked_provider_account_id(identity)
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise GeminiManagedAuthError("Gemini source and destination profiles must differ")
    source_credentials = _managed_credentials_path(source)
    if not source_credentials.is_file():
        raise GeminiManagedAuthError("Gemini source credentials are unavailable")

    staging_root = destination.parent / ("." + destination.name + ".gemini-promote-" + secrets.token_hex(8))
    staged_credentials = _managed_credentials_path(staging_root)
    destination_credentials = _managed_credentials_path(destination)
    backup_credentials = destination_credentials.with_name(destination_credentials.name + ".backup-" + secrets.token_hex(8))
    replaced = False
    retain_backup = False
    had_destination = destination_credentials.is_file()
    try:
        _copy_0600(source_credentials, staged_credentials)
        validated = _identity_from_managed_profile(staging_root)
        candidate_identity = validated.get("identity") if isinstance(validated.get("identity"), dict) else {}
        if _checked_provider_account_id(candidate_identity) != expected_account_id:
            raise GeminiManagedAuthError("Gemini staged credentials belong to another account")

        if had_destination:
            _copy_0600(destination_credentials, backup_credentials)
        destination_credentials.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_credentials, destination_credentials)
        replaced = True
        destination_credentials.chmod(0o600)
    except Exception as error:
        if replaced:
            try:
                if had_destination:
                    os.replace(backup_credentials, destination_credentials)
                    destination_credentials.chmod(0o600)
                else:
                    destination_credentials.unlink(missing_ok=True)
            except OSError:
                # Keep the same-filesystem backup if restoring it also fails.
                # The source credential is retained in every failure case.
                retain_backup = had_destination
        if isinstance(error, GeminiManagedAuthError):
            raise
        raise GeminiManagedAuthError("Gemini credentials could not be promoted") from error
    finally:
        paths_to_remove = [staged_credentials]
        if not retain_backup:
            paths_to_remove.append(backup_credentials)
        for path in paths_to_remove:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for directory in (staging_root / ".gemini", staging_root):
            try:
                directory.rmdir()
            except OSError:
                pass


def _code_assist_post(method: str, payload: Dict[str, Any], access_token: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        f"{GEMINI_CODE_ASSIST_URL}:{method}", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "AgentCat/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        _record_refresh_diagnostic("code_assist_" + method, outcome="failed", httpStatus=error.code)
        raise
    except OSError:
        _record_refresh_diagnostic("code_assist_" + method, outcome="failed")
        raise
    _record_refresh_diagnostic("code_assist_" + method, outcome="received")
    return value if isinstance(value, dict) else {}


def _unavailable_usage(reason: str) -> Dict[str, Any]:
    """Return the managed-account usage schema without implying a quota value."""
    return {
        "source": "gemini_code_assist",
        "freshness": "unavailable",
        "windows": [],
        "credits": None,
        "spendControl": None,
        "rateLimitReachedType": None,
        "tokenUsage": None,
        "tokenUsageAvailable": False,
        "scope": "gemini_code_assist_request_quota",
        "reason": reason,
    }


def _usage_from_profile(profile_dir: Path) -> Dict[str, Any]:
    creds = _managed_credentials(profile_dir)
    if not creds:
        return _unavailable_usage("sign_in_required")
    token = _access_token(creds, profile_dir)
    metadata = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
    try:
        tier = _code_assist_post("loadCodeAssist", {"metadata": metadata}, token)
    except Exception:
        tier = {}
    project = tier.get("cloudaicompanionProject") if isinstance(tier.get("cloudaicompanionProject"), str) else ""
    current_tier = tier.get("currentTier") if isinstance(tier.get("currentTier"), dict) else None
    allowed_tiers = tier.get("allowedTiers") if isinstance(tier.get("allowedTiers"), list) else []
    ineligible_tiers = tier.get("ineligibleTiers") if isinstance(tier.get("ineligibleTiers"), list) else []
    if any(isinstance(item, dict) and item.get("reasonCode") == "UNSUPPORTED_CLIENT" for item in ineligible_tiers):
        # Gemini reports this consumer tier as unsupported for this Code Assist
        # client.  A projectless quota request for the same managed token is
        # rejected with SUBSCRIPTION_REQUIRED, so do not turn it into a false
        # project-configuration requirement.
        return _unavailable_usage("gemini_consumer_tier_unsupported")
    if not project and current_tier is None:
        # Current Gemini CLI calls onboardUser for this response shape.  That
        # changes the Google account's Code Assist setup, so a read-only usage
        # refresh must not perform it.  Avoid a misleading quota request that
        # the service rejects with 403 for the absent project/setup.
        default_tier = next((item for item in allowed_tiers if isinstance(item, dict) and item.get("isDefault") is True), None)
        has_default_tier = default_tier is not None
        default_requires_project = isinstance(default_tier, dict) and default_tier.get("userDefinedCloudaicompanionProject") is True
        return _unavailable_usage("code_assist_onboarding_required" if has_default_tier and not default_requires_project else "google_cloud_project_required")
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
    if not normalized:
        return _unavailable_usage("quota_not_exposed")
    return {
        "source": "gemini_code_assist",
        "freshness": "live",
        "windows": normalized,
        "credits": None,
        "spendControl": None,
        "rateLimitReachedType": None,
        "tokenUsage": None,
        "tokenUsageAvailable": False,
        "scope": "gemini_code_assist_request_quota",
        **({"planType": str(plan)} if plan else {}),
    }


def refresh(profile_dir: Path) -> Dict[str, Any]:
    profile_dir = Path(profile_dir)
    if not _has_managed_authentication(profile_dir):
        return {
            "status": "needs_reconnect",
            **_identity_unavailable("sign_in_required"),
            "usage": _unavailable_usage("sign_in_required"),
        }
    identity = _identity_from_managed_profile(profile_dir)
    if "identity" not in identity:
        # A managed credential file alone is not authentication proof.  A
        # verified Google userinfo response is required before the registry can
        # keep this account connected or associate usage with it.
        status = identity.get("identityStatus")
        reason = status.get("reason") if isinstance(status, dict) else "sign_in_required"
        return {"status": "needs_reconnect", **identity, "usage": _unavailable_usage(reason)}
    try:
        usage = _usage_from_profile(profile_dir)
    except GeminiManagedAuthError as error:
        # Preserve the specific, allowlisted refresh failure instead of
        # replacing it with a generic sign-in message.
        if "identity" not in identity:
            identity = _identity_unavailable(error.reason)
        return {"status": "needs_reconnect", **identity, "usage": _unavailable_usage(error.reason)}
    except Exception:
        # Usage access and account authentication are separate provider
        # capabilities.  Do not discard a verified managed login merely
        # because the Code Assist quota endpoint is temporarily unavailable.
        return {"status": "connected", "authenticated": True, **identity, "usage": _unavailable_usage("usage_unavailable")}
    return {"status": "connected", "authenticated": True, **identity, "usage": usage}


def diagnose_refresh(profile_dir: Path) -> Dict[str, Any]:
    """Run the normal refresh path with a deliberately redacted stage trace.

    This is for a one-shot operator diagnosis only.  It calls :func:`refresh`,
    so any valid Google refresh-token successor follows the ordinary atomic
    native-credential persistence path.  Its return value intentionally omits
    profile paths, identity, tokens, response bodies, and request URLs.
    """
    events: list[Dict[str, Any]] = []
    reset = _REFRESH_DIAGNOSTIC.set(events)
    try:
        result = refresh(Path(profile_dir))
    except Exception:
        _record_refresh_diagnostic("refresh", outcome="failed")
        result = {"status": "error"}
    finally:
        _REFRESH_DIAGNOSTIC.reset(reset)

    status = result.get("status")
    diagnostic: Dict[str, Any] = {
        "status": status if status in {"connected", "needs_reconnect", "error"} else "error",
        "authenticated": result.get("authenticated") is True,
        "events": events,
    }
    identity_status = result.get("identityStatus")
    if isinstance(identity_status, dict):
        identity_reason = identity_status.get("reason")
        diagnostic["identityStatus"] = {
            "status": "unavailable",
            **({"reason": identity_reason} if isinstance(identity_reason, str) else {}),
        }
    usage = result.get("usage")
    if isinstance(usage, dict):
        diagnostic["usage"] = {
            key: usage[key]
            for key in ("freshness", "reason")
            if isinstance(usage.get(key), str)
        }
    return diagnostic


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
