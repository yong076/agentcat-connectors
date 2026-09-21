"""Provider-neutral lifecycle for explicitly managed local accounts.

Adapters own their native CLI/app-server protocol.  This module owns only
metadata, leases, and safe state transitions; credentials and raw provider
output never cross this boundary or enter the registry.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from agentcat_managed_platform import restrict_private


PENDING = frozenset({"pending_browser", "pending_device"})
TERMINAL = frozenset({"failed", "canceled", "error", "needs_reconnect"})
# Adapter startup may expose only these stable, non-sensitive reason codes to
# the authenticated local client.  Raw CLI output and provider detail stay out
# of both the registry and HTTP responses.
SAFE_ADAPTER_START_ERRORS = frozenset({
    "kimi_cli_unsupported", "kimi_browser_not_supported",
    "kimi_login_start_failed", "kimi_login_surface_unavailable",
    "grok_cli_unsupported", "grok_login_mode_unsupported",
    "grok_browser_not_supported", "grok_login_start_failed",
    "grok_login_surface_unavailable",
    "claude_cli_unsupported", "claude_browser_only", "claude_login_start_failed",
})
PUBLIC_FIELDS = (
    "id", "provider", "label", "kind", "scope", "status", "createdAt",
    "lastSyncAt", "lastSuccessfulSyncAt", "error", "identity", "identityStatus", "usage",
    "operationID",
)


def unavailable_usage(source: str) -> Dict[str, Any]:
    return {
        "source": source,
        "freshness": "unavailable",
        "windows": [],
        "credits": None,
        "spendControl": None,
        "rateLimitReachedType": None,
        "tokenUsage": None,
        "tokenUsageAvailable": False,
    }


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


AUTH_STATES = ("connected", "expired", "missing", "malformed", "keychain_denied", "unknown")
_AUTH_PROVIDERS = frozenset({"codex", "claude"})
_JSON_MAX_BYTES = 2 * 1024 * 1024
_MISSING = object()
_MALFORMED = object()
_UNKNOWN = object()


def _cli_home() -> Path:
    """User home the default CLI would use. Honors $HOME; never Agent Cat's registry home."""
    return Path.home()


def _read_json_object(path: Path) -> Any:
    """Read a bounded JSON object from disk. Sentinels never include path or payload."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _MISSING
    except OSError:
        return _UNKNOWN
    if not raw or len(raw) > _JSON_MAX_BYTES:
        return _MALFORMED
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _MALFORMED
    return value if isinstance(value, dict) else _MALFORMED


def _expiry_epoch(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return number
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    return None


def _record_expiry_epoch(record: Mapping[str, Any]) -> Optional[float]:
    for key in ("expiresAt", "expires_at", "expiry", "expires"):
        epoch = _expiry_epoch(record.get(key))
        if epoch is not None:
            return epoch
    return None


def _expired(record: Mapping[str, Any]) -> bool:
    epoch = _record_expiry_epoch(record)
    return epoch is not None and epoch <= dt.datetime.now(dt.timezone.utc).timestamp()


def _codex_tokens(auth: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(auth, dict):
        return None
    tokens = auth.get("tokens")
    if isinstance(tokens, dict):
        return tokens
    return None


def _codex_account_id(auth: Any) -> Optional[str]:
    tokens = _codex_tokens(auth)
    if tokens is None:
        return None
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None


def _claude_oauth(record: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    oauth = record.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = record.get("claude_ai_oauth")
    if not isinstance(oauth, dict):
        oauth = record
    if oauth.get("accessToken") or oauth.get("access_token"):
        return oauth
    return None


def _claude_json_account_uuid(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    cache = raw.get("cachedUsageUtilization")
    if isinstance(cache, dict):
        value = cache.get("accountUuid")
        if isinstance(value, str) and value.strip():
            return value.strip()
    oauth = raw.get("oauthAccount")
    if isinstance(oauth, dict):
        value = oauth.get("accountUuid")
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = raw.get("accountUuid")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _claude_keychain_service(profile_dir: Path) -> str:
    value = str(Path(profile_dir).expanduser().resolve()).encode("utf-8")
    return "Claude Code-credentials-" + hashlib.sha256(value).hexdigest()[:8]


def _auth_fields(state: str, reason: Optional[str], observed_at: str, default_active: bool) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "authState": state,
        "authObservedAt": observed_at,
        "defaultActive": bool(default_active),
    }
    if state != "connected":
        fields["authReason"] = reason or state
    return fields


def _inspect_codex_profile(profile: Path) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (authState, authReason, account_id) from the managed Codex profile."""
    auth = _read_json_object(Path(profile) / "auth.json")
    if auth is _MISSING:
        return "missing", "credential_missing", None
    if auth is _UNKNOWN:
        return "unknown", "unreadable", None
    if auth is _MALFORMED:
        return "malformed", "malformed_json", None
    tokens = _codex_tokens(auth)
    if tokens is None:
        return "malformed", "malformed_credential", None
    account_id = _codex_account_id(auth)
    if _expired(tokens) or _expired(auth):
        return "expired", "expired", account_id
    return "connected", None, account_id


