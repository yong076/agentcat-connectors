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
import ntpath
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


def path_key(value: object, *, windows: Optional[bool] = None) -> str:
    """Membership key for paths and PATHEXT names.

    ``windows`` permits Windows matching rules to be tested off-Windows.
    """
    is_windows = IS_WINDOWS if windows is None else windows
    path_module = ntpath if is_windows else os.path
    text = path_module.normpath(str(value) if value else "")
    folded = path_module.normcase(text)
    return folded.lower() if is_windows else folded


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
        key = path_key(text)
        if text and key not in seen:
            seen.add(key)
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


def augment_search_path(
    current: Optional[str],
    fallback: str,
    *,
    pathsep: Optional[str] = None,
    windows: Optional[bool] = None,
) -> str:
    """Append trusted fallback dirs when PATH is empty or system-only."""
    if not isinstance(current, str) or not current.strip():
        return fallback
    separator = os.pathsep if pathsep is None else pathsep
    existing = [part for part in current.split(separator) if part]
    present = {path_key(part, windows=windows) for part in existing}
    extra = [
        part for part in fallback.split(separator)
        if part and path_key(part, windows=windows) not in present
    ]
    if not extra:
        return current
    return separator.join([*existing, *extra])


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
    seen: set[str] = {path_key(item) for item in names}
    for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";"):
        ext = ext.strip()
        if not ext:
            continue
        candidate = name + ext
        key = path_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        names.append(candidate)
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

    def add(path: Path) -> None:
        key = path_key(path)
        if key and key not in seen:
            seen.add(key)
            roots.append(path)

    for env_key in ("HOME", "USERPROFILE"):
        value = os.environ.get(env_key)
        if value:
            add(Path(value))
    try:
        add(Path.home())
    except RuntimeError:
        pass
    return roots


def _dir_files_by_key(directory: Path) -> dict[str, Path]:
    """Map case-folded names to the directory entry that actually exists."""
    files: dict[str, Path] = {}
    try:
        entries = directory.iterdir()
    except OSError:
        return files
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        files.setdefault(path_key(entry.name), entry)
    return files


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

    seen_dirs: set[str] = set()
    for home in _home_roots():
        bindir = home / ".local" / "bin"
        dir_key = path_key(bindir)
        if dir_key in seen_dirs:
            continue
        seen_dirs.add(dir_key)
        files = _dir_files_by_key(bindir)
        for cli_name in cli_names(name):
            entry = files.get(path_key(cli_name))
            if entry is None:
                constructed = bindir / cli_name
                if is_runnable_cli(constructed, explicit=False):
                    entry = constructed
            if entry is not None and is_runnable_cli(entry, explicit=False):
                return str(entry)
    if not IS_WINDOWS:
        seen_paths: set[str] = set()
        for directory in posix_dirs:
            candidate = str(Path(directory) / name)
            key = path_key(candidate)
            if not candidate or key in seen_paths:
                continue
            seen_paths.add(key)
            if is_runnable_cli(Path(candidate), explicit=False):
                return candidate
    return None
