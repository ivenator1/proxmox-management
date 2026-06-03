"""remote_host_update flow — the Python decomposition of roles/remote_host_update/tasks/*.

Owns the full per-host control flow that used to live in main.yml's block/rescue:

  try: [pre_update_cmd] → detect_pkg_mgr → upgrade → reboot? → health → report
  except: rescue (record FAILED — no snapshot, no always block)

All decisions are here in Python; the Executor + http module do the actual work.
Status strings come from proxmox_fleet.status (byte-parity with the old Jinja).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from proxmox_fleet import http as http_mod
from proxmox_fleet.changes import pkg_changed as _pkg_changed
from proxmox_fleet.executor import Executor
from proxmox_fleet.flows._pkg import detect_pkg_mgr, kuma_healthy, upgrade_cmd
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import ErrorEntry, RemoteRecord, WarningEntry
from proxmox_fleet.status import remote_should_report, remote_status


@dataclass
class RemoteFlowOutcome:
    """Everything the driver needs to fold this host into the FleetState."""

    record: Optional[RemoteRecord] = None
    changed: bool = False
    failed: bool = False
    error: Optional[ErrorEntry] = None
    warnings: List[WarningEntry] = field(default_factory=list)


def run_remote_update(
    hostname: str,
    executor: Executor,
    settings: GlobalSettings,
    *,
    dry_run: bool = False,
    pre_update_cmd: str = "",
) -> RemoteFlowOutcome:
    """Run the full remote_host_update flow for one host. Never raises.

    Args:
        hostname:        Inventory hostname (used in RemoteRecord.host + Kuma map).
        executor:        Bound to the remote host; uses run_shell / reboot.
        settings:        GlobalSettings from vars.yml.
        dry_run:         When True, simulate upgrade but apply no changes.
        pre_update_cmd:  Optional shell command to run before upgrading (per-host).
    """

    outcome = RemoteFlowOutcome()

    try:
        # ------------------------------------------------------------------
        # Optional pre-update hook (not in dry-run)
        # ------------------------------------------------------------------
        effective_pre_cmd = pre_update_cmd or settings.remote_pre_update_cmd
        if effective_pre_cmd and not dry_run:
            executor.run_shell(effective_pre_cmd)

        # ------------------------------------------------------------------
        # Detect package manager (Python decides, Ansible executes)
        # ------------------------------------------------------------------
        pkg_mgr = detect_pkg_mgr(executor)

        # ------------------------------------------------------------------
        # Upgrade
        # ------------------------------------------------------------------
        pkg_res = executor.run_shell(upgrade_cmd(pkg_mgr, dry_run=dry_run), ignore_errors=True)
        if pkg_res.failed and not dry_run:
            raise RuntimeError(
                f"Package upgrade failed (rc={pkg_res.rc}): {pkg_res.stderr or pkg_res.stdout}"
            )
        changed = _pkg_changed(pkg_res.stdout, pkg_mgr)

        # ------------------------------------------------------------------
        # Reboot check (only when changed, not dry-run)
        # ------------------------------------------------------------------
        rebooted = False
        if not dry_run and changed and settings.remote_auto_reboot:
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
        kuma_id = str(settings.remote_kuma_map.get(hostname, ""))
        if kuma_id and settings.kuma_url and (changed or rebooted):
            kuma_url = (
                f"{settings.kuma_url}/api/status-page/heartbeat/{settings.kuma_slug}"
            )
            try:
                http_mod.poll_until(
                    lambda: http_mod.get_json(kuma_url),
                    lambda p: kuma_healthy(p, monitor_id=kuma_id),
                    retries=settings.kuma_health_check_retries,
                    delay=settings.kuma_health_check_delay,
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Kuma health check failed for {hostname}: {exc}"
                ) from exc

        # ------------------------------------------------------------------
        # Report
        # ------------------------------------------------------------------
        status_str = remote_status(
            pkg_changed=changed, rebooted=rebooted, dry_run=dry_run, failed=False
        )
        outcome.changed = changed or rebooted
        if remote_should_report(pkg_changed=changed, rebooted=rebooted, failed=False):
            outcome.record = RemoteRecord(host=hostname, status=status_str)
        return outcome

    except Exception as exc:  # noqa: BLE001 - mirror Ansible rescue catch-all
        # ------------------------------------------------------------------
        # Rescue — no snapshot/rollback for remote hosts
        # ------------------------------------------------------------------
        failed_task = getattr(exc, "step_name", type(exc).__name__)
        outcome.failed = True
        outcome.record = RemoteRecord(host=hostname, status="FAILED")
        outcome.error = ErrorEntry(
            host=hostname, task=str(failed_task), error=str(exc)[:300]
        )
        outcome.warnings = list(outcome.warnings)
        return outcome
