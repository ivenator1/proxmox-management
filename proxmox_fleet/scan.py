"""Pending-updates scan — the read-only fleet walk behind ``fleet-update --scan``.

Collects what *would* update without changing anything: pending OS packages for
remote hosts, VMs, and Proxmox nodes (simulate commands per package manager),
for managed LXCs both pending OS packages and community-script app versions,
and fixed vendor checks for appliances in ``[manual_update_hosts]``. Manual
systems are reported for GUI action only and never enter mutating fleet phases.

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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, Union

from proxmox_fleet import http as http_mod
from proxmox_fleet import inventory
from proxmox_fleet import ledger
from proxmox_fleet import manual_updates, scan_notifications
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
    # The LXC helper returns {id, version_id}; the scan-local parse_os_release
    # below extends it with pretty_name. Aliased so the two never shadow each
    # other — lxc_parse's stays byte-identical for flows/lxc.py and its tests.
    parse_os_release as _lxc_parse_os_release,
    parse_pct_config,
    parse_pct_status,
    script_name_from_update,
)
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.orchestration import run_concurrent
from proxmox_fleet.pkg_detail import pkg_mgr_for_ostype
from proxmox_fleet.runner import UnreachableHostError, is_unreachable_error


# Sentinel markers delimiting the scan output sections inside one command.
# They split the pending table from the dnf ``--security`` run, and both from
# the machine-readable tail (reboot flag + /etc/os-release capture). They are
# parse_scan_output()'s contract — chosen to be unreachable in real command
# output, so splitting on them is safe. No single quote may appear anywhere in
# the commands: scan_lxc wraps the whole thing in ``pct exec <id> -- <shell>
# -c '<cmd>'``, so an embedded ``'`` breaks the shell line (the old apk
# ``-l '<'`` form did exactly that — bug #2, fixed by using ``"<"`` instead).
_SEC_SENTINEL = "__FLEET_SEC__"
_META_SENTINEL = "__FLEET_META__"
_REBOOT_MARKER = "reboot_required"


def _as_int(value: Any, default: int = 0) -> int:
    """Best-effort integer coercion for runner facts and legacy snapshots."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def scan_cmd(pkg_mgr: str) -> str:
    """Read-only pending-updates command per package manager (``LC_ALL=C``).

    apt reuses the dry-run simulate (``-s dist-upgrade``); dnf uses
    ``check-update`` (built for exactly this, exits 100 when updates exist —
    callers must ignore the rc); apk lists upgradable packages.

    Everything rides in one command — scan roundtrips are whole ansible-runner
    subprocesses, so a second call would double the SSH overhead. After the
    primary scan output the command emits sentinel-delimited sections:
    ``__FLEET_SEC__`` (dnf only: the ``--security`` table) and ``__FLEET_META__``
    (reboot flag + ``/etc/os-release``, parsed by :func:`parse_scan_output`).
    ``exit $rc`` re-raises the scan section's real exit code so scan_host's
    failed/rc checks still see it while the metadata tail stays best-effort.
    """
    # Shared tail: mark the metadata section, report the reboot-required flag
    # (Debian's /var/run/reboot-required sentinel file — apk hosts skip this:
    # Alpine has no such concept), capture /etc/os-release, then exit with the
    # preserved scan rc. ``test -f ... && echo ...`` prints nothing (and exits
    # 1, discarded by the following ``;``) when no reboot is needed.
    meta_tail = (
        f"echo {_META_SENTINEL}; "
        f"test -f /var/run/reboot-required && echo {_REBOOT_MARKER}; "
        f"cat /etc/os-release 2>/dev/null; exit $rc"
    )
    if pkg_mgr == "apt":
        prefix = "LC_ALL=C DEBIAN_FRONTEND=noninteractive"
        scan = f"{prefix} apt-get update -qq && {prefix} apt-get -s dist-upgrade"
        return f"{scan}; rc=$?; {meta_tail}"
    if pkg_mgr == "dnf":
        sec = "LC_ALL=C dnf -q check-update --security"
        # rc comes from the *first* check-update (100 when anything is
        # pending); the --security run also exits 100 when security updates
        # exist, so its rc is deliberately discarded.
        return f"LC_ALL=C dnf -q check-update; rc=$?; echo {_SEC_SENTINEL}; {sec}; {meta_tail}"
    if pkg_mgr == "apk":
        scan = 'LC_ALL=C apk update -q >/dev/null 2>&1; LC_ALL=C apk version -l "<"'
        # No reboot check and no security section: Alpine has neither concept,
        # so parse_scan_output defaults them (security=[] / reboot False).
        apk_tail = f"echo {_META_SENTINEL}; cat /etc/os-release 2>/dev/null; exit $rc"
        return f"{scan}; rc=$?; {apk_tail}"
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


