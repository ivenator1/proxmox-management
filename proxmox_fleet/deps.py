"""Dependency-order validation and runtime dep-failed detection.

Pure-Python ports of two Jinja expressions that currently live in YAML:
  - validate_depends_order  ← Phase 0a _dep_problems set_fact in fleet-update.yml
  - dependency_failed       ← _dep_failed set_fact in roles/custom_update/tasks/main.yml
"""
from __future__ import annotations

from typing import Dict, List, Set


def validate_depends_order(
    hosts: List[str],
    depends_on_map: Dict[str, List[str]],
) -> List[str]:
    """Return a list of ordering-problem strings (empty list = OK).

    Checks two conditions for each dependency ``d`` of host ``h``:
    1. ``d`` is not in the custom_hosts group at all → missing host error.
    2. ``d`` appears after ``h`` in the list → must be reordered.

    Mirrors the Phase 0a ``_dep_problems`` Jinja expression exactly so
    error messages are identical.
    """
    problems: List[str] = []
    for h in hosts:
        for d in depends_on_map.get(h, []):
            if d not in hosts:
                problems.append(f"{h} depends_on missing custom host {d}")
            elif hosts.index(d) > hosts.index(h):
                problems.append(
                    f"{h} depends_on {d} which is listed AFTER it; "
                    "reorder [custom_hosts] so dependencies come first"
                )
    return problems


def dependency_failed(
    host: str,
    depends_on: List[str],
    failed_hosts: Set[str],
) -> bool:
    """True if any of this host's dependencies is in failed_hosts.

    Mirrors the ``_dep_failed`` set_fact in roles/custom_update/tasks/main.yml.
    ``host`` is unused but accepted for call-site clarity.
    """
    return bool(set(depends_on) & failed_hosts)
