"""Durable, run-scoped suppression for transient housekeeping failures.

Only completed, non-dry maintenance runs contribute to the streak. Failure
before discovery/introspection finishes still counts, while a successful run
must have actually invoked maintenance to clear it. Persistence failures never
hide a current failure; diagnostics deliberately omit paths and exception text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


_FAILURE_THRESHOLD = 12
_STATE_FILE = "housekeeping-alert-state.json"
_STATE_VERSION = 1


@dataclass(frozen=True)
class AlertDecision:
    """Dispatch eligibility and an optional non-notifying diagnostic."""

    allow_automatic: bool
    warning: Optional[str] = None


def _load_failures(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    if not isinstance(data, dict):
        raise ValueError("invalid alert state")
    version = data.get("version")
    count = data.get("consecutive_failures")
    if type(version) is not int or version != _STATE_VERSION or type(count) is not int or count < 0:
        raise ValueError("invalid alert state")
    return count


def _save_failures(path: Path, count: int) -> None:
    # Match the scan notification state's atomic JSON replacement convention.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"version": _STATE_VERSION, "consecutive_failures": count}, indent=4, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def record_run(
    history_dir: Union[str, Path],
    *,
    failed: bool,
    attempted: bool,
    dry_run: bool,
) -> AlertDecision:
    """Record one maintenance outcome and gate automatic failure/warning alerts.

    Missing state starts at zero. An unreadable or malformed counter is retained
    for diagnosis and fails open on every current failure until a real success
    resets it. A save failure also fails open. Dry runs and skipped-only clean
    runs do no state I/O and do not trigger automatic housekeeping alerts.
    Callers retain the actual failed status for history and explicit dispatch.
    """
    if dry_run or (not failed and not attempted):
        return AlertDecision(False)

    path = Path(history_dir) / _STATE_FILE
    if failed:
        try:
            count = _load_failures(path) + 1
        except (OSError, ValueError, RecursionError):
            return AlertDecision(
                True,
                "Housekeeping alert state is unavailable; automatic failure suppression is disabled.",
            )
    else:
        count = 0

    try:
        _save_failures(path, count)
    except (OSError, ValueError):
        return AlertDecision(
            failed,
            "Housekeeping alert state could not be saved; failure notification history may be incomplete.",
        )
    return AlertDecision(failed and count >= _FAILURE_THRESHOLD)
