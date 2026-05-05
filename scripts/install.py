#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


HOME = Path.home()
AGENTCAT_HOME = HOME / ".agentcat"
BACKUPS_DIR = AGENTCAT_HOME / "backups"
LOCAL_BIN = HOME / ".local" / "bin"
BIN_PATH = LOCAL_BIN / "agentcat"
PLIST_PATH = HOME / "Library" / "LaunchAgents" / "com.trappist.agentcatd.plist"
LABEL = "com.trappist.agentcatd"
GEMINI_TELEMETRY = AGENTCAT_HOME / "gemini" / "telemetry.log"
CODEX_BEGIN = "# agentcat-connectors:begin"
CODEX_END = "# agentcat-connectors:end"


def log(message: str) -> None:
    print(f"[agentcat] {message}")


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def run(args: List[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)


def mkdirs() -> None:
    AGENTCAT_HOME.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEMINI_TELEMETRY.parent.mkdir(parents=True, exist_ok=True)


def backup(path: Path, backup_dir: Path) -> Optional[Path]:
    if not path.exists() and not path.is_symlink():
        return None
    backup_path = backup_dir / path.name
    if backup_path.exists():
        backup_path = backup_dir / f"{path.name}.{timestamp()}"
    if path.is_dir() and not path.is_symlink():
        shutil.copytree(path, backup_path)
    else:
        shutil.copy2(path, backup_path, follow_symlinks=False)
    return backup_path


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def install_binary(repo_dir: Path, backup_dir: Path) -> None:
    src = repo_dir / "bin" / "agentcat"
    if not src.exists():
        raise FileNotFoundError(src)
    mode = src.stat().st_mode
    src.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if BIN_PATH.exists() or BIN_PATH.is_symlink():
        if BIN_PATH.is_symlink() or BIN_PATH.resolve() == src.resolve():
            BIN_PATH.unlink()
        else:
            backup_path = backup(BIN_PATH, backup_dir)
            BIN_PATH.unlink()
            if backup_path:
                log(f"backed up existing {BIN_PATH} to {backup_path}")
    BIN_PATH.symlink_to(src)
    log(f"installed {BIN_PATH}")


def plist_text() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{BIN_PATH}</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{AGENTCAT_HOME}/agentcatd.out.log</string>
  <key>StandardErrorPath</key>
  <string>{AGENTCAT_HOME}/agentcatd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AGENTCAT_HOME</key>
    <string>{AGENTCAT_HOME}</string>
    <key>HOME</key>
    <string>{HOME}</string>
    <key>PATH</key>
    <string>{LOCAL_BIN}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
"""


def unload_launch_agent() -> None:
    uid = os.getuid()
    run(["launchctl", "bootout", f"gui/{uid}", str(PLIST_PATH)])
    run(["launchctl", "unload", str(PLIST_PATH)])


def load_launch_agent() -> None:
    uid = os.getuid()
    unload_launch_agent()
    PLIST_PATH.write_text(plist_text(), encoding="utf-8")
    result = run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)])
    if result.returncode == 0:
        run(["launchctl", "enable", f"gui/{uid}/{LABEL}"])
        log(f"loaded LaunchAgent {LABEL}")
        return
    fallback = run(["launchctl", "load", "-w", str(PLIST_PATH)])
    if fallback.returncode == 0:
        log(f"loaded LaunchAgent {LABEL}")
    else:
        log(f"LaunchAgent load failed: {fallback.stderr.strip() or result.stderr.strip()}")


def ensure_claude_hook(settings: Dict[str, Any], event_name: str) -> None:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    entries = hooks.setdefault(event_name, [])
    if not isinstance(entries, list):
        entries = []
        hooks[event_name] = entries

    command = f"{BIN_PATH} claude-hook --event {event_name}"
    for entry in entries:
        if "agentcat" in json.dumps(entry):
            if command in json.dumps(entry):
                return

    entries.append(
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 5,
                }
            ],
        }
    )


def install_claude_settings(backup_dir: Path) -> None:
    path = HOME / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = backup(path, backup_dir)
        if backup_path:
            log(f"backed up Claude settings to {backup_path}")
    try:
        settings = read_json(path)
    except Exception as exc:
        log(f"skipped Claude settings, invalid JSON: {exc}")
        return

    settings["statusLine"] = {
        "type": "command",
        "command": f"{BIN_PATH} claude-statusline",
    }
    for event_name in ("SessionStart", "UserPromptSubmit", "Stop"):
        ensure_claude_hook(settings, event_name)
    write_json(path, settings)
    log("patched Claude Code statusLine and hooks")


def install_gemini_settings(backup_dir: Path) -> None:
    path = HOME / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = backup(path, backup_dir)
        if backup_path:
            log(f"backed up Gemini settings to {backup_path}")
    try:
        settings = read_json(path)
    except Exception as exc:
        log(f"skipped Gemini settings, invalid JSON: {exc}")
        return

    telemetry = settings.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    telemetry.update(
        {
            "enabled": True,
            "target": "local",
            "outfile": str(GEMINI_TELEMETRY),
            "logPrompts": False,
        }
    )
    settings["telemetry"] = telemetry
    write_json(path, settings)
    log("enabled Gemini local telemetry")


def remove_managed_block(text: str) -> str:
    pattern = re.compile(rf"\n?{re.escape(CODEX_BEGIN)}.*?{re.escape(CODEX_END)}\n?", re.DOTALL)
    return pattern.sub("\n", text).strip() + "\n"


def install_codex_config(backup_dir: Path) -> None:
    path = HOME / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = backup(path, backup_dir)
        if backup_path:
            log(f"backed up Codex config to {backup_path}")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text_without_block = remove_managed_block(text) if CODEX_BEGIN in text else text

    if re.search(r"(?m)^\s*notify\s*=", text_without_block):
        path.write_text(text_without_block, encoding="utf-8")
        log("Codex notify already exists; skipped notify patch, local SQLite usage still works")
        return

    block = f'{CODEX_BEGIN}\nnotify = ["{BIN_PATH}", "codex-notify"]\n{CODEX_END}\n'
    new_text = text_without_block.rstrip() + "\n\n" + block
    path.write_text(new_text.lstrip(), encoding="utf-8")
    log("patched Codex notify hook")


def remove_agentcat_claude_settings(backup_dir: Path) -> None:
    path = HOME / ".claude" / "settings.json"
    if not path.exists():
        return
    backup(path, backup_dir)
    try:
        settings = read_json(path)
    except Exception as exc:
        log(f"skipped Claude cleanup, invalid JSON: {exc}")
        return

    status_line = settings.get("statusLine")
    if isinstance(status_line, dict) and "agentcat" in str(status_line.get("command", "")):
        settings.pop("statusLine", None)

    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event_name in list(hooks.keys()):
            entries = hooks.get(event_name)
            if isinstance(entries, list):
                filtered = [entry for entry in entries if "agentcat" not in json.dumps(entry)]
                if filtered:
                    hooks[event_name] = filtered
                else:
                    hooks.pop(event_name, None)
        if not hooks:
            settings.pop("hooks", None)
    write_json(path, settings)
    log("removed Agent Cat Claude settings")


def remove_agentcat_gemini_settings(backup_dir: Path) -> None:
    path = HOME / ".gemini" / "settings.json"
    if not path.exists():
        return
    backup(path, backup_dir)
    try:
        settings = read_json(path)
    except Exception as exc:
        log(f"skipped Gemini cleanup, invalid JSON: {exc}")
        return
    telemetry = settings.get("telemetry")
    if isinstance(telemetry, dict) and str(telemetry.get("outfile", "")).startswith(str(AGENTCAT_HOME)):
        settings.pop("telemetry", None)
        write_json(path, settings)
        log("removed Agent Cat Gemini telemetry settings")


def remove_agentcat_codex_config(backup_dir: Path) -> None:
    path = HOME / ".codex" / "config.toml"
    if not path.exists():
        return
    backup(path, backup_dir)
    text = path.read_text(encoding="utf-8")
    if CODEX_BEGIN in text:
        path.write_text(remove_managed_block(text), encoding="utf-8")
        log("removed Agent Cat Codex config block")


def install(repo_dir: Path) -> int:
    mkdirs()
    backup_dir = BACKUPS_DIR / timestamp()
    backup_dir.mkdir(parents=True, exist_ok=True)

    install_binary(repo_dir, backup_dir)
    load_launch_agent()
    install_claude_settings(backup_dir)
    install_gemini_settings(backup_dir)
    install_codex_config(backup_dir)

    result = run([str(BIN_PATH), "snapshot"])
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.returncode != 0:
        log(f"snapshot command failed: {result.stderr.strip()}")

    log(f"backups stored in {backup_dir}")
    log("done. Run: agentcat snapshot")
    return 0


def uninstall(repo_dir: Path, purge: bool = False) -> int:
    mkdirs()
    backup_dir = BACKUPS_DIR / f"uninstall-{timestamp()}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    unload_launch_agent()
    if PLIST_PATH.exists():
        backup(PLIST_PATH, backup_dir)
        PLIST_PATH.unlink()
        log(f"removed {PLIST_PATH}")

    if BIN_PATH.is_symlink():
        BIN_PATH.unlink()
        log(f"removed {BIN_PATH}")

    remove_agentcat_claude_settings(backup_dir)
    remove_agentcat_gemini_settings(backup_dir)
    remove_agentcat_codex_config(backup_dir)

    if purge and AGENTCAT_HOME.exists():
        shutil.rmtree(AGENTCAT_HOME)
        log(f"purged {AGENTCAT_HOME}")
    else:
        log(f"local data retained in {AGENTCAT_HOME}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", default=str(Path(__file__).resolve().parents[1]))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install")
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--purge", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_dir = Path(args.repo_dir).expanduser().resolve()
    if args.command == "install":
        return install(repo_dir)
    if args.command == "uninstall":
        return uninstall(repo_dir, purge=bool(getattr(args, "purge", False)))
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
