"""housekeeping flow — maintenance-only pass over one managed container.

Read-only introspection decides whether the guest is eligible, then the
manager-owned policy (``proxmox_fleet.housekeeping.run_housekeeping``) performs
logging preparation, acknowledged archival and gated retention/cache cleanup.
No package work, snapshot, reboot or stopped-container start happens here.
"""

from __future__ import annotations

from typing import Optional

from proxmox_fleet.alloy import DesiredAlloyConfig
from proxmox_fleet.cluster import DEFAULT_CLUSTER
from proxmox_fleet.executor import Executor
from proxmox_fleet.flows.lxc import LxcFlowOutcome
from proxmox_fleet.housekeeping import run_housekeeping
from proxmox_fleet.lxc_parse import parse_pct_config, parse_pct_status
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import LxcRecord, WarningEntry




def run_lxc_housekeeping(
    node: str,
    lxc_id: str,
    executor: Executor,
    settings: GlobalSettings,
    *,
    dry_run: bool = False,
    cluster: str = DEFAULT_CLUSTER,
    alloy_config: Optional[DesiredAlloyConfig] = None,
) -> LxcFlowOutcome:
    """Run maintenance only for one container.

    Read-only introspection (``pct config``/``pct status`` inside the existing
    introspect primitive) skips templates and stopped containers without
    starting them.  Otherwise the policy is invoked with the caller's base
    desired Alloy config; the policy applies the exclusion lists and returns the
    effective per-guest config for later reconciliation.  The returned outcome
    reuses :class:`LxcFlowOutcome` so the driver folds it exactly like an update
    record without introducing a second result shape.

    Introspection failures propagate (fail-loud, mirroring ``run_lxc_update``)
    so a controller that cannot be read is an error rather than a silent skip.
    """
    introspect_res = executor.introspect(lxc_id)
    if not introspect_res.ok:
        raise RuntimeError(
            f"introspect failed for {node}/{lxc_id} "
            f"(rc={introspect_res.rc}): {(introspect_res.stderr or '(no stderr)')[-400:]}"
        )

    pct_info = parse_pct_config(str(introspect_res.facts.get("config_stdout", "")))
    name: str = pct_info["name"]

    if pct_info["is_template"]:
        return LxcFlowOutcome()

    status_info = parse_pct_status(str(introspect_res.facts.get("status_stdout", "")))
    if not status_info["is_running"] or status_info["was_stopped"]:
        return LxcFlowOutcome()

    result = run_housekeeping(
        executor,
        settings,
        node=node,
        cluster=cluster,
        lxc_id=lxc_id,
        name=name,
        desired_alloy=alloy_config,
        dry_run=dry_run,
    )
    outcome = LxcFlowOutcome(changed=result.changed, failed=result.failed)
    for message in result.warnings:
        outcome.warnings.append(WarningEntry(
            host=f"{node}/{lxc_id}",
            task="Housekeeping",
            warning=message,
            notifying=result.failed,
        ))
    if result.summary is not None:
        outcome.record = LxcRecord(
            node=node,
            name=name,
            id=lxc_id,
            app="",
            os="",
            snap=False,
            housekeeping=result.summary,
        )
    return outcome
