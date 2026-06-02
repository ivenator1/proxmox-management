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


def lxc_os_changed(stdout: str) -> bool:
    """True if apt/apk/dnf output indicates packages were upgraded.

    Mirrors the changed_when logic in roles/lxc_update/tasks/update.yml:
    output contains upgrade keywords AND not '0 upgraded, 0 newly installed'.
    """
    if not stdout:
        return False
    keywords = re.search(r"upgraded|Installing|Upgrading|Setting up", stdout)
    if not keywords:
        return False
    return "0 upgraded, 0 newly installed" not in stdout


def lxc_os_pkg_count(stdout: str) -> Optional[int]:
    """Extract the N from 'N upgraded' in apt output.

    Returns None when the pattern is absent (non-apt OS or no count in output).
    """
    m = re.search(r"(\d+) upgraded", stdout)
    return int(m.group(1)) if m else None


def dpkg_hash_differs(before: str, after: str) -> bool:
    """True when both hashes are non-empty and differ after strip.

    An empty string on either side (non-apt OS or hash not collected) → False,
    so callers fall through to the next branch in lxc_app_status().
    """
    b, a = before.strip(), after.strip()
    return bool(b and a and b != a)


def pkg_changed(stdout: str, pkg_mgr: str) -> bool:
    """True if the package manager output indicates packages were upgraded.

    Python interprets raw stdout from the apt/dnf/apk upgrade command.
    Works identically for dry-run (simulate) and real-run outputs since both
    use the same output format. All decisions are here in Python.

    pkg_mgr: 'apt' | 'dnf' | 'apk'
    """
    if not stdout:
        return False
    if pkg_mgr == "apt":
        return "0 upgraded, 0 newly installed" not in stdout
    if pkg_mgr == "dnf":
        return "Nothing to do" not in stdout
    if pkg_mgr == "apk":
        return "0 upgraded, 0 installed, 0 removed" not in stdout
    # Unknown package manager — assume changed when there is any output
    return bool(stdout.strip())


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
