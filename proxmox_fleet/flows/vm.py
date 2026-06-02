"""vm_update flow — the Python decomposition of roles/vm_update/tasks/*.

Owns the full per-VM control flow that used to live in main.yml's
block/rescue/always:

  try: backup → detect_pkg_mgr → upgrade → reboot? → health → report
  except: rescue (qm rollback if snapshot taken)
  finally: delete snapshot

All decisions are here in Python; the Executor + http module do the actual work.
Status strings come from proxmox_fleet.status (byte-parity with the old Jinja).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from proxmox_fleet import http as http_mod
from proxmox_fleet.changes import pkg_changed as _pkg_changed
from proxmox_fleet.executor import Executor
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import ErrorEntry, VmRecord, WarningEntry
from proxmox_fleet.status import vm_rescue_status, vm_should_report, vm_status


@dataclass
class VmFlowOutcome:
    """Everything the driver needs to fold this VM into the FleetState."""

    record: Optional[VmRecord] = None
    changed: bool = False
    failed: bool = False
    error: Optional[ErrorEntry] = None
    warnings: List[WarningEntry] = field(default_factory=list)


def _detect_pkg_mgr(executor: Executor) -> str:
    """Detect the package manager via `which`. Returns 'apt', 'dnf', or 'apk'."""
    res = executor.run_shell(
        "if which apt-get 2>/dev/null; then echo apt; "
        "elif which dnf 2>/dev/null; then echo dnf; "
        "elif which apk 2>/dev/null; then echo apk; "
        "else echo unknown; fi",
        changed_when=False,
        ignore_errors=True,
    )
    for word in res.stdout.split():
        if word in ("apt", "dnf", "apk"):
            return word
    return "apt"  # safe fallback for Debian-family VMs


def _upgrade_cmd(pkg_mgr: str, *, dry_run: bool) -> str:
    """Build the upgrade command. Python decides simulate vs. real — Ansible executes."""
    if pkg_mgr == "apt":
        prefix = "DEBIAN_FRONTEND=noninteractive"
        if dry_run:
            return f"{prefix} apt-get update -qq && {prefix} apt-get -s dist-upgrade"
        return f"{prefix} apt-get update -qq && {prefix} apt-get dist-upgrade -y --autoremove"
    if pkg_mgr == "dnf":
        return "dnf upgrade --assumeno" if dry_run else "dnf upgrade -y"
    if pkg_mgr == "apk":
        return "apk -s upgrade" if dry_run else "apk -U upgrade"
    raise RuntimeError(f"Unknown package manager: {pkg_mgr!r}")


def _kuma_healthy(payload: Any, *, monitor_id: str) -> bool:
    beats = (payload or {}).get("heartbeatList", {}).get(monitor_id, [])
    return bool(beats) and beats[-1].get("status") == 1


def run_vm_update(
    node: str,
    vmid: str,
    inventory_hostname: str,
    executor: Executor,
    node_executor: Executor,
    settings: GlobalSettings,
    *,
    dry_run: bool = False,
    api_host: str = "",
) -> VmFlowOutcome:
    """Run the full vm_update flow for one VM. Never raises — failures are
    captured into the outcome (FAILED record + error entry), mirroring rescue.

    Args:
        node:               Proxmox node inventory hostname (used in VmRecord.node).
        vmid:               VM ID string (e.g. "200").
        inventory_hostname: VM's inventory hostname for Kuma map lookups.
        executor:           Bound to the VM (SSH); used for package upgrades only.
        node_executor:      Bound to the Proxmox node (SSH); used for qm commands
                            (rollback, status). Must reflect the VM's *current* node
                            — the driver resolves this via cluster discovery so HA
                            migrations are handled correctly.
        settings:           GlobalSettings from vars.yml.
        dry_run:            When True, simulate upgrade but apply no changes.
        api_host:           The node's ansible_host IP for Proxmox API (snapshot).
    """

    snap_taken = False
    snapshot_failed = False
    rollback_done = False
    outcome = VmFlowOutcome()

    api_params: Dict[str, str] = {
        "api_host": api_host,
        "api_user": settings.pve_api_user,
        "api_token_id": settings.pve_api_token_id,
        "api_token_secret": settings.pve_api_token_secret,
    }

    try:
        # ------------------------------------------------------------------
        # Backup (skip in dry-run — no state mutations)
        # ------------------------------------------------------------------
        strategy = settings.vm_backup_strategy
        if not dry_run:
            if strategy in ("vzdump", "both"):
                vzdump_cmd = (
                    f"vzdump {vmid} --compress zstd --storage {settings.vm_backup_storage}"
                    f' --notes-template "{inventory_hostname} - ansible fleet-update"'
                )
                executor.run_shell(vzdump_cmd)

            if strategy in ("snapshot", "both"):
                snap_res = executor.snapshot(vmid, snap_state="present", **api_params)
                snap_taken = snap_res.changed
                if not snap_taken:
                    outcome.warnings.append(WarningEntry(
                        host=f"{node}/vm-{vmid}",
                        task=f"Snapshot {vmid}",
                        warning="snapshot failed — automatic rollback unavailable for this update",
                    ))
                    snapshot_failed = True

        # ------------------------------------------------------------------
        # Detect package manager (Python decides, Ansible executes)
        # ------------------------------------------------------------------
        pkg_mgr = _detect_pkg_mgr(executor)

        # ------------------------------------------------------------------
        # Upgrade
        # ------------------------------------------------------------------
        upgrade_cmd = _upgrade_cmd(pkg_mgr, dry_run=dry_run)
        pkg_res = executor.run_shell(upgrade_cmd, ignore_errors=True)
        if pkg_res.failed and not dry_run:
            raise RuntimeError(
                f"Package upgrade failed (rc={pkg_res.rc}): {pkg_res.stderr or pkg_res.stdout}"
            )
        changed = _pkg_changed(pkg_res.stdout, pkg_mgr)

        # ------------------------------------------------------------------
        # Reboot check (only when something actually changed, not dry-run)
        # ------------------------------------------------------------------
        rebooted = False
        if not dry_run and changed and settings.vm_auto_reboot:
            reboot_check = executor.run_shell(
                "test -f /var/run/reboot-required",
                changed_when=False,
                ignore_errors=True,
            )
            if reboot_check.rc == 0:
                executor.reboot(timeout=600)
                rebooted = True

        # ------------------------------------------------------------------
        # Health check — only when something changed
        # ------------------------------------------------------------------
        kuma_id = str(settings.vm_kuma_map.get(inventory_hostname, ""))
        if kuma_id and settings.kuma_url and (changed or rebooted):
            kuma_url = (
                f"{settings.kuma_url}/api/status-page/heartbeat/{settings.kuma_slug}"
            )
            try:
                http_mod.poll_until(
                    lambda: http_mod.get_json(kuma_url),
                    lambda p: _kuma_healthy(p, monitor_id=kuma_id),
                    retries=settings.kuma_health_check_retries,
                    delay=settings.kuma_health_check_delay,
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Kuma health check failed for vm-{vmid}: {exc}"
                ) from exc

        # ------------------------------------------------------------------
        # Report
        # ------------------------------------------------------------------
        status_str = vm_status(
            pkg_changed=changed, rebooted=rebooted, dry_run=dry_run, failed=False
        )
        outcome.changed = changed or rebooted
        if vm_should_report(pkg_changed=changed, rebooted=rebooted, failed=False):
            outcome.record = VmRecord(
                node=node, vmid=vmid, name=inventory_hostname, status=status_str
            )
        return outcome

    except Exception as exc:  # noqa: BLE001 - mirror Ansible rescue catch-all
        # ------------------------------------------------------------------
        # Rescue — rollback if snapshot was taken.
        # node_executor (bound to the Proxmox node) is used here — qm commands
        # must run on the node, not the VM guest. rollback_done is set only when
        # the rollback command succeeds AND qm status confirms the VM is running.
        # ------------------------------------------------------------------
        failed_task = getattr(exc, "step_name", type(exc).__name__)

        if snap_taken:
            try:
                rb = node_executor.run_shell(
                    f"qm rollback {vmid} BEFORE_UPDATE_AUTO",
                    ignore_errors=True,
                )
                if not rb.failed:
                    # Poll until VM is running again (up to 12 × 10 s)
                    for _ in range(12):
                        time.sleep(10)
                        chk = node_executor.run_shell(
                            f"qm status {vmid}", changed_when=False, ignore_errors=True
                        )
                        if "running" in chk.stdout:
                            rollback_done = True
                            break
            except Exception:  # noqa: BLE001
                pass

        rescue_str = vm_rescue_status(
            rollback_done=rollback_done, snapshot_failed=snapshot_failed
        )
        outcome.failed = True
        outcome.record = VmRecord(
            node=node, vmid=vmid, name=inventory_hostname, status=rescue_str
        )
        outcome.error = ErrorEntry(
            host=f"{node}/vm-{vmid}", task=str(failed_task), error=str(exc)[:300]
        )
        outcome.warnings = list(outcome.warnings)
        return outcome

    finally:
        # ------------------------------------------------------------------
        # Always — delete snapshot
        # ------------------------------------------------------------------
        if snap_taken:
            try:
                executor.snapshot(vmid, snap_state="absent", **api_params)
            except Exception:  # noqa: BLE001
                pass