def _expiry_fields(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: record[key] for key in ("expiresAt", "expires_at", "expiry", "expires") if key in record}


def _read_claude_keychain(profile: Path) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Read the profile-scoped Keychain item. Only expiry fields leave this helper."""
    if sys.platform != "darwin":
        return "skip", None
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", _claude_keychain_service(profile), "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 44:
            return "missing", None
        return "denied", None
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", None
    if not raw or len(raw) > _JSON_MAX_BYTES:
        return "malformed", None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "malformed", None
    oauth = _claude_oauth(value)
    if oauth is None:
        return "malformed", None
    return "ok", _expiry_fields(oauth)


def _inspect_claude_profile(profile: Path) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (authState, authReason, account_uuid) from the managed Claude profile."""
    profile = Path(profile)
    account_uuid = _claude_json_account_uuid(_read_json_object(profile / ".claude.json"))
    keychain_state, keychain_oauth = _read_claude_keychain(profile)
    oauth: Optional[Dict[str, Any]] = keychain_oauth if keychain_state == "ok" else None
    file_malformed = False
    file_unknown = False
    if oauth is None:
        for name in (".credentials.json", "credentials.json"):
            record = _read_json_object(profile / name)
            if record is _MISSING:
                continue
            if record is _UNKNOWN:
                file_unknown = True
                continue
            if record is _MALFORMED:
                file_malformed = True
                continue
            candidate = _claude_oauth(record)
            if candidate is None:
                file_malformed = True
                continue
            oauth = candidate
            break
    if oauth is not None:
        if _expired(oauth):
            return "expired", "expired", account_uuid
        return "connected", None, account_uuid
    if keychain_state == "denied":
        return "keychain_denied", "keychain_denied", account_uuid
    if keychain_state == "malformed" or file_malformed:
        return "malformed", "malformed_json", account_uuid
    if keychain_state == "unknown" or file_unknown:
        return "unknown", "unreadable", account_uuid
    return "missing", "credential_missing", account_uuid


def _default_codex_account_id() -> Optional[str]:
    return _codex_account_id(_read_json_object(_cli_home() / ".codex" / "auth.json"))


def _default_claude_account_uuid() -> Optional[str]:
    home = _cli_home()
    for path in (home / ".claude.json", home / ".claude" / ".claude.json"):
        uuid_value = _claude_json_account_uuid(_read_json_object(path))
        if uuid_value:
            return uuid_value
    return None


