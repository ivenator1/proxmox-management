"""Pending-updates scan — the read-only fleet walk behind ``fleet-update --scan``.

Collects what *would* update without changing anything: pending OS packages for
remote hosts, VMs, and Proxmox nodes (simulate commands per package manager),
and for managed LXCs both the pending OS packages (``pct exec`` simulate) and
the community-script app version (installed ``~/.{script}`` file vs. the latest
GitHub release — the same detect chain as the lxc flow's dry-check).

Results are written to ``pending-<UTC-ts>.json`` + ``pending-latest.json`` in
``fleet_history_dir`` (pruned to ``scan_history_keep``), next to the run
history, so tooling (and the future dashboard) reads both from one place.

Strictly side-effect-free on the fleet: stopped containers are skipped (never
started), templates are skipped, and only metadata-refresh + simulate commands
run on the hosts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from proxmox_fleet import http as http_mod
from proxmox_fleet import inventory
from proxmox_fleet.cluster import DEFAULT_CLUSTER, limit_selects_id, token_is_id
from proxmox_fleet.executor import Executor
from proxmox_fleet.flows._pkg import detect_pkg_mgr
from proxmox_fleet.flows.lxc import (
    _build_shell,
    _discover_lxcs,
    _read_version,
    os_mismatch_warning,
)
from proxmox_fleet.lxc_parse import (
    parse_ct_script,
    parse_df_percent,
    parse_os_release,
    parse_pct_config,
    parse_pct_status,
    script_name_from_update,
)
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.orchestration import run_concurrent
from proxmox_fleet.runner import UnreachableHostError, is_unreachable_error

# OS type (pct config ostype) → package manager, for containers.
_OSTYPE_PKG_MGR = {
    "debian": "apt", "ubuntu": "apt", "devuan": "apt",
    "alpine": "apk",
}


def scan_cmd(pkg_mgr: str) -> str:
    """Read-only pending-updates command per package manager (``LC_ALL=C``).

    apt reuses the dry-run simulate (``-s dist-upgrade``); dnf uses
    ``check-update`` (built for exactly this, exits 100 when updates exist —
    callers must ignore the rc); apk lists upgradable packages.
    """
    if pkg_mgr == "apt":
        prefix = "LC_ALL=C DEBIAN_FRONTEND=noninteractive"
        return f"{prefix} apt-get update -qq && {prefix} apt-get -s dist-upgrade"
    if pkg_mgr == "dnf":
        return "LC_ALL=C dnf -q check-update"
    if pkg_mgr == "apk":
        return "LC_ALL=C apk update -q >/dev/null 2>&1; LC_ALL=C apk version -l '<'"
    raise RuntimeError(f"Unknown package manager: {pkg_mgr!r}")


def parse_pending(stdout: str, pkg_mgr: str) -> List[str]:
    """Extract pending package names from a scan_cmd() output."""
    pkgs: List[str] = []
    if pkg_mgr == "apt":
        # Simulate lines: "Inst <pkg> [old-ver] (new-ver repo) [...]"
        for line in stdout.splitlines():
            if line.startswith("Inst "):
                parts = line.split()
                if len(parts) >= 2:
                    pkgs.append(parts[1])
    elif pkg_mgr == "dnf":
        # check-update table: "name.arch    version    repo" (3 columns).
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and "." in parts[0] and not parts[0].endswith(":"):
                pkgs.append(parts[0].rsplit(".", 1)[0])
    elif pkg_mgr == "apk":
        # "name-1.2.3-r1 < 1.2.4-r0" — strip the trailing version-rN suffix.
        for line in stdout.splitlines():
            if " < " in line:
                left = line.split(" < ")[0].strip()
                pkgs.append(re.sub(r"-\d[^-]*-r\d+$", "", left))
    return pkgs


def scan_host(executor: Executor) -> Dict[str, Any]:
    """Scan one SSH-reachable host: detect the package manager, list pending.

    Never raises — errors land in the returned dict's ``error`` key.
    """
    try:
        pkg_mgr = detect_pkg_mgr(executor)
        # ignore_errors: dnf check-update exits 100 when updates are pending.
        res = executor.run_shell(scan_cmd(pkg_mgr), changed_when=False, ignore_errors=True)
        if pkg_mgr != "dnf" and res.failed:
            raise RuntimeError(f"scan command failed (rc={res.rc}): {res.stderr or res.stdout}"[:300])
        pending = parse_pending(res.stdout, pkg_mgr)
        return {"pkg_mgr": pkg_mgr, "pending_count": len(pending),
                "pending": pending, "unreachable": False, "error": None}
    except Exception as exc:  # noqa: BLE001 - a scan must never abort the walk
        return {"pkg_mgr": "", "pending_count": 0, "pending": [],
                "unreachable": (isinstance(exc, UnreachableHostError)
                                or is_unreachable_error(str(exc))),
                "error": str(exc)[:300]}


def _lxc_app_pending(
    executor: Executor,
    lxc_id: str,
    os_type: str,
    introspect_facts: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], str]:
    """App version current → latest for a community-script container.

    Returns ``(app, ct_info, script_name)``. *app* is None when the container has
    no update script (os_only containers). *ct_info* and *script_name* are handed
    back so the caller can run the OS-target check without re-fetching the script.
    Mirrors the lxc flow's dry-check: script name from the pulled
    ``/usr/bin/update``, latest tag from the ct script's GitHub repo.
    """
    if int(introspect_facts.get("pull_rc", 1)) != 0:
        return None, {}, ""
    script_name = script_name_from_update(str(introspect_facts.get("script_stdout", ""))) or ""
    if not script_name:
        return None, {}, ""

    ct_info: Dict[str, Any] = {}
    gh_repo = ""
    try:
        ct_url = (f"https://raw.githubusercontent.com/community-scripts/ProxmoxVE"
                  f"/main/ct/{script_name}.sh")
        ct_info = parse_ct_script(http_mod.request(ct_url).body)
        gh_repo = ct_info.get("gh_repo", "")
    except Exception:  # noqa: BLE001 - fail-open like the flow's detect
        pass

    current = _read_version(executor, lxc_id, script_name, os_type)
    latest = ""
    if gh_repo:
        try:
            data = http_mod.get_json(
                f"https://api.github.com/repos/{gh_repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            latest = str((data or {}).get("tag_name", "")).strip()
        except Exception:  # noqa: BLE001
            pass

    outdated = bool(current and latest
                    and current.lstrip("v") != latest.lstrip("v"))
    return ({"script": script_name, "current": current, "latest": latest,
             "outdated": outdated}, ct_info, script_name)


def _empty_lxc_entry(node: str, lxc_id: str) -> Dict[str, Any]:
    """The full shape of a pending-scan lxc entry, with nothing filled in.

    Both scan_lxc() and run_fleet_scan()'s failure fallbacks start from this, so
    a key added here can never reach only one of them (which is exactly how
    disk_percent/os/os_mismatch previously went missing from the error path).
    """
    return {"node": node, "id": str(lxc_id), "name": str(lxc_id), "skipped": None,
            "os_pending_count": 0, "os_pending": [], "app": None,
            "disk_percent": None, "os": "", "os_mismatch": None,
            "unreachable": False, "error": None}


def scan_lxc(executor: Executor, lxc_id: str, node: str) -> Dict[str, Any]:
    """Scan one container on its node: pending OS packages + app version.

    Stopped containers and templates are skipped (a scan never starts a CT).
    Never raises — errors land in the returned dict's ``error`` key.
    """
    out: Dict[str, Any] = _empty_lxc_entry(node, lxc_id)
    try:
        intro = executor.introspect(lxc_id)
        info = parse_pct_config(str(intro.facts.get("config_stdout", "")))
        out["name"] = info["name"] or lxc_id
        if info["is_template"]:
            out["skipped"] = "template"
            return out
        if not parse_pct_status(str(intro.facts.get("status_stdout", "")))["is_running"]:
            out["skipped"] = "stopped"
            return out

        os_type = info["os_type"]
        pkg_mgr = _OSTYPE_PKG_MGR.get(os_type, "dnf")
        shell = _build_shell(os_type)
        res = executor.run_shell(
            f"pct exec {lxc_id} -- {shell} -c '{scan_cmd(pkg_mgr)}'",
            changed_when=False, ignore_errors=True,
        )
        pending = parse_pending(res.stdout, pkg_mgr)
        out["os_pending_count"] = len(pending)
        out["os_pending"] = pending
        app, ct_info, script_name = _lxc_app_pending(executor, lxc_id, os_type, intro.facts)
        out["app"] = app

        # Health signals — read from the same introspect pass, no extra commands.
        out["disk_percent"] = parse_df_percent(str(intro.facts.get("df_stdout", "")))
        cur_os = parse_os_release(str(intro.facts.get("os_release_stdout", "")))
        out["os"] = f"{cur_os['id']} {cur_os['version_id']}".strip()
        if script_name:
            out["os_mismatch"] = os_mismatch_warning(intro.facts, ct_info, script_name)
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:300]
        out["unreachable"] = isinstance(exc, UnreachableHostError) or is_unreachable_error(str(exc))
        return out


def write_pending(
    scan: Dict[str, Any],
    *,
    history_dir: Union[str, Path],
    keep: int = 30,
) -> Path:
    """Write ``pending-<ts>.json`` + ``pending-latest.json``, prune to *keep*.

    Same conventions as history.write_history() so the readers/dashboard can
    treat both file families uniformly.
    """
    directory = Path(history_dir)
    directory.mkdir(parents=True, exist_ok=True)
    ts = str(scan.get("timestamp", ""))
    payload = json.dumps(scan, indent=4, sort_keys=True, ensure_ascii=False)
    scan_file = directory / f"pending-{ts}.json"
    scan_file.write_text(payload, encoding="utf-8")
    (directory / "pending-latest.json").write_text(payload, encoding="utf-8")
    if keep > 0:
        for stale in sorted(directory.glob("pending-*.json"))[:-(keep + 1)]:
            if stale.name != "pending-latest.json":
                stale.unlink(missing_ok=True)
    return scan_file


def pending_summary(
    history_dir: Union[str, Path],
    *,
    limit: int = 10,
    disk_threshold: int = 75,
) -> List[Dict[str, Any]]:
    """Read back the newest *limit* pending-scan summaries, newest first.

    Mirrors :func:`proxmox_fleet.history.history_summary`: one row per scan
    with the table-level aggregates, unreadable/corrupt files skipped,
    ``limit <= 0`` meaning "all scans". ``pending-latest.json`` is excluded
    (it duplicates the newest timestamped file).

    *disk_threshold* counts containers at or above that rootfs percentage; pass
    ``settings.lxc_disk_warn_percent`` to keep the scan page and the briefing
    warnings agreeing on what "low" means.
    """
    directory = Path(history_dir)
    scans = sorted(
        (p for p in directory.glob("pending-*.json") if p.name != "pending-latest.json"),
        reverse=True,
    )
    if limit > 0:
        scans = scans[:limit]

    out: List[Dict[str, Any]] = []
    for scan_file in scans:
        try:
            data = json.loads(scan_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        hosts = data.get("hosts", {}) or {}
        lxc = data.get("lxc", {}) or {}
        out.append({
            # scan_file.stem is "pending-<ts>" — strip the prefix as a fallback.
            "timestamp": data.get("timestamp", scan_file.stem[8:]),
            "hosts_pending": sum(int(h.get("pending_count", 0)) for h in hosts.values()),
            "lxc_os_pending": sum(int(c.get("os_pending_count", 0)) for c in lxc.values()),
            "outdated_apps": sum(1 for c in lxc.values()
                                 if (c.get("app") or {}).get("outdated")),
            # Health signals — containers that need attention before they fail.
            # Read defensively: scans written before these keys existed lack them.
            "low_disk": sum(1 for c in lxc.values()
                            if (c.get("disk_percent") or 0) >= disk_threshold),
            "os_mismatch": sum(1 for c in lxc.values() if c.get("os_mismatch")),
            # Unreachable hosts are reported separately: they are "could not
            # look", not a broken scan, and they do not fail the run either.
            "unreachable": sum(1 for entry in (*hosts.values(), *lxc.values())
                               if entry.get("unreachable")),
            "errors": sum(1 for entry in (*hosts.values(), *lxc.values())
                          if entry.get("error") and not entry.get("unreachable")),
        })
    return out


def read_pending(
    history_dir: Union[str, Path],
    ref: str = "latest",
) -> Dict[str, Any]:
    """Read one persisted pending-scan snapshot by reference.

    *ref* is ``latest`` (→ ``pending-latest.json``) or a scan timestamp — bare,
    ``pending-<ts>`` prefixed, or a full filename. Raises ``FileNotFoundError``
    when absent (parity with :func:`proxmox_fleet.history.read_run`).
    """
    directory = Path(history_dir)
    if ref == "latest":
        path = directory / "pending-latest.json"
    else:
        name = ref if ref.startswith("pending-") else f"pending-{ref}"
        if not name.endswith(".json"):
            name += ".json"
        path = directory / name
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unexpected pending payload in {path}: not a JSON object")
    return data


def run_fleet_scan(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    limit: Optional[Set[str]] = None,
) -> int:
    """Walk the fleet read-only and persist the pending-updates snapshot.

    Covers [remote_hosts], [proxmox_vms] (guest SSH), [proxmox_nodes] (node OS
    + per-container scans). ``--limit`` semantics match run_fleet(). Returns
    exit code 1 when any host/container scan recorded an error, else 0.
    """
    from proxmox_fleet.executor import RunnerExecutor  # lazy: needs ansible-runner

    remote = inventory.load_remote_hosts(inventory_path, host_vars_dir=settings.host_vars_dir)
    vms = inventory.load_proxmox_vms(inventory_path, host_vars_dir=settings.host_vars_dir)
    nodes = inventory.load_proxmox_nodes(inventory_path, host_vars_dir=settings.host_vars_dir)
    inventory.validate_node_uniqueness(nodes)
    node_clusters = {n["name"]: n["cluster"] for n in nodes}

    def _vm_cluster_hint(v: inventory.VmSpec) -> str:
        # Same cheap pre-execution guess as the driver's VM --limit filter:
        # explicit cluster= var, else the pve_node hint's cluster, else default.
        if v.cluster:
            return v.cluster
        if v.pve_node:
            return node_clusters.get(v.pve_node, DEFAULT_CLUSTER)
        return DEFAULT_CLUSTER

    limit_has_ids = limit is not None and any(token_is_id(t) for t in limit)
    if limit is not None:
        remote = [h for h in remote if h.name in limit]
        vms = [v for v in vms
               if v.name in limit or limit_selects_id(limit, _vm_cluster_hint(v), v.vmid)]

    scan: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
        "hosts": {},
        "lxc": {},
    }
    failed = False

    def _scan_named(kind: str, name: str) -> Dict[str, Any]:
        ex = RunnerExecutor(name, inventory=inventory_path, check=False)
        result = scan_host(ex)
        result["kind"] = kind
        return result

    targets = ([("remote", h.name) for h in remote]
               + [("vm", v.name) for v in vms]
               + [("node", n["name"]) for n in nodes
                  if limit is None or n["name"] in limit])

    results = run_concurrent(targets, lambda t: _scan_named(*t),
                             max_workers=settings.remote_forks)
    for (kind, name), result, run_err in results:
        if result is None:
            result = {"kind": kind, "pkg_mgr": "", "pending_count": 0,
                      "pending": [],
                      "unreachable": is_unreachable_error(str(run_err)),
                      "error": str(run_err)[:300]}
        scan["hosts"][name] = result
        # An unreachable host is "could not look", not "the scan is broken" —
        # a powered-off or down node must not make a read-only scan exit 1.
        if result.get("error") and not result.get("unreachable"):
            failed = True
        if result.get("unreachable"):
            print(f"  [{name}] unreachable — skipped")
        else:
            err = f"  ERROR: {result['error']}" if result.get("error") else ""
            print(f"  [{name}] {result['pending_count']} OS package(s) pending{err}")

    # Per-node container scans (same node-targeting rules as the lxc phase).
    for node_info in nodes:
        node_name = node_info["name"]
        if limit is not None and node_name not in limit and not limit_has_ids:
            continue
        node_cluster = node_info["cluster"]
        ex = RunnerExecutor(node_name, inventory=inventory_path, check=False)
        print(f"[{node_name}] discovering LXCs...")
        try:
            lxc_ids = _discover_lxcs(ex, settings, cluster=node_cluster)
        except Exception as exc:  # noqa: BLE001
            entry = _empty_lxc_entry(node_name, node_name)
            entry["error"] = f"discovery failed: {exc}"[:300]
            entry["unreachable"] = (isinstance(exc, UnreachableHostError)
                                    or is_unreachable_error(str(exc)))
            if entry["unreachable"]:
                # Same rule as the update path: a node that never answered is
                # skipped, not failed. No quorum check — a read-only scan takes
                # no snapshots, so pmxcfs going read-only cannot affect it.
                entry["skipped"] = "unreachable"
                print(f"[{node_name}] unreachable — containers skipped")
            else:
                failed = True
            scan["lxc"][node_name] = entry
            continue
        if limit is not None and node_name not in limit:
            lxc_ids = [i for i in lxc_ids if limit_selects_id(limit, node_cluster, i)]

        def _scan_one(lxc_id: str, _node: str = node_name) -> Dict[str, Any]:
            cex = RunnerExecutor(_node, inventory=inventory_path, check=False)
            return scan_lxc(cex, lxc_id, _node)

        for lxc_id, result, run_err in run_concurrent(
                lxc_ids, _scan_one, max_workers=settings.lxc_forks):
            if result is None:
                result = _empty_lxc_entry(node_name, lxc_id)
                result["error"] = str(run_err)[:300]
                result["unreachable"] = is_unreachable_error(str(run_err))
            # Keyed by node/id — a bare vmid is not unique across clusters, and
            # two same-id containers must not overwrite each other in the JSON.
            scan["lxc"][f"{node_name}/{lxc_id}"] = result
            if result.get("error") and not result.get("unreachable"):
                failed = True
            app = result.get("app")
            app_str = ""
            if app:
                marker = " (outdated)" if app["outdated"] else ""
                app_str = f"  app {app['current'] or '?'} → {app['latest'] or '?'}{marker}"
            skip_str = f"  skipped ({result['skipped']})" if result.get("skipped") else ""
            err_str = f"  ERROR: {result['error']}" if result.get("error") else ""
            print(f"  [{node_name}/{lxc_id}] {result['name']}: "
                  f"os={result['os_pending_count']}{app_str}{skip_str}{err_str}")

    if settings.fleet_history_enabled:
        path = write_pending(scan, history_dir=settings.fleet_history_dir,
                             keep=settings.scan_history_keep)
        print(f"pending snapshot written to {path}")

    return 1 if failed else 0
