"""The fleet driver — the Python orchestrator and per-phase runners.

``run_fleet()`` is the entrypoint ``cli.py`` calls: it runs the pre-flight
proxy check and every phase in order (remote → custom → lxc → vm →
node+manager → notify), merging each phase's :class:`FleetState` into one
fleet-wide state. The per-phase ``run_*_phase()`` helpers can also be called
individually (used by tests and the molecule harness).

Conventions:
- All decisions are here in Python; Ansible executes only via RunnerExecutor.
- deep_merge: dict values recurse, list values REPLACE — matching
  ``combine(recursive=true)`` documented Ansible behaviour.
- Each phase returns a FleetState; ``_merge_state()`` folds them together
  (the in-Python replacement for the old "Merge Python … state" plays). The
  ``state_output_path`` arg still dumps per-phase JSON when set, for tooling.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import yaml

from proxmox_fleet import briefing, deps, history, http, inventory, notifiers, window
from proxmox_fleet.cluster import (
    DEFAULT_CLUSTER,
    api_creds,
    limit_selects_id,
    map_lookup,
    matches_any,
    token_is_id,
)
from proxmox_fleet.executor import RunnerExecutor
from proxmox_fleet.flows._pkg import kuma_healthy
from proxmox_fleet.flows.custom import run_custom_update
from proxmox_fleet.flows.lxc import LxcFlowOutcome, _discover_lxcs, run_lxc_update
from proxmox_fleet.flows.node import run_manager_update, run_node_update
from proxmox_fleet.flows.remote import RemoteFlowOutcome, run_remote_update
from proxmox_fleet.flows.vm import VmFlowOutcome, run_vm_update
from proxmox_fleet.inventory import HostSpec, RemoteHostSpec, VmSpec
from proxmox_fleet.models.config import CustomConfig
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import (
    ErrorEntry,
    FleetState,
    LxcRecord,
    NodeRecord,
    RemoteRecord,
    VmRecord,
    WarningEntry,
)
from proxmox_fleet.orchestration import run_concurrent
from proxmox_fleet.runner import (
    UNREACHABLE_MARKERS,
    UnreachableHostError,
    is_unreachable_error,
)


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


def _truthy(val: Any) -> bool:
    """Coerce an extra-var value to bool — '-e' values arrive as raw strings,
    so 'false'/'0'/'no' must be False (bool('false') is True)."""
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


# Phase names accepted by run_fleet(phases=...) / the --phases flag, in run order.
# "node" is Phase 2 (PVE node OS updates), "manager" is Phase 3 (self-update) —
# both execute inside run_node_phase() but are individually selectable.
PHASE_NAMES = ("remote", "custom", "lxc", "vm", "node", "manager")

# Limit tokens that address the manager self-update (it is not an inventory host).
_MANAGER_LIMIT_TOKENS = frozenset({"manager", "localhost", "Ansible-Manager"})

# Status recorded for hosts whose wave was aborted by a canary failure.
CANARY_SKIP_STATUS = "SKIPPED (canary failed)"


def _soak_canaries(
    settings: GlobalSettings,
    monitor_map: Dict[str, str],
    tokens: Sequence[str],
    *,
    _sleep: Callable[[float], None] = time.sleep,
) -> Optional[str]:
    """Soak after a successful canary wave: wait ``canary_soak_minutes``, then
    poll Kuma for every canary token that has a monitor mapping.

    *monitor_map* is a pre-resolved ``{display_token: monitor_id}`` dict built
    by the caller — the lxc phase resolves it via ``cluster.map_lookup()``
    (qualified-id aware, display token ``f"{cluster}/{id}"``); the vm/remote
    phases pass ``{inventory_name: monitor_id}`` since their kuma maps are
    keyed by inventory name and stay unqualified. *tokens* is the ordered
    list of canary display tokens (for a stable "no mapping" skip and the
    error message).

    Returns None when all monitored canaries are healthy (or nothing is
    monitored), else an error message — the caller aborts the second wave.
    """
    if settings.canary_soak_minutes > 0:
        _sleep(settings.canary_soak_minutes * 60)
    if not settings.kuma_url:
        return None
    url = f"{settings.kuma_url}/api/status-page/heartbeat/{settings.kuma_slug}"
    for token in tokens:
        monitor_id = str(monitor_map.get(token, ""))
        if not monitor_id:
            continue
        try:
            http.poll_until(
                lambda: http.get_json(url),
                lambda p: kuma_healthy(p, monitor_id=monitor_id),
                retries=settings.kuma_health_check_retries,
                delay=settings.kuma_health_check_delay,
            )
        except Exception as exc:  # noqa: BLE001
            return f"canary {token} unhealthy after soak: {exc}"
    return None


def run_custom_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    extra_vars: Optional[Dict[str, Any]] = None,
    check: bool = False,
    state_output_path: Optional[Union[str, Path]] = "/tmp/fleet_custom_state.json",  # nosec B108 - tooling output path
    limit: Optional[Set[str]] = None,
) -> FleetState:
    """Run Phase 0a dependency validation + Phase 0b custom host updates.

    When *state_output_path* is set, writes the resulting FleetState via
    dump_for_ansible() for tooling / the molecule harness (whose verify.yml
    loads it with include_vars). run_fleet() passes None and merges in-memory.

    *limit* (host names) restricts execution to the listed hosts; everything
    else is silently omitted (mirroring the maintenance-window skip). Dependency
    *validation* still covers the full inventory — ordering is an inventory
    property, not a run property.

    Raises SystemExit(1) on Phase 0a dependency-order problems (fail-loud on bad
    ordering). Never raises for per-host execution failures — those are captured
    into the FleetState as FAILED records, matching run_custom_update() semantics.
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

    if limit is not None:
        specs = [s for s in specs if s.name in limit]

    # Phase 0b: serial execution loop.
    state = FleetState()
    failed_hosts: Set[str] = set()
    # Node name → {ansible_host, cluster} map, loaded lazily on the first
    # config that opts into PVE snapshots (pve_vmid). cluster is threaded
    # through so the snapshot picks up that cluster's API credential
    # override, if any (proxmox_fleet.cluster.api_creds).
    nodes_map: Optional[Dict[str, Dict[str, str]]] = None

    for spec in specs:
        # Maintenance window gate (silently skip, mirroring role behaviour).
        if spec.maintenance_window is not None:
            force = settings.force_window or _truthy(ev.get("force_window", False))
            if not window.in_window(spec.maintenance_window, force=force):
                continue

        dep_failed = deps.dependency_failed(spec.name, spec.depends_on, failed_hosts)

        # check | fleet_dry_run | custom_dry_run from settings OR from -e extra vars.
        dry_run = (
            check
            or settings.fleet_dry_run
            or settings.custom_dry_run
            or _truthy(ev.get("fleet_dry_run", False))
            or _truthy(ev.get("custom_dry_run", False))
        )

        config = _load_config(spec, settings.configs_dir)

        executor = RunnerExecutor(spec.name, inventory=inventory_path, check=check)

        # PVE snapshot/rollback wiring (config v2): resolve the owning node's
        # ansible_host for the snapshot API and bind a node executor for
        # pct/qm rollback. An unknown pve_node fails loud, like config validation.
        node_executor = None
        api_params: Optional[Dict[str, Any]] = None
        if config.pve_vmid.strip():
            if nodes_map is None:
                nodes_map = {
                    n["name"]: {"ansible_host": n["ansible_host"], "cluster": n["cluster"]}
                    for n in inventory.load_proxmox_nodes(
                        inventory_path, host_vars_dir=settings.host_vars_dir)
                }
            node_info = nodes_map.get(config.pve_node)
            if node_info is None:
                print(f"FATAL: config {spec.custom_config!r}: pve_node {config.pve_node!r} "
                      "is not in [proxmox_nodes]", file=sys.stderr)
                raise SystemExit(1)
            node_executor = RunnerExecutor(config.pve_node, inventory=inventory_path, check=check)
            api_params = {
                "api_host": node_info["ansible_host"],
                **api_creds(settings, node_info["cluster"]),
            }

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
            node_executor=node_executor,
            api_params=api_params,
            snapshot_retries=settings.snapshot_retries,
            snapshot_retry_delay=settings.snapshot_retry_delay,
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

    if state_output_path is not None:
        state.dump_for_ansible(state_output_path)
    return state


