"""vars.yml editor — targeted, comment-preserving edits via ruamel.yaml.

ruamel's round-trip mode keeps comments, key order and quoting intact, so the
dashboard can change one key without flattening a hand-maintained secrets
file. Every change is validated through ``GlobalSettings.model_validate()``
*before* anything is written, and writes share the same timestamped-backup +
atomic-replace scheme as :mod:`proxmox_fleet.inventory_edit`.

ruamel.yaml ships with the ``[web]`` extra — this module is only imported by
the dashboard (import errors surface there, not in the CLI).
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml as pyyaml
from ruamel.yaml import YAML

from proxmox_fleet.inventory_edit import _atomic_write
from proxmox_fleet.models.settings import GlobalSettings


class VarsEditError(ValueError):
    """Invalid input or a change that fails GlobalSettings validation."""


def _yaml() -> YAML:
    y = YAML()                      # round-trip mode: comments/order preserved
    y.preserve_quotes = True
    y.width = 4096                  # never wrap long webhook URLs
    return y


def read_raw(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load_data(path: str) -> Any:
    """The round-trip document (CommentedMap), or an empty map for a missing file."""
    raw = read_raw(path)
    data = _yaml().load(raw) if raw.strip() else None
    return data if data is not None else _yaml().map()


def _dump(data: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def _validate(data: Any) -> None:
    try:
        GlobalSettings.model_validate(dict(data))
    except Exception as exc:        # pydantic.ValidationError, TypeError, ...
        raise VarsEditError(f"vars.yml validation failed: {exc}") from exc


def apply_changes(
    path: str,
    *,
    set_keys: Optional[Dict[str, Any]] = None,
    list_add: Optional[Dict[str, List[str]]] = None,
    list_remove: Optional[Dict[str, List[str]]] = None,
    map_set: Optional[Dict[str, Dict[str, str]]] = None,
    map_remove: Optional[Dict[str, List[str]]] = None,
) -> bool:
    """Apply targeted edits; validate the merged result; write atomically.

    - ``set_keys``: replace top-level keys wholesale.
    - ``list_add``/``list_remove``: append-if-absent / drop-if-present on list
      keys (entries compared as str — vars.yml mixes ints and strings).
    - ``map_set``/``map_remove``: set / delete entries of dict keys
      (kuma maps).

    Missing keys are created. Returns True when the file actually changed.
    """
    data = load_data(path)
    before = _dump(data)

    for key, value in (set_keys or {}).items():
        data[key] = value

    for key, values in (list_add or {}).items():
        target = data.get(key)
        if not isinstance(target, list):
            target = [] if target is None else list(target)
            data[key] = target
        existing = {str(v) for v in target}
        for value in values:
            if str(value) not in existing:
                target.append(value)
                existing.add(str(value))

    for key, values in (list_remove or {}).items():
        target = data.get(key)
        if isinstance(target, list):
            doomed = {str(v) for v in values}
            for i in range(len(target) - 1, -1, -1):
                if str(target[i]) in doomed:
                    del target[i]

    for key, entries in (map_set or {}).items():
        target = data.get(key)
        if not isinstance(target, dict):
            target = _yaml().map()
            data[key] = target
        for sub, value in entries.items():
            target[str(sub)] = value

    for key, subs in (map_remove or {}).items():
        target = data.get(key)
        if isinstance(target, dict):
            for sub in subs:
                target.pop(str(sub), None)

    after = _dump(data)
    if after == before and Path(path).exists():
        return False
    _validate(data)
    _atomic_write(Path(path), after)
    return True


def replace_raw(path: str, text: str) -> None:
    """Full-file replace (the raw editor): parse + validate, then write."""
    try:
        data = _yaml().load(text) or {}
    except Exception as exc:
        raise VarsEditError(f"not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise VarsEditError("vars.yml must be a YAML mapping at the top level")
    _validate(data)
    _atomic_write(Path(path), text if text.endswith("\n") else text + "\n")


# --- host_vars/<host>.yml (new files — nothing to preserve, plain PyYAML) --- #

def host_vars_write(host_vars_dir: str, host: str, data: Dict[str, Any]) -> str:
    d = Path(host_vars_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{host}.yml"
    _atomic_write(target, pyyaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    return str(target)


def host_vars_delete(host_vars_dir: str, host: str) -> bool:
    target = Path(host_vars_dir) / f"{host}.yml"
    if target.exists():
        target.unlink()
        return True
    return False
