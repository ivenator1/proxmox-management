"""hosts.ini editor — add/remove host lines while preserving everything else.

Pure stdlib, no web dependencies (the dashboard is just one caller). Edits are
line-based, never a parse/re-serialize round trip, so comments, blank lines,
``[group:vars]`` sections and host order all survive untouched — order matters
for ``[custom_hosts]`` (the Phase 0a dependency guarantee).

Every write goes through :func:`_atomic_write`: a timestamped ``.bak-<ts>``
backup of the previous content (pruned to the newest ``_BACKUP_KEEP``), then a
tempfile + ``os.replace`` in the same directory so a crash can never leave a
half-written inventory.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from proxmox_fleet import inventory

GROUPS = ("proxmox_nodes", "proxmox_vms", "remote_hosts", "custom_hosts", "manual_update_hosts")

# Manual-update hosts are tracked/reminded only and must never be auto-updated,
# so a name is forbidden from appearing in both [manual_update_hosts] and any
# auto-update group. Cross-group duplicates among the auto groups themselves
# stay legal (ordinary multi-group hosts are preserved).
_MANUAL_GROUP = "manual_update_hosts"
_AUTO_GROUPS = tuple(g for g in GROUPS if g != _MANUAL_GROUP)

# Same shape as web.app._TOKEN_RE — inventory-safe identifiers only.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The parser's _KV_PAIR is (\S+)=(\S+): values must be non-empty and contain
# no whitespace — anything else can't round-trip through hosts.ini.
_VALUE_RE = re.compile(r"^\S+$")

_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")

_BACKUP_KEEP = 10

_BOOTSTRAP = """\
[proxmox_nodes]

[proxmox_nodes:vars]
ansible_user=root
ansible_ssh_private_key_file=~/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3

[proxmox_vms]

[proxmox_vms:vars]
ansible_user=root
ansible_ssh_private_key_file=~/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3

[remote_hosts]

[remote_hosts:vars]
ansible_user=root
ansible_ssh_private_key_file=~/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3

[custom_hosts]

[custom_hosts:vars]
ansible_user=root
ansible_ssh_private_key_file=~/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3

[manual_update_hosts]

