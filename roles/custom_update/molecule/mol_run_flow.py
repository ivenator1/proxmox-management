#!/usr/bin/env python3
"""Molecule converge helper — drives flows/custom.py via RunnerExecutor.

Called from each scenario's converge.yml. Loads a config YAML, builds a
RunnerExecutor bound to the target host (localhost in molecule), runs
run_custom_update(), folds the outcome into a FleetState, and writes the
state JSON with fleet_* keys so verify.yml's existing assertions work unchanged.

Shell stubs installed by prepare.yml in /tmp/mol_stubs/ are on PATH (set in
molecule.yml provisioner env and inherited by subprocess inside ansible-runner).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

# mol_run_flow.py lives at roles/custom_update/molecule/mol_run_flow.py.
# parents[3] is the project root (proxmox-management/).
_project_root = Path(__file__).resolve().parents[3]
# Ensure ansible-runner finds ansible/primitives/ relative to the project root.
os.chdir(_project_root)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from proxmox_fleet.executor import RunnerExecutor
from proxmox_fleet.flows.custom import run_custom_update
from proxmox_fleet.models.config import CustomConfig
from proxmox_fleet.models.state import FleetState
from proxmox_fleet.runner import PrimitiveResult


class MolCustomExecutor(RunnerExecutor):
    """RunnerExecutor with a scriptable snapshot() for molecule runs.

    snapshot() cannot use the real proxmox_snap primitive (needs PVE API).
    Instead it writes a touch file that verify.yml can assert on, and returns
    changed=snap_changed (passed in at construction) — mirroring the
    MolLxcExecutor in roles/lxc_update/molecule/mol_run_flow.py.
    """

    def __init__(
        self,
        host: str,
        *,
        inventory: str,
        snap_changed: bool = True,
        snap_touch_prefix: str = "/tmp/mol_custom",
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
    ap = argparse.ArgumentParser(description="Molecule flow driver for custom_update scenarios.")
    ap.add_argument("--config-file", required=True, help="Path to the scenario config YAML.")
    ap.add_argument("--host", default="localhost", help="Inventory hostname to bind the executor to.")
    ap.add_argument("--inventory", default="/tmp/mol_hosts.ini", help="Ansible inventory for RunnerExecutor.")
    ap.add_argument("--state-out", default="/tmp/mol_fleet_state.json", help="Output path for fleet state JSON.")
    ap.add_argument("--dry-run", action="store_true", help="Pass dry_run=True to run_custom_update.")
    ap.add_argument("--dep-failed", action="store_true", help="Simulate a failed dependency (dep_failed=True).")
    ap.add_argument("--pve-snapshots", action="store_true",
                    help="Enable the PVE snapshot path: stubbed snapshot() + a node "
                         "executor bound to --host (pct/qm stubs must be on PATH).")
    ap.add_argument("--snap-fails", action="store_true",
                    help="snapshot() returns changed=False (simulate API failure).")
    args = ap.parse_args()

    raw = yaml.safe_load(Path(args.config_file).read_text(encoding="utf-8")) or {}
    config = CustomConfig.model_validate(raw)

    snap_kwargs: dict = {}
    if args.pve_snapshots:
        executor: RunnerExecutor = MolCustomExecutor(
            args.host, inventory=args.inventory, snap_changed=not args.snap_fails,
        )
        snap_kwargs = {
            # The "node" executor is localhost too — the pct/qm stubs on PATH
            # stand in for the Proxmox node.
            "node_executor": RunnerExecutor(args.host, inventory=args.inventory),
            "api_params": {"api_host": "127.0.0.1", "api_user": "root@pam",
                           "api_token_id": "mol", "api_token_secret": "mol-secret"},
            "snapshot_retries": 0,
            "snapshot_retry_delay": 0,
            "_sleep": lambda s: None,  # skip the 12×10s rollback status poll delays
        }
    else:
        executor = RunnerExecutor(args.host, inventory=args.inventory)

    outcome = run_custom_update(
        args.host,
        config,
        executor,
        dry_run=args.dry_run,
        dep_failed=args.dep_failed,
        **snap_kwargs,
    )

    state = FleetState()
    if outcome.record is not None:
        state.custom.append(outcome.record)
    if outcome.changed:
        state.changed = True
    if outcome.failed:
        state.failed = True
    if outcome.error is not None:
        state.errors.append(outcome.error)
    state.warnings.extend(outcome.warnings)

    state.dump_for_ansible(args.state_out)
    print(f"mol_run_flow: wrote state to {args.state_out}")
    print(f"  custom records: {len(state.custom)}, changed={state.changed}, failed={state.failed}")


if __name__ == "__main__":
    main()
