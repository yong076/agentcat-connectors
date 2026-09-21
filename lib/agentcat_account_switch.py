"""Safe default-account switching for stored Codex and Claude credentials.

A switch backs up the current default under $AGENTCAT_HOME, writes the stored
account's credential atomically, reads it back to confirm identity, and
restores the backup if verification fails.  Tokens, credential paths, and
Keychain values never enter logs, HTTP payloads, or audit rows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

import agentcat_managed_accounts as managed_accounts
from agentcat_managed_platform import restrict_private

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


SWITCHABLE_PROVIDERS = frozenset({"codex", "claude"})
_ORCA_STORE_FRAGMENTS = (
    "library/application support/orca",
    "appdata/roaming/orca",
    "appdata/local/orca",
)
_PROCESS_LOCK = threading.Lock()
# Test seams. Production never sets these.
HOOKS: Dict[str, Callable[..., Any]] = {}
PATH_RECORDER: Optional[Callable[[Path], None]] = None
KEYCHAIN_STORE: Optional[Dict[str, bytes]] = None


class AccountSwitchError(RuntimeError):
    """Stable, path-free reason code for a refused or rolled-back switch."""


def _hook(name: str, *args: Any) -> None:
    fn = HOOKS.get(name)
    if fn is not None:
        fn(*args)


def _normalize_path_text(path: Path) -> str:
    return str(path).replace("\\", "/").lower()


def _is_orca_store(path: Path) -> bool:
    text = _normalize_path_text(path)
    return any(fragment in text for fragment in _ORCA_STORE_FRAGMENTS)


def _guarded(path: Path) -> Path:
    candidate = Path(path)
    if PATH_RECORDER is not None:
        PATH_RECORDER(candidate)
    if _is_orca_store(candidate):
        raise AccountSwitchError("orca_store_forbidden")
    return candidate


def _private_dir(path: Path) -> Path:
    path = _guarded(path)
    path.mkdir(parents=True, exist_ok=True)
    restrict_private(path, directory=True)
    return path


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(_guarded(path)), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes, *, owner_dir: bool = False) -> None:
    path = _guarded(path)
    parent = _guarded(path.parent)
    parent.mkdir(parents=True, exist_ok=True)
    if owner_dir:
        restrict_private(parent, directory=True)
    tmp = _guarded(path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp"))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    restrict_private(tmp)
    os.replace(str(tmp), str(path))
    restrict_private(path)
    _fsync_dir(path.parent)


def _read_bytes(path: Path) -> Optional[bytes]:
    path = _guarded(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _remove_if_exists(path: Path) -> None:
    path = _guarded(path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AccountSwitchError("switch_restore_failed") from exc


def _manager_root(agentcat_home: Path) -> Path:
    return _private_dir(Path(agentcat_home) / "account-manager")


@contextmanager
def _acquire_locks(agentcat_home: Path) -> Iterator[None]:
    root = _manager_root(agentcat_home)
    lock_path = _guarded(root / "switch.lock")
    _PROCESS_LOCK.acquire()
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        restrict_private(lock_path)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fd is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)
        _PROCESS_LOCK.release()


def _cli_home() -> Path:
    return _guarded(managed_accounts._cli_home())


def _codex_default_path() -> Path:
    return _guarded(_cli_home() / ".codex" / "auth.json")


def _claude_default_profile() -> Path:
    return _guarded(_cli_home() / ".claude")


def _claude_json_paths() -> Tuple[Path, Path]:
    home = _cli_home()
    return _guarded(home / ".claude.json"), _guarded(home / ".claude" / ".claude.json")


def _claude_default_credential_path() -> Path:
    return _guarded(_claude_default_profile() / ".credentials.json")


def _keychain_service(profile: Path) -> str:
    return managed_accounts._claude_keychain_service(_guarded(profile))


def _keychain_get(service: str) -> Optional[bytes]:
    if KEYCHAIN_STORE is not None:
        return KEYCHAIN_STORE.get(service)
    if sys.platform != "darwin":
        return None
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-w"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except subprocess.CalledProcessError:
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not raw or len(raw.encode("utf-8")) > managed_accounts._JSON_MAX_BYTES:
        return None
    return raw.encode("utf-8")


def _keychain_set(service: str, payload: bytes) -> None:
    if KEYCHAIN_STORE is not None:
        KEYCHAIN_STORE[service] = payload
        return
    if sys.platform != "darwin":
        return
    text = payload.decode("utf-8")
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", "Claude Code", "-w", text],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
        check=False,
    )
    if result.returncode != 0:
        raise AccountSwitchError("switch_write_failed")


def _keychain_delete(service: str) -> None:
    if KEYCHAIN_STORE is not None:
        KEYCHAIN_STORE.pop(service, None)
        return
    if sys.platform != "darwin":
        return
    subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", "Claude Code"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
        check=False,
    )


def count_running_cli_sessions(provider: str) -> int:
    """Count live `claude` / `codex` processes. Failures become zero, never a refusal."""
    names = {"claude": {"claude", "claude.exe"}, "codex": {"codex", "codex.exe"}}
    wanted = names.get(provider, set())
    if not wanted:
        return 0
    try:
        completed = subprocess.run(
            ["ps", "-ax", "-o", "comm="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    count = 0
    for line in completed.stdout.splitlines():
        base = Path(line.strip()).name.lower()
        if base in wanted:
            count += 1
    return count


def _running_warning(provider: str, count: int) -> Optional[str]:
    if count <= 0:
        return None
    noun = "session" if count == 1 else "sessions"
    return (
        f"{count} {provider} {noun} are running and will keep using the previous "
        "account until restarted."
        if count != 1
        else (
            f"1 {provider} session is running and will keep using the previous "
            "account until restarted."
        )
    )


def _audit(agentcat_home: Path, entry: Dict[str, Any]) -> None:
    root = _manager_root(agentcat_home)
    path = _guarded(root / "audit.jsonl")
    line = json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    restrict_private(path)


def _source_identity(provider: str, profile: Path) -> str:
    profile = _guarded(profile)
    if provider == "codex":
        state, _reason, account_id = managed_accounts._inspect_codex_profile(profile)
        if state == "missing":
            raise AccountSwitchError("target_credential_missing")
        if state == "expired":
            raise AccountSwitchError("target_credential_expired")
        if state != "connected" or not account_id:
            raise AccountSwitchError("target_credential_unusable")
        return account_id
    state, _reason, account_uuid = managed_accounts._inspect_claude_profile(profile)
    if state == "missing":
        raise AccountSwitchError("target_credential_missing")
    if state == "expired":
        raise AccountSwitchError("target_credential_expired")
    if state != "connected" or not account_uuid:
        raise AccountSwitchError("target_credential_unusable")
    return account_uuid


def _current_default_identity(provider: str) -> Optional[str]:
    if provider == "codex":
        return managed_accounts._default_codex_account_id()
    return managed_accounts._default_claude_account_uuid()


def _codex_source_bytes(profile: Path) -> bytes:
    data = _read_bytes(_guarded(profile) / "auth.json")
    if not data:
        raise AccountSwitchError("target_credential_missing")
    return data


def _claude_source_bytes(profile: Path) -> bytes:
    profile = _guarded(profile)
    keychain = _keychain_get(_keychain_service(profile))
    if keychain:
        return keychain
    for name in (".credentials.json", "credentials.json"):
        data = _read_bytes(profile / name)
        if data:
            return data
    raise AccountSwitchError("target_credential_missing")


def _merge_claude_uuid(raw: Optional[bytes], account_uuid: str) -> bytes:
    payload: Any = {}
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
    oauth = payload.get("oauthAccount")
    if not isinstance(oauth, dict):
        oauth = {}
        payload["oauthAccount"] = oauth
    oauth["accountUuid"] = account_uuid
    cache = payload.get("cachedUsageUtilization")
    if not isinstance(cache, dict):
        cache = {}
        payload["cachedUsageUtilization"] = cache
    cache["accountUuid"] = account_uuid
    payload["accountUuid"] = account_uuid
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _new_backup_dir(agentcat_home: Path, provider: str) -> Path:
    stamp = managed_accounts.now_iso().replace(":", "").replace("+00:00", "Z")
    return _private_dir(_manager_root(agentcat_home) / "backups" / provider / (stamp + "-" + uuid.uuid4().hex[:12]))


def _write_backup_file(directory: Path, name: str, data: Optional[bytes]) -> bool:
    if data is None:
        return False
    _atomic_write_bytes(directory / name, data, owner_dir=True)
    return True


def _backup_codex(backup_dir: Path) -> Dict[str, Any]:
    current = _read_bytes(_codex_default_path())
    meta = {"hadCredential": _write_backup_file(backup_dir, "credential", current)}
    _atomic_write_bytes(backup_dir / "meta.json", (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8"), owner_dir=True)
    return meta


def _backup_claude(backup_dir: Path) -> Dict[str, Any]:
    primary, nested = _claude_json_paths()
    meta = {
        "hadCredential": _write_backup_file(backup_dir, "credential", _read_bytes(_claude_default_credential_path())),
        "hadClaudeJson": _write_backup_file(backup_dir, "claude-json", _read_bytes(primary)),
        "hadClaudeNested": _write_backup_file(backup_dir, "claude-nested", _read_bytes(nested)),
        "hadKeychain": _write_backup_file(
            backup_dir, "keychain", _keychain_get(_keychain_service(_claude_default_profile()))
        ),
    }
    _atomic_write_bytes(backup_dir / "meta.json", (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8"), owner_dir=True)
    return meta


def _load_backup_file(backup_dir: Path, name: str) -> Optional[bytes]:
    return _read_bytes(backup_dir / name)


def _restore_optional_file(path: Path, data: Optional[bytes], had: bool) -> None:
    if had and data is not None:
        _atomic_write_bytes(path, data)
        return
    _remove_if_exists(path)


def _restore_codex(backup_dir: Path, meta: Dict[str, Any]) -> None:
    _restore_optional_file(_codex_default_path(), _load_backup_file(backup_dir, "credential"), bool(meta.get("hadCredential")))


def _restore_claude(backup_dir: Path, meta: Dict[str, Any]) -> None:
    primary, nested = _claude_json_paths()
    _restore_optional_file(
        _claude_default_credential_path(),
        _load_backup_file(backup_dir, "credential"),
        bool(meta.get("hadCredential")),
    )
    _restore_optional_file(primary, _load_backup_file(backup_dir, "claude-json"), bool(meta.get("hadClaudeJson")))
    _restore_optional_file(nested, _load_backup_file(backup_dir, "claude-nested"), bool(meta.get("hadClaudeNested")))
    service = _keychain_service(_claude_default_profile())
    keychain = _load_backup_file(backup_dir, "keychain")
    if meta.get("hadKeychain") and keychain is not None:
        _keychain_set(service, keychain)
    else:
        _keychain_delete(service)


def _write_codex_default(data: bytes) -> None:
    _atomic_write_bytes(_codex_default_path(), data)


def _write_claude_default(credential: bytes, account_uuid: str) -> None:
    primary, nested = _claude_json_paths()
    targets = [path for path in (primary, nested) if path.is_file()] or [primary]
    for path in targets:
        _atomic_write_bytes(path, _merge_claude_uuid(_read_bytes(path), account_uuid))
    _atomic_write_bytes(_claude_default_credential_path(), credential)
    _keychain_set(_keychain_service(_claude_default_profile()), credential)


def _verify_codex(expected_id: str) -> bool:
    auth = managed_accounts._read_json_object(_codex_default_path())
    actual = managed_accounts._codex_account_id(auth)
    return bool(actual) and actual == expected_id


def _verify_claude(expected_id: str) -> bool:
    actual = managed_accounts._default_claude_account_uuid()
    if actual != expected_id:
        return False
    state, _reason, _uuid = managed_accounts._inspect_claude_profile(_claude_default_profile())
    if state == "connected":
        return True
    # Home-level .claude.json carries identity; file/keychain must still be a live credential.
    record = managed_accounts._read_json_object(_claude_default_credential_path())
    oauth = managed_accounts._claude_oauth(record)
    if oauth is None:
        keychain = _keychain_get(_keychain_service(_claude_default_profile()))
        if keychain:
            try:
                parsed = json.loads(keychain.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                parsed = None
            oauth = managed_accounts._claude_oauth(parsed)
    return oauth is not None and not managed_accounts._expired(oauth)


def make_default_account(
    *,
    agentcat_home: Path,
    provider: str,
    connection_id: str,
    source_profile: Path,
    confirmed: bool,
) -> Dict[str, Any]:
    """Replace the default CLI credential with the stored account's credential."""
    if confirmed is not True:
        raise ValueError("confirmed_required")
    if provider not in SWITCHABLE_PROVIDERS:
        raise ValueError("provider_not_supported")
    agentcat_home = Path(agentcat_home)
    source_profile = _guarded(source_profile)
    _guarded(agentcat_home)
    _cli_home()

    with _acquire_locks(agentcat_home):
        return _switch_locked(
            agentcat_home=agentcat_home,
            provider=provider,
            connection_id=connection_id,
            source_profile=source_profile,
        )