def parse_os_release(text: str) -> Dict[str, str]:
    """Extract {id, version_id, pretty_name} from ``/etc/os-release`` contents.

    Extends the LXC helper (aliased ``_lxc_parse_os_release`` at import) with
    ``pretty_name`` — the human display string (e.g. ``Debian GNU/Linux 12
    (bookworm)``) that the dashboard surfaces. Values may or may not be
    quoted; missing fields come back as ``""``.
    """
    base = _lxc_parse_os_release(text)
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)"?$', text, re.MULTILINE)
    base["pretty_name"] = m.group(1).strip() if m else ""
    return base


_APT_ARCHIVE_RE = re.compile(
    r"^Inst (\S+?)(?::\S+)?\s+(?:\[[^]]*\]\s+)?\([^)\s]+\s+(\S+)",
    re.MULTILINE,
)


def _apt_security_names(stdout: str) -> List[str]:
    """Pending package names whose simulate archive is a ``*-security`` suite.

    apt marks security updates by archive: ``Debian-Security:12/stable-security``
    or ``Ubuntu:24.04-security`` — the second word inside the Inst line's
    parens, after the version. Detection needs no extra command: it reads the
    same simulate output that produced the pending list.
    """
    names = [m.group(1) for m in _APT_ARCHIVE_RE.finditer(stdout) if "-security" in m.group(2)]
    return list(dict.fromkeys(names))


def parse_scan_output(stdout: str, pkg_mgr: str) -> Dict[str, Any]:
    """Split one :func:`scan_cmd` output into its sentinel-delimited sections.

    Returns ``{"pending", "security", "reboot_required", "os_release"}``:

    - ``pending``: package names from the primary scan section (before the
      first sentinel) — exactly what :func:`parse_pending` would extract.
    - ``security``: the security subset — for dnf the ``--security`` table
      between ``__FLEET_SEC__`` and ``__FLEET_META__``; for apt the subset of
      pending Inst lines whose archive is a ``*-security`` suite; apk always
      ``[]`` (no such concept).
    - ``reboot_required``: whether the metadata tail carried the
      ``reboot_required`` marker (never for apk — no such concept).
    - ``os_release``: ``{id, version_id, pretty_name}`` parsed from the tail's
      ``/etc/os-release`` capture (empty strings when absent).

    Section-missing outputs (older commands, partial captures, mocks) degrade
    to defaults: the whole output becomes the pending section, security is
    ``[]``, no reboot, empty os_release — never an exception.
    """
    lines = stdout.splitlines()
    sec_idx = next((i for i, line in enumerate(lines) if line.strip() == _SEC_SENTINEL), None)
    meta_idx = next((i for i, line in enumerate(lines) if line.strip() == _META_SENTINEL), None)

    end = len(lines)
    pending_end = sec_idx if sec_idx is not None else (meta_idx if meta_idx is not None else end)
    pending_lines = lines[:pending_end]
    if sec_idx is not None:
        security_lines = lines[sec_idx + 1 : meta_idx if meta_idx is not None else end]
    else:
        security_lines = []
    meta_lines = lines[meta_idx + 1 :] if meta_idx is not None else []

    pending = parse_pending("\n".join(pending_lines), pkg_mgr)
    if pkg_mgr == "dnf":
        security = parse_pending("\n".join(security_lines), "dnf")
    elif pkg_mgr == "apt":
        security = _apt_security_names("\n".join(pending_lines))
    else:
        security = []
    return {
        "pending": pending,
        "security": security,
        "reboot_required": any(line.strip() == _REBOOT_MARKER for line in meta_lines),
        "os_release": parse_os_release("\n".join(meta_lines)),
    }


def _empty_host_entry() -> Dict[str, Any]:
    """The full shape of a pending-scan host entry, with nothing filled in.

    Both scan_host()'s error path and run_fleet_scan()'s failure fallback
    start from this, so a key added here can never reach only one of them
    (the same drift guard as _empty_lxc_entry for containers).
    """
    return {
        "pkg_mgr": "",
        "pending_count": 0,
        "pending": [],
        "security_count": 0,
        "security": [],
        "reboot_required": False,
        "os_release": {"id": "", "version_id": "", "pretty_name": ""},
        "unreachable": False,
        "error": None,
    }