def _fold_outcome(state: FleetState, outcome: Any, bucket: List[Any]) -> None:
    """Merge one per-host flow outcome into the running FleetState.

    Appends ``outcome.record`` to ``bucket`` (the matching record list), OR-joins
    the changed/failed flags, and accumulates errors/warnings. Node outcomes carry
    no warnings, and only the lxc flow carries the plural ``errors`` list (its
    non-raising OS/app update failures), so both fields are read defensively.
    """
    if outcome.record is not None:
        bucket.append(outcome.record)
    if outcome.changed:
        state.changed = True
    if outcome.failed:
        state.failed = True
    if outcome.error is not None:
        state.errors.append(outcome.error)
    state.errors.extend(getattr(outcome, "errors", []))
    state.warnings.extend(getattr(outcome, "warnings", []))


# Kept as module-local names for the existing call sites and tests; the markers
# and the predicate itself now live in runner.py, next to UnreachableHostError,
# so scan.py can apply exactly the same rule.
_UNREACHABLE_MARKERS = UNREACHABLE_MARKERS


def _error_is_unreachable(text: str) -> bool:
    return is_unreachable_error(text)


def _cluster_quorate(
    nodes: List[Dict[str, str]],
    *,
    inventory_path: str,
    skip: Set[str],
) -> Optional[bool]:
    """Ask the first answering node (not in *skip*) whether the cluster is quorate.

    Returns True/False from ``pvesh get /cluster/status``, True for a standalone
    node (no cluster entry — quorum doesn't apply), or None when no node answered.
    Used to decide whether an unreachable node can be safely skipped: with quorum
    intact the rest of the fleet (including snapshots) still works.
    """
    for node_info in nodes:
        node_name = node_info["name"]
        if node_name in skip:
            continue
        ex = RunnerExecutor(node_name, inventory=inventory_path, check=False)
        try:
            res = ex.run_shell(
                "pvesh get /cluster/status --output-format json 2>/dev/null",
                changed_when=False,
                ignore_errors=True,
            )
            if res.failed or not res.stdout.strip():
                continue
            items = json.loads(res.stdout)
            for item in (items if isinstance(items, list) else []):
                if item.get("type") == "cluster":
                    return bool(item.get("quorate"))
            return True  # standalone node — no quorum concept
        except Exception:  # noqa: BLE001 — try the next node
            continue
    return None


