"""Shared package-manager helpers for the update flows.

Single source of truth for:
- ``detect_pkg_mgr`` — which package manager a host uses (apt/dnf/apk).
- ``upgrade_cmd``    — the upgrade command string (simulate vs. real).
- ``kuma_healthy``   — the Uptime-Kuma heartbeat predicate for poll_until.

All upgrade commands pin ``LC_ALL=C`` so the English summary lines that
``proxmox_fleet.changes.pkg_changed``/``vm_pkg_count`` parse are emitted
regardless of the host's configured locale.
"""

from __future__ import annotations

from typing import Any

from proxmox_fleet.executor import Executor

_DETECT_CMD = (
    "if which apt-get 2>/dev/null; then echo apt; "
    "elif which dnf 2>/dev/null; then echo dnf; "
    "elif which apk 2>/dev/null; then echo apk; "
    "else echo unknown; fi"
)


def detect_pkg_mgr(executor: Executor) -> str:
    """Detect the package manager via ``which``. Returns 'apt', 'dnf', or 'apk'.

    Falls back to 'apt' (Debian-family default) when detection is inconclusive.
    """
    res = executor.run_shell(_DETECT_CMD, changed_when=False, ignore_errors=True)
    for word in res.stdout.split():
        if word in ("apt", "dnf", "apk"):
            return word
    return "apt"


def upgrade_cmd(pkg_mgr: str, *, dry_run: bool) -> str:
    """Build the upgrade command. Python decides simulate vs. real — Ansible executes.

    Every branch is prefixed with ``LC_ALL=C`` so change detection sees the
    canonical English summary lines regardless of the target's locale.
    """
    if pkg_mgr == "apt":
        prefix = "LC_ALL=C DEBIAN_FRONTEND=noninteractive"
        if dry_run:
            return f"{prefix} apt-get update -qq && {prefix} apt-get -s dist-upgrade"
        return (
            f"{prefix} apt-get update -qq && "
            f"{prefix} apt-get dist-upgrade -y --autoremove && "
            f"{prefix} apt-get clean"
        )
    if pkg_mgr == "dnf":
        return "LC_ALL=C dnf upgrade --assumeno" if dry_run else "LC_ALL=C dnf upgrade -y"
    if pkg_mgr == "apk":
        return "LC_ALL=C apk -s upgrade" if dry_run else "LC_ALL=C apk -U upgrade"
    raise RuntimeError(f"Unknown package manager: {pkg_mgr!r}")


def kuma_healthy(payload: Any, *, monitor_id: str) -> bool:
    """Predicate for poll_until: True when the Kuma monitor's last beat is status=1."""
    beats = (payload or {}).get("heartbeatList", {}).get(monitor_id, [])
    return bool(beats) and beats[-1].get("status") == 1