class ManagedAccounts:
    """Small metadata registry with process-memory OAuth surfaces."""

    def __init__(
        self,
        home: Path,
        adapters: Mapping[str, Any],
        *,
        pending_ttl_seconds: int = 600,
        dedup_secret: Optional[bytes] = None,
    ) -> None:
        self.home = Path(home)
        self.registry = self.home / "managed-connections.json"
        self.profile_root = self.home / "managed-connection-profiles"
        self.adapters = dict(adapters)
        self.pending_ttl_seconds = pending_ttl_seconds
        # This device-local secret is separate from the loopback bearer token.
        # It pseudonymizes provider-issued stable IDs for durable deduplication.
        self.dedup_secret = self._load_dedup_secret(dedup_secret)
        self.lock = threading.RLock()
        self.surfaces: Dict[str, Dict[str, str]] = {}
        # A terminal OAuth status may be requested twice by a UI task that was
        # resumed while its prior poll was completing.  Keep only the public
        # row locator for the pending lease; never retain a login surface.
        self.completed_operations: Dict[tuple[str, str], tuple[str, Optional[str], dt.datetime]] = {}

    def _load_dedup_secret(self, supplied: Optional[bytes]) -> bytes:
        if isinstance(supplied, bytes) and len(supplied) >= 32:
            return supplied
        secret_path = self.home / "managed-account-dedup-secret"
        try:
            value = secret_path.read_bytes()
            if len(value) >= 32:
                return value
        except OSError:
            pass
        self.home.mkdir(parents=True, exist_ok=True)
        value = os.urandom(32)
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, value)
            finally:
                os.close(fd)
        except FileExistsError:
            try:
                existing = secret_path.read_bytes()
                if len(existing) >= 32:
                    return existing
            except OSError:
                pass
        except OSError:
            # A read-only test/install still gets process-local dedup safety;
            # production supplies its durable provider-instance secret.
            pass
        return value

    def _provider_identity_key(self, row: Mapping[str, Any], payload: Mapping[str, Any]) -> Optional[str]:
        if payload.get("status") != "connected" or payload.get("authenticated") is not True:
            return None
        identity = payload.get("providerIdentity")
        if not isinstance(identity, Mapping):
            return None
        account_id = identity.get("accountID")
        tenant_id = identity.get("tenantID")
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 256:
            return None
        if any(ord(char) < 32 for char in account_id):
            return None
        if tenant_id is not None and (not isinstance(tenant_id, str) or len(tenant_id) > 256 or any(ord(char) < 32 for char in tenant_id)):
            return None
        provider = row.get("provider")
        scope = row.get("scope")
        if not isinstance(provider, str) or not isinstance(scope, str):
            return None
        material = "\0".join(("agentcat.managed-dedup.v1", provider, scope, account_id.strip(), (tenant_id or "").strip()))
        return hmac.new(self.dedup_secret, material.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _public_identity(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        identity = payload.get("identity")
        if not isinstance(identity, Mapping):
            return None
        # Stable IDs are a private one-shot handoff to the HMAC registry.  The
        # app needs only verified display identity, never the native account ID.
        return {str(key): copy.deepcopy(value) for key, value in identity.items()
                if key not in {"accountID", "tenantID"}}

    def _rows(self) -> list[Dict[str, Any]]:
        try:
            raw = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("connections") if isinstance(raw, dict) else None
        return [dict(row) for row in rows] if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else []

    def _write(self, rows: list[Dict[str, Any]]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        restrict_private(self.home, directory=True)
        stored = []
        for row in rows:
            stored_row = {key: copy.deepcopy(row[key]) for key in PUBLIC_FIELDS if key in row}
            for key in ("pendingStartedAt", "dedupKey", "supersededBy"):
                if isinstance(row.get(key), str):
                    stored_row[key] = row[key]
            stored.append(stored_row)
        tmp = self.registry.with_name(self.registry.name + ".tmp-" + uuid.uuid4().hex)
        tmp.write_text(json.dumps({"version": 1, "connections": stored}, ensure_ascii=False) + "\n", encoding="utf-8")
        restrict_private(tmp)
        tmp.replace(self.registry)
        restrict_private(self.registry)

    def _adapter(self, provider: str) -> Any:
        adapter = self.adapters.get(provider)
        if adapter is None:
            raise KeyError("provider_not_supported")
        return adapter

    def _profile(self, row: Dict[str, Any]) -> Path:
        return self.profile_root / str(row["provider"]) / str(row["id"])

    @staticmethod
    def public(row: Dict[str, Any]) -> Dict[str, Any]:
        return {key: copy.deepcopy(row[key]) for key in PUBLIC_FIELDS if key in row}

    def capabilities(self) -> Dict[str, Any]:
        providers = []
        for provider, adapter in sorted(self.adapters.items()):
            value = adapter.adapter_capability()
            if not isinstance(value, dict):
                value = {}
            providers.append({
                "provider": provider,
                "supported": bool(value.get("supported")),
                "available": bool(value.get("available")),
                "reason": value.get("reason") if isinstance(value.get("reason"), str) else None,
                "modes": [item for item in value.get("modes", []) if isinstance(item, str)],
            })
        return {"providers": providers}

    def _surface(self, result: Dict[str, Any]) -> Dict[str, str]:
        surface = {key: value for key, value in result.items() if key in {"mode", "authorizationURL", "verificationURL", "userCode"} and isinstance(value, str)}
        return surface

    def _begin(self, row: Dict[str, Any], mode: str) -> Dict[str, Any]:
        adapter = self._adapter(str(row["provider"]))
        result = adapter.start(self._profile(row), mode)
        operation = result.get("operationID") if isinstance(result, dict) else None
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(operation, str) or not operation or status not in PENDING:
            error = result.get("error") if isinstance(result, dict) else None
            if isinstance(error, str) and error in SAFE_ADAPTER_START_ERRORS:
                raise RuntimeError(error)
            raise RuntimeError("managed_oauth_start_failed")
        row["operationID"] = operation
        row["status"] = status
        row["pendingStartedAt"] = now_iso()
        row["usage"] = unavailable_usage("managed-" + str(row["provider"]))
        row.pop("error", None)
        surface = self._surface(result)
        surface["mode"] = "device" if status == "pending_device" else "browser"
        if status == "pending_browser" and result.get("browserLaunchMode") == "provider":
            # The installed provider owns its native browser launch.  There
            # is intentionally no URL to persist or reopen from Agent Cat.
            surface["browserLaunchMode"] = "provider"
        self.surfaces[operation] = surface
        return {key: copy.deepcopy(value) for key, value in result.items() if key in {"operationID", "status", "authorizationURL", "verificationURL", "userCode"}}

    def start(self, provider: str, label: str, mode: str) -> Dict[str, Any]:
        with self.lock:
            adapter = self._adapter(provider)
            cap = adapter.adapter_capability()
            if not isinstance(cap, dict) or not cap.get("available"):
                raise RuntimeError(str(cap.get("reason") if isinstance(cap, dict) else "provider_unavailable"))
            row: Dict[str, Any] = {
                "id": uuid.uuid4().hex,
                "provider": provider,
                # A pending operation has no account identity yet.  Never use
                # a caller label as the eventual account title.
                "label": "",
                "kind": "managed_native_auth",
                "scope": "managed_provider_profile",
                "status": "starting",
                "createdAt": now_iso(),
                "usage": unavailable_usage("managed-" + provider),
            }
            result = self._begin(row, mode)
            rows = self._rows()
            rows.append(row)
            self._write(rows)
            return result

    def _pending_expired(self, row: Dict[str, Any]) -> bool:
        value = str(row.get("pendingStartedAt") or "")
        try:
            started = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return True
        return (dt.datetime.now(dt.timezone.utc) - started).total_seconds() > self.pending_ttl_seconds

    def _remember_completed_operation(self, provider: str, operation: str, row: Dict[str, Any], *, superseded_connection_id: Optional[str] = None) -> None:
        connection_id = row.get("id")
        if not isinstance(connection_id, str) or len(connection_id) != 32:
            return
        self.completed_operations[(provider, operation)] = (
            connection_id,
            superseded_connection_id if isinstance(superseded_connection_id, str) else None,
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.pending_ttl_seconds),
        )

    def _completed_operation_result(self, provider: str, operation: str, rows: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        remembered = self.completed_operations.get((provider, operation))
        if remembered is None:
            return None
        connection_id, superseded_connection_id, expires_at = remembered
        if dt.datetime.now(dt.timezone.utc) > expires_at:
            self.completed_operations.pop((provider, operation), None)
            return None
        row = next((item for item in rows if item.get("id") == connection_id), None)
        if row is None or row.get("status") != "connected":
            self.completed_operations.pop((provider, operation), None)
            return None
        result: Dict[str, Any] = {"status": "connected", "connection": self.public(row)}
        if superseded_connection_id:
            result["supersededConnectionID"] = superseded_connection_id
        return result

    def _backfill_verified_identity(self, rows: list[Dict[str, Any]], source: Dict[str, Any]) -> None:
        """Give legacy connected rows a private key only from live provider proof.

        Older registries contain no HMAC key.  A new verified login may make a
        bounded refresh of same-provider, same-scope connected or reconnecting
        candidates so it can reuse an existing canonical row.  A reconnecting
        row is eligible only when its own managed profile can freshly prove a
        stable provider identity. Display email/label is never an input, and a
        failed candidate refresh leaves that row unmerged.
        """
        provider = source.get("provider")
        scope = source.get("scope")
        adapter = self._adapter(str(provider))
        candidates = [
            row for row in rows
            if row is not source and row.get("provider") == provider and row.get("scope") == scope
            and row.get("status") in {"connected", "needs_reconnect"}
            and not isinstance(row.get("dedupKey"), str)
        ][:8]
        for candidate in candidates:
            try:
                payload = adapter.refresh(self._profile(candidate))
                if not isinstance(payload, dict):
                    continue
                key = self._provider_identity_key(candidate, payload)
                if key is None:
                    continue
                self._apply(candidate, payload)
                if candidate.get("status") == "connected":
                    candidate["dedupKey"] = key
            except Exception:
                continue

    def _collapse_verified_duplicates(self, rows: list[Dict[str, Any]], canonical: Dict[str, Any], dedup_key: str, *, keep: Optional[Dict[str, Any]] = None) -> None:
        """Hide only already HMAC-matched rows; profiles remain recoverable."""
        for candidate in rows:
            if candidate is canonical or candidate is keep:
                continue
            if candidate.get("status") != "connected" or candidate.get("provider") != canonical.get("provider") or candidate.get("scope") != canonical.get("scope"):
                continue
            if not hmac.compare_digest(str(candidate.get("dedupKey") or ""), dedup_key):
                continue
            candidate["status"] = "superseded"
            candidate["supersededBy"] = canonical["id"]
            candidate.pop("error", None)
            candidate.pop("pendingStartedAt", None)

    def _canonical_duplicate(self, rows: list[Dict[str, Any]], source: Dict[str, Any], dedup_key: str) -> Optional[Dict[str, Any]]:
        candidates = [
            row for row in rows
            if row is not source and row.get("provider") == source.get("provider")
            and row.get("scope") == source.get("scope") and row.get("status") == "connected"
            and hmac.compare_digest(str(row.get("dedupKey") or ""), dedup_key)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda row: (str(row.get("createdAt") or ""), str(row.get("id") or "")))

    def _promote_duplicate(self, rows: list[Dict[str, Any]], source: Dict[str, Any], payload: Dict[str, Any], operation: Optional[str]) -> Optional[Dict[str, Any]]:
        dedup_key = self._provider_identity_key(source, payload)
        if dedup_key is None or source.get("status") != "connected":
            return None
        source["dedupKey"] = dedup_key
        self._backfill_verified_identity(rows, source)
        canonical = self._canonical_duplicate(rows, source, dedup_key)
        if canonical is None:
            return None
        promote = getattr(self._adapter(str(source["provider"])), "promote_verified_profile", None)
        if not callable(promote):
            return None
        provider_identity = payload.get("providerIdentity")
        if not isinstance(provider_identity, Mapping):
            return None
        try:
            promoted = promote(self._profile(source), self._profile(canonical), provider_identity)
        except Exception:
            return None
        if promoted is not True:
            return None
        # The newly authenticated source data is authoritative, but keeps the
        # older canonical row ID that the app already displays.
        self._apply(canonical, payload)
        canonical["dedupKey"] = dedup_key
        source["status"] = "superseded"
        source["supersededBy"] = canonical["id"]
        self._collapse_verified_duplicates(rows, canonical, dedup_key, keep=source)
        source.pop("error", None)
        source.pop("pendingStartedAt", None)
        if isinstance(operation, str):
            source["operationID"] = operation
            self.surfaces.pop(operation, None)
            self._remember_completed_operation(str(source["provider"]), operation, canonical, superseded_connection_id=str(source["id"]))
        return canonical

    def _superseded_result(self, row: Dict[str, Any], rows: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        canonical_id = row.get("supersededBy")
        if not isinstance(canonical_id, str):
            return None
        canonical = next((item for item in rows if item.get("id") == canonical_id), None)
        if canonical is None or canonical.get("status") != "connected":
            return None
        return {"status": "connected", "connection": self.public(canonical), "supersededConnectionID": row.get("id")}

    def _fail_pending(self, row: Dict[str, Any], message: str) -> None:
        operation = row.pop("operationID", None)
        if isinstance(operation, str):
            self.surfaces.pop(operation, None)
        row.pop("pendingStartedAt", None)
        row["status"] = "failed"
        row["error"] = message

    def _apply(self, row: Dict[str, Any], payload: Dict[str, Any]) -> None:
        status = payload.get("status")
        if status not in PENDING | TERMINAL | {"connected"}:
            raise RuntimeError("managed_provider_invalid_status")
        if status == "connected" and payload.get("authenticated") is not True:
            # A CLI exiting zero is not proof that it wrote a valid credential
            # to this exact managed profile.  Keep the prior account data and
            # require adapters to verify their native auth state first.
            status = "failed"
            payload = {"status": status, "error": "managed_auth_verification_required"}
        candidate_key = self._provider_identity_key(row, payload)
        prior_key = row.get("dedupKey") if isinstance(row.get("dedupKey"), str) else None
        if status == "connected" and prior_key and candidate_key and not hmac.compare_digest(prior_key, candidate_key):
            # A profile resolving to a different provider-verified account must
            # never take over this registered connection.
            status = "needs_reconnect"
            payload = {"status": status, "error": "managed_account_identity_mismatch"}
        row["status"] = status
        identity = self._public_identity(payload)
        email = identity.get("email") if identity else None
        verified = identity.get("verification") is True if identity else False
        if isinstance(email, str) and "@" in email and verified:
            normalized_identity = copy.deepcopy(identity)
            normalized_identity["email"] = email.strip().lower()
            row["identity"] = normalized_identity
            row["label"] = normalized_identity["email"]
            row.pop("identityStatus", None)
        elif isinstance(payload.get("identityStatus"), dict):
            row["identityStatus"] = copy.deepcopy(payload["identityStatus"])
        elif status == "connected" and not (isinstance(row.get("identity"), dict) and row["identity"].get("verification") is True and isinstance(row["identity"].get("email"), str)):
            row["identityStatus"] = {"status": "unavailable", "reason": "verified_email_not_exposed"}
        if isinstance(payload.get("usage"), dict):
            row["usage"] = copy.deepcopy(payload["usage"])
        if isinstance(payload.get("error"), str):
            row["error"] = payload["error"]
        elif status == "connected":
            row.pop("error", None)
        if status == "connected":
            if candidate_key is not None:
                row["dedupKey"] = candidate_key
            operation = row.pop("operationID", None)
            if isinstance(operation, str):
                self.surfaces.pop(operation, None)
                self._remember_completed_operation(str(row.get("provider") or ""), operation, row)
            row.pop("pendingStartedAt", None)
            row["lastSuccessfulSyncAt"] = now_iso()

    def status(self, provider: str, operation: str) -> Dict[str, Any]:
        with self.lock:
            rows = self._rows()
            row = next((item for item in rows if item.get("provider") == provider and item.get("operationID") == operation), None)
            if row is None:
                completed = self._completed_operation_result(provider, operation, rows)
                if completed is None:
                    raise KeyError("oauth_operation_not_found")
                return completed
            if row.get("status") == "superseded":
                result = self._superseded_result(row, rows)
                if result is None:
                    raise KeyError("oauth_operation_not_found")
                return result
            superseded_connection_id: Optional[str] = None
            if row.get("status") in PENDING:
                if operation not in self.surfaces:
                    self._fail_pending(row, "The sign-in session was interrupted by a connector restart. Start sign-in again.")
                elif self._pending_expired(row):
                    self._fail_pending(row, "The sign-in session expired. Start sign-in again.")
                else:
                    try:
                        payload = self._adapter(provider).poll(self._profile(row), operation)
                        if isinstance(payload, dict):
                            updated_surface = self._surface(payload)
                            if updated_surface:
                                updated_surface.setdefault("mode", "device" if row.get("status") == "pending_device" else "browser")
                                prior = self.surfaces.get(operation, {})
                                prior.update(updated_surface)
                                self.surfaces[operation] = prior
                        self._apply(row, payload)
                        if row.get("status") == "connected":
                            source_id = row.get("id")
                            canonical = self._promote_duplicate(rows, row, payload, operation)
                            if canonical is not None:
                                row = canonical
                                superseded_connection_id = source_id if isinstance(source_id, str) else None
                    except Exception:
                        # Do not leave a dead child looking pending forever.
                        self._fail_pending(row, "The managed sign-in process stopped. Start sign-in again.")
                self._write(rows)
            result: Dict[str, Any] = {"status": row.get("status")}
            if row.get("status") in PENDING:
                started = dt.datetime.fromisoformat(str(row["pendingStartedAt"]).replace("Z", "+00:00"))
                result["lease"] = {"expiresAt": (started + dt.timedelta(seconds=self.pending_ttl_seconds)).isoformat().replace("+00:00", "Z")}
                surface = self.surfaces.get(operation)
                if surface is not None:
                    result["resume"] = copy.deepcopy(surface)
            if row.get("status") == "connected":
                result["connection"] = self.public(row)
                if superseded_connection_id is not None:
                    result["supersededConnectionID"] = superseded_connection_id
            if isinstance(row.get("error"), str):
                result["error"] = row["error"]
            return result

    def cancel(self, provider: str, operation: str) -> str:
        with self.lock:
            rows = self._rows()
            row = next((item for item in rows if item.get("provider") == provider and item.get("operationID") == operation), None)
            if row is None:
                return "notFound"
            if row.get("status") == "superseded":
                # A durable alias resolves a completed operation; a late UI
                # cancel must not clobber its canonical connected account.
                return "alreadyCompleted"
            try:
                self._adapter(provider).cancel(self._profile(row), operation)
            except Exception:
                pass
            self._fail_pending(row, "")
            row["status"] = "canceled"
            row.pop("error", None)
            self._write(rows)
            return "canceled"

    def retry(self, provider: str, connection_id: str, mode: str) -> Dict[str, Any]:
        with self.lock:
            rows = self._rows()
            row = next((item for item in rows if item.get("provider") == provider and item.get("id") == connection_id), None)
            if row is None:
                raise KeyError("connection_not_found")
            if row.get("status") not in TERMINAL:
                raise RuntimeError("managed_oauth_retry_not_allowed")
            result = self._begin(row, mode)
            self._write(rows)
            return result

    def refresh(self, provider: str, connection_id: str) -> Dict[str, Any]:
        with self.lock:
            rows = self._rows()
            row = next((item for item in rows if item.get("provider") == provider and item.get("id") == connection_id and item.get("status") != "removed"), None)
            if row is None:
                raise KeyError("connection_not_found")
            row["lastSyncAt"] = now_iso()
            try:
                payload = self._adapter(provider).refresh(self._profile(row))
                self._apply(row, payload)
                if row.get("status") == "connected":
                    canonical = self._promote_duplicate(rows, row, payload, None)
                    if canonical is not None:
                        row = canonical
            except Exception:
                row["status"] = "error"
                row["error"] = "Could not refresh this managed account. Last successful usage is retained."
            self._write(rows)
            return self.public(row)

    def remove(self, provider: str, connection_id: str) -> Dict[str, Any]:
        with self.lock:
            rows = self._rows()
            row = next((item for item in rows if item.get("provider") == provider and item.get("id") == connection_id and item.get("status") != "removed"), None)
            if row is None:
                raise KeyError("connection_not_found")
            operation = row.pop("operationID", None)
            if isinstance(operation, str):
                self.surfaces.pop(operation, None)
            for key, remembered in list(self.completed_operations.items()):
                if key[0] == provider and remembered[0] == connection_id:
                    self.completed_operations.pop(key, None)
            try:
                self._adapter(provider).remove(self._profile(row))
            except Exception:
                # Local removal must not claim a provider-side revoke failed or
                # strand the metadata record.  Adapters remove only their own
                # isolated profile.
                pass
            row["status"] = "removed"
            row.pop("error", None)
            self._write(rows)
            return self.public(row)

    def _auth_snapshot_fields(self, row: Mapping[str, Any], observed_at: str, defaults: Mapping[str, Optional[str]]) -> Dict[str, Any]:
        provider = row.get("provider")
        try:
            profile = self._profile(dict(row))
            if provider == "codex":
                state, reason, account_id = _inspect_codex_profile(profile)
                default_id = defaults.get("codex")
                active = bool(account_id and default_id and account_id == default_id)
            elif provider == "claude":
                state, reason, account_uuid = _inspect_claude_profile(profile)
                default_id = defaults.get("claude")
                active = bool(account_uuid and default_id and account_uuid == default_id)
            else:
                return {}
        except Exception:
            return _auth_fields("unknown", "unknown", observed_at, False)
        return _auth_fields(state, reason, observed_at, active)

    def snapshot(self) -> list[Dict[str, Any]]:
        with self.lock:
            rows = self._rows()
            observed_at = now_iso()
            visible = [row for row in rows if row.get("status") not in {"removed", "superseded"}]
            need_auth = any(row.get("provider") in _AUTH_PROVIDERS for row in visible)
            defaults = {
                "codex": _default_codex_account_id(),
                "claude": _default_claude_account_uuid(),
            } if need_auth else {}
            result = []
            for row in visible:
                item = self.public(row)
                if item.get("provider") in _AUTH_PROVIDERS:
                    item.update(self._auth_snapshot_fields(row, observed_at, defaults))
                result.append(item)
            return result

    def make_default(self, connection_id: str, *, confirmed: bool = False) -> Dict[str, Any]:
        """Point the default CLI home at this stored Codex or Claude account."""
        if confirmed is not True:
            raise ValueError("confirmed_required")
        with self.lock:
            rows = self._rows()
            row = next(
                (item for item in rows
                 if item.get("id") == connection_id and item.get("status") not in {"removed", "superseded"}),
                None,
            )
            if row is None:
                raise KeyError("connection_not_found")
            provider = row.get("provider")
            if provider not in _AUTH_PROVIDERS:
                raise ValueError("provider_not_supported")
            profile = self._profile(row)
            public_row = dict(row)
        from agentcat_account_switch import make_default_account
        result = make_default_account(
            agentcat_home=self.home,
            provider=str(provider),
            connection_id=connection_id,
            source_profile=profile,
            confirmed=True,
        )
        observed_at = now_iso()
        defaults = {
            "codex": _default_codex_account_id(),
            "claude": _default_claude_account_uuid(),
        }
        public = self.public(public_row)
        public.update(self._auth_snapshot_fields(public_row, observed_at, defaults))
        result["connection"] = public
        return result

    def resolve_connection_id(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """Resolve only a durable supersession chain for a saved UI selection.

        This intentionally does not search by email, provider, or identity.  A
        caller can remap an ID only when the registry itself recorded its exact
        same-provider successor during verified duplicate promotion.
        """
        with self.lock:
            rows = self._rows()
            current = next((row for row in rows if row.get("id") == connection_id), None)
            if current is None:
                return None
            provider = current.get("provider")
            if not isinstance(provider, str):
                return None
            alias = False
            seen: set[str] = set()
            for _ in range(8):
                current_id = current.get("id")
                if not isinstance(current_id, str) or current_id in seen:
                    raise RuntimeError("connection_alias_cycle")
                seen.add(current_id)
                if current.get("status") != "superseded":
                    canonical = None if current.get("status") == "removed" else current_id
                    return {"requestConnectionID": connection_id, "canonicalConnectionID": canonical,
                            "provider": provider, "status": current.get("status"), "alias": alias}
                alias = True
                target_id = current.get("supersededBy")
                if not isinstance(target_id, str):
                    return {"requestConnectionID": connection_id, "canonicalConnectionID": None,
                            "provider": provider, "status": "missing", "alias": True}
                target = next((row for row in rows if row.get("id") == target_id and row.get("provider") == provider), None)
                if target is None:
                    return {"requestConnectionID": connection_id, "canonicalConnectionID": None,
                            "provider": provider, "status": "missing", "alias": True}
                current = target
            raise RuntimeError("connection_alias_cycle")
