#!/usr/bin/env python3
"""Molecule converge helper — drives flows/lxc.py via RunnerExecutor.

Called from each scenario's converge.yml. Builds a RunnerExecutor bound to
localhost (with /tmp/mol_stubs/ on PATH via prepare.yml), runs run_lxc_update(),
folds the outcome into a FleetState, and writes the state JSON with fleet_* keys
so verify.yml can load it with include_vars.

The MolLxcExecutor subclass overrides snapshot() because community.proxmox
cannot run in molecule without real PVE infrastructure — it is replaced by a
touch-file side-effect stub that validate.yml can inspect.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

# mol_run_flow.py lives at roles/lxc_update/molecule/mol_run_flow.py.
# parents[3] is the project root (proxmox-management/).
_project_root = Path(__file__).resolve().parents[3]
# Ensure ansible-runner finds ansible/primitives/ relative to the project root.
os.chdir(_project_root)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from proxmox_fleet.executor import RunnerExecutor
from proxmox_fleet.flows.lxc import run_lxc_update
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import FleetState
from proxmox_fleet.runner import PrimitiveResult


class MolLxcExecutor(RunnerExecutor):
    """RunnerExecutor with a scriptable snapshot() for molecule runs.

    snapshot() cannot use the real proxmox_snap primitive (needs PVE API).
    Instead it writes a touch file that verify.yml can assert on, and
    returns changed=snap_changed (passed in at construction).
    """

    def __init__(
        self,
        host: str,
        *,
        inventory: str,
        snap_changed: bool = True,
        snap_touch_prefix: str = "/tmp/mol",
    ) -> None:
        super().__init__(host, inventory=inventory)
        self._snap_changed = snap_changed
        self._snap_touch_prefix = snap_touch_prefix

    def snapshot(self, vmid: str, *, snap_state: str, **api_params: Any) -> PrimitiveResult:
        if snap_state == "present":
            Path(f"{self._snap_touch_prefix}_snap_created").touch()
        else:
            Path(f"{self._snap_touch_prefix}_snap_deleted").touch()
        return PrimitiveResult(
            rc=0, changed=self._snap_changed,
            failed=not self._snap_changed,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Molecule flow driver for lxc_update scenarios.")
    ap.add_argument("--lxc-id", default="101", help="Container VMID to update.")
    ap.add_argument("--node", default="pve-01", help="Inventory hostname of the Proxmox node.")
    ap.add_argument("--host", default="localhost", help="Inventory hostname to bind the executor to.")
    ap.add_argument("--inventory", default="/tmp/mol_hosts.ini", help="Ansible inventory for RunnerExecutor.")
    ap.add_argument("--state-out", default="/tmp/mol_fleet_state.json", help="Output path for fleet state JSON.")
    ap.add_argument("--dry-run", action="store_true", help="Pass dry_run=True to run_lxc_update.")
    ap.add_argument("--strategy", default="snapshot", help="lxc_backup_strategy value.")
    ap.add_argument("--snap-changed", action="store_true", default=True,
                    help="snapshot() returns changed=True (default).")
    ap.add_argument("--snap-fails", action="store_true",
                    help="snapshot() returns changed=False (simulate API failure).")
    ap.add_argument("--kuma-url", default="", help="Kuma URL (empty = no health check).")
    ap.add_argument("--kuma-monitor-id", default="", help="Monitor ID to poll (empty = no check).")
    args = ap.parse_args()

    snap_changed = not args.snap_fails

    settings = GlobalSettings.model_validate({
        "lxc_backup_strategy": args.strategy,
        "lxc_backup_storage": "local",
        "lxc_auto_reboot": False,
        "lxc_unattended": True,
        "kuma_url": args.kuma_url,
        "kuma_slug": "mol-fleet",
        "kuma_health_check_retries": 1,
        "kuma_health_check_delay": 0,
        "lxc_kuma_map": {args.lxc_id: int(args.kuma_monitor_id)} if args.kuma_monitor_id else {},
        "pve_api_user": "root@pam",
        "pve_api_token_id": "mol",
        "pve_api_token_secret": "mol-secret",
    })

    executor = MolLxcExecutor(
        args.host,
        inventory=args.inventory,
        snap_changed=snap_changed,
    )

    outcome = run_lxc_update(
        args.node,
        args.lxc_id,
        executor,
        settings,
        dry_run=args.dry_run,
        api_host="127.0.0.1",
    )

    state = FleetState()
    if outcome.record is not None:
        state.lxc.append(outcome.record)
    if outcome.changed:
        state.changed = True
    if outcome.failed:
        state.failed = True
    if outcome.error is not None:
        state.errors.append(outcome.error)
    state.warnings.extend(outcome.warnings)

    state.dump_for_ansible(args.state_out)
    print(f"mol_run_flow: wrote state to {args.state_out}")
    print(f"  lxc records: {len(state.lxc)}, changed={state.changed}, failed={state.failed}")
    if state.warnings:
        for w in state.warnings:
            print(f"  warning: {w.warning[:80]}")


if __name__ == "__main__":
    main()
