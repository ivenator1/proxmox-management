"""node_update flow — Python port of Phase 2 + Phase 3 of fleet-update.yml.

Phase 2 (run_node_update): serial node OS update + robust reboot loop.
  try: is_manager? → apt dist-upgrade (5 retries) → reboot check → reboot? → proxy wait
  except: FAILED record

Phase 3 (run_manager_update): manager LXC self-update on localhost.
  try: apt dist-upgrade (ignore_errors) → reboot-required check
  except: FAILED record
  Never reboots — that would kill the run.

All decisions are here in Python; Executor + http do the actual work.
Status strings come from proxmox_fleet.status (byte-parity with the old Jinja).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from proxmox_fleet import http as http_mod
from proxmox_fleet.changes import pkg_changed as _pkg_changed
from proxmox_fleet.cluster import DEFAULT_CLUSTER, split_qualified
from proxmox_fleet.executor import Executor
from proxmox_fleet.flows._pkg import upgrade_cmd
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import ErrorEntry, NodeRecord
from proxmox_fleet.orchestration import retry
from proxmox_fleet.status import manager_status, node_status

# Nodes are always Debian/apt — no pkg_mgr detection step needed; the apt
# upgrade command (incl. LC_ALL=C locale pin) is shared with the other flows.

# Mirrors the "Robust Reboot Check" shell block (Phase 2 node OS update).
_REBOOT_CHECK = (
    "RUNNING=$(uname -r); "
    "LATEST=$(ls -1 /boot/vmlinuz-* | sort -V | tail -n1 | sed 's|.*vmlinuz-||'); "
    'if [ -f /var/run/reboot-required ] || [ "$RUNNING" != "$LATEST" ]; '
    "then echo reboot; else echo ok; fi"
)


@dataclass
class NodeFlowOutcome:
    """Everything the driver needs to fold this node/manager into the FleetState."""

    record: Optional[NodeRecord] = None
    changed: bool = False
    failed: bool = False
    error: Optional[ErrorEntry] = None


def run_node_update(
    node: str,
    executor: Executor,
    settings: GlobalSettings,
    *,
    dry_run: bool = False,
    cluster: str = DEFAULT_CLUSTER,
    _sleep: Callable[[float], None] = time.sleep,
) -> NodeFlowOutcome:
    """Run the Phase 2 node OS update flow for one Proxmox node. Never raises.

    Args:
        node:     Proxmox node inventory hostname (used in NodeRecord and error log).
        executor: Bound to the Proxmox node via SSH.
        settings: GlobalSettings from vars.yml.
        dry_run:  When True, simulate apt upgrade but apply no changes and skip reboot.
        cluster:  This node's cluster (inventory `cluster=`, default DEFAULT_CLUSTER).
                  Gates the manager-detection probe below when manager_lxc_id is
                  cluster-qualified.
        _sleep:   Injectable sleep for tests (replaces both retry delay and proxy settle).
    """
    outcome = NodeFlowOutcome()

    try:
        # ------------------------------------------------------------------
        # Is this node hosting the manager LXC? (skip reboot if so)
        #
        # manager_lxc_id may be a bare id ("121", matches in every cluster —
        # today's behaviour) or cluster-qualified ("alpha/121"). A qualified
        # id only ever refers to a container in that one cluster, so the probe
        # must not run against a different cluster's node — otherwise a
        # same-numbered, unrelated container there could be mistaken for the
        # manager and have its node's reboot wrongly suppressed.
        # ------------------------------------------------------------------
        is_manager = False
        if settings.manager_lxc_id:
            mgr_cluster, mgr_id = split_qualified(settings.manager_lxc_id)
            if mgr_cluster is None or mgr_cluster == cluster:
                chk = executor.run_shell(
                    f"pct list | grep -q '^{mgr_id} '; echo $?",
                    changed_when=False,
                    ignore_errors=True,
                )
                is_manager = chk.stdout.strip() == "0"

        # ------------------------------------------------------------------
        # Apt upgrade (5 retries, 30 s delay — mirrors Ansible retries/delay)
        # ------------------------------------------------------------------
        apt_cmd = upgrade_cmd("apt", dry_run=dry_run)

        def _apt() -> str:
            res = executor.run_shell(apt_cmd, ignore_errors=True)
            if res.failed and not dry_run:
                raise RuntimeError(
                    f"apt dist-upgrade failed (rc={res.rc}): {res.stderr or res.stdout}"
                )
            return res.stdout

        apt_stdout = retry(_apt, retries=settings.node_apt_retries,
                           delay=settings.node_apt_retry_delay, sleep=_sleep)
        apt_changed = _pkg_changed(apt_stdout, "apt")

        # ------------------------------------------------------------------
        # Robust reboot check: /var/run/reboot-required OR kernel mismatch
        # ------------------------------------------------------------------
        reboot_res = executor.run_shell(_REBOOT_CHECK, changed_when=False, ignore_errors=True)
        reboot_needed = "reboot" in reboot_res.stdout

        # ------------------------------------------------------------------
        # Reboot (not in dry-run, not on manager-host node, only when needed)
        # ------------------------------------------------------------------
        rebooted = False
        if reboot_needed and not is_manager and not dry_run and settings.node_auto_reboot:
            executor.reboot(timeout=900)
            rebooted = True
            # Wait for apt proxy to come back, then give it 15 s to settle
            if settings.apt_proxy_ip:
                http_mod.wait_for_port(
                    settings.apt_proxy_ip,
                    settings.apt_proxy_port,
                    timeout=settings.node_reboot_port_wait_timeout,
                )
            _sleep(15)

        # ------------------------------------------------------------------
        # Report (nodes always get a record — no idle suppression)
        # ------------------------------------------------------------------
        status = node_status(apt_changed, reboot_needed, rebooted, is_manager)
        outcome.changed = apt_changed or rebooted or (reboot_needed and is_manager)
        outcome.record = NodeRecord(node=node, status=status)
        return outcome

    except Exception as exc:  # noqa: BLE001 - mirror Ansible rescue catch-all
        failed_task = getattr(exc, "step_name", type(exc).__name__)
        outcome.failed = True
        outcome.record = NodeRecord(node=node, status="FAILED")
        outcome.error = ErrorEntry(
            host=node, task=str(failed_task), error=str(exc)[:300]
        )
        return outcome


def run_manager_update(
    executor: Executor,
    settings: GlobalSettings,
    *,
    dry_run: bool = False,
) -> NodeFlowOutcome:
    """Run the Phase 3 manager self-update on localhost. Never raises. Never reboots.

    Args:
        executor: Bound to localhost (the manager LXC).
        settings: GlobalSettings from vars.yml.
        dry_run:  When True, simulate apt upgrade but apply no changes.
    """
    outcome = NodeFlowOutcome()

    try:
        # ------------------------------------------------------------------
        # Apt upgrade (ignore_errors — Phase 3 continues even if apt fails)
        # ------------------------------------------------------------------
        apt_cmd = upgrade_cmd("apt", dry_run=dry_run)
        res = executor.run_shell(apt_cmd, ignore_errors=True)
        # Only count as changed when apt actually succeeded
        apt_changed = (not res.failed) and _pkg_changed(res.stdout, "apt")

        # ------------------------------------------------------------------
        # Check /var/run/reboot-required (stat equivalent)
        # ------------------------------------------------------------------
        reboot_res = executor.run_shell(
            "test -f /var/run/reboot-required && echo reboot || echo ok",
            changed_when=False,
            ignore_errors=True,
        )
        reboot_needed = "reboot" in reboot_res.stdout

        # ------------------------------------------------------------------
        # Report — never reboot (would kill the run)
        # ------------------------------------------------------------------
        status = manager_status(apt_changed, reboot_needed)
        outcome.changed = apt_changed
        outcome.record = NodeRecord(node="Ansible-Manager", status=status)
        return outcome

    except Exception as exc:  # noqa: BLE001
        failed_task = getattr(exc, "step_name", type(exc).__name__)
        outcome.failed = True
        outcome.record = NodeRecord(node="Ansible-Manager", status="FAILED")
        outcome.error = ErrorEntry(
            host="Ansible-Manager", task=str(failed_task), error=str(exc)[:300]
        )
        return outcome
