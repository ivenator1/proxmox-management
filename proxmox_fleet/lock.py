"""Fleet-wide run lock — one fleet mutation at a time, shared by every entrypoint.

`fleet-update` (cron or shell) and the `fleet-dashboard` run trigger must never
overlap: two concurrent runs would race snapshots, reboots, and history writes.
The lock is an ``fcntl.flock`` on ``<fleet_history_dir>/.fleet-update.lock`` —
the one directory every invocation already shares and creates. flock conflicts
are per open-file-description, so even two ``open()``s inside one process
conflict (which keeps the tests fork-free), and the kernel releases the lock
when the holder dies — a crashed run can never wedge the fleet.

The lock file is never deleted (unlinking flock files is racy); while held it
carries a JSON ``{"pid", "started", "argv"}`` payload so contenders can report
*who* holds it.

Read-only commands (``--history``, ``--history-show``) intentionally skip the
lock.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

_LOCK_FILENAME = ".fleet-update.lock"


class FleetLockHeld(RuntimeError):
    """Another fleet run holds the lock. ``info`` is the holder's JSON payload
    (best-effort — empty dict when unreadable)."""

    def __init__(self, info: Optional[Dict[str, Any]] = None) -> None:
        self.info: Dict[str, Any] = info or {}
        pid = self.info.get("pid", "?")
        started = self.info.get("started", "?")
        super().__init__(f"fleet run lock held (pid {pid}, started {started})")

    @property
    def pid(self) -> Any:
        return self.info.get("pid", "?")

    @property
    def started(self) -> Any:
        return self.info.get("started", "?")


def _lock_path(history_dir: Union[str, Path]) -> Path:
    directory = Path(history_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / _LOCK_FILENAME


def _read_holder_info(path: Path) -> Dict[str, Any]:
    """Best-effort read of the holder's JSON payload (may be mid-write/empty)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@contextmanager
def acquire_run_lock(
    history_dir: Union[str, Path],
    *,
    argv: Optional[List[str]] = None,
) -> Iterator[None]:
    """Hold the fleet run lock for the duration of the ``with`` block.

    Non-blocking: raises :class:`FleetLockHeld` (carrying the current holder's
    info) when another run is active. While held, the lock file carries this
    process's pid/start-time/argv for contenders to report.
    """
    path = _lock_path(history_dir)
    fd = open(path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FleetLockHeld(_read_holder_info(path)) from None
        fd.seek(0)
        fd.truncate()
        json.dump({
            "pid": os.getpid(),
            "started": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
            "argv": list(argv or []),
        }, fd)
        fd.flush()
        try:
            yield
        finally:
            # Blank the payload before releasing so a stale pid is never
            # reported as the holder by a best-effort reader.
            fd.seek(0)
            fd.truncate()
            fd.flush()
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


def probe_lock(history_dir: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """Check whether the run lock is currently held, without taking it.

    Returns None when free, else the holder's info payload (possibly empty).
    Advisory only — the answer can change immediately after returning; callers
    that need the lock must still :func:`acquire_run_lock`.
    """
    path = _lock_path(history_dir)
    with open(path, "a+", encoding="utf-8") as fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _read_holder_info(path)
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return None
