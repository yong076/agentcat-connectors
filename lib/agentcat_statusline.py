"""Merge local statusline observations within a verified account scope.

No provider calls or identity lookup belong here. Each retained window keeps
its own collector timestamp, so a partial or older report cannot freshen it.
"""
import math
from typing import Any, Dict


def merge_windows(current: Dict[str, Any], payload: Dict[str, Any], now: int) -> Dict[str, Any]:
    result = dict(current)
    incoming = payload.get("rate_limits")
    if not isinstance(incoming, dict):
        return result
    for name in ("five_hour", "seven_day"):
        window = incoming.get(name)
        if not isinstance(window, dict):
            continue
        used, reset = window.get("used_percentage"), window.get("resets_at")
        # Invalid reset times must not poison the high-water mark forever.
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not math.isfinite(used) or not 0 <= used <= 100
                or isinstance(reset, bool) or not isinstance(reset, (int, float))
                or not math.isfinite(reset) or not 0 < reset <= now + 400 * 86400):
            continue
        previous = result.get(name)
        if previous and (reset < previous["resets_at"] or
                         (reset == previous["resets_at"] and used < previous["used_percentage"])):
            continue
        result[name] = {"used_percentage": used, "resets_at": reset, "observed_at": now}
    return result
