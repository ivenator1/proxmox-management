"""Dashboard-triggered fleet runs — detached subprocess launch + log streaming.

Each trigger launches ``python -u -m proxmox_fleet.cli <flags>`` in its own
session, with stdout+stderr appended to ``<fleet_history_dir>/dashboard-runs/
<run-id>.log`` and a small ``<run-id>.json`` meta record alongside. Because
the child owns its output file (not a pipe to the dashboard), a dashboard
restart never kills a mid-flight run — streaming just re-tails the log.

Single-flight is enforced twice: the manager refuses to start while a
dashboard child is alive or :func:`proxmox_fleet.lock.probe_lock` reports the
fleet run lock held, and the CLI subprocess itself re-acquires the same lock
(so a cron run that sneaks in between probe and exec still wins safely).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess  # nosec B404 - the dashboard's whole job is launching the CLI
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from proxmox_fleet.history import _ts_now
from proxmox_fleet.lock import probe_lock

_RUNS_SUBDIR = "dashboard-runs"


class RunActive(RuntimeError):
    """A fleet run is already in flight — refuse to overlap."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunManager:
    """Launch, track, and tail dashboard-triggered ``fleet-update`` runs.

    *command* is the argv prefix the trigger flags are appended to; tests
    substitute a stub script. *cwd* defaults to the current directory —
    ``invoke_primitive`` requires CWD = project root, so start the dashboard
    from the project root exactly like ``fleet-update`` itself.
    """

    def __init__(
        self,
        history_dir: Union[str, Path],
        *,
        command: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.history_dir = Path(history_dir)
        self.runs_dir = self.history_dir / _RUNS_SUBDIR
        self.command: List[str] = list(command) if command is not None else [
            sys.executable, "-u", "-m", "proxmox_fleet.cli",
        ]
        self.cwd = cwd or os.getcwd()
        self._start_mutex = threading.Lock()

    # --- paths / meta -----------------------------------------------------

    def _meta_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def _log_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.log"

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        self._meta_path(str(meta["id"])).write_text(
            json.dumps(meta, indent=4, sort_keys=True), encoding="utf-8",
        )

    def read_meta(self, run_id: str) -> Dict[str, Any]:
        """Read one run's meta record; finalizes it first if the child died
        without a watcher (e.g. the dashboard restarted mid-run): ``finished``
        is set but ``rc`` stays None ("outcome unknown — see the log")."""
        data = json.loads(self._meta_path(run_id).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"unexpected run meta for {run_id!r}: not a JSON object")
        if data.get("finished") is None and not _pid_alive(int(data.get("pid", 0))):
            data["finished"] = _ts_now()
            self._write_meta(data)
        return data

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Newest-first meta records of dashboard-triggered runs.

        Corrupt/unreadable metas are skipped; ``limit <= 0`` means all.
        """
        if not self.runs_dir.exists():
            return []
        out: List[Dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*.json"), reverse=True):
            try:
                out.append(self.read_meta(path.stem))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return out[:limit] if limit > 0 else out

    def read_log(self, run_id: str) -> str:
        try:
            return self._log_path(run_id).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    # --- launch -----------------------------------------------------------

    def active_run(self) -> Optional[str]:
        """The id of the in-flight dashboard run, or None."""
        for meta in self.list_runs():
            if meta.get("finished") is None:
                return str(meta["id"])
        return None

    def start(self, args: Sequence[str]) -> str:
        """Launch one run; returns its id. Raises :class:`RunActive` when a
        dashboard child is still running or the fleet run lock is held (cron
        or shell run in flight)."""
        with self._start_mutex:
            active = self.active_run()
            if active is not None:
                raise RunActive(f"dashboard run {active} is still in flight")
            holder = probe_lock(self.history_dir)
            if holder is not None:
                raise RunActive(
                    f"fleet run lock held (pid {holder.get('pid', '?')}, "
                    f"started {holder.get('started', '?')})"
                )

            run_id = _ts_now()
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            argv = self.command + list(args)
            meta: Dict[str, Any] = {
                "id": run_id,
                "argv": argv,
                "args": list(args),
                "started": run_id,
                "pid": 0,
                "finished": None,
                "rc": None,
            }
            with open(self._log_path(run_id), "wb") as log_fd:
                proc = subprocess.Popen(  # nosec B603 - argv is built from validated flags
                    argv,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    cwd=self.cwd,
                    start_new_session=True,
                )
            meta["pid"] = proc.pid
            self._write_meta(meta)

            watcher = threading.Thread(
                target=self._watch, args=(run_id, proc), daemon=True,
            )
            watcher.start()
            return run_id

    def _watch(self, run_id: str, proc: "subprocess.Popen[bytes]") -> None:
        rc = proc.wait()
        meta = json.loads(self._meta_path(run_id).read_text(encoding="utf-8"))
        meta["rc"] = rc
        meta["finished"] = _ts_now()
        self._write_meta(meta)

    # --- streaming ----------------------------------------------------------

    def _drain_log(self, log_path: Path, pos: int,
                   pending: str) -> Tuple[List[Dict[str, str]], int, str]:
        """Read new bytes since *pos*, split into complete-line events; the
        trailing partial line stays in *pending* for the next poll."""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                pending += fh.read()
                pos = fh.tell()
        except FileNotFoundError:
            pass
        events: List[Dict[str, str]] = []
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            events.append({"event": "line", "data": line})
        return events, pos, pending

    @staticmethod
    def _done_events(meta: Dict[str, Any], pending: str) -> List[Dict[str, str]]:
        """The final flush: the unterminated last line (if any) + one done."""
        events: List[Dict[str, str]] = []
        if pending:
            events.append({"event": "line", "data": pending})
        rc = meta.get("rc")
        events.append({"event": "done", "data": "" if rc is None else str(rc)})
        return events

    def stream(
        self,
        run_id: str,
        *,
        poll: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Iterator[Dict[str, str]]:
        """Tail a run's log from the beginning, yielding events until it ends.

        Yields ``{"event": "line", "data": <log line>}`` per complete line,
        then exactly one ``{"event": "done", "data": <rc or "">}``. Replays
        the full log each time, so reconnects and finished runs render
        identically. *sleep* is injectable for tests.
        """
        meta = self.read_meta(run_id)  # raises FileNotFoundError for unknown ids
        log_path = self._log_path(run_id)
        pos = 0
        pending = ""
        while True:
            events, pos, pending = self._drain_log(log_path, pos, pending)
            yield from events
            if meta.get("finished") is not None:
                yield from self._done_events(meta, pending)
                return
            sleep(poll)
            meta = self.read_meta(run_id)

    async def astream(
        self, run_id: str, *, poll: float = 0.5,
    ) -> AsyncIterator[Dict[str, str]]:
        """`stream()` for async consumers (the SSE endpoint): identical events,
        but waits with ``asyncio.sleep`` so a live tail never pins a worker
        thread for the duration of the run."""
        meta = self.read_meta(run_id)
        log_path = self._log_path(run_id)
        pos = 0
        pending = ""
        while True:
            events, pos, pending = self._drain_log(log_path, pos, pending)
            for event in events:
                yield event
            if meta.get("finished") is not None:
                for event in self._done_events(meta, pending):
                    yield event
                return
            await asyncio.sleep(poll)
            meta = self.read_meta(run_id)
