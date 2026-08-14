"""Inventory parser — reads [custom_hosts] from hosts.ini + host_vars/ per host.

Pure stdlib + PyYAML, no ansible-runner required. Preserves inventory order
so the caller's serial loop respects the Phase 0a ordering guarantee.

Uses manual line-by-line parsing instead of configparser because hosts.ini
host lines have the form ``hostname key=val key=val …`` which configparser
would mis-parse (splitting on the first ``=`` makes the key ``hostname key``).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

from proxmox_fleet.cluster import DEFAULT_CLUSTER
from proxmox_fleet.models.config import MaintenanceWindow

# Matches: hostname  key=value key=value …
_HOST_LINE = re.compile(r"^(\S+)\s*(.*)")
# Matches individual key=value pairs in the remainder of a host line.
_KV_PAIR = re.compile(r"(\S+)=(\S+)")


@dataclass
class HostSpec:
    """All driver-relevant data for one [custom_hosts] entry."""

    name: str
    ansible_host: str
    custom_config: str
    depends_on: List[str] = field(default_factory=list)
    maintenance_window: Optional[MaintenanceWindow] = None
    custom_overrides: Dict[str, Any] = field(default_factory=dict)


def _parse_inline_vars(remainder: str) -> Dict[str, str]:
    """Split 'key=value key=value …' into a dict."""
    return {m.group(1): m.group(2) for m in _KV_PAIR.finditer(remainder)}


def _as_bool(val: Any) -> bool:
    """Coerce an inline-var string or host_vars YAML value to bool.

    Inline vars arrive as raw strings ('true'/'false'), where bool() would be
    wrong; host_vars YAML gives real booleans.
    """
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


def _load_host_vars(host: str, host_vars_dir: Path) -> Dict[str, Any]:
    """Read host_vars/<host>.yml if it exists; return {} otherwise."""
    candidate = host_vars_dir / f"{host}.yml"
    if candidate.is_file():
        return yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    return {}


def _iter_section(inventory_path: str, section: str) -> Iterator[Tuple[str, Dict[str, str]]]:
    """Yield ``(host_name, inline_vars)`` for each host line in ``[section]``.

    Comment/blank lines are skipped; a missing file or missing section yields
    nothing. Hosts are yielded in inventory-file order (the Phase 0a ordering
    guarantee for custom_hosts). This is the single source of section parsing —
    each loader merges host_vars on top of the inline vars it returns.
    """
    try:
        lines = Path(inventory_path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    in_section = False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("["):
            in_section = line.strip("[]") == section
            continue
        if not in_section:
            continue
        m = _HOST_LINE.match(line)
        if not m:
            continue
        yield m.group(1), _parse_inline_vars(m.group(2))


def load_proxmox_nodes(
    inventory_path: str = "hosts.ini",
    *,
    host_vars_dir: str = "host_vars",
) -> List[Dict[str, Any]]:
    """Parse ``[proxmox_nodes]`` from *inventory_path* and merge per-host vars.

    Returns a list of ``{name, ansible_host, cluster, nvidia_host}`` dicts in
    inventory order. ansible_host resolution order: inline var →
    host_vars/<node>.yml → node name. Merging host_vars matters because
    ``ansible_host`` becomes the snapshot API ``api_host`` (which must be an IP,
    not the inventory name). ``cluster`` resolves the same way, falling back to
    DEFAULT_CLUSTER — single-cluster inventories need no ``cluster=`` vars at
    all. ``nvidia_host`` is a real bool (inline string or host_vars YAML value),
    default False — it gates the read-only NVIDIA post-upgrade driver checks.
    """
    hvdir = Path(host_vars_dir)
    nodes: List[Dict[str, Any]] = []
    for name, inline in _iter_section(inventory_path, "proxmox_nodes"):
        host_vars = _load_host_vars(name, hvdir)
        nodes.append({
            "name": name,
            "ansible_host": str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
            "cluster": str(inline.get("cluster", host_vars.get("cluster", DEFAULT_CLUSTER))),
            "nvidia_host": _as_bool(inline.get("nvidia_host", host_vars.get("nvidia_host", False))),
        })
    return nodes


def validate_node_uniqueness(nodes: List[Dict[str, Any]]) -> None:
    """Fail loud on duplicate node names; warn on cross-cluster IP reuse.

    Node names are the join key everywhere (records, briefing grouping, the
    dashboard host pages, node→cluster maps) — two [proxmox_nodes] entries
    sharing a name would silently merge two machines, so that's SystemExit(1).
    The same ansible_host appearing under two clusters is legal but suspicious
    (likely a copy-paste error), so it only warns.
    """
    seen_names: Dict[str, str] = {}
    seen_hosts: Dict[str, str] = {}
    for n in nodes:
        name, cluster = n["name"], n.get("cluster", DEFAULT_CLUSTER)
        if name in seen_names:
            raise SystemExit(
                f"duplicate [proxmox_nodes] entry '{name}' (clusters "
                f"'{seen_names[name]}' and '{cluster}') — node names must be "
                "unique across all clusters"
            )
        seen_names[name] = cluster
        host = n.get("ansible_host", "")
        if host and host in seen_hosts and seen_hosts[host] != cluster:
            print(
                f"WARNING: ansible_host {host} appears in clusters "
                f"'{seen_hosts[host]}' and '{cluster}' — check for a "
                "copy-paste error in hosts.ini",
                file=sys.stderr,
            )
        seen_hosts.setdefault(host, cluster)


@dataclass
class VmSpec:
    """All driver-relevant data for one [proxmox_vms] entry."""

    name: str           # inventory hostname
    ansible_host: str   # SSH reachable IP
    vmid: str           # PVE VM ID (e.g. "200") — NOT unique across clusters
    pve_node: str       # inventory hostname of the Proxmox node that owns this VM
    maintenance_window: Optional[MaintenanceWindow] = None
    canary: bool = False  # updated in the canary wave before the rest of the fleet
    cluster: str = ""   # owning cluster; "" = infer (from pve_node, else discovery)


@dataclass
class RemoteHostSpec:
    """All driver-relevant data for one [remote_hosts] entry."""

    name: str           # inventory hostname
    ansible_host: str   # SSH reachable IP
    maintenance_window: Optional[MaintenanceWindow] = None
    pre_update_cmd: str = ""
    canary: bool = False  # updated in the canary wave before the rest of the fleet


def load_proxmox_vms(
    inventory_path: str = "hosts.ini",
    *,
    host_vars_dir: str = "host_vars",
) -> List[VmSpec]:
    """Parse ``[proxmox_vms]`` from *inventory_path* and merge per-host vars.

    Returns hosts in inventory-file order. ``vmid`` and ``pve_node`` are
    expected as inline vars on the host line (matches the example inventory).
    A missing ``[proxmox_vms]`` section returns an empty list without error.
    """
    hvdir = Path(host_vars_dir)
    specs: List[VmSpec] = []
    for name, inline in _iter_section(inventory_path, "proxmox_vms"):
        host_vars = _load_host_vars(name, hvdir)
        raw_mw = host_vars.get("maintenance_window")
        specs.append(VmSpec(
            name=name,
            ansible_host=str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
            vmid=str(inline.get("vmid", host_vars.get("vmid", ""))),
            pve_node=str(inline.get("pve_node", host_vars.get("pve_node", ""))),
            maintenance_window=MaintenanceWindow(**raw_mw) if isinstance(raw_mw, dict) else None,
            canary=_as_bool(inline.get("canary", host_vars.get("canary", False))),
            cluster=str(inline.get("cluster", host_vars.get("cluster", ""))),
        ))
    return specs


def load_remote_hosts(
    inventory_path: str = "hosts.ini",
    *,
    host_vars_dir: str = "host_vars",
) -> List[RemoteHostSpec]:
    """Parse ``[remote_hosts]`` from *inventory_path* and merge per-host vars.

    Returns hosts in inventory-file order. A missing ``[remote_hosts]`` section
    returns an empty list without error.
    """
    hvdir = Path(host_vars_dir)
    specs: List[RemoteHostSpec] = []
    for name, inline in _iter_section(inventory_path, "remote_hosts"):
        host_vars = _load_host_vars(name, hvdir)
        raw_mw = host_vars.get("maintenance_window")
        specs.append(RemoteHostSpec(
            name=name,
            ansible_host=str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
            maintenance_window=MaintenanceWindow(**raw_mw) if isinstance(raw_mw, dict) else None,
            pre_update_cmd=str(host_vars.get("pre_update_cmd", "")),
            canary=_as_bool(inline.get("canary", host_vars.get("canary", False))),
        ))
    return specs


def load_custom_hosts(
    inventory_path: str = "hosts.ini",
    *,
    host_vars_dir: str = "host_vars",
) -> List[HostSpec]:
    """Parse ``[custom_hosts]`` from *inventory_path* and merge per-host vars.

    Returns hosts in inventory-file order (the Phase 0a ordering guarantee).
    Commented-out lines (``#``, ``;``) and blank lines are skipped.
    A missing ``[custom_hosts]`` section returns an empty list without error.
    """
    hvdir = Path(host_vars_dir)
    specs: List[HostSpec] = []
    for name, inline in _iter_section(inventory_path, "custom_hosts"):
        host_vars = _load_host_vars(name, hvdir)
        raw_mw = host_vars.get("maintenance_window")
        specs.append(
            HostSpec(
                name=name,
                ansible_host=str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
                custom_config=str(inline.get("custom_config", host_vars.get("custom_config", "")) or ""),
                depends_on=host_vars.get("depends_on", []),
                maintenance_window=MaintenanceWindow(**raw_mw) if isinstance(raw_mw, dict) else None,
                custom_overrides=host_vars.get("custom_overrides", {}),
            )
        )
    return specs


# --- manual_update_hosts (scan-tracked, never auto-updated) -----------------

@dataclass
class ManualUpdateHostSpec:
    """All driver-relevant data for one [manual_update_hosts] entry.

    Manual-update hosts are never touched by an automated phase — the
    read-only scan adapter only tracks their state and reminds the operator to
    apply updates. ``manual_adapter`` selects the vendor driver
    (TrueNAS/OPNsense/...); supported names are validated by the manual_update
    phase, but the loader requires the value to be present and non-blank so a
    misconfigured host fails loudly at load time, before any host contact.
    """

    name: str                 # inventory hostname — must not overlap auto-update groups
    ansible_host: str         # SSH reachable IP
    manual_adapter: str       # required; supported names validated later (manual_updates.py)
    display_name: Optional[str] = None  # human-friendly label for reports/reminders
    apply_hint: Optional[str] = None    # free-form note for the reminder (when/how to apply)


def _optional_str(value: Any) -> Optional[str]:
    """Coerce an inline/host_vars value to str; blank or missing → None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_manual_update_hosts(
    inventory_path: str = "hosts.ini",
    *,
    host_vars_dir: str = "host_vars",
) -> List[ManualUpdateHostSpec]:
    """Parse ``[manual_update_hosts]`` and merge per-host vars.

    Returns hosts in inventory-file order. Inline vars win over
    host_vars/<name>.yml, matching every other loader. ``manual_adapter`` is
    required and must be non-blank — a missing/empty value raises SystemExit
    here (load time, before any executor or host contact) so the operator sees
    exactly which host is misconfigured. Supported adapter names are validated
    by the scan adapter registry (proxmox_fleet/manual_updates.py); this module
    deliberately does not import that phase module.
    """
    hvdir = Path(host_vars_dir)
    specs: List[ManualUpdateHostSpec] = []
    for name, inline in _iter_section(inventory_path, "manual_update_hosts"):
        host_vars = _load_host_vars(name, hvdir)
        adapter = _optional_str(inline.get("manual_adapter", host_vars.get("manual_adapter")))
        if not adapter:
            raise SystemExit(
                f"manual_update_hosts entry '{name}' has no manual_adapter — "
                "set manual_adapter=<name> inline or in host_vars/<name>.yml"
            )
        specs.append(ManualUpdateHostSpec(
            name=name,
            ansible_host=str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
            manual_adapter=adapter,
            display_name=_optional_str(inline.get("display_name", host_vars.get("display_name"))),
            apply_hint=_optional_str(inline.get("apply_hint", host_vars.get("apply_hint"))),
        ))
    return specs


# Auto-update groups: a host listed here is updated by an automated phase, so it
# must never also be a [manual_update_hosts] entry (which is tracked/reminded
# only). Overlap between the auto-update groups themselves stays legal.
_AUTO_UPDATE_GROUPS = ("remote_hosts", "proxmox_vms", "custom_hosts", "proxmox_nodes")


def validate_manual_update_overlap(
    inventory_path: str = "hosts.ini",
) -> None:
    """Fail loud when a [manual_update_hosts] name also appears in an
    auto-update group ([remote_hosts], [proxmox_vms], [custom_hosts],
    [proxmox_nodes]).

    A host in both groups would be double-managed: an automated phase updates
    it while the manual_update phase treats it as operator-owned. Overlap is
    judged by inventory hostname only. The check is pure inventory parsing —
    no executors, no host contact — so it is safe to run in pre-flight before
    any executor is constructed. The error names every offending host and the
    groups it collides with.
    """
    manual = [name for name, _ in _iter_section(inventory_path, "manual_update_hosts")]
    if not manual:
        return
    manual_set = set(manual)
    clashes: List[Tuple[str, str]] = []
    for group in _AUTO_UPDATE_GROUPS:
        for name, _ in _iter_section(inventory_path, group):
            if name in manual_set:
                clashes.append((name, group))
    if clashes:
        details = ", ".join(f"'{name}' in [{group}]" for name, group in clashes)
        raise SystemExit(
            f"manual-update overlap: {details} — hosts in [manual_update_hosts] "
            "must not also appear in any auto-update group "
            "([remote_hosts], [proxmox_vms], [custom_hosts], [proxmox_nodes])"
        )
