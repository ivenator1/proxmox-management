"""Change-detection logic — the typed replacement for the Jinja `custom_changed`
and `custom_is_outdated` set_facts (roles/custom_update/tasks/update.yml +
detect.yml). Pure functions; the single source of truth consumed by both the
health-check gate and the report status.
"""

from __future__ import annotations

import re
from typing import Optional


def normalize_version(value: str) -> str:
    """Trim and strip a single leading 'v' (mirrors `| trim | regex_replace('^v', '')`)."""
    return re.sub(r"^v", "", (value or "").strip())


def is_outdated(ver_before: str, latest_ver: str) -> bool:
    """Tier-5 outdated gate. Fail-open: if latest is unresolved, treat as outdated.

    Mirrors detect.yml: True when latest is blank, else normalized before != latest.
    """
    if (latest_ver or "").strip() == "":
        return True
    return normalize_version(ver_before) != normalize_version(latest_ver)


def custom_changed(
    *,
    changed_when_type: str = "version",
    ver_before: str = "",
    ver_after: str = "",
    changed_cmd_rc: Optional[int] = None,
    changed_cmd_skipped: bool = False,
) -> bool:
    """Did this custom system actually change?

    Mirrors the `custom_changed` set_fact decision tree:
      - type 'always'  -> True
      - type 'command' (and a non-skipped result exists) -> rc == 0
      - else version compare: both non-empty -> differ; otherwise -> True (fallback)
    """
    if changed_when_type == "always":
        return True
    if changed_when_type == "command" and changed_cmd_rc is not None and not changed_cmd_skipped:
        return changed_cmd_rc == 0
    before, after = ver_before.strip(), ver_after.strip()
    if before != "" and after != "":
        return before != after
    return True
