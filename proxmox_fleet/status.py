"""Status decision trees — typed replacements for the `tmp_*` Jinja set_facts.

Each function returns the exact display string the current Jinja produces, so the
Discord briefing stays byte-identical. Parity is locked by tests that mirror the
existing test_*_report cases.

Phase 2 implements the custom_update tree (`custom_status`); the lxc/vm/node trees
follow in later phases.
"""

from __future__ import annotations

from typing import Optional


def _reboot_suffix(reboot_done: bool) -> str:
    return " + Rebooted" if reboot_done else ""


def custom_status(
    *,
    dry_run: bool = False,
    changed_when_type: str = "version",
    update_only_if_outdated: bool = False,
    is_outdated: bool = True,
    ver_before: Optional[str] = None,
    ver_after: Optional[str] = None,
    latest_ver: Optional[str] = None,
    changed_cmd_rc: Optional[int] = None,
    changed_cmd_skipped: bool = False,
    reboot_done: bool = False,
) -> str:
    """The `tmp_custom` decision tree from roles/custom_update/tasks/report.yml.

    ``None`` for ver_before/latest_ver models a Jinja *undefined* value (so
    ``default('?')`` / ``default('unknown')`` apply); an empty string models a
    defined-but-empty fact (left as-is, matching Jinja ``default()`` semantics).
    """
    reboot = _reboot_suffix(reboot_done)

    # 1. dry-run is informational
    if dry_run:
        before = "?" if ver_before is None else ver_before
        latest = "unknown" if latest_ver is None else latest_ver
        return f"dry-run: {before} → {latest}"

    # 2. outdated gate satisfied and already current
    if update_only_if_outdated and not is_outdated:
        return "OK (up to date)"

    # 3. always
    if changed_when_type == "always":
        return f"Updated{reboot}"

    # 4. command-based change detection (only when a non-skipped result exists)
    if changed_when_type == "command" and changed_cmd_rc is not None and not changed_cmd_skipped:
        return f"Updated{reboot}" if changed_cmd_rc == 0 else "OK"

    # 5. version comparison (both present)
    before = (ver_before or "").strip()
    after = (ver_after or "").strip()
    if before != "" and after != "":
        if before != after:
            return f"Updated: {before} → {after}{reboot}"
        return "OK"

    # 6. fallback: no version data
    return f"Updated{reboot}"


def custom_should_report(status: str, *, dry_run: bool) -> bool:
    """The report `when:` gate: append a record on dry-run, or when the status
    string contains 'updated' or 'failed' (idle OK systems are suppressed)."""
    lowered = status.strip().lower()
    return dry_run or ("updated" in lowered) or ("failed" in lowered)