def run_lxc_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Optional[Union[str, Path]] = "/tmp/fleet_lxc_state.json",  # nosec B108 - tooling output path
    limit: Optional[Set[str]] = None,
) -> FleetState:
    """Run Phase 1 (LXC container updates) via the Python driver.

    For each Proxmox node: discovers tagged LXCs, then updates them
    concurrently (max_workers=lxc_forks, default 20), mirroring Ansible's
    forks setting. Nodes are processed serially.

    *limit* may mix node names and container ids: a node name keeps that node's
    whole discovery, a bare id keeps just that container wherever it lives. A
    node is skipped before discovery when it isn't named and the limit holds no
    ids at all.

    Canary staging: container ids listed in ``settings.canary_hosts`` update
    first (across all nodes); the rest run only if no canary failed and the
    post-soak Kuma checks pass — otherwise they are recorded as
    ``SKIPPED (canary failed)``.

    When *state_output_path* is set, writes the resulting FleetState via
    dump_for_ansible() for tooling / molecule; run_fleet() passes None and
    merges each phase in-memory via _merge_state().

    Never raises for per-container failures — those become FAILED records.
    """
    nodes = inventory.load_proxmox_nodes(inventory_path, host_vars_dir=settings.host_vars_dir)
    dry_run = check or settings.fleet_dry_run or settings.lxc_dry_run
    state = FleetState()

    limit_has_ids = limit is not None and any(token_is_id(token) for token in limit)

    # Discovery pass: resolve every node's managed container ids up-front so
    # the canary wave can span nodes. A discovery failure on one node is
    # recorded and skips that node only — it never gates the canary waves.
    # Each tuple carries the node's cluster so every id-matching decision
    # below (limit, canary, kuma map) resolves qualified tokens correctly.
    discovered: List[Tuple[str, str, str, List[str]]] = []
    unreachable_nodes: List[str] = []
    for node_info in nodes:
        node_name = node_info["name"]
        api_host = node_info["ansible_host"]
        node_cluster = node_info["cluster"]

        # Node not targeted and no container ids to look for anywhere → skip
        # the discovery SSH round-trip entirely.
        if limit is not None and node_name not in limit and not limit_has_ids:
            continue

        # Discovery is read-only — never pass --check or pct list won't run remotely.
        discovery_executor = RunnerExecutor(node_name, inventory=inventory_path, check=False)

        # Discover which LXCs are on this node (filters exclude_list)
        print(f"[{node_name}] discovering LXCs (first run loads Ansible — may take a moment)...")
        try:
            lxc_ids = _discover_lxcs(discovery_executor, settings, cluster=node_cluster)
        except UnreachableHostError:
            # The node never answered — decided after the loop (quorum gate).
            unreachable_nodes.append(node_name)
            print(f"[{node_name}] WARNING: node unreachable — skipping its containers")
            continue
        except Exception as exc:  # noqa: BLE001
            state.failed = True
            state.errors.append(ErrorEntry(
                host=node_name, task="discover_lxcs", error=str(exc)[:300]
            ))
            continue

        if limit is not None and node_name not in limit:
            lxc_ids = [i for i in lxc_ids if limit_selects_id(limit, node_cluster, i)]

        print(f"[{node_name}] found {len(lxc_ids)} managed LXC(s): {', '.join(lxc_ids) or 'none'}")
        discovered.append((node_name, api_host, node_cluster, lxc_ids))

    # A down node is tolerable while the cluster is still quorate: the other
    # nodes (and their snapshots) keep working, so record a warning instead of
    # failing the run. Without quorum /etc/pve goes read-only fleet-wide —
    # that IS a run-level failure.
    if unreachable_nodes:
        quorate = _cluster_quorate(
            nodes, inventory_path=inventory_path, skip=set(unreachable_nodes))
        for name in unreachable_nodes:
            if quorate:
                state.warnings.append(WarningEntry(
                    host=name, task="node reachability",
                    warning="node down/unreachable — containers skipped this run "
                            "(cluster still quorate)"))
            else:
                reason = ("cluster NOT quorate" if quorate is False
                          else "no node answered the quorum check")
                state.failed = True
                state.errors.append(ErrorEntry(
                    host=name, task="discover_lxcs",
                    error=f"node unreachable and {reason}"))

    def _run_node_ids(node_name: str, api_host: str, cluster: str, ids: List[str]) -> bool:
        """Run one node's containers concurrently; returns True if any failed."""
        wave_failed = False

        # Concurrent per-container updates (lxc_continue_on_error is the default)
        def _run_one(lxc_id: str) -> LxcFlowOutcome:
            ex = RunnerExecutor(node_name, inventory=inventory_path, check=check)
            return run_lxc_update(
                node_name, lxc_id, ex, settings,
                dry_run=dry_run, api_host=api_host, cluster=cluster,
            )

        results = run_concurrent(
            ids,
            _run_one,
            max_workers=settings.lxc_forks,
        )

        for lxc_id, outcome, run_err in results:
            if outcome is not None:
                _fold_outcome(state, outcome, state.lxc)
                wave_failed = wave_failed or outcome.failed
                rec = outcome.record
                if rec is not None:
                    # Always print both lines: the record encodes the failure in
                    # app/os already ("FAILED + ROLLED BACK", "FAILED"), and a bare
                    # "FAILED" hides which of the two actually broke.
                    print(f"  [{node_name}/{lxc_id}] {rec.name}: app={rec.app}  os={rec.os}")
                else:
                    print(f"  [{node_name}/{lxc_id}] idle (no changes)")
            elif run_err is not None:
                state.failed = True
                wave_failed = True
                state.errors.append(ErrorEntry(
                    host=str(lxc_id),
                    task="run_lxc_update",
                    error=str(run_err)[:300],
                ))
                print(f"  [{node_name}/{lxc_id}] ERROR: {run_err}")
        return wave_failed

    all_pairs = [(cluster, i) for _, _, cluster, ids in discovered for i in ids]
    canary_pairs = [(c, i) for c, i in all_pairs if matches_any(settings.canary_hosts, c, i)]
    rest_pairs = [(c, i) for c, i in all_pairs if not matches_any(settings.canary_hosts, c, i)]

    if not canary_pairs or not rest_pairs:
        for node_name, api_host, cluster, ids in discovered:
            _run_node_ids(node_name, api_host, cluster, ids)
    else:
        canary_tokens = [f"{c}/{i}" for c, i in canary_pairs]
        print(f"[lxc phase] canary wave: {', '.join(canary_tokens)}")
        canary_failed = False
        for node_name, api_host, cluster, ids in discovered:
            wave = [i for i in ids if matches_any(settings.canary_hosts, cluster, i)]
            if wave:
                canary_failed = _run_node_ids(node_name, api_host, cluster, wave) or canary_failed
        abort: Optional[str] = None
        if canary_failed:
            abort = "a canary container failed"
        elif not dry_run:
            monitor_map = {
                f"{c}/{i}": str(map_lookup(settings.lxc_kuma_map, c, i))
                for c, i in canary_pairs
            }
            abort = _soak_canaries(settings, monitor_map, canary_tokens)
            if abort is not None:
                state.failed = True
                state.errors.append(ErrorEntry(
                    host="lxc-canary", task="canary soak", error=abort[:300]))
        if abort is not None:
            print(f"[lxc phase] canary gate failed ({abort}) — "
                  f"skipping {len(rest_pairs)} container(s)")
            for node_name, _, cluster, ids in discovered:
                for lxc_id in ids:
                    if not matches_any(settings.canary_hosts, cluster, lxc_id):
                        state.lxc.append(LxcRecord(node=node_name, name=lxc_id,
                                                   id=lxc_id, app=CANARY_SKIP_STATUS,
                                                   snap=False))
        else:
            for node_name, api_host, cluster, ids in discovered:
                wave = [i for i in ids if not matches_any(settings.canary_hosts, cluster, i)]
                if wave:
                    _run_node_ids(node_name, api_host, cluster, wave)

    if state_output_path is not None:
        state.dump_for_ansible(state_output_path)
    return state


