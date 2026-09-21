"""Assertions and CLI stubs that match POSIX mode bits vs Windows NTFS/PATHEXT."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

# Tests patch ``subprocess.run`` / ``Popen`` on the shared stdlib module.
# Capture the real constructor so Windows ACL checks still reach icacls.
_REAL_POPEN = subprocess.Popen


_WINDOWS_DAEMON_KEEP = (
    "COMSPEC",
    "ComSpec",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "SYSTEMDRIVE",
    "PATHEXT",
    "USERNAME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "LOCALAPPDATA",
    "OS",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def platform_cli_name(base: str) -> str:
    """Return the filename CreateProcess / execve will actually run."""
    if os.name != "nt":
        return base
    for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";"):
        ext = ext.strip()
        if ext.lower() in {".cmd", ".bat", ".exe"}:
            return base + (ext.lower() if ext.lower() == ".cmd" else ext)
    return base + ".cmd"


def write_noop_cli(path: Path) -> Path:
    """Write a spawnable CLI that exits 0 on the current platform."""
    path = Path(path)
    if os.name == "nt" and path.suffix.lower() not in {".cmd", ".bat", ".exe", ".com"}:
        path = path.with_name(path.name + ".cmd")
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Inner ``cmd`` lookup uses PATH; CreateProcess still finds this .cmd via COMSPEC.
        path.write_text("@echo off\r\ncmd /c exit /b 0\r\n", encoding="utf-8")
    else:
        path.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def write_path_interpreter(directory: Path, name: str) -> str:
    """Install an interpreter looked up by PATH, return the name to invoke."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = directory / (name + ".cmd")
        path.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        return name
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return name


def write_env_wrapper(path: Path, interpreter_name: str) -> Path:
    """Write a wrapper that finds ``interpreter_name`` on PATH."""
    path = Path(path)
    if os.name == "nt" and path.suffix.lower() not in {".cmd", ".bat", ".exe", ".com"}:
        path = path.with_name(path.name + ".cmd")
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path.write_text(
            "@echo off\r\n" + interpreter_name + " %*\r\n",
            encoding="utf-8",
        )
    else:
        path.write_text("#!/usr/bin/env " + interpreter_name + "\n", encoding="utf-8")
        path.chmod(0o700)
    return path


def system_only_path() -> str:
    if os.name == "nt":
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        return os.pathsep.join((str(Path(windir) / "System32"), windir))
    return "/usr/bin:/bin:/usr/sbin:/sbin"


def patch_daemon_env(updates: dict[str, str]):
    """Simulate a daemon env: empty/minimal PATH, Windows keeps COMSPEC/PATHEXT."""
    if os.name != "nt":
        return patch.dict(os.environ, updates, clear=True)
    kept = {key: os.environ[key] for key in _WINDOWS_DAEMON_KEEP if key in os.environ}
    kept.update(updates)
    return patch.dict(os.environ, kept, clear=True)


def assert_same_path(test, actual, expected) -> None:
    """Compare filesystem paths the way Windows does (case-insensitive)."""
    test.assertEqual(
        os.path.normcase(os.fspath(actual)),
        os.path.normcase(os.fspath(expected)),
    )


def assert_owner_private(test, path: Path, *, directory: bool = False) -> None:
    """POSIX: 0o600/0o700.  Windows: the NTFS DACL must not grant world read."""
    test.assertTrue(path.exists(), msg=str(path))
    if os.name != "nt":
        expected = 0o700 if directory else 0o600
        test.assertEqual(path.stat().st_mode & 0o777, expected)
        return
    proc = _REAL_POPEN(
        ["icacls", os.fspath(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output, _ = proc.communicate()
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, ["icacls", os.fspath(path)], output)
    lowered = (output or "").lower()
    for principal in ("everyone:", "builtin\\users:"):
        test.assertNotIn(
            principal,
            lowered,
            msg="%s DACL grants %s: %s" % (path, principal.rstrip(":"), output),
        )