def _switch_locked(
    *,
    agentcat_home: Path,
    provider: str,
    connection_id: str,
    source_profile: Path,
) -> Dict[str, Any]:
    from_id = _current_default_identity(provider)
    running = count_running_cli_sessions(provider)
    to_id: Optional[str] = None
    backup_dir: Optional[Path] = None
    meta: Dict[str, Any] = {}

    def record(outcome: str) -> None:
        _audit(agentcat_home, {
            "timestamp": managed_accounts.now_iso(),
            "provider": provider,
            "fromAccountId": from_id,
            "toAccountId": to_id or connection_id,
            "outcome": outcome,
        })

    try:
        to_id = _source_identity(provider, source_profile)
    except AccountSwitchError:
        record("refused")
        raise

    try:
        _hook("before_backup")
        backup_dir = _new_backup_dir(agentcat_home, provider)
        meta = _backup_codex(backup_dir) if provider == "codex" else _backup_claude(backup_dir)
    except AccountSwitchError:
        record("backup_failed")
        raise
    except OSError as exc:
        record("backup_failed")
        raise AccountSwitchError("switch_backup_failed") from exc

    def restore() -> None:
        if backup_dir is None:
            return
        if provider == "codex":
            _restore_codex(backup_dir, meta)
        else:
            _restore_claude(backup_dir, meta)

    try:
        _hook("before_write")
        _hook("during_write")
        if provider == "codex":
            _write_codex_default(_codex_source_bytes(source_profile))
        else:
            _write_claude_default(_claude_source_bytes(source_profile), to_id)
        _hook("before_verify")
        verified = _verify_codex(to_id) if provider == "codex" else _verify_claude(to_id)
        if not verified:
            raise AccountSwitchError("switch_verification_failed")
    except AccountSwitchError as exc:
        try:
            restore()
        except (AccountSwitchError, OSError) as restore_exc:
            record("restore_failed")
            raise AccountSwitchError("switch_restore_failed") from restore_exc
        code = str(exc)
        if code == "switch_verification_failed":
            record("verification_failed")
        else:
            record("write_failed")
        raise
    except OSError as exc:
        try:
            restore()
        except Exception as restore_exc:
            record("restore_failed")
            raise AccountSwitchError("switch_restore_failed") from restore_exc
        record("write_failed")
        raise AccountSwitchError("switch_write_failed") from exc
    except Exception as exc:
        try:
            restore()
        except Exception as restore_exc:
            record("restore_failed")
            raise AccountSwitchError("switch_restore_failed") from restore_exc
        record("write_failed")
        raise AccountSwitchError("switch_write_failed") from exc

    record("success")
    result: Dict[str, Any] = {"ok": True, "provider": provider}
    warning = _running_warning(provider, running)
    if warning:
        result["warning"] = warning
    return result
