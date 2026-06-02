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

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import yaml

from proxmox_fleet import deps, inventory, window
from proxmox_fleet.executor import RunnerExecutor
from proxmox_fleet.flows.custom import run_custom_update
from proxmox_fleet.flows.lxc import LxcFlowOutcome, _discover_lxcs, run_lxc_update
from proxmox_fleet.flows.node import NodeFlowOutcome, run_manager_update, run_node_update
from proxmox_fleet.flows.remote import RemoteFlowOutcome, run_remote_update
from proxmox_fleet.flows.vm import VmFlowOutcome, run_vm_update
from proxmox_fleet.inventory import HostSpec, RemoteHostSpec, VmSpec
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
                rec = outcome.record
                if rec is not None:
                    status = "FAILED" if outcome.failed else f"app={rec.app}  os={rec.os}"
                    print(f"  [{node_name}/{lxc_id}] {rec.name}: {status}")
                else:
                    print(f"  [{node_name}/{lxc_id}] idle (no changes)")
            elif run_err is not None:
                state.failed = True
                state.errors.append(ErrorEntry(
                    host=str(lxc_id),
                    task="run_lxc_update",
                    error=str(run_err)[:300],
                ))
                print(f"  [{node_name}/{lxc_id}] ERROR: {run_err}")

    state.dump_for_ansible(state_output_path)
    return state


def _discover_vm_locations(
    nodes: List[Dict[str, str]],
    *,
    inventory_path: str,
) -> Dict[str, Tuple[str, str]]:
    """Query the Proxmox cluster for current VM locations via pvesh over SSH.

    Runs ``pvesh get /cluster/resources --type vm`` on the first available node.
    Returns {vmid_str: (node_name, ansible_host_ip)}.
    Returns an empty dict on any failure (caller falls back to pve_node hint).

    Rationale: pvesh reuses the existing SSH executor pattern, adds no new
    SSL/auth surface, and runs as root so no API token is needed.
    """
    if not nodes:
        return {}

    nodes_map = {n["name"]: n["ansible_host"] for n in nodes}
    # Use the first node — cluster resources are visible from any member.
    first_node = nodes[0]["name"]
    # check=False: discovery is read-only and must run even in dry-run mode.
    disc_executor = RunnerExecutor(first_node, inventory=inventory_path, check=False)

    try:
        res = disc_executor.run_shell(
            "pvesh get /cluster/resources --type vm --output-format json 2>/dev/null",
            changed_when=False,
            ignore_errors=True,
        )
        if res.failed or not res.stdout.strip():
            return {}
        resources = json.loads(res.stdout)
        result: Dict[str, Tuple[str, str]] = {}
        for item in (resources if isinstance(resources, list) else []):
            vmid = str(item.get("vmid", ""))
            node_name = str(item.get("node", ""))
            if vmid and node_name:
                result[vmid] = (node_name, nodes_map.get(node_name, node_name))
        return result
    except Exception:  # noqa: BLE001
        return {}


def _fold_vm_outcome(state: FleetState, vm_name: str, outcome: VmFlowOutcome) -> None:
    if outcome.record is not None:
        state.vm.append(outcome.record)
    if outcome.changed:
        state.changed = True
    if outcome.failed:
        state.failed = True
    if outcome.error is not None:
        state.errors.append(outcome.error)
    state.warnings.extend(outcome.warnings)


def run_vm_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Union[str, Path] = "/tmp/fleet_vm_state.json",
) -> FleetState:
    """Run Phase 1b (QEMU VM updates) via the Python driver.

    Loads all VMs from inventory, runs them concurrently (max_workers=vm_forks),
    and writes the resulting FleetState so fleet-update.yml can merge it before
    Phase 2.

    Never raises for per-VM failures — those become FAILED records.
    """
    vms = inventory.load_proxmox_vms(inventory_path)
    nodes = inventory.load_proxmox_nodes(inventory_path)
    nodes_map = {n["name"]: n["ansible_host"] for n in nodes}

    dry_run = check or settings.fleet_dry_run or settings.vm_dry_run
    state = FleetState()

    # Discover VM locations once up-front via pvesh. Live cluster state always
    # wins over static pve_node inventory hints — pve_node is only a fallback
    # for non-cluster / non-HA setups where discovery is unavailable.
    vm_locations = _discover_vm_locations(nodes, inventory_path=inventory_path)
    if vm_locations:
        print(f"[vm phase] discovered {len(vm_locations)} VM location(s) from cluster")
    else:
        print("[vm phase] cluster discovery unavailable — using pve_node inventory hints")

    def _run_one(vm_spec: VmSpec) -> VmFlowOutcome:
        # Maintenance window gate
        if vm_spec.maintenance_window is not None:
            if not window.in_window(vm_spec.maintenance_window, force=settings.force_window):
                return VmFlowOutcome()  # silently skipped

        # Prefer live cluster location; fall back to inventory hint.
        if vm_spec.vmid in vm_locations:
            node_name, api_host = vm_locations[vm_spec.vmid]
        elif vm_spec.pve_node:
            node_name = vm_spec.pve_node
            api_host = nodes_map.get(node_name, node_name)
        else:
            raise RuntimeError(
                f"Cannot determine node for vmid {vm_spec.vmid} ({vm_spec.name}): "
                "cluster discovery failed and no pve_node set in inventory"
            )

        vm_ex = RunnerExecutor(vm_spec.name, inventory=inventory_path, check=check)
        node_ex = RunnerExecutor(node_name, inventory=inventory_path, check=check)
        return run_vm_update(
            node_name, vm_spec.vmid, vm_spec.name, vm_ex, node_ex, settings,
            dry_run=dry_run, api_host=api_host,
        )

    results = run_concurrent(vms, _run_one, max_workers=settings.vm_forks)

    for vm_spec, outcome, run_err in results:
        vm_name = vm_spec.name if isinstance(vm_spec, VmSpec) else str(vm_spec)
        if outcome is not None:
            _fold_vm_outcome(state, vm_name, outcome)
            rec = outcome.record
            if rec is not None:
                status = "FAILED" if outcome.failed else rec.status
                print(f"  [{vm_name}] {status}")
            else:
                print(f"  [{vm_name}] idle (no changes)")
        elif run_err is not None:
            state.failed = True
            state.errors.append(ErrorEntry(
                host=vm_name, task="run_vm_update", error=str(run_err)[:300]
            ))
            print(f"  [{vm_name}] ERROR: {run_err}")

    state.dump_for_ansible(state_output_path)
    return state