def _discover_vm_locations(
    nodes: List[Dict[str, str]],
    *,
    inventory_path: str,
) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """Query each Proxmox cluster for current VM locations via pvesh over SSH.

    ``pvesh get /cluster/resources`` only sees the cluster the queried node
    belongs to, so nodes are grouped by their ``cluster`` inventory var and
    each cluster is queried separately — its nodes tried in inventory order
    until one answers (a powered-off first node must not blind that cluster;
    resources are visible from any member).
    Returns {(cluster, vmid_str): (node_name, ansible_host_ip)} — vmids are
    NOT unique across clusters, so the key must carry the cluster.
    A cluster where every node fails is absent from the result (callers fall
    back to the pve_node hint for its VMs).

    Rationale: pvesh reuses the existing SSH executor pattern, adds no new
    SSL/auth surface, and runs as root so no API token is needed.
    """
    nodes_map = {n["name"]: n["ansible_host"] for n in nodes}
    clusters: Dict[str, List[Dict[str, str]]] = {}
    for n in nodes:
        clusters.setdefault(n.get("cluster", DEFAULT_CLUSTER), []).append(n)

    result: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for cluster, members in clusters.items():
        for candidate in members:
            disc_node = candidate["name"]
            # check=False: discovery is read-only and must run even in dry-run mode.
            disc_executor = RunnerExecutor(disc_node, inventory=inventory_path, check=False)
            try:
                res = disc_executor.run_shell(
                    "pvesh get /cluster/resources --type vm --output-format json 2>/dev/null",
                    changed_when=False,
                    ignore_errors=True,
                )
                if res.failed or not res.stdout.strip():
                    raise RuntimeError(res.stderr.strip() or "pvesh returned no output")
                resources = json.loads(res.stdout)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[vm phase] WARNING: VM discovery for cluster '{cluster}' via "
                    f"{disc_node} failed ({exc!s}), trying next node",
                    file=sys.stderr,
                )
                continue
            for item in (resources if isinstance(resources, list) else []):
                vmid = str(item.get("vmid", ""))
                node_name = str(item.get("node", ""))
                if vmid and node_name:
                    result[(cluster, vmid)] = (node_name, nodes_map.get(node_name, node_name))
            break
    return result


