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
from typing import Any, Dict, List, Optional

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


def load_proxmox_nodes(
    inventory_path: str = "hosts.ini",
) -> List[Dict[str, str]]:
    """Parse ``[proxmox_nodes]`` from *inventory_path*.

    Returns a list of ``{name, ansible_host}`` dicts in inventory order.
    ansible_host falls back to the node name if not set inline or in host_vars.
    """
    nodes: List[Dict[str, str]] = []
    in_section = False

    try:
        lines = Path(inventory_path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("["):
            section_name = line.strip("[]").split(":")[0]  # strip :vars, :children etc.
            in_section = section_name == "proxmox_nodes"
            continue
        if not in_section:
            continue

        m = _HOST_LINE.match(line)
        if not m:
            continue

        name = m.group(1)
        inline = _parse_inline_vars(m.group(2))
        nodes.append({
            "name": name,
            "ansible_host": inline.get("ansible_host", name),
        })

    return nodes


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
    in_section = False

    try:
        lines = Path(inventory_path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        if line.startswith("["):
            section_name = line.strip("[]")
            in_section = section_name == "custom_hosts"
            continue

        if not in_section:
            continue

        m = _HOST_LINE.match(line)
        if not m:
            continue

        name = m.group(1)
        inline = _parse_inline_vars(m.group(2))
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
