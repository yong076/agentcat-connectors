"""Provider-neutral lifecycle for explicitly managed local accounts.

Adapters own their native CLI/app-server protocol.  This module owns only
metadata, leases, and safe state transitions; credentials and raw provider
output never cross this boundary or enter the registry.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional


PENDING = frozenset({"pending_browser", "pending_device"})
TERMINAL = frozenset({"failed", "canceled", "error", "needs_reconnect"})
PUBLIC_FIELDS = (
    "id", "provider", "label", "kind", "scope", "status", "createdAt",
    "lastSyncAt", "lastSuccessfulSyncAt", "error", "identity", "usage",
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


class ManagedAccounts:
    """Small metadata registry with process-memory OAuth surfaces."""

    def __init__(
        self,
        home: Path,
        adapters: Mapping[str, Any],
        *,
        pending_ttl_seconds: int = 600,
    ) -> None:
        self.home = Path(home)
        self.registry = self.home / "managed-connections.json"
        self.profile_root = self.home / "managed-connection-profiles"
        self.adapters = dict(adapters)
        self.pending_ttl_seconds = pending_ttl_seconds
        self.lock = threading.RLock()
        self.surfaces: Dict[str, Dict[str, str]] = {}

    def _rows(self) -> list[Dict[str, Any]]:
        try:
            raw = json.loads(self.registry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("connections") if isinstance(raw, dict) else None
        return [dict(row) for row in rows] if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else []

    def _write(self, rows: list[Dict[str, Any]]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass
        stored = []
        for row in rows:
            public = {key: copy.deepcopy(row[key]) for key in PUBLIC_FIELDS if key in row}
            for key in ("pendingStartedAt",):
                if isinstance(row.get(key), str):
                    public[key] = row[key]
            stored.append(public)
        tmp = self.registry.with_name(self.registry.name + ".tmp-" + uuid.uuid4().hex)
        tmp.write_text(json.dumps({"version": 1, "connections": stored}, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.registry)
        try:
            self.registry.chmod(0o600)
        except OSError:
            pass

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
                "label": label.strip()[:80] or provider.title(),
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
        prior_identity = row.get("identity") if isinstance(row.get("identity"), dict) else None
        candidate_identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else None
        prior_account = prior_identity.get("accountID") if prior_identity else None
        candidate_account = candidate_identity.get("accountID") if candidate_identity else None
        if status == "connected" and isinstance(prior_account, str) and prior_account and isinstance(candidate_account, str) and candidate_account and prior_account != candidate_account:
            # A profile that now resolves to another native account must not
            # silently take over this registered connection.
            status = "needs_reconnect"
            payload = {"status": status, "error": "managed_account_identity_mismatch"}
        row["status"] = status
        if isinstance(payload.get("identity"), dict):
            row["identity"] = copy.deepcopy(payload["identity"])
        if isinstance(payload.get("usage"), dict):
            row["usage"] = copy.deepcopy(payload["usage"])
        if isinstance(payload.get("error"), str):
            row["error"] = payload["error"]
        elif status == "connected":
            row.pop("error", None)
        if status == "connected":
            operation = row.pop("operationID", None)
            if isinstance(operation, str):
                self.surfaces.pop(operation, None)
            row.pop("pendingStartedAt", None)
            row["lastSuccessfulSyncAt"] = now_iso()

    def status(self, provider: str, operation: str) -> Dict[str, Any]:
        with self.lock:
            rows = self._rows()
            row = next((item for item in rows if item.get("provider") == provider and item.get("operationID") == operation), None)
            if row is None:
                raise KeyError("oauth_operation_not_found")
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
            if isinstance(row.get("error"), str):
                result["error"] = row["error"]
            return result

    def cancel(self, provider: str, operation: str) -> str:
        with self.lock:
            rows = self._rows()
            row = next((item for item in rows if item.get("provider") == provider and item.get("operationID") == operation), None)
            if row is None:
                return "notFound"
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
                self._apply(row, self._adapter(provider).refresh(self._profile(row)))
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

    def snapshot(self) -> list[Dict[str, Any]]:
        with self.lock:
            rows = self._rows()
            return [self.public(row) for row in rows if row.get("status") != "removed"]