def _resolve_vm_cluster(
    spec: VmSpec,
    node_clusters: Dict[str, str],
    vm_locations: Dict[Tuple[str, str], Tuple[str, str]],
) -> str:
    """Resolve which cluster a VmSpec belongs to. Never guesses.

    Order: explicit ``cluster=`` inventory var → the cluster of its
    ``pve_node`` hint → the single cluster discovery found the vmid in.
    Raises RuntimeError when the vmid was discovered in several clusters and
    no hint disambiguates (guessing would snapshot/rollback the wrong VM).
    """
    if spec.cluster:
        return spec.cluster
    if spec.pve_node and spec.pve_node in node_clusters:
        return node_clusters[spec.pve_node]
    found = sorted({c for (c, vmid) in vm_locations if vmid == spec.vmid})
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise RuntimeError(
            f"vmid {spec.vmid} ({spec.name}) exists in clusters "
            f"{', '.join(found)} — set cluster= or pve_node= in inventory"
        )
    return DEFAULT_CLUSTER


def run_vm_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Optional[Union[str, Path]] = "/tmp/fleet_vm_state.json",  # nosec B108 - tooling output path
    limit: Optional[Set[str]] = None,
) -> FleetState:
    """Run Phase 1b (QEMU VM updates) via the Python driver.

    Loads all VMs from inventory, runs them concurrently (max_workers=vm_forks).
    *limit* matches inventory names or vmids; non-matching VMs are silently
    omitted. VMs flagged ``canary`` (inventory/host_vars) or listed in
    ``settings.canary_hosts`` (name or vmid) update first; the rest run only
    if the canary wave succeeded and the post-soak Kuma checks pass. When
    *state_output_path* is set, writes the FleetState for tooling / molecule;
    run_fleet() passes None and merges in-memory.

    Never raises for per-VM failures — those become FAILED records.
    """
    vms = inventory.load_proxmox_vms(inventory_path, host_vars_dir=settings.host_vars_dir)
    nodes = inventory.load_proxmox_nodes(inventory_path, host_vars_dir=settings.host_vars_dir)
    nodes_map = {n["name"]: n["ansible_host"] for n in nodes}
    node_clusters = {n["name"]: n.get("cluster", DEFAULT_CLUSTER) for n in nodes}

    def _vm_cluster_hint(v: VmSpec) -> str:
        # A cheap pre-execution guess for --limit filtering and canary
        # partitioning only — the authoritative resolution (raising on
        # ambiguity) happens per-VM in _run_one() via _resolve_vm_cluster()
        # once vm_locations is set.
        if v.cluster:
            return v.cluster
        if v.pve_node:
            return node_clusters.get(v.pve_node, DEFAULT_CLUSTER)
        return DEFAULT_CLUSTER

    if limit is not None:
        vms = [v for v in vms
               if v.name in limit or limit_selects_id(limit, _vm_cluster_hint(v), v.vmid)]

    dry_run = check or settings.fleet_dry_run or settings.vm_dry_run
    state = FleetState()

    # Discover VM locations once up-front via pvesh. Live cluster state always
    # wins over static pve_node inventory hints — pve_node is only a fallback
    # for non-cluster / non-HA setups where discovery is unavailable.
    # Skipped when nothing is targeted (no point in the SSH round-trip).
    vm_locations = (
        _discover_vm_locations(nodes, inventory_path=inventory_path) if vms else {}
    )
    if vm_locations:
        print(f"[vm phase] discovered {len(vm_locations)} VM location(s) from cluster")
    elif vms:
        print("[vm phase] cluster discovery unavailable — using pve_node inventory hints")

    def _run_one(vm_spec: VmSpec) -> VmFlowOutcome:
        # Maintenance window gate
        if vm_spec.maintenance_window is not None:
            if not window.in_window(vm_spec.maintenance_window, force=settings.force_window):
                return VmFlowOutcome()  # silently skipped

        # Prefer live cluster location; fall back to inventory hint. The vmid
        # alone is ambiguous across clusters, so resolve the cluster first
        # (raises into a FAILED record when it can't be determined safely).
        vm_cluster = _resolve_vm_cluster(vm_spec, node_clusters, vm_locations)
        if (vm_cluster, vm_spec.vmid) in vm_locations:
            node_name, api_host = vm_locations[(vm_cluster, vm_spec.vmid)]
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
            dry_run=dry_run, api_host=api_host, cluster=vm_cluster,
        )

    def _run_wave(wave: List[VmSpec]) -> None:
        results = run_concurrent(wave, _run_one, max_workers=settings.vm_forks)
        for vm_spec, outcome, run_err in results:
            vm_name = vm_spec.name if isinstance(vm_spec, VmSpec) else str(vm_spec)
            if outcome is not None:
                _fold_outcome(state, outcome, state.vm)
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

    canary = [v for v in vms
              if v.canary or v.name in settings.canary_hosts
              or matches_any(settings.canary_hosts, _vm_cluster_hint(v), v.vmid)]
    rest = [v for v in vms if v not in canary]

    if not canary or not rest:
        _run_wave(vms)
    else:
        print(f"[vm phase] canary wave: {', '.join(v.name for v in canary)}")
        _run_wave(canary)
        abort: Optional[str] = None
        if state.failed:
            abort = "a canary VM failed"
        elif not dry_run:
            canary_names = [v.name for v in canary]
            monitor_map = {name: str(settings.vm_kuma_map.get(name, "")) for name in canary_names}
            abort = _soak_canaries(settings, monitor_map, canary_names)
            if abort is not None:
                state.failed = True
                state.errors.append(ErrorEntry(
                    host="vm-canary", task="canary soak", error=abort[:300]))
        if abort is not None:
            print(f"[vm phase] canary gate failed ({abort}) — skipping {len(rest)} VM(s)")
            for v in rest:
                # Live cluster location wins over the pve_node hint, like _run_one.
                # An unresolvable cluster must not raise here — this is only a
                # display field on a SKIPPED record.
                try:
                    v_cluster = _resolve_vm_cluster(v, node_clusters, vm_locations)
                except RuntimeError:
                    v_cluster = DEFAULT_CLUSTER
                node_name = vm_locations.get((v_cluster, v.vmid), (v.pve_node or "?", ""))[0]
                state.vm.append(VmRecord(node=node_name, vmid=v.vmid, name=v.name,
                                         status=CANARY_SKIP_STATUS))
        else:
            _run_wave(rest)

    if state_output_path is not None:
        state.dump_for_ansible(state_output_path)
    return state


