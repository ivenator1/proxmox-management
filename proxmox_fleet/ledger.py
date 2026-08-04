"""Per-host ledger — ``hosts.json`` accumulator + OS-upgrade events (PR3).

Run files are pruned to ``fleet_history_keep``, so "when did this host last
update?" cannot be answered from run history alone. The ledger is a
totals.json-style accumulator that survives pruning: every run observation
and every pending scan folds into ``hosts.json`` next to the run files. It
lives in its own module so :mod:`proxmox_fleet.scan` can feed it without
importing :mod:`proxmox_fleet.history`.

File shape (``hosts.json``)::

    {
      "hosts": {
        "<host-key>": {
          "last_run_ts": "...",       # newest run that observed the host
          "last_status": "UPDATED",   # that record's OS status string
          "last_changed_ts": "...",   # newest run with an applied OS update
          "os_release": {"id": "debian", "version_id": "12",
                         "pretty_name": "Debian GNU/Linux 12 (bookworm)"}
        }
      },
      "events": [
        {"type": "os-upgrade", "host": "<key>", "from": "11",
         "to": "12", "ts": "..."},   # newest first, capped at 100
      ]
    }

Run identities are deliberately multi-cluster-safe: **lxc → ``node/id``**
(a bare vmid is not fleet-unique — two clusters can each have a 101), vm →
``name``, remote → ``host``, node/manager → ``node``. Custom-config records
are excluded (they are not OS-managed hosts). ``last_changed_ts`` is set only
for an **applied OS update**, tested with history's shared ``_UPDATED_RE``
predicate (the same test ``count_updates`` uses) — dry-run's "WOULD UPDATE",
FAILED/ROLLED BACK strings, and an LXC community-script *app* update alone do
not count.

OS-upgrade events come from the pending scan only (it runs every 6h — ample
resolution for release upgrades; the upgrade is logged at the next scan, not
the moment it happens). The first observation of a host is a baseline: its
version is stored but no event is emitted. A later change of ``version_id``
(falling back to ``pretty_name``) appends
``{"type": "os-upgrade", "host", "from", "to", "ts"}``.

The ledger never fails a run or scan: :func:`read_ledger` returns a fresh
empty ledger on any read problem, and the observe functions swallow write
errors (an auxiliary accumulator must not abort the fleet update).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from proxmox_fleet.history import _UPDATED_RE

# Newest events kept in hosts.json; older ones are dropped on append.
_EVENTS_CAP = 100

_LEDGER_FILE = "hosts.json"

# Run-record buckets observed by the ledger. custom is deliberately absent —
# custom-config hosts run arbitrary upgrade commands, not fleet-managed OS.
_RUN_BUCKETS = ("lxc", "vm", "remote", "node")


def read_ledger(history_dir: Union[str, Path]) -> Dict[str, Any]:
    """The full ledger: ``{"hosts": {key: {...}}, "events": [...]}``.

    Missing, unreadable, or corrupt ``hosts.json`` yields a fresh empty
    ledger — the ledger is an auxiliary accumulator and must never fail the
    caller (run writer, scan writer, dashboard). A valid object missing one
    of the two keys is normalised rather than discarded.
    """
    directory = Path(history_dir)
    try:
        data = json.loads((directory / _LEDGER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # ValueError covers UnicodeDecodeError; deeply nested corrupt JSON may
        # exceed the decoder's recursion limit instead.
        data = None
    if not isinstance(data, dict):
        return {"hosts": {}, "events": []}
    hosts = data.get("hosts", {})
    events = data.get("events", [])
    if not isinstance(hosts, dict) or not isinstance(events, list):
        return {"hosts": {}, "events": []}
    # Structurally corrupt JSON is treated like syntactically corrupt JSON.
    # Without this guard, setdefault() could return a string/list host entry
    # and make an auxiliary observation fail the fleet run.
    if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in hosts.items()):
        return {"hosts": {}, "events": []}
    event_fields = ("type", "host", "from", "to", "ts")
    if any(
        not isinstance(event, dict) or any(not isinstance(event.get(field), str) for field in event_fields)
        for event in events
    ):
        return {"hosts": {}, "events": []}
    return {"hosts": hosts, "events": events}


def _write_ledger(history_dir: Union[str, Path], data: Mapping[str, Any]) -> None:
    """Persist ``hosts.json`` (best-effort — callers swallow ``OSError``)."""
    directory = Path(history_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / _LEDGER_FILE).write_text(
        json.dumps(data, indent=4, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _host_key(bucket: str, record: Mapping[str, Any]) -> Optional[str]:
    """The ledger host key for one run record; None when unidentifiable.

    lxc → ``node/id`` (multi-cluster-safe — a bare vmid is not fleet-unique),
    vm → ``name``, remote → ``host``, node/manager → ``node``. Unknown
    buckets (custom included) and records missing their identifying fields
    (legacy shapes) return None and are skipped.
    """
    if bucket == "lxc":
        node = record.get("node")
        lxc_id = record.get("id")
        if node is None or lxc_id is None:
            return None
        return f"{node}/{lxc_id}"
    if bucket == "vm":
        name = record.get("name")
        return str(name) if name else None
    if bucket == "remote":
        host = record.get("host")
        return str(host) if host else None
    if bucket == "node":
        node = record.get("node")
        return str(node) if node else None
    return None


def _os_status(bucket: str, record: Mapping[str, Any]) -> str:
    """The record field that reports OS applied-update status.

    LXC records split OS (``os``) from the community-script app (``app``) —
    only the OS line is an OS update; vm/remote/node use ``status``.
    """
    field = "os" if bucket == "lxc" else "status"
    return str(record.get(field) or "")


def _fold_run(data: Dict[str, Any], summary: Mapping[str, Any]) -> bool:
    """Merge one run summary's records into ``data["hosts"]``; True if any.

    Sets ``last_run_ts``/``last_status`` for every observed host and
    ``last_changed_ts`` only when the record's OS status matches the shared
    applied-update predicate (:data:`history._UPDATED_RE`).
    """
    run_ts = str(summary.get("timestamp") or "")
    changed = False
    for bucket in _RUN_BUCKETS:
        for record in summary.get(bucket) or []:
            if not isinstance(record, Mapping):
                continue
            key = _host_key(bucket, record)
            if key is None:
                continue
            entry = data["hosts"].setdefault(key, {})
            entry["last_run_ts"] = run_ts
            entry["last_status"] = _os_status(bucket, record)
            # Applied OS update only — the shared predicate; WOULD UPDATE and
            # FAILED/ROLLED BACK strings do not match.
            if _UPDATED_RE.search(entry["last_status"]) and not record.get("dry_run", False):
                entry["last_changed_ts"] = run_ts
            changed = True
    return changed


def observe_run(history_dir: Union[str, Path], summary: Mapping[str, Any]) -> None:
    """Fold one run summary into the ledger. Never raises.

    Called from :func:`proxmox_fleet.history.write_history` after the totals
    accumulator, so the ledger stays in step with the persisted run files.
    """
    if not isinstance(summary, Mapping):
        return
    data = read_ledger(history_dir)
    if _fold_run(data, summary):
        try:
            _write_ledger(history_dir, data)
        except (OSError, TypeError, ValueError):
            # An unwritable or unserialisable hosts.json must not fail the run.
            return


def _os_version(release: Mapping[str, Any]) -> str:
    """The comparable OS version: ``version_id``, falling back to ``pretty_name``."""
    return str(release.get("version_id") or release.get("pretty_name") or "")


def _scan_lxc_key(key: str, entry: Mapping[str, Any]) -> Optional[str]:
    """A pending-scan lxc entry's ledger host key (``node/id``).

    New snapshots are keyed ``node/id`` and carry explicit ``node``/``id``
    fields; pre-PR3 snapshots are keyed by the bare id. Normalise both to the
    composite key — an entry's own ``node``/``id`` wins over the dict key.
    An old bare-keyed entry without a node cannot be made multi-cluster-safe
    and is skipped rather than inventing ``id/id``.
    """
    node_value = entry.get("node")
    node = str(node_value) if node_value else (key.rsplit("/", 1)[0] if "/" in key else "")
    lxc_id = str(entry.get("id") or key.rsplit("/", 1)[-1])
    return f"{node}/{lxc_id}" if node and lxc_id else None


def _observe_release(
    hosts: Dict[str, Any],
    key: str,
    entry: Mapping[str, Any],
    ts: str,
    events: List[Dict[str, Any]],
) -> bool:
    """Record one entry's os_release; emit an os-upgrade event on change.

    The first observation is a baseline — the version is stored, no event.
    A change of the resolved version (``version_id``, else ``pretty_name``)
    prepends ``{"type": "os-upgrade", "host", "from", "to", "ts"}`` (newest
    first) and drops anything beyond the newest 100. Returns True when the
    ledger changed.
    """
    release = entry.get("os_release")
    if not isinstance(release, Mapping):
        return False
    new_ver = _os_version(release)
    if not new_ver:
        return False
    host_entry = hosts.setdefault(key, {})
    stored = host_entry.get("os_release")
    old_ver = _os_version(stored) if isinstance(stored, Mapping) else ""
    if old_ver and old_ver != new_ver:
        events.insert(
            0,
            {
                "type": "os-upgrade",
                "host": key,
                "from": old_ver,
                "to": new_ver,
                "ts": ts,
            },
        )
        del events[_EVENTS_CAP:]
    host_entry["os_release"] = dict(release)
    return True


def observe_scan(history_dir: Union[str, Path], scan: Mapping[str, Any]) -> None:
    """Fold one pending scan into the ledger; emit OS-upgrade events.

    Host entries keep their snapshot name; lxc entries are normalised to
    ``node/id``. Entries without a usable ``os_release`` are skipped. Never
    raises.
    """
    if not isinstance(scan, Mapping):
        return
    data = read_ledger(history_dir)
    ts = str(scan.get("timestamp") or "")
    hosts = data["hosts"]
    events = data["events"]
    changed = False
    hosts_map = scan.get("hosts")
    if isinstance(hosts_map, Mapping):
        for key, entry in hosts_map.items():
            if isinstance(entry, Mapping):
                changed |= _observe_release(hosts, str(key), entry, ts, events)
    lxc_map = scan.get("lxc")
    if isinstance(lxc_map, Mapping):
        for key, entry in lxc_map.items():
            if isinstance(entry, Mapping):
                host_key = _scan_lxc_key(str(key), entry)
                if host_key is not None:
                    changed |= _observe_release(hosts, host_key, entry, ts, events)
    if changed:
        try:
            _write_ledger(history_dir, data)
        except (OSError, TypeError, ValueError):
            # An unwritable or unserialisable hosts.json must not fail the scan.
            return
