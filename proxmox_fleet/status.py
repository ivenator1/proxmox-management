"""Status decision trees — typed replacements for the `tmp_*` Jinja set_facts.

Each function returns the exact display string the current Jinja produces, so the
Discord briefing stays byte-identical. Parity is locked by tests that mirror the
existing test_*_report cases.

Phase 2 implements the custom_update tree (`custom_status`); Phase 3 adds lxc_*;
vm/node trees follow in later phases.
"""

from __future__ import annotations

import re
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


# ---------------------------------------------------------------------------
# Phase 3: lxc_update trees
# ---------------------------------------------------------------------------


def lxc_app_status(
    *,
    is_template: bool = False,
    is_running: bool = True,
    dry_run: bool = False,
    dry_run_status: str = "",
    no_update_script: bool = False,
    app_failed: bool = False,
    app_changed: bool = False,
    ver_before: str = "",
    ver_after: str = "",
    dpkg_before: str = "",
    dpkg_after: str = "",
) -> str:
    """The 11-branch `tmp_app` decision tree from roles/lxc_update/tasks/report.yml.

    Priority order matches the Jinja exactly; parity locked by test_status_lxc.py
    which mirrors test_report_tmp_app.py case-for-case.
    """
    if is_template:
        return "TEMPLATE"
    if not is_running:
        return "NEVER STARTED"
    if dry_run:
        return dry_run_status if dry_run_status else "dry-run"
    if no_update_script:
        return "NO SCRIPT"
    if app_failed:
        return "FAILED"

    if app_changed:
        # Version-file comparison (strip leading 'v' before equality check)
        before = ver_before.strip()
        after = ver_after.strip()
        if before and after:
            if re.sub(r"^v", "", before) != re.sub(r"^v", "", after):
                return f"Updated: {before} → {after}"
            return "OK"

        # dpkg-hash comparison
        db = dpkg_before.strip()
        da = dpkg_after.strip()
        if db and da:
            return "UPDATED" if db != da else "OK"

        # No version data, no hash data — non-apt OS or silent run
        return "UPDATED"

    return "OK"


def lxc_os_status(
    *,
    is_template: bool = False,
    is_running: bool = True,
    dry_run: bool = False,
    excluded: bool = False,
    os_failed: bool = False,
    os_changed: bool = False,
    pkg_count: Optional[int] = None,
    reboot_done: bool = False,
) -> str:
    """The `tmp_os` expression from roles/lxc_update/tasks/report.yml.

    Returns '' for template/not-running/dry-run (the Jinja empty branch stores
    None which the payload None-guard converts to '').
    """
    if is_template or not is_running or dry_run:
        return ""
    if excluded:
        return "SKIPPED"
    if os_failed:
        return "FAILED"
    if reboot_done:
        pkg = f" ({pkg_count} upgraded)" if pkg_count is not None else ""
        return f"Updated{pkg} & Rebooted"
    if os_changed:
        pkg = f" ({pkg_count} upgraded)" if pkg_count is not None else ""
        return f"Updated{pkg}"
    return "OK"


def lxc_rescue_app_status(
    *,
    rollback_done: bool = False,
    snapshot_failed: bool = False,
) -> str:
    """Rescue block app string from roles/lxc_update/tasks/main.yml.

    Priority: rollback_done wins over snapshot_failed (if rollback happened,
    a snapshot existed, so 'ROLLED BACK' is the accurate description).
    """
    if rollback_done:
        return "FAILED + ROLLED BACK"
    if snapshot_failed:
        return "FAILED (NO SNAPSHOT)"
    return "FAILED"


def lxc_should_report(app: str, os: str, *, dry_run: bool) -> bool:
    """The `when:` gate on the 'Append LXC record' task in report.yml.

    Idle containers (app='OK', os='OK') are suppressed. Mirrors:
      lxc_dry_run | bool or
      (tmp_app | lower) is search('updated|failed|no script|never started') or
      (tmp_os  | lower) is search('updated|failed')
    """
    if dry_run:
        return True
    app_l = app.strip().lower()
    if re.search(r"updated|failed|no script|never started", app_l):
        return True
    os_l = os.strip().lower()
    return bool(re.search(r"updated|failed", os_l))


def lxc_dry_run_status(
    *,
    gh_repo: str = "",
    fetch_ok: bool = True,
    installed_ver: str = "",
    latest_tag: str = "",
) -> str:
    """The `dry_run_status` expression from roles/lxc_update/tasks/dry_check.yml.

    Mirrors the 5-branch Jinja tree; parity locked by test_status_lxc.py which
    mirrors test_dry_check_status.py case-for-case.
    """
    if not gh_repo:
        return "no ver info"
    if not fetch_ok:
        return "fetch failed"
    installed = installed_ver.strip()
    tag = (latest_tag or "?").strip()
    if not installed:
        return f"unknown (latest: {tag})"
    if re.sub(r"^v", "", installed) == re.sub(r"^v", "", tag):
        return installed
    return f"{installed} → {tag}"