def run_remote_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Optional[Union[str, Path]] = "/tmp/fleet_remote_state.json",  # nosec B108 - tooling output path
    limit: Optional[Set[str]] = None,
) -> FleetState:
    """Run Phase 0 (remote host updates) via the Python driver.

    Loads all [remote_hosts] from inventory, runs them concurrently
    (max_workers=remote_forks). *limit* restricts to the named hosts. Hosts
    flagged ``canary`` (inventory/host_vars) or listed in
    ``settings.canary_hosts`` update first; the rest run only if the canary
    wave succeeded and the post-soak Kuma checks pass. When
    *state_output_path* is set, writes the FleetState for tooling / molecule;
    run_fleet() passes None and merges in-memory.

    Never raises for per-host failures — those become FAILED records.
    """
    hosts = inventory.load_remote_hosts(inventory_path, host_vars_dir=settings.host_vars_dir)
    if limit is not None:
        hosts = [h for h in hosts if h.name in limit]
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

    def _run_wave(wave: List[RemoteHostSpec]) -> None:
        results = run_concurrent(wave, _run_one, max_workers=settings.remote_forks)
        for spec, outcome, run_err in results:
            host_name = spec.name if isinstance(spec, RemoteHostSpec) else str(spec)
            if outcome is not None:
                _fold_outcome(state, outcome, state.remote)
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

    canary = [h for h in hosts
              if h.canary or h.name in settings.canary_hosts]
    rest = [h for h in hosts if h not in canary]

    if not canary or not rest:
        _run_wave(hosts)
    else:
        print(f"[remote phase] canary wave: {', '.join(h.name for h in canary)}")
        _run_wave(canary)
        abort: Optional[str] = None
        if state.failed:
            abort = "a canary host failed"
        elif not dry_run:
            canary_names = [h.name for h in canary]
            monitor_map = {name: str(settings.remote_kuma_map.get(name, "")) for name in canary_names}
            abort = _soak_canaries(settings, monitor_map, canary_names)
            if abort is not None:
                state.failed = True
                state.errors.append(ErrorEntry(
                    host="remote-canary", task="canary soak", error=abort[:300]))
        if abort is not None:
            print(f"[remote phase] canary gate failed ({abort}) — "
                  f"skipping {len(rest)} host(s)")
            for spec in rest:
                state.remote.append(RemoteRecord(host=spec.name, status=CANARY_SKIP_STATUS))
        else:
            _run_wave(rest)

    if state_output_path is not None:
        state.dump_for_ansible(state_output_path)
    return state


