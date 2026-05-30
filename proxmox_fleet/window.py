"""Maintenance-window evaluation — pure-Python port of tasks/check-window.yml.

Uses stdlib ``datetime``/``zoneinfo`` (Python ≥ 3.9). Parity contract is
defined by the existing ``test_check_window.py`` Jinja-shim tests; the new
``test_window.py`` covers this implementation case-for-case.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


def in_window(
    window: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    force: bool = False,
) -> bool:
    """Return True when ``now`` falls inside the maintenance window dict.

    ``window`` keys (all optional except none):
      days  : list of abbreviated day names, e.g. ["Sat", "Sun"] — absent → any day
      start : "HH:MM" (default "00:00")
      end   : "HH:MM" (default "23:59") — may be < start for midnight-wrap windows
      tz    : IANA timezone string (default "UTC")

    ``force=True`` always returns True (mirrors ``force_window=true`` extravar).
    ``now`` may be injected for testing; defaults to the current wall-clock time.
    """
    if force:
        return True

    tz_name: str = window.get("tz", "UTC") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if now is None:
        now = datetime.now(tz=tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    # Day check — absent days key means any day is allowed.
    days: Optional[List[str]] = window.get("days")
    if days is not None:
        day_now = now.strftime("%a")  # "Mon", "Tue", …
        if day_now not in days:
            return False

    # Time check.
    start: str = window.get("start", "00:00") or "00:00"
    end: str = window.get("end", "23:59") or "23:59"
    current = now.strftime("%H:%M")

    if start <= end:
        # Normal window: start ≤ current ≤ end
        return start <= current <= end
    else:
        # Midnight-wrap window: current ≥ start OR current ≤ end
        return current >= start or current <= end
