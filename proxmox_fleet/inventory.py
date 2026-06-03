"""Inventory parser — reads [custom_hosts] from hosts.ini + host_vars/ per host.

Pure stdlib + PyYAML, no ansible-runner required. Preserves inventory order
so the caller's serial loop respects the Phase 0a ordering guarantee.

Uses manual line-by-line parsing instead of configparser because hosts.ini
host lines have the form ``hostname key=val key=val …`` which configparser
would mis-parse (splitting on the first ``=`` makes the key ``hostname key``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

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
    maintenance_window: Optional[Dict[str, Any]] = None
    custom_overrides: Dict[str, Any] = field(default_factory=dict)


def _parse_inline_vars(remainder: str) -> Dict[str, str]:
    """Split 'key=value key=value …' into a dict."""
    return {m.group(1): m.group(2) for m in _KV_PAIR.finditer(remainder)}


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

    Returns a list of ``{name, ansible_host}`` dicts in inventory order.
    ansible_host resolution order: inline var → host_vars/<node>.yml → node name.
    Merging host_vars matters because ``ansible_host`` becomes the snapshot API
    ``api_host`` (which must be an IP, not the inventory name).
    """
    hvdir = Path(host_vars_dir)
    nodes: List[Dict[str, str]] = []
    for name, inline in _iter_section(inventory_path, "proxmox_nodes"):
        host_vars = _load_host_vars(name, hvdir)
        nodes.append({
            "name": name,
            "ansible_host": str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
        })
    return nodes


@dataclass
class VmSpec:
    """All driver-relevant data for one [proxmox_vms] entry."""

    name: str           # inventory hostname
    ansible_host: str   # SSH reachable IP
    vmid: str           # PVE VM ID (e.g. "200")
    pve_node: str       # inventory hostname of the Proxmox node that owns this VM
    maintenance_window: Optional[Dict[str, Any]] = None


@dataclass
class RemoteHostSpec:
    """All driver-relevant data for one [remote_hosts] entry."""

    name: str           # inventory hostname
    ansible_host: str   # SSH reachable IP
    maintenance_window: Optional[Dict[str, Any]] = None
    pre_update_cmd: str = ""


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
        specs.append(VmSpec(
            name=name,
            ansible_host=str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
            vmid=str(inline.get("vmid", host_vars.get("vmid", ""))),
            pve_node=str(inline.get("pve_node", host_vars.get("pve_node", ""))),
            maintenance_window=host_vars.get("maintenance_window"),
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
        specs.append(RemoteHostSpec(
            name=name,
            ansible_host=str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
            maintenance_window=host_vars.get("maintenance_window"),
            pre_update_cmd=str(host_vars.get("pre_update_cmd", "")),
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
        specs.append(
            HostSpec(
                name=name,
                ansible_host=str(inline.get("ansible_host", host_vars.get("ansible_host", name))),
                custom_config=inline.get("custom_config", host_vars.get("custom_config", "")),
                depends_on=host_vars.get("depends_on", []),
                maintenance_window=host_vars.get("maintenance_window"),
                custom_overrides=host_vars.get("custom_overrides", {}),
            )
        )
    return specs