def run_node_phase(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    state_output_path: Optional[Union[str, Path]] = "/tmp/fleet_node_state.json",  # nosec B108 - tooling output path
    limit: Optional[Set[str]] = None,
    include_nodes: bool = True,
    include_manager: bool = True,
) -> FleetState:
    """Run Phase 2 (node OS updates) + Phase 3 (manager self-update) via the Python driver.

    Node processing is serial (one node at a time) and aborts on the first failure
    — mirroring Ansible's ``any_errors_fatal: true`` / ``serial: 1``. The manager
    self-update always runs regardless of node failures (Phase 3 is independent of
    Phase 2 outcomes).

    *include_nodes* / *include_manager* let run_fleet() map the ``node`` and
    ``manager`` phase selections onto this single helper. *limit* restricts
    Phase 2 to the named nodes; the manager (not an inventory host) runs only
    when the limit is unset or names it via ``manager``/``localhost``/
    ``Ansible-Manager``.

    When *state_output_path* is set, writes the FleetState via dump_for_ansible()
    for tooling / molecule; run_fleet() passes None and merges in-memory.

    Never raises for per-node failures — those become FAILED records.
    """
    nodes = inventory.load_proxmox_nodes(inventory_path, host_vars_dir=settings.host_vars_dir)
    if limit is not None:
        nodes = [n for n in nodes if n["name"] in limit]
    dry_run = check or settings.fleet_dry_run or settings.node_dry_run
    state = FleetState()

    # Phase 2: serial node loop — abort on first failure (any_errors_fatal equivalent).
    # Exception: a node that never answered (powered off / no route) is skipped
    # with a warning instead, as long as the cluster is still quorate — a down
    # node must not block updates for the healthy ones.
    if include_nodes:
        for node_info in nodes:
            node_name = node_info["name"]
            executor = RunnerExecutor(node_name, inventory=inventory_path, check=check)
            node_cluster = node_info.get("cluster", DEFAULT_CLUSTER)
            outcome = run_node_update(
                node_name, executor, settings, dry_run=dry_run, cluster=node_cluster
            )
            if (
                outcome.failed
                and outcome.error is not None
                and _error_is_unreachable(outcome.error.error)
                and _cluster_quorate(nodes, inventory_path=inventory_path,
                                     skip={node_name})
            ):
                state.node.append(NodeRecord(node=node_name, status="SKIPPED (unreachable)"))
                state.warnings.append(WarningEntry(
                    host=node_name, task=outcome.error.task,
                    warning="node down/unreachable — OS update skipped this run "
                            "(cluster still quorate)"))
                print(f"  [{node_name}] SKIPPED (unreachable)")
                continue
            _fold_outcome(state, outcome, state.node)
            status = outcome.record.status if outcome.record else "?"
            print(f"  [{node_name}] {status}")
            if outcome.failed:
                break  # abort remaining nodes; manager update still runs

    # Phase 3: manager self-update always runs — independent of node failures.
    if include_manager and (limit is None or not _MANAGER_LIMIT_TOKENS.isdisjoint(limit)):
        manager_ex = RunnerExecutor("localhost", inventory=inventory_path, check=check)
        manager_outcome = run_manager_update(manager_ex, settings, dry_run=dry_run)
        _fold_outcome(state, manager_outcome, state.node)
        mgr_status = manager_outcome.record.status if manager_outcome.record else "?"
        print(f"  [Ansible-Manager] {mgr_status}")

    if state_output_path is not None:
        state.dump_for_ansible(state_output_path)
    return state


def run_notify_phase(
    *,
    settings: GlobalSettings,
    state: FleetState,
    check: bool = False,
) -> str:
    """Run Phase 4 (final briefing) via the Python driver.

    Consumes the *merged* FleetState (all phases), renders the briefing body
    once, then: dispatches it to the resolved notifiers when ``should_notify``
    holds; records the run history (carrying the rendered body) when history is
    enabled; and pings the dead-man's-switch. The body is rendered
    unconditionally so the history file captures the message even when
    notification is suppressed.

    Returns the rendered briefing body (handy for tests / logging). Never raises
    — notification/history failures must not abort the run.
    """
    body = briefing.prepare_body(state)
    failed = state.failed

    if briefing.should_notify(
        force_notify=settings.force_notify,
        dry_run=settings.fleet_dry_run,
        changed=state.changed,
        failed=failed,
    ):
        notifiers.dispatch(
            notifiers.resolve_notifiers(settings),
            title=briefing.briefing_title(failed),
            ntfy_title=briefing.ntfy_title(failed),
            body=body,
            color=briefing.discord_color(failed),
            failed=failed,
            retries=settings.notifier_retries,
        )

    if settings.fleet_history_enabled:
        history.write_history(
            state,
            history_dir=settings.fleet_history_dir,
            keep=settings.fleet_history_keep,
            briefing=body,
        )

    notifiers.ping_deadmans(settings.fleet_deadmans_url, failed=failed,
                            retries=settings.deadmans_retries)
    return body


