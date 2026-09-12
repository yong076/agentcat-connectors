"""Small, credential-blind client for Codex's official app-server protocol.

The connector deliberately delegates browser/device OAuth, token storage, token
refresh, and account-usage requests to the installed ``codex app-server``.  It
never reads an auth file or a token.  Each managed connection receives its own
``CODEX_HOME`` in the child process only.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


class CodexAppServerError(RuntimeError):
    pass


class CodexAppServerUnsupported(CodexAppServerError):
    pass


def child_environment(profile_dir: Path) -> Dict[str, str]:
    """Return an isolated child environment without inherited auth overrides."""
    env = dict(os.environ)
    # These variables can make Codex select a caller's API-key identity.  Do
    # not log or inspect their values; removing them affects this child only.
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_AUTH_TOKEN", "CODEX_AUTH_TOKEN"):
        env.pop(name, None)
    env["CODEX_HOME"] = str(profile_dir)
    return env


def normalize_identity(result: Any) -> Optional[Dict[str, Optional[str]]]:
    account = result.get("account") if isinstance(result, dict) else None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return None
    email = account.get("email")
    plan = account.get("planType")
    return {
        "email": email if isinstance(email, str) else None,
        "planType": plan if isinstance(plan, str) else None,
    }


def normalize_usage(rate_limits: Any, token_usage: Any, *, token_usage_available: bool) -> Dict[str, Any]:
    """Preserve native account quota semantics; absent values stay unavailable."""
    response = rate_limits if isinstance(rate_limits, dict) else {}
    legacy = response.get("rateLimits") if isinstance(response.get("rateLimits"), dict) else response
    by_limit_id = response.get("rateLimitsByLimitId") if isinstance(response.get("rateLimitsByLimitId"), dict) else {}
    snapshots = list(by_limit_id.items()) if by_limit_id else [("default", legacy)]
    windows = []
    for limit_id, snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        for key, is_primary in (("primary", True), ("secondary", False)):
            raw = snapshot.get(key)
            if not isinstance(raw, dict):
                continue
            used = raw.get("usedPercent")
            used_value = float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else None
            windows.append({
                "id": f"{limit_id}:{key}",
                "limitID": limit_id,
                "name": snapshot.get("limitName") if isinstance(snapshot.get("limitName"), str) else None,
                "model": snapshot.get("normalModelSlug") if isinstance(snapshot.get("normalModelSlug"), str) else None,
                "primary": is_primary,
                "usedPercent": used_value,
                "remainingPercent": (max(0.0, 100.0 - used_value) if used_value is not None else None),
                "windowDurationMins": raw.get("windowDurationMins") if isinstance(raw.get("windowDurationMins"), int) else None,
                "resetsAt": raw.get("resetsAt") if isinstance(raw.get("resetsAt"), int) else None,
            })
    credits = legacy.get("credits")
    normalized_credits = None
    if isinstance(credits, dict):
        normalized_credits = {
            "hasCredits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance") if isinstance(credits.get("balance"), str) else None,
        }
    spend = legacy.get("individualLimit")
    normalized_spend = None
    if isinstance(spend, dict):
        normalized_spend = {key: spend.get(key) for key in ("limit", "used", "remainingPercent", "resetsAt") if spend.get(key) is not None}
    normalized_tokens = None
    if token_usage_available and isinstance(token_usage, dict):
        summary = token_usage.get("summary")
        daily = token_usage.get("dailyUsageBuckets")
        normalized_tokens = {
            "dailyBuckets": [
                {"startDate": row.get("startDate"), "tokens": row.get("tokens")}
                for row in daily if isinstance(row, dict) and isinstance(row.get("startDate"), str) and isinstance(row.get("tokens"), int)
            ] if isinstance(daily, list) else None,
            "summary": {
                key: summary.get(key)
                for key in ("lifetimeTokens", "currentStreakDays", "longestStreakDays", "peakDailyTokens", "longestRunningTurnSec")
                if isinstance(summary, dict) and isinstance(summary.get(key), int)
            } if isinstance(summary, dict) else None,
        }
    reset_credits = response.get("rateLimitResetCredits")
    normalized_reset_credits = None
    if isinstance(reset_credits, dict):
        details = reset_credits.get("credits")
        normalized_reset_credits = {
            "availableCount": reset_credits.get("availableCount") if isinstance(reset_credits.get("availableCount"), int) else None,
            # Opaque ids can authorize a consume request. Keep display-only data.
            "details": [
                {key: item.get(key) for key in ("title", "description", "status", "resetType", "grantedAt", "expiresAt") if item.get(key) is not None}
                for item in details if isinstance(item, dict)
            ] if isinstance(details, list) else None,
        }
    return {
        "source": "codex-app-server",
        "freshness": "live",
        "fetchedAt": int(time.time()),
        "windows": windows,
        "credits": normalized_credits,
        "spendControl": normalized_spend,
        "rateLimitReachedType": legacy.get("rateLimitReachedType") if isinstance(legacy.get("rateLimitReachedType"), str) else None,
        "ordinaryUsageAllowed": response.get("ordinaryUsageAllowed") if isinstance(response.get("ordinaryUsageAllowed"), bool) else None,
        "resetCredits": normalized_reset_credits,
        "tokenUsage": normalized_tokens,
        "tokenUsageAvailable": token_usage_available,
    }


class CodexAppServer:
    """A single managed app-server child. All protocol messages are JSON-RPC."""

    def __init__(self, profile_dir: Path, executable: Optional[str] = None):
        self.profile_dir = Path(profile_dir)
        self.executable = executable or shutil.which("codex") or "/opt/homebrew/bin/codex"
        self.process: Optional[subprocess.Popen[str]] = None
        self.messages: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.write_lock = threading.Lock()
        self.request_id = 0
        self.generation = 0

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.is_alive():
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.profile_dir.chmod(0o700)
        except OSError:
            pass
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=child_environment(self.profile_dir),
        )
        self.generation += 1
        assert self.process.stdout is not None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "Agent Cat", "version": "1"}, "capabilities": {}}, timeout=12)

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self.messages.put(message)

    def request(self, method: str, params: Dict[str, Any], *, timeout: float = 15) -> Dict[str, Any]:
        self.start()
        assert self.process is not None and self.process.stdin is not None
        with self.write_lock:
            self.request_id += 1
            request_id = self.request_id
            self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
            self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        deferred = []
        try:
            while time.monotonic() < deadline:
                try:
                    message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if message.get("id") != request_id:
                    deferred.append(message)
                    continue
                error = message.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or "app-server request failed")[:300]
                    if "unknown variant" in detail or "unknown method" in detail:
                        raise CodexAppServerUnsupported(detail)
                    raise CodexAppServerError(detail)
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        finally:
            for message in deferred:
                self.messages.put(message)
        raise CodexAppServerError("app-server request timed out")

    def login(self, mode: str) -> Dict[str, Any]:
        if mode == "device":
            return self.request("account/login/start", {"type": "chatgptDeviceCode"})
        return self.request("account/login/start", {"type": "chatgpt", "appBrand": "codex", "codexStreamlinedLogin": True})

    def cancel_login(self, login_id: str) -> Dict[str, Any]:
        return self.request("account/login/cancel", {"loginId": login_id})

    def account(self) -> Optional[Dict[str, Optional[str]]]:
        return normalize_identity(self.request("account/read", {}))

    def usage(self) -> Dict[str, Any]:
        limits = self.request("account/rateLimits/read", {"excludeResetCreditDetails": False})
        # 0.154.0's generated schema still calls this TokenUsage while the
        # installed server accepts account/usage/read.  Prefer the live method
        # and retain the schema spelling only as an unsupported-method alias.
        try:
            tokens = self.request("account/usage/read", {})
            token_available = True
        except CodexAppServerUnsupported:
            try:
                tokens = self.request("account/tokenUsage/read", {})
                token_available = True
            except CodexAppServerUnsupported:
                tokens = None
                token_available = False
        return normalize_usage(limits, tokens, token_usage_available=token_available)

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self.process = None