def scan_host(executor: Executor) -> Dict[str, Any]:
    """Scan one SSH-reachable host: detect the package manager, list pending.

    Also reports the security subset, the reboot-required flag and the host's
    os-release — all read from the same single command (see
    :func:`parse_scan_output`). Never raises — errors land in the returned
    dict's ``error`` key with the entry otherwise shaped by
    :func:`_empty_host_entry`.
    """
    try:
        pkg_mgr = detect_pkg_mgr(executor)
        # ignore_errors: dnf check-update exits 100 when updates are pending.
        res = executor.run_shell(scan_cmd(pkg_mgr), changed_when=False, ignore_errors=True)
        if res.failed and not (pkg_mgr == "dnf" and res.rc == 100):
            raise RuntimeError(f"scan command failed (rc={res.rc}): {res.stderr or res.stdout}"[:300])
        parsed = parse_scan_output(res.stdout, pkg_mgr)
        pending = parsed["pending"]
        return {
            "pkg_mgr": pkg_mgr,
            "pending_count": len(pending),
            "pending": pending,
            "security_count": len(parsed["security"]),
            "security": parsed["security"],
            "reboot_required": parsed["reboot_required"],
            "os_release": parsed["os_release"],
            "unreachable": False,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - a scan must never abort the walk
        out = _empty_host_entry()
        out["unreachable"] = isinstance(exc, UnreachableHostError) or is_unreachable_error(str(exc))
        out["error"] = str(exc)[:300]
        return out


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
    if _as_int(introspect_facts.get("pull_rc", 1), 1) != 0:
        return None, {}, ""
    script_name = script_name_from_update(str(introspect_facts.get("script_stdout", ""))) or ""
    if not script_name:
        return None, {}, ""

    ct_info: Dict[str, Any] = {}
    gh_repo = ""
    try:
        ct_url = f"https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/{script_name}.sh"
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

    outdated = bool(current and latest and current.lstrip("v") != latest.lstrip("v"))
    return ({"script": script_name, "current": current, "latest": latest, "outdated": outdated}, ct_info, script_name)


def _empty_lxc_entry(node: str, lxc_id: str) -> Dict[str, Any]:
    """The full shape of a pending-scan lxc entry, with nothing filled in.

    Both scan_lxc() and run_fleet_scan()'s failure fallbacks start from this, so
    a key added here can never reach only one of them (which is exactly how
    disk_percent/os/os_mismatch previously went missing from the error path).
    """
    return {
        "node": node,
        "id": str(lxc_id),
        "name": str(lxc_id),
        "skipped": None,
        "os_pending_count": 0,
        "os_pending": [],
        "app": None,
        "os_security_count": 0,
        "os_security": [],
        "reboot_required": False,
        "os_release": {"id": "", "version_id": "", "pretty_name": ""},
        "disk_percent": None,
        "os": "",
        "os_mismatch": None,
        "unreachable": False,
        "error": None,
    }


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
        pkg_mgr = pkg_mgr_for_ostype(os_type)
        shell = _build_shell(os_type)
        res = executor.run_shell(
            f"pct exec {lxc_id} -- {shell} -c '{scan_cmd(pkg_mgr)}'",
            changed_when=False,
            ignore_errors=True,
        )
        if res.failed and not (pkg_mgr == "dnf" and res.rc == 100):
            raise RuntimeError(f"scan command failed (rc={res.rc}): {res.stderr or res.stdout}"[:300])
        parsed = parse_scan_output(res.stdout, pkg_mgr)
        pending = parsed["pending"]
        out["os_pending_count"] = len(pending)
        out["os_pending"] = pending
        out["os_security_count"] = len(parsed["security"])
        out["os_security"] = parsed["security"]
        out["reboot_required"] = parsed["reboot_required"]
        app, ct_info, script_name = _lxc_app_pending(executor, lxc_id, os_type, intro.facts)
        out["app"] = app

        # Health signals — read from the same introspect pass, no extra commands.
        out["disk_percent"] = parse_df_percent(str(intro.facts.get("df_stdout", "")))
        cur_os = _lxc_parse_os_release(str(intro.facts.get("os_release_stdout", "")))
        out["os"] = f"{cur_os['id']} {cur_os['version_id']}".strip()
        out["os_release"] = parsed["os_release"]
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
    # Feed the per-host ledger BEFORE pruning: every scanned host/container
    # folds into hosts.json regardless of how many pending files are later
    # retained. Never raises — the ledger must not fail a scan.
    ledger.observe_scan(directory, scan)
    if keep > 0:
        for stale in sorted(directory.glob("pending-*.json"))[: -(keep + 1)]:
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
        def _entries(value: Any) -> Tuple[Mapping[str, Any], ...]:
            if not isinstance(value, Mapping):
                return ()
            return tuple(entry for entry in value.values() if isinstance(entry, Mapping))

        hosts = _entries(data.get("hosts"))
        lxc = _entries(data.get("lxc"))
        manual = _entries(data.get("manual"))
        all_entries = (*hosts, *lxc, *manual)
        out.append(
            {
                # scan_file.stem is "pending-<ts>" — strip the prefix as a fallback.
                "timestamp": data.get("timestamp", scan_file.stem[8:]),
                "hosts_pending": sum(_as_int(h.get("pending_count", 0)) for h in hosts),
                "lxc_os_pending": sum(_as_int(c.get("os_pending_count", 0)) for c in lxc),
                "outdated_apps": sum(1 for c in lxc if (c.get("app") or {}).get("outdated")),
                # Manual appliances need operator action, not a fleet-applied update.
                # Legacy snapshots have no manual mapping and therefore count zero.
                "manual_updates": sum(1 for entry in manual if entry.get("update_available")),
                "manual_reboots": sum(1 for entry in manual if entry.get("reboot_required")),
                # Security + reboot — read defensively: scans written before these
                # keys existed (or on apk hosts, which track neither) lack them.
                "security_pending": (
                    sum(_as_int(h.get("security_count", 0)) for h in hosts)
                    + sum(_as_int(c.get("os_security_count", 0)) for c in lxc)
                ),
                "reboot_hosts": sum(1 for entry in (*hosts, *lxc) if entry.get("reboot_required")),
                # Health signals — containers that need attention before they fail.
                # Read defensively: scans written before these keys existed lack them.
                "low_disk": sum(1 for c in lxc if (c.get("disk_percent") or 0) >= disk_threshold),
                "os_mismatch": sum(1 for c in lxc if c.get("os_mismatch")),
                # Unreachable hosts are reported separately: they are "could not
                # look", not a broken scan, and they do not fail the run either.
                "unreachable": sum(1 for entry in all_entries if entry.get("unreachable")),
                "errors": sum(
                    1
                    for entry in all_entries
                    if entry.get("error") and not entry.get("unreachable")
                ),
            }
        )
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
    + per-container scans), and read-only adapters for [manual_update_hosts].
    ``--limit`` semantics match run_fleet(). Returns exit code 1 when any
    reachable target records a genuine check error; unreachable targets are
    persisted as skipped and do not fail the scan.
    """
    # Safety pre-flight happens before the first executor is constructed. A
    # manual appliance may never also be eligible for an automated phase, and
    # every adapter name/command invariant must be known before host contact.
    inventory.validate_manual_update_overlap(inventory_path)
    manual_hosts = inventory.load_manual_update_hosts(
        inventory_path, host_vars_dir=settings.host_vars_dir
    )

    def _api_config(host: inventory.ManualUpdateHostSpec) -> manual_updates.ManualUpdateApiConfig:
        """Per-host connection settings for the *_api manual-update adapters.

        A blank ``api_url`` falls back to the SSH reachability address
        (``ansible_host``) — the appliance's web UI usually lives on the same
        interface; override with ``api_url`` when it does not.
        """
        return manual_updates.ManualUpdateApiConfig(
            api_url=host.api_url or f"https://{host.ansible_host}",
            api_key=host.api_key or "",
            api_secret=host.api_secret or "",
            verify_ssl=host.verify_ssl,
            timeout=settings.manual_update_api_timeout,
        )

    for host in manual_hosts:
        try:
            adapter = manual_updates.MANUAL_UPDATE_REGISTRY.get(host.manual_adapter)
            adapter.validate(_api_config(host) if adapter.transport == "api" else None)
        except manual_updates.ManualUpdateError as exc:
            raise SystemExit(
                f"manual_update_hosts entry '{host.name}' is invalid: {exc}"
            ) from exc

    remote = inventory.load_remote_hosts(inventory_path, host_vars_dir=settings.host_vars_dir)
    vms = inventory.load_proxmox_vms(inventory_path, host_vars_dir=settings.host_vars_dir)
    nodes = inventory.load_proxmox_nodes(inventory_path, host_vars_dir=settings.host_vars_dir)
    inventory.validate_node_uniqueness(nodes)

    from proxmox_fleet.executor import RunnerExecutor  # lazy: needs ansible-runner
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
        vms = [v for v in vms if v.name in limit or limit_selects_id(limit, _vm_cluster_hint(v), v.vmid)]
        manual_hosts = [h for h in manual_hosts if h.name in limit]

    scan: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
        "hosts": {},
        "lxc": {},
        "manual": {},
    }
    failed = False

    def _scan_named(kind: str, name: str) -> Dict[str, Any]:
        ex = RunnerExecutor(name, inventory=inventory_path, check=False)
        result = scan_host(ex)
        result["kind"] = kind
        return result

    targets = (
        [("remote", h.name) for h in remote]
        + [("vm", v.name) for v in vms]
        + [("node", n["name"]) for n in nodes if limit is None or n["name"] in limit]
    )

    results = run_concurrent(targets, lambda t: _scan_named(*t), max_workers=settings.remote_forks)
    for (kind, name), result, run_err in results:
        if result is None:
            result = _empty_host_entry()
            result["kind"] = kind
            result["unreachable"] = is_unreachable_error(str(run_err))
            result["error"] = str(run_err)[:300]
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

    def _scan_manual(host: inventory.ManualUpdateHostSpec) -> Dict[str, Any]:
        adapter = manual_updates.MANUAL_UPDATE_REGISTRY.get(host.manual_adapter)
        config = _api_config(host) if adapter.transport == "api" else None
        # API-transport adapters run manager-side HTTPS and never need an
        # executor; SSH adapters still get one bound to the inventory.
        ex = (
            None
            if config is not None
            else RunnerExecutor(host.name, inventory=inventory_path, check=False)
        )
        result = manual_updates.run_manual_update_check(
            host.manual_adapter, ex, host.name, config=config
        )
        if host.display_name:
            result.display_name = host.display_name
        if host.apply_hint:
            result.apply_hint = host.apply_hint
        entry = asdict(result)
        # Match the rest of the persisted snapshot: successful checks carry a
        # JSON null error rather than an empty display string.
        entry["error"] = entry.get("error") or None
        return entry

    for host, result, run_err in run_concurrent(
        manual_hosts, _scan_manual, max_workers=settings.manual_update_forks
    ):
        if result is None:
            adapter = manual_updates.MANUAL_UPDATE_REGISTRY.get(host.manual_adapter)
            result = asdict(
                manual_updates.ManualUpdateResult(
                    host=host.name,
                    display_name=host.display_name or adapter.display_name,
                    adapter=host.manual_adapter,
                    apply_hint=host.apply_hint or adapter.apply_hint,
                    unreachable=is_unreachable_error(str(run_err)),
                    error=str(run_err)[:300],
                )
            )
        scan["manual"][host.name] = result
        if result.get("error") and not result.get("unreachable"):
            failed = True
        if result.get("unreachable"):
            reason = str(result.get("error") or "").strip()
            suffix = f" — {reason[:160]}" if reason else ""
            print(f"  [{host.name}] manual check unreachable — skipped{suffix}")
        elif result.get("error"):
            print(f"  [{host.name}] manual check ERROR: {result['error']}")
        elif result.get("update_available") or result.get("reboot_required"):
            current = result.get("current") or "?"
            latest = result.get("latest") or current
            print(
                f"  [{host.name}] {current} → {latest}; manual apply: "
                f"{result.get('apply_hint') or 'vendor UI'}"
            )
        else:
            print(f"  [{host.name}] current ({result.get('current') or 'version unknown'})")

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
            entry["unreachable"] = isinstance(exc, UnreachableHostError) or is_unreachable_error(str(exc))
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

        for lxc_id, result, run_err in run_concurrent(lxc_ids, _scan_one, max_workers=settings.lxc_forks):
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
            print(
                f"  [{node_name}/{lxc_id}] {result['name']}: "
                f"os={result['os_pending_count']}{app_str}{skip_str}{err_str}"
            )

    if settings.fleet_history_enabled:
        path = write_pending(scan, history_dir=settings.fleet_history_dir, keep=settings.scan_history_keep)
        print(f"pending snapshot written to {path}")

    if settings.manual_update_notifications:
        # Scans refresh dashboard and reminder state only. Due manual entries
        # piggyback on the next ordinary fleet briefing in Phase 4.
        scan_notifications.record_manual_results(
            list(scan["manual"].values()),
            history_dir=settings.fleet_history_dir,
        )

    return 1 if failed else 0
