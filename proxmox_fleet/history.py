"""Run-history persistence — the Python port of ``tasks/persist-history.yml``.

Writes a timestamped ``run-<UTC-ts>.json`` plus an overwritten ``latest.json``
to the manager's history directory and prunes to the newest N runs. Beyond the
legacy summary it also records the **rendered Discord message body** for the run,
so each history file carries the exact briefing that was (or would be) sent.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from proxmox_fleet.models.state import FleetState


def _ts_now() -> str:
    """UTC timestamp with microsecond precision to avoid same-second collisions."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def build_run_summary(
    state: FleetState,
    *,
    timestamp: str,
    briefing: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the run-summary dict written to history (mirrors persist-history.yml).

    When *briefing* is provided, a ``briefing`` key carrying the exact message
    body (``briefing.prepare_body(state)``) is added so the persisted record
    holds the Discord message verbatim.
    """
    lxc = [r.model_dump() for r in state.lxc]
    vm = [r.model_dump() for r in state.vm]
    remote = [r.model_dump() for r in state.remote]
    node = [r.model_dump() for r in state.node]
    custom = [r.model_dump() for r in state.custom]
    errors = [r.model_dump() for r in state.errors]
    warnings = [r.model_dump() for r in state.warnings]

    summary: Dict[str, Any] = {
        "timestamp": timestamp,
        "changed": state.changed,
        "failed": state.failed,
        "counts": {
            "lxc": len(lxc),
            "vm": len(vm),
            "remote": len(remote),
            "node": len(node),
            "custom": len(custom),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "lxc": lxc,
        "vm": vm,
        "remote": remote,
        "node": node,
        "custom": custom,
        "errors": errors,
        "warnings": warnings,
    }
    if briefing is not None:
        summary["briefing"] = briefing
    return summary


def write_history(
    state: FleetState,
    *,
    history_dir: Union[str, Path] = "/var/log/fleet-update",
    keep: int = 30,
    timestamp: Optional[str] = None,
    briefing: Optional[str] = None,
) -> Path:
    """Write ``run-<ts>.json`` + ``latest.json`` and prune to the newest *keep* runs.

    Returns the path of the timestamped run file. Mirrors persist-history.yml:
    ``to_nice_json`` → ``json.dump(indent=4, sort_keys=True)``.
    """
    ts = timestamp or _ts_now()
    summary = build_run_summary(state, timestamp=ts, briefing=briefing)

    directory = Path(history_dir)
    directory.mkdir(parents=True, exist_ok=True)

    run_file = directory / f"run-{ts}.json"
    payload = json.dumps(summary, indent=4, sort_keys=True, ensure_ascii=False)
    run_file.write_text(payload, encoding="utf-8")
    (directory / "latest.json").write_text(payload, encoding="utf-8")

    _accumulate_totals(directory, summary)

    if keep > 0:
        # Timestamps sort lexically; newest last. Keep the newest `keep`, drop the rest.
        runs = sorted(directory.glob("run-*.json"))
        for stale in runs[:-keep]:
            stale.unlink(missing_ok=True)

    return run_file


# "Applied" statuses all contain the word "updated" ("UPDATED",
# "Updated (3 upgraded) & Rebooted", "UPDATED (MANUAL REBOOT REQ)", ...);
# dry-run's "WOULD UPDATE" and the FAILED/ROLLED BACK strings do not.
_UPDATED_RE = re.compile(r"updated", re.IGNORECASE)


def count_updates(run: Mapping[str, Any]) -> Dict[str, int]:
    """Derived per-run counts for one persisted run summary.

    ``os``: hosts/containers whose OS packages were updated (lxc ``os`` line
    plus vm/remote/node ``status``); ``app``: LXCs whose community-script app
    was updated (lxc ``app`` line). Counts records, not packages.
    """
    os_updates = sum(
        1 for rec in run.get("lxc") or []
        if _UPDATED_RE.search(str(rec.get("os") or ""))
    ) + sum(
        1
        for bucket in ("vm", "remote", "node")
        for rec in run.get(bucket) or []
        if _UPDATED_RE.search(str(rec.get("status") or ""))
    )
    app_updates = sum(
        1 for rec in run.get("lxc") or []
        if _UPDATED_RE.search(str(rec.get("app") or ""))
    )
    return {"os": os_updates, "app": app_updates}


# "Updated (12 upgraded)" — the only place package counts survive into the
# persisted record for LXC/remote/node; VM records carry `pkg_count` directly.
_PKG_COUNT_RE = re.compile(r"\((\d+) upgraded\)")


def count_packages(run: Mapping[str, Any]) -> int:
    """Total OS packages upgraded in one persisted run summary.

    LXC records embed the apt count in the ``os`` string; VM records carry a
    ``pkg_count`` field (with the status string as fallback). Records without
    a count (remote/node flows don't parse one) contribute 0 — this is a
    lower bound, never an estimate.
    """
    total = 0
    for rec in run.get("lxc") or []:
        m = _PKG_COUNT_RE.search(str(rec.get("os") or ""))
        if m:
            total += int(m.group(1))
    for bucket in ("vm", "remote", "node", "custom"):
        for rec in run.get(bucket) or []:
            pkg_count = rec.get("pkg_count")
            if isinstance(pkg_count, int):
                total += pkg_count
                continue
            m = _PKG_COUNT_RE.search(str(rec.get("status") or ""))
            if m:
                total += int(m.group(1))
    return total


def _seed_totals(directory: Path) -> Dict[str, Any]:
    """Build all-time totals by summing every retained run file."""
    totals: Dict[str, Any] = {"packages": 0, "app_updates": 0, "runs": 0, "since": None}
    for run_file in sorted(directory.glob("run-*.json")):
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if totals["since"] is None:
            totals["since"] = data.get("timestamp", run_file.stem[4:])
        totals["runs"] += 1
        totals["packages"] += count_packages(data)
        totals["app_updates"] += count_updates(data)["app"]
    return totals


def _accumulate_totals(directory: Path, summary: Mapping[str, Any]) -> None:
    """Fold one just-written run into ``totals.json``.

    The accumulator exists because run files are pruned to
    ``fleet_history_keep`` — summing retained files undercounts as soon as
    the first prune happens. Missing/corrupt totals reseed from whatever run
    files still exist (the just-written run included, so it is not re-added).
    """
    path = directory / "totals.json"
    totals: Any = None
    try:
        totals = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if isinstance(totals, dict):
        totals["packages"] = int(totals.get("packages") or 0) + count_packages(summary)
        totals["app_updates"] = (
            int(totals.get("app_updates") or 0) + count_updates(summary)["app"]
        )
        totals["runs"] = int(totals.get("runs") or 0) + 1
        totals.setdefault("since", summary.get("timestamp"))
    else:
        totals = _seed_totals(directory)
    path.write_text(
        json.dumps(totals, indent=4, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def read_totals(history_dir: Union[str, Path]) -> Dict[str, Any]:
    """All-time update counters for *history_dir*.

    Prefers ``totals.json`` (maintained by :func:`write_history`); until the
    first run writes one, falls back to summing the retained run files —
    read-only, so the dashboard never writes into the history directory.
    """
    directory = Path(history_dir)
    try:
        data = json.loads((directory / "totals.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        return {
            "packages": int(data.get("packages") or 0),
            "app_updates": int(data.get("app_updates") or 0),
            "runs": int(data.get("runs") or 0),
            "since": data.get("since"),
        }
    return _seed_totals(directory)


def history_summary(
    history_dir: Union[str, Path],
    *,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Read back the newest *limit* run summaries, newest first.

    Returns one dict per run with ``timestamp``/``changed``/``failed``/``counts``
    (the table-level fields of :func:`build_run_summary`) plus the derived
    ``updates`` counts (:func:`count_updates`). Unreadable or corrupt run
    files are skipped — history viewing must not fail because one file was
    truncated mid-write. ``limit <= 0`` means "all runs".
    """
    directory = Path(history_dir)
    runs = sorted(directory.glob("run-*.json"), reverse=True)
    if limit > 0:
        runs = runs[:limit]

    out: List[Dict[str, Any]] = []
    for run_file in runs:
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            # run_file.stem is "run-<ts>" — strip the prefix as a fallback.
            "timestamp": data.get("timestamp", run_file.stem[4:]),
            "changed": bool(data.get("changed", False)),
            "failed": bool(data.get("failed", False)),
            "counts": data.get("counts", {}),
            "updates": count_updates(data),
        })
    return out


def read_run(
    history_dir: Union[str, Path],
    ref: str = "latest",
) -> Dict[str, Any]:
    """Read one persisted run summary by reference.

    *ref* is ``latest`` (→ ``latest.json``) or a run timestamp — bare
    (``20260101T000000000000Z``), prefixed (``run-<ts>``), or a full filename;
    all resolve to the same file. Raises ``FileNotFoundError`` when absent.
    """
    directory = Path(history_dir)
    if ref == "latest":
        path = directory / "latest.json"
    else:
        name = ref if ref.startswith("run-") else f"run-{ref}"
        if not name.endswith(".json"):
            name += ".json"
        path = directory / name
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unexpected history payload in {path}: not a JSON object")
    return data
