"""Credential-blind managed OpenRouter PKCE lifecycle.

The daemon persists only presentation-safe operation metadata.  The browser
URL, PKCE verifier, authorization code, and issued key stay in this process.
The app claims the issued key over its authenticated loopback route and writes
it to Keychain before asking the daemon to complete registration.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional


OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"


class OpenRouterManagedError(RuntimeError):
    """Safe lifecycle failure suitable for a local control response."""


@dataclass
class _Session:
    label: str
    verifier: str
    created: float
    expires: float
    callback_consumed: bool = False
    key: Optional[str] = None
    claimed: bool = False


class OpenRouterManagedAuth:
    """Reusable PKCE state machine with injectable storage and exchange hooks.

    ``read_metadata`` and ``write_metadata`` must handle only the public
    metadata dict returned by :meth:`_public`.  ``exchange_code`` receives the
    OAuth code and verifier and returns the issued API key.  ``complete`` is
    invoked only after the app has made its authenticated, one-time Keychain
    claim; it should register the connection metadata and return its public
    connection row.
    """

    def __init__(
        self,
        *,
        callback_port: Callable[[], int],
        read_metadata: Callable[[str], Optional[Mapping[str, Any]]],
        write_metadata: Callable[[str, Mapping[str, Any]], None],
        exchange_code: Callable[[str, str], str],
        complete: Callable[[str, str, str], Dict[str, Any]],
        auth_url: str = OPENROUTER_AUTH_URL,
        ttl_seconds: float = 10 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._callback_port = callback_port
        self._read_metadata = read_metadata
        self._write_metadata = write_metadata
        self._exchange_code = exchange_code
        self._complete = complete
        self._auth_url = auth_url
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: Dict[str, _Session] = {}
        self._lock = threading.RLock()

    @staticmethod
    def adapter_capability() -> Dict[str, Any]:
        return {
            "provider": "openrouter",
            "available": True,
            "auth": "oauth_pkce",
            "keyStorage": "app_keychain_claim",
            "accountHistory": "unavailable",
        }

    def start(self, profile_dir: object = None, mode: str = "browser", *, label: str = "OpenRouter") -> Dict[str, str]:
        """Begin hosted PKCE. ``profile_dir`` is intentionally ignored."""
        del profile_dir
        if mode not in ("browser", "oauth_pkce"):
            raise OpenRouterManagedError("unsupported_openrouter_auth_mode")
        operation_id = self._random_urlsafe(32)
        verifier = self._random_urlsafe(48)
        challenge = self._b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        now = self._clock()
        expires = now + self._ttl
        clean_label = (label or "OpenRouter").strip()[:80] or "OpenRouter"
        callback_path = f"/v1/connections/openrouter/callback/{operation_id}"
        callback_url = f"http://127.0.0.1:{self._callback_port()}{callback_path}"
        query = urllib.parse.urlencode({
            "callback_url": callback_url,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        with self._lock:
            self._sessions[operation_id] = _Session(clean_label, verifier, now, expires)
            self._write_metadata(operation_id, self._public(operation_id, "pending_browser", clean_label, now, expires))
        return {
            "operationID": operation_id,
            "status": "pending_browser",
            "authorizationURL": f"{self._auth_url}?{query}",
            "callbackPath": callback_path,
        }

    def callback(self, operation_id: str, code: str) -> None:
        """Atomically consume a callback and exchange its code once."""
        with self._lock:
            session = self._active(operation_id)
            if session.callback_consumed:
                raise OpenRouterManagedError("oauth_state_consumed")
            if not code or len(code) > 4096:
                raise OpenRouterManagedError("oauth_code_required")
            session.callback_consumed = True
            verifier = session.verifier
        try:
            key = self._exchange_code(code, verifier)
        except Exception as exc:
            with self._lock:
                self._fail(operation_id, "oauth_exchange_failed")
            raise OpenRouterManagedError("oauth_exchange_failed") from exc
        if not isinstance(key, str) or not key:
            with self._lock:
                self._fail(operation_id, "oauth_exchange_failed")
            raise OpenRouterManagedError("oauth_exchange_failed")
        with self._lock:
            session = self._active(operation_id)
            session.key = key
            self._write_metadata(operation_id, self._public(operation_id, "ready", session.label, session.created, session.expires))

    def poll(self, profile_dir: object, operation_id: str) -> Dict[str, Any]:
        del profile_dir
        with self._lock:
            session = self._sessions.get(operation_id)
            if session is not None:
                if self._clock() > session.expires:
                    self._sessions.pop(operation_id, None)
                    metadata = self._read_metadata(operation_id)
                    if metadata:
                        self._write_metadata(operation_id, self._terminal(metadata, "expired"))
                    return {"status": "failed", "error": "oauth_state_expired"}
                if session.key is not None and not session.claimed:
                    return {"status": "ready"}
                if session.claimed:
                    return {"status": "claimed"}
                return {"status": "pending_browser"}
            metadata = self._read_metadata(operation_id)
            if not metadata:
                return {"status": "failed", "error": "oauth_operation_not_found"}
            # A durable pending row without its process-memory PKCE material is
            # deliberately not resumable after daemon restart.
            status = metadata.get("status")
            if status in ("pending_browser", "ready", "claimed"):
                self._write_metadata(operation_id, self._terminal(metadata, "daemon_restarted_restart_required"))
                return {"status": "failed", "error": "daemon_restarted_restart_required"}
            return {"status": "failed", "error": str(metadata.get("error") or "oauth_failed")}

    def claim(self, operation_id: str) -> str:
        with self._lock:
            session = self._active(operation_id)
            if session.claimed or not isinstance(session.key, str):
                raise OpenRouterManagedError("oauth_key_not_ready")
            session.claimed = True
            key = session.key
            session.key = None
            self._write_metadata(operation_id, self._public(operation_id, "claimed", session.label, session.created, session.expires))
            return key

    def complete(self, operation_id: str, connection_id: str, label: str) -> Dict[str, Any]:
        with self._lock:
            session = self._active(operation_id)
            if not session.claimed:
                raise OpenRouterManagedError("oauth_key_not_ready")
        row = self._complete(connection_id, label, "oauth_pkce")
        with self._lock:
            self._sessions.pop(operation_id, None)
            self._write_metadata(operation_id, self._terminal({"operationID": operation_id, "label": label}, "completed"))
        return row

    def cancel(self, profile_dir: object, operation_id: str) -> Dict[str, Any]:
        del profile_dir
        with self._lock:
            session = self._sessions.pop(operation_id, None)
            metadata = self._read_metadata(operation_id)
            if metadata is None and session is None:
                return {"status": "canceled"}
            source: Mapping[str, Any] = metadata or {"operationID": operation_id, "label": session.label}
            self._write_metadata(operation_id, self._terminal(source, "canceled"))
            return {"status": "canceled"}

    def _active(self, operation_id: str) -> _Session:
        session = self._sessions.get(operation_id)
        if session is None:
            raise OpenRouterManagedError("oauth_state_expired")
        if self._clock() > session.expires:
            self._sessions.pop(operation_id, None)
            metadata = self._read_metadata(operation_id)
            if metadata:
                self._write_metadata(operation_id, self._terminal(metadata, "expired"))
            raise OpenRouterManagedError("oauth_state_expired")
        return session

    def _fail(self, operation_id: str, reason: str) -> None:
        session = self._sessions.pop(operation_id, None)
        metadata = self._read_metadata(operation_id)
        source: Mapping[str, Any] = metadata or ({"operationID": operation_id, "label": session.label} if session else {"operationID": operation_id})
        self._write_metadata(operation_id, self._terminal(source, reason))

    @staticmethod
    def _random_urlsafe(byte_count: int) -> str:
        return base64.urlsafe_b64encode(os.urandom(byte_count)).decode("ascii").rstrip("=")

    @staticmethod
    def _b64url(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _public(operation_id: str, status: str, label: str, created: float, expires: float) -> Dict[str, Any]:
        return {
            "provider": "openrouter",
            "operationID": operation_id,
            "label": label,
            "status": status,
            "createdAtMonotonic": created,
            "expiresAtMonotonic": expires,
        }

    @staticmethod
    def _terminal(metadata: Mapping[str, Any], reason: str) -> Dict[str, Any]:
        result = {
            "provider": "openrouter",
            "operationID": str(metadata.get("operationID") or ""),
            "label": str(metadata.get("label") or "OpenRouter"),
            "status": "failed" if reason not in ("canceled", "completed") else reason,
            "error": reason,
        }
        return result


def build_adapter(
    *,
    callback_port: Callable[[], int],
    read_metadata: Callable[[str], Optional[Mapping[str, Any]]],
    write_metadata: Callable[[str, Mapping[str, Any]], None],
    exchange_code: Callable[[str, str], str],
    complete: Callable[[str, str, str], Dict[str, Any]],
    auth_url: str = OPENROUTER_AUTH_URL,
    ttl_seconds: float = 10 * 60,
    clock: Callable[[], float] = time.monotonic,
) -> OpenRouterManagedAuth:
    """Construct the adapter lazily from central daemon callbacks.

    Importing this module and calling the factory do not probe OpenRouter or
    spawn a process.  The caller owns authenticated route guards, registry
    persistence, the PKCE exchange request, and the Keychain-before-complete
    handoff.
    """
    return OpenRouterManagedAuth(
        callback_port=callback_port,
        read_metadata=read_metadata,
        write_metadata=write_metadata,
        exchange_code=exchange_code,
        complete=complete,
        auth_url=auth_url,
        ttl_seconds=ttl_seconds,
        clock=clock,
    )