def _merge_state(dst: FleetState, src: FleetState) -> None:
    """Fold one phase's FleetState into the running fleet-wide state.

    Record lists concatenate, the changed/failed flags OR-join, and the
    error/warning logs accumulate. (Replaces the old per-phase merge plays —
    everything now happens in-memory in run_fleet().)
    """
    dst.lxc.extend(src.lxc)
    dst.vm.extend(src.vm)
    dst.remote.extend(src.remote)
    dst.node.extend(src.node)
    dst.custom.extend(src.custom)
    dst.errors.extend(src.errors)
    dst.warnings.extend(src.warnings)
    dst.changed = dst.changed or src.changed
    dst.failed = dst.failed or src.failed


def run_fleet(
    *,
    settings: GlobalSettings,
    inventory_path: str = "hosts.ini",
    check: bool = False,
    extra_vars: Optional[Dict[str, Any]] = None,
    limit: Optional[Set[str]] = None,
    phases: Optional[Set[str]] = None,
) -> int:
    """End-to-end fleet update — the Python orchestrator (the sole entrypoint).

    Runs the pre-flight apt-proxy check, then every phase in order
    (remote → custom → lxc → vm → node+manager), merging each phase's FleetState
    into one fleet-wide state, then renders/dispatches the final briefing.

    *phases* (subset of :data:`PHASE_NAMES`) selects which phases run; the
    pre-flight check and Phase 4 (notify/history) always run. *limit* restricts
    every selected phase to the named hosts / LXC or VM ids — hosts outside it
    are silently omitted, like maintenance-window skips. The manager
    self-update obeys the limit too (token ``manager``/``localhost``).

    Returns a process exit code: 1 if any phase recorded a failure, else 0.
    Raises SystemExit(1) on a pre-flight proxy failure, a custom dependency-order
    problem (both fail-loud, mirroring the legacy playbook), or an unknown phase
    name.
    """
    ev = extra_vars or {}

    if phases is not None:
        unknown = sorted(set(phases) - set(PHASE_NAMES))
        if unknown:
            raise SystemExit(
                f"unknown phase(s): {', '.join(unknown)} (valid: {', '.join(PHASE_NAMES)})"
            )

    def _phase_on(name: str) -> bool:
        return phases is None or name in phases

    # Pre-flight: node names must be unique across clusters — they are the join
    # key in records, briefing grouping, and the dashboard host pages.
    inventory.validate_node_uniqueness(
        inventory.load_proxmox_nodes(inventory_path, host_vars_dir=settings.host_vars_dir))

    # Pre-flight: the apt-cacher-ng proxy must be reachable or the whole run aborts
    # (port of the "Verify Apt-Cacher-NG is Online" / "Halt if Proxy is Offline" play).
    if settings.apt_proxy_ip:
        try:
            http.wait_for_port(settings.apt_proxy_ip, settings.apt_proxy_port,
                               timeout=settings.apt_proxy_check_timeout)
        except (TimeoutError, OSError):
            print(
                f"CRITICAL: Apt Proxy ({settings.apt_proxy_ip}) is unreachable. "
                "Aborting fleet update.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    state = FleetState()
    if _phase_on("remote"):
        _merge_state(state, run_remote_phase(
            settings=settings, inventory_path=inventory_path, check=check,
            state_output_path=None, limit=limit))
    if _phase_on("custom"):
        _merge_state(state, run_custom_phase(
            settings=settings, inventory_path=inventory_path, extra_vars=ev, check=check,
            state_output_path=None, limit=limit))
    if _phase_on("lxc"):
        _merge_state(state, run_lxc_phase(
            settings=settings, inventory_path=inventory_path, check=check,
            state_output_path=None, limit=limit))
    if _phase_on("vm"):
        _merge_state(state, run_vm_phase(
            settings=settings, inventory_path=inventory_path, check=check,
            state_output_path=None, limit=limit))
    if _phase_on("node") or _phase_on("manager"):
        _merge_state(state, run_node_phase(
            settings=settings, inventory_path=inventory_path, check=check,
            state_output_path=None, limit=limit,
            include_nodes=_phase_on("node"), include_manager=_phase_on("manager")))

    run_notify_phase(settings=settings, state=state, check=check)

    return 1 if state.failed else 0
