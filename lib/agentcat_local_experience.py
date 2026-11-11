"""Local-only hook observations. Counts never include prompts or identifiers."""
import datetime as dt
from typing import Any, Optional


def stamp(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def session_observation(event: str, payload: Any, session_key: str, now: int) -> Optional[dict]:
    if not session_key or not isinstance(payload, dict):
        return None
    state = {"SessionStart": "running", "UserPromptSubmit": "running", "PostToolUse": "running",
             "Stop": "completed", "SessionEnd": "completed", "PermissionRequest": "waiting"}.get(event)
    if event == "Notification" and payload.get("notification_type") in ("permission_prompt", "idle_prompt"):
        state = "waiting"
    if not state:
        return None
    return {"session": session_key, "state": state, "observedAt": now}


def session_summary(observations: list, now: int) -> dict:
    latest = {}
    for observation in observations:
        key, when = observation.get("session"), observation.get("observedAt")
        if not isinstance(key, str) or type(when) is not int or not 0 <= now - when <= 600:
            continue
        if key not in latest or when >= latest[key]["observedAt"]:
            latest[key] = observation
    counts = {"waiting": 0, "running": 0, "completed": 0}
    for row in latest.values():
        if row.get("state") in counts:
            counts[row["state"]] += 1
    return {**counts, "provider": "claude", "updatedAt": stamp(now),
            "expiresAt": stamp(min((x["observedAt"] + 600 for x in latest.values()), default=now)),
            "source": "claude-hooks"}
