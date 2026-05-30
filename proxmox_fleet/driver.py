"""Python driver for Phase 0a + 0b — custom system updates.

Replaces the Ansible ``custom_update`` role and fleet-update.yml Phase 0a/0b
logic. Invoked by ``cli.py`` when ``--use-custom-flow`` is set; the playbook
is told to skip its Phase 0b via the ``skip_phase_0b=true`` extravar.

Conventions:
- All decisions are here in Python; Ansible executes only via RunnerExecutor.
- deep_merge: dict values recurse, list values REPLACE — matching
  ``combine(recursive=true)`` documented Ansible behaviour.
- State is returned as a FleetState and also written to a JSON file via
  dump_for_ansible() so fleet-update.yml can seed localhost facts before
  Phases 1–3 run their fleet-state-append.yml accumulations.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set, Union

import yaml

from proxmox_fleet import deps, inventory, window
from proxmox_fleet.executor import RunnerExecutor
from proxmox_fleet.flows.custom import run_custom_update
from proxmox_fleet.flows.lxc import LxcFlowOutcome, _discover_lxcs, run_lxc_update
from proxmox_fleet.inventory import HostSpec
from proxmox_fleet.models.config import CustomConfig
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import ErrorEntry, FleetState
from proxmox_fleet.orchestration import run_concurrent


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base. List values REPLACE (not extend).

    Matches Ansible combine(recursive=true) documented behaviour.
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_config(spec: HostSpec, configs_dir: str) -> CustomConfig:
    """Load configs/<name>.yml, deep-merge custom_overrides, validate."""
    path = Path(configs_dir) / f"{spec.custom_config}.yml"
    raw: Dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if spec.custom_overrides:
        raw = _deep_merge(raw, spec.custom_overrides)
    return CustomConfig.model_validate(raw)


def run_custom_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    extra_vars: Optional[Dict[str, Any]] = None,
    check: bool = False,
    state_output_path: Union[str, Path] = "/tmp/fleet_custom_state.json",
) -> FleetState:
    """Run Phase 0a dependency validation + Phase 0b custom host updates.

    Writes the resulting FleetState to *state_output_path* via
    dump_for_ansible() so fleet-update.yml can load it with include_vars.

    Raises SystemExit(1) on Phase 0a dependency-order problems (mirrors the
    ``assert`` task in fleet-update.yml that fails loud on bad ordering).
    Never raises for per-host execution failures — those are captured into the
    FleetState as FAILED records, matching run_custom_update() semantics.
    """
    ev = extra_vars or {}

    # Phase 0a: load inventory and validate dependency order.
    specs = inventory.load_custom_hosts(inventory_path, host_vars_dir=settings.host_vars_dir)
    hosts = [s.name for s in specs]
    dep_map = {s.name: s.depends_on for s in specs}

    problems = deps.validate_depends_order(hosts, dep_map)
    if problems:
        for p in problems:
            print(f"FATAL: {p}", file=sys.stderr)
        raise SystemExit(1)

    # Phase 0b: serial execution loop.
    state = FleetState()
    failed_hosts: Set[str] = set()

    for spec in specs:
        # Maintenance window gate (silently skip, mirroring role behaviour).
        if spec.maintenance_window is not None:
            force = settings.force_window or bool(ev.get("force_window", False))
            if not window.in_window(spec.maintenance_window, force=force):
                continue

        dep_failed = deps.dependency_failed(spec.name, spec.depends_on, failed_hosts)

        # fleet_dry_run | custom_dry_run from settings OR from -e extra vars.
        dry_run = (
            settings.fleet_dry_run
            or settings.custom_dry_run
            or bool(ev.get("fleet_dry_run", False))
            or bool(ev.get("custom_dry_run", False))
        )

        config = _load_config(spec, settings.configs_dir)

        executor = RunnerExecutor(spec.name, inventory=inventory_path, check=check)

        outcome = run_custom_update(
            spec.name,
            config,
            executor,
            dry_run=dry_run,
            dep_failed=dep_failed,
            allow_reboot=settings.custom_allow_reboot,
            kuma_url=settings.kuma_url,
            kuma_retries=settings.kuma_health_check_retries,
            kuma_delay=settings.kuma_health_check_delay,
        )

        if outcome.record is not None:
            state.custom.append(outcome.record)
        if outcome.changed:
            state.changed = True
        if outcome.failed:
            state.failed = True
            failed_hosts.add(spec.name)
        if outcome.error is not None:
            state.errors.append(outcome.error)
        state.warnings.extend(outcome.warnings)

    state.dump_for_ansible(state_output_path)
    return state


def _fold_lxc_outcome(
    state: FleetState,
    lxc_id: str,
    outcome: LxcFlowOutcome,
) -> None:
    """Merge one container's outcome into the running FleetState."""
    if outcome.record is not None:
        state.lxc.append(outcome.record)
    if outcome.changed:
        state.changed = True
    if outcome.failed:
        state.failed = True
    if outcome.error is not None:
        state.errors.append(outcome.error)
    state.warnings.extend(outcome.warnings)


def run_lxc_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Union[str, Path] = "/tmp/fleet_lxc_state.json",
) -> FleetState:
    """Run Phase 1 (LXC container updates) via the Python driver.

    For each Proxmox node: discovers tagged LXCs, then updates them
    concurrently (max_workers=lxc_forks, default 20), mirroring Ansible's
    forks setting. Nodes are processed serially.

    Writes the resulting FleetState via dump_for_ansible() so fleet-update.yml
    can seed fleet_lxc_data / fleet_changed / fleet_failed before Phase 1b.

    Never raises for per-container failures — those become FAILED records.
    """
    nodes = inventory.load_proxmox_nodes(inventory_path)
    dry_run = check or settings.fleet_dry_run or settings.lxc_dry_run
    state = FleetState()

    for node_info in nodes:
        node_name = node_info["name"]
        api_host = node_info["ansible_host"]

        executor = RunnerExecutor(node_name, inventory=inventory_path, check=check)
        # Discovery is read-only — never pass --check or pct list won't run remotely.
        discovery_executor = RunnerExecutor(node_name, inventory=inventory_path, check=False)

        # Discover which LXCs are on this node (filters exclude_list)
        print(f"[{node_name}] discovering LXCs (first run loads Ansible — may take a moment)...")
        try:
            lxc_ids = _discover_lxcs(discovery_executor, settings)
        except Exception as exc:  # noqa: BLE001
            state.failed = True
            state.errors.append(ErrorEntry(
                host=node_name, task="discover_lxcs", error=str(exc)[:300]
            ))
            continue

        print(f"[{node_name}] found {len(lxc_ids)} tagged LXC(s): {', '.join(lxc_ids) or 'none'}")

        # Concurrent per-container updates (lxc_continue_on_error is the default)
        def _run_one(lxc_id: str) -> LxcFlowOutcome:
            ex = RunnerExecutor(node_name, inventory=inventory_path, check=check)
            return run_lxc_update(
                node_name, lxc_id, ex, settings,
                dry_run=dry_run, api_host=api_host,
            )

        results = run_concurrent(
            lxc_ids,
            _run_one,
            max_workers=settings.lxc_forks,
        )

        for lxc_id, outcome, run_err in results:
            if outcome is not None:
                _fold_lxc_outcome(state, lxc_id, outcome)
            elif run_err is not None:
                state.failed = True
                state.errors.append(ErrorEntry(
                    host=str(lxc_id),
                    task="run_lxc_update",
                    error=str(run_err)[:300],
                ))

    state.dump_for_ansible(state_output_path)
    return state
