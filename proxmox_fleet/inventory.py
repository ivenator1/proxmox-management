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
) -> List[Dict[str, str]]:
    """Parse ``[proxmox_nodes]`` from *inventory_path* and merge per-host vars.

    Returns a list of ``{name, ansible_host, cluster}`` dicts in inventory order.
    ansible_host resolution order: inline var → host_vars/<node>.yml → node name.
    Merging host_vars matters because ``ansible_host`` becomes the snapshot API
    ``api_host`` (which must be an IP, not the inventory name). ``cluster``
    resolves the same way, falling back to DEFAULT_CLUSTER — single-cluster
    inventories need no ``cluster=`` vars at all.
    """
    hvdir = Path(host_vars_dir)
    nodes: List[Dict[str, str]] = []
    for name, inline in _iter_section(inventory_path, "proxmox_nodes"):
        host_vars = _load_host_vars(name, hvdir)
        nodes.append({
            "name": name,
            "ansible_host": str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
            "cluster": str(inline.get("cluster", host_vars.get("cluster", DEFAULT_CLUSTER))),
        })
    return nodes


def validate_node_uniqueness(nodes: List[Dict[str, str]]) -> None:
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
                custom_config=inline.get("custom_config", host_vars.get("custom_config", "")),
                depends_on=host_vars.get("depends_on", []),
                maintenance_window=MaintenanceWindow(**raw_mw) if isinstance(raw_mw, dict) else None,
                custom_overrides=host_vars.get("custom_overrides", {}),
            )
        )
    return specs