def _fold_remote_outcome(
    state: FleetState, host_name: str, outcome: RemoteFlowOutcome
) -> None:
    if outcome.record is not None:
        state.remote.append(outcome.record)
    if outcome.changed:
        state.changed = True
    if outcome.failed:
        state.failed = True
    if outcome.error is not None:
        state.errors.append(outcome.error)
    state.warnings.extend(outcome.warnings)


def run_remote_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Union[str, Path] = "/tmp/fleet_remote_state.json",
) -> FleetState:
    """Run Phase 0 (remote host updates) via the Python driver.

    Loads all [remote_hosts] from inventory, runs them concurrently
    (max_workers=remote_forks), and writes the resulting FleetState so
    fleet-update.yml can merge it before Phase 0a.

    Never raises for per-host failures — those become FAILED records.
    """
    hosts = inventory.load_remote_hosts(inventory_path)
    dry_run = check or settings.fleet_dry_run or settings.remote_dry_run
    state = FleetState()

    def _run_one(spec: RemoteHostSpec) -> RemoteFlowOutcome:
        # Maintenance window gate
        if spec.maintenance_window is not None:
            if not window.in_window(spec.maintenance_window, force=settings.force_window):
                return RemoteFlowOutcome()  # silently skipped

        ex = RunnerExecutor(spec.name, inventory=inventory_path, check=check)
        return run_remote_update(
            spec.name, ex, settings, dry_run=dry_run, pre_update_cmd=spec.pre_update_cmd
        )

    results = run_concurrent(hosts, _run_one, max_workers=settings.remote_forks)

    for spec, outcome, run_err in results:
        host_name = spec.name if isinstance(spec, RemoteHostSpec) else str(spec)
        if outcome is not None:
            _fold_remote_outcome(state, host_name, outcome)
            rec = outcome.record
            if rec is not None:
                status = "FAILED" if outcome.failed else rec.status
                print(f"  [{host_name}] {status}")
            else:
                print(f"  [{host_name}] idle (no changes)")
        elif run_err is not None:
            state.failed = True
            state.errors.append(ErrorEntry(
                host=host_name, task="run_remote_update", error=str(run_err)[:300]
            ))
            print(f"  [{host_name}] ERROR: {run_err}")

    state.dump_for_ansible(state_output_path)
    return state


def _fold_node_outcome(state: FleetState, outcome: NodeFlowOutcome) -> None:
    if outcome.record is not None:
        state.node.append(outcome.record)
    if outcome.changed:
        state.changed = True
    if outcome.failed:
        state.failed = True
    if outcome.error is not None:
        state.errors.append(outcome.error)


def run_node_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Union[str, Path] = "/tmp/fleet_node_state.json",
) -> FleetState:
    """Run Phase 2 (node OS updates) + Phase 3 (manager self-update) via the Python driver.

    Node processing is serial (one node at a time) and aborts on the first failure
    — mirroring Ansible's ``any_errors_fatal: true`` / ``serial: 1``. The manager
    self-update always runs regardless of node failures (Phase 3 is ``ignore_errors``
    in the legacy playbook and is independent of Phase 2 outcomes).

    Writes the resulting FleetState via dump_for_ansible() so fleet-update.yml can
    seed fleet_node_data / fleet_changed / fleet_failed before Phase 4.

    Never raises for per-node failures — those become FAILED records.
    """
    nodes = inventory.load_proxmox_nodes(inventory_path)
    dry_run = check or settings.fleet_dry_run or settings.node_dry_run
    state = FleetState()

    # Phase 2: serial node loop — abort on first failure (any_errors_fatal equivalent).
    for node_info in nodes:
        node_name = node_info["name"]
        executor = RunnerExecutor(node_name, inventory=inventory_path, check=check)
        outcome = run_node_update(node_name, executor, settings, dry_run=dry_run)
        _fold_node_outcome(state, outcome)
        status = outcome.record.status if outcome.record else "?"
        print(f"  [{node_name}] {status}")
        if outcome.failed:
            break  # abort remaining nodes; manager update still runs

    # Phase 3: manager self-update always runs — independent of node failures.
    manager_ex = RunnerExecutor("localhost", inventory=inventory_path, check=check)
    manager_outcome = run_manager_update(manager_ex, settings, dry_run=dry_run)
    _fold_node_outcome(state, manager_outcome)
    mgr_status = manager_outcome.record.status if manager_outcome.record else "?"
    print(f"  [Ansible-Manager] {mgr_status}")

    state.dump_for_ansible(state_output_path)
    return state
