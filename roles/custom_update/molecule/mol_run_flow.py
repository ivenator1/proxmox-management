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


def main() -> None:
    ap = argparse.ArgumentParser(description="Molecule flow driver for custom_update scenarios.")
    ap.add_argument("--config-file", required=True, help="Path to the scenario config YAML.")
    ap.add_argument("--host", default="localhost", help="Inventory hostname to bind the executor to.")
    ap.add_argument("--inventory", default="/tmp/mol_hosts.ini", help="Ansible inventory for RunnerExecutor.")
    ap.add_argument("--state-out", default="/tmp/mol_fleet_state.json", help="Output path for fleet state JSON.")
    ap.add_argument("--dry-run", action="store_true", help="Pass dry_run=True to run_custom_update.")
    ap.add_argument("--dep-failed", action="store_true", help="Simulate a failed dependency (dep_failed=True).")
    args = ap.parse_args()

    raw = yaml.safe_load(Path(args.config_file).read_text(encoding="utf-8")) or {}
    config = CustomConfig.model_validate(raw)

    executor = RunnerExecutor(args.host, inventory=args.inventory)

    outcome = run_custom_update(
        args.host,
        config,
        executor,
        dry_run=args.dry_run,
        dep_failed=args.dep_failed,
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