[manual_update_hosts:vars]
ansible_user=root
ansible_ssh_private_key_file=~/.ssh/id_ed25519
ansible_python_interpreter=/usr/bin/python3
"""


class InventoryEditError(ValueError):
    """Raised on invalid input or an impossible edit (duplicate/missing host)."""


def _backup(path: Path) -> None:
    """Write ``<path>.bak-<UTC-ts>`` of the current content; prune old backups."""
    if not path.exists():
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    backup = path.with_name(f"{path.name}.bak-{ts}")
    backup.write_bytes(path.read_bytes())
    siblings = sorted(path.parent.glob(f"{path.name}.bak-*"))
    for stale in siblings[:-_BACKUP_KEEP]:
        stale.unlink()


def _atomic_write(path: Path, text: str) -> None:
    """Backup the old file, then tempfile + os.replace (same-dir, crash-safe)."""
    _backup(path)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_host_name(name: str) -> str:
    name = name.strip()
    if not _NAME_RE.match(name):
        raise InventoryEditError(
            f"invalid host name {name!r} (letters/digits/._- only)")
    return name


def validate_inline_vars(inline_vars: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in inline_vars.items():
        key, value = str(key).strip(), str(value).strip()
        if not _KEY_RE.match(key):
            raise InventoryEditError(f"invalid var name {key!r}")
        if not _VALUE_RE.match(value):
            raise InventoryEditError(
                f"invalid value for {key!r}: must be non-empty without whitespace")
        out[key] = value
    return out


def list_hosts(path: str = "hosts.ini") -> Dict[str, List[Dict[str, Any]]]:
    """All hosts per group: ``{group: [{"name": ..., "vars": {...}}, ...]}``.

    Thin wrapper over the canonical section parser so the dashboard listing
    can never drift from what the driver actually loads.
    """
    return {
        group: [
            {"name": name, "vars": inline}
            for name, inline in inventory._iter_section(path, group)
        ]
        for group in GROUPS
    }


def ensure_inventory(path: str = "hosts.ini") -> bool:
    """Bootstrap a fresh hosts.ini (all group headers + [group:vars] defaults,
    mirroring hosts.ini.example) when missing. Returns True if created."""
    p = Path(path)
    if p.exists():
        return False
    _atomic_write(p, _BOOTSTRAP)
    return True


def _group_names(path: str, group: str) -> List[str]:
    return [name for name, _ in inventory._iter_section(path, group)]


def add_host(path: str, group: str, name: str, inline_vars: Dict[str, str]) -> None:
    """Append ``name key=val …`` at the end of ``[group]``'s host block.

    The line lands just before the next ``[section]`` header (or EOF), after
    any existing hosts — appending keeps [custom_hosts] dependency order
    stable for existing entries. Creates the ``[group]`` header at EOF when
    the section is missing entirely.
    """
    if group not in GROUPS:
        raise InventoryEditError(f"unknown group {group!r}")
    name = validate_host_name(name)
    inline_vars = validate_inline_vars(inline_vars)
    if name in _group_names(path, group):
        raise InventoryEditError(f"host {name!r} already exists in [{group}]")

    # A host can sit in several auto-update groups (ordinary cross-group
    # duplicates are preserved) but never in both an auto-update group and
    # [manual_update_hosts] — a manual-update host must not be auto-updated.
    if group == _MANUAL_GROUP:
        clashes = [g for g in _AUTO_GROUPS if name in _group_names(path, g)]
        if clashes:
            groups = ", ".join(f"[{g}]" for g in clashes)
            raise InventoryEditError(
                f"host {name!r} already exists in {groups} — manual-update hosts "
                "must not overlap any auto-update group")
        if "manual_adapter" not in inline_vars:
            raise InventoryEditError(
                f"host {name!r} in [{_MANUAL_GROUP}] requires manual_adapter=<name>")
    elif name in _group_names(path, _MANUAL_GROUP):
        raise InventoryEditError(
            f"host {name!r} already exists in [{_MANUAL_GROUP}] — manual-update "
            "hosts must not overlap any auto-update group")

    host_line = name
    if inline_vars:
        host_line += "  " + " ".join(f"{k}={v}" for k, v in inline_vars.items())

    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    # find the group's section and the index just past its last content line
    insert_at = None
    in_section = False
    for i, raw in enumerate(lines):
        m = _SECTION_RE.match(raw.strip())
        if m:
            if in_section:           # next section header ends our block
                break
            in_section = m.group(1) == group
            if in_section:
                insert_at = i + 1
            continue
        if in_section and raw.strip() and not raw.strip().startswith(("#", ";")):
            insert_at = i + 1        # after the last real host line

    if insert_at is None:            # section missing → create it at EOF
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{group}]", host_line]
    else:
        lines.insert(insert_at, host_line)

    _atomic_write(p, "\n".join(lines) + "\n")


def remove_host(path: str, group: str, name: str) -> None:
    """Drop the single matching host line from ``[group]``; everything else
    (comments, blanks, [group:vars]) is untouched."""
    if group not in GROUPS:
        raise InventoryEditError(f"unknown group {group!r}")
    name = validate_host_name(name)

    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise InventoryEditError(f"{path} does not exist")

    out: List[str] = []
    in_section = False
    removed = False
    for raw in lines:
        stripped = raw.strip()
        m = _SECTION_RE.match(stripped)
        if m:
            in_section = m.group(1) == group
            out.append(raw)
            continue
        if (in_section and not removed and stripped
                and not stripped.startswith(("#", ";"))):
            hm = inventory._HOST_LINE.match(stripped)
            if hm and hm.group(1) == name:
                removed = True
                continue
        out.append(raw)

    if not removed:
        raise InventoryEditError(f"host {name!r} not found in [{group}]")
    _atomic_write(p, "\n".join(out) + "\n")
