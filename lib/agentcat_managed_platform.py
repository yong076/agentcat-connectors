"""Platform-aware private-file and CLI lookup helpers for managed adapters.

POSIX uses owner-only mode bits (0o600 files, 0o700 directories).  Windows
``chmod`` cannot express that contract: it only toggles the read-only
attribute, and ``stat().st_mode`` still reports 0o666/0o777.  Credential
files live under the user's profile directory, whose inherited NTFS DACL
already excludes Everyone / BUILTIN\\Users.  Skipping chmod on Windows is
therefore not a silent widening of access.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Iterable, Optional


IS_WINDOWS = os.name == "nt"
_POSIX_CLI_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
_POSIX_PATH_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def restrict_private(path: Path, *, directory: bool = False) -> bool:
    """Apply owner-only POSIX mode.  On Windows, rely on inherited NTFS ACLs."""
    if IS_WINDOWS:
        return True
    try:
        path.chmod(0o700 if directory else 0o600)
        return True
    except OSError:
        return False


def default_cli_fallback_path() -> str:
    """PATH entries a daemon still needs after resolving a trusted absolute CLI."""
    parts: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        text = str(value) if value else ""
        if text and text not in seen:
            seen.add(text)
            parts.append(text)

    for key in ("HOME", "USERPROFILE"):
        home = os.environ.get(key)
        if home:
            add(Path(home) / ".local" / "bin")
    try:
        add(Path.home() / ".local" / "bin")
    except RuntimeError:
        pass
    if IS_WINDOWS:
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        add(Path(windir) / "System32")
        add(windir)
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            add(Path(local_app) / "Programs")
    else:
        for directory in _POSIX_PATH_DIRS:
            add(directory)
    return os.pathsep.join(parts)


def augment_search_path(current: Optional[str], fallback: str) -> str:
    """Append trusted fallback dirs when PATH is empty or system-only."""
    if not isinstance(current, str) or not current.strip():
        return fallback
    existing = [part for part in current.split(os.pathsep) if part]
    extra = [part for part in fallback.split(os.pathsep) if part and part not in existing]
    if not extra:
        return current
    return os.pathsep.join([*existing, *extra])


def cli_names(name: str, *, bare: Optional[bool] = None) -> list[str]:
    """Executable names the current platform will actually spawn.

    Discovery on Windows requires a PATHEXT suffix (``gemini.cmd`` / ``gemini.exe``).
    An explicit ``AGENTCAT_*_CLI`` override may still be a bare path.
    """
    include_bare = not IS_WINDOWS if bare is None else bare
    names: list[str] = [name] if include_bare else []
    if not IS_WINDOWS:
        return names or [name]
    # PATHEXT is a Windows semicolon list, independent of os.pathsep.
    for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";"):
        ext = ext.strip()
        if not ext:
            continue
        names.append(name + ext)
        lowered = name + ext.lower()
        if lowered not in names:
            names.append(lowered)
    return names or [name]


def is_runnable_cli(path: Path, *, explicit: bool = False) -> bool:
    if not path.is_file():
        return False
    if not IS_WINDOWS:
        return os.access(path, os.X_OK)
    if explicit:
        return True
    suffix = path.suffix.upper()
    allowed = {
        ext.strip().upper()
        for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        if ext.strip()
    }
    return suffix in allowed


def _home_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for key in ("HOME", "USERPROFILE"):
        value = os.environ.get(key)
        if value:
            text = str(Path(value))
            if text not in seen:
                seen.add(text)
                roots.append(Path(value))
    try:
        home = Path.home()
    except RuntimeError:
        return roots
    text = str(home)
    if text not in seen:
        roots.append(home)
    return roots


def resolve_cli(
    name: str,
    env_var: str,
    *,
    posix_dirs: Iterable[str] = _POSIX_CLI_DIRS,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Locate a trusted CLI.

    Resolution order:
    1. Explicit ``AGENTCAT_*_CLI`` override, as given (no PATHEXT rewriting).
    2. Injected/PATH lookup via ``which`` / ``shutil.which``, as given.
    3. Discovery fallback: ``~/.local/bin``, then trusted posix dirs.
       Windows PATHEXT suffixes apply only in this step.
    """
    configured = os.environ.get(env_var)
    if configured and is_runnable_cli(Path(configured), explicit=True):
        return configured

    finder = shutil.which if which is None else which
    found = finder(name)
    if found:
        return found

    candidates: list[str] = []
    for home in _home_roots():
        bindir = home / ".local" / "bin"
        for cli_name in cli_names(name):
            candidates.append(str(bindir / cli_name))
    if not IS_WINDOWS:
        for directory in posix_dirs:
            candidates.append(str(Path(directory) / name))
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if is_runnable_cli(Path(candidate), explicit=False):
            return candidate
    return None
