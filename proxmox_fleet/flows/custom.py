"""custom_update flow — the Python decomposition of roles/custom_update/tasks/*.

Owns the full control flow that used to live in main.yml's block/rescue/always:
  load (done by caller) -> detect -> backup -> update (steps) -> change-detect ->
  reboot -> health-check -> report, with dependency-skip gating and a rescue path
  that rolls back and records FAILED.

v2 adds optional PVE snapshot/rollback: when the config carries ``pve_vmid`` (and
the driver supplies a node executor + API params), a ``BEFORE_UPDATE_AUTO``
snapshot is taken before the update steps, rolled back in rescue, and deleted in
``finally`` — the same try/except/finally shape as the lxc/vm flows. Hosts
without ``pve_vmid`` keep the legacy rescue (run ``rollback_command``, errors
ignored, plain ``FAILED``).

All decisions are here in Python; the Executor + http module do the actual work.
Status strings come from proxmox_fleet.status (byte-parity with the old Jinja).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from proxmox_fleet import http as http_mod
from proxmox_fleet.changes import custom_changed, is_outdated
from proxmox_fleet.executor import Executor, snapshot_failure_warning, snapshot_with_retry
from proxmox_fleet.flows._pkg import kuma_healthy
from proxmox_fleet.models.config import CustomConfig
from proxmox_fleet.models.state import CustomRecord, ErrorEntry, WarningEntry
from proxmox_fleet.status import custom_rescue_status, custom_should_report, custom_status
from proxmox_fleet.steps import run_steps


class HealthCheckError(RuntimeError):
    """Health check failed after the retry window — triggers rescue/rollback."""


@dataclass
class CustomFlowOutcome:
    """Everything the driver needs to fold this host into the FleetState."""

    record: Optional[CustomRecord] = None
    changed: bool = False
    failed: bool = False
    error: Optional[ErrorEntry] = None
    warnings: List[WarningEntry] = field(default_factory=list)

    @property
    def should_report(self) -> bool:
        return self.record is not None


def _detect_latest(
    config: CustomConfig,
    executor: Executor,
    *,
    need_latest: bool,
) -> str:
    """Resolve the latest available version (informational / outdated gate)."""
    if not need_latest:
        return ""
    lv = config.latest_version
    if lv.type == "github_release" and lv.repo.strip():
        try:
            data = http_mod.get_json(
                f"https://api.github.com/repos/{lv.repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            return str((data or {}).get("tag_name", "")).strip()
        except Exception:  # noqa: BLE001 - fail-open like the role (failed_when: false)
            return ""
    if lv.type == "command" and lv.command.strip():
        res = executor.run_local(lv.command)
        return res.stdout.strip() if res.ok else ""
    return ""


def _run_health_check(
    config: CustomConfig,
    executor: Executor,
    *,
    kuma_url: str,
    retries: int,
    delay: float,
) -> None:
    """Poll the configured health check. Raise HealthCheckError on failure."""
    hc = config.health_check
    if hc.type == "kuma":
        url = f"{kuma_url}/api/status-page/heartbeat/{hc.kuma_slug}"
        monitor_id = str(hc.kuma_monitor_id)

        try:
            http_mod.poll_until(
                lambda: http_mod.get_json(url),
                lambda p: kuma_healthy(p, monitor_id=monitor_id),
                retries=retries, delay=delay,
            )
        except Exception as exc:  # noqa: BLE001
            raise HealthCheckError(f"kuma health check failed: {exc}") from exc

    elif hc.type == "http":
        try:
            http_mod.poll_until(
                lambda: http_mod.request(hc.url).status, lambda s: s == 200, retries=retries, delay=delay
            )
        except Exception as exc:  # noqa: BLE001
            raise HealthCheckError(f"http health check failed: {exc}") from exc

    elif hc.type == "command":
        res = executor.run_shell(hc.command, changed_when=False)
        if not res.ok:
            raise HealthCheckError(f"command health check failed (rc={res.rc})")


def run_custom_update(
    host: str,
    config: CustomConfig,
    executor: Executor,
    *,
    dry_run: bool = False,
    dep_failed: bool = False,
    allow_reboot: bool = True,
    when_context: Optional[Dict[str, Any]] = None,
    kuma_url: str = "",
    kuma_retries: int = 5,
    kuma_delay: float = 30.0,
    node_executor: Optional[Executor] = None,
    api_params: Optional[Dict[str, Any]] = None,
    snapshot_retries: int = 3,
    snapshot_retry_delay: float = 15.0,
    _sleep: Callable[[float], None] = time.sleep,
) -> CustomFlowOutcome:
    """Run the whole custom_update flow for one host. Never raises — failures are
    captured into the outcome (FAILED record + error entry), mirroring rescue.

    PVE snapshots fire only when the config has ``pve_vmid`` AND the driver
    passed both *node_executor* (SSH to the owning Proxmox node, for
    ``pct/qm rollback``) and *api_params* (``api_host``/``api_user``/
    ``api_token_id``/``api_token_secret`` for the snapshot primitive).
    """

    name = config.name or host
    snap_enabled = bool(
        config.pve_vmid.strip() and node_executor is not None and api_params is not None
    )
    snap_taken = False
    snapshot_failed = False
    rollback_done = False

    # Dependency gating: a dependency failed earlier this run → skip with a warning.
    if dep_failed:
        return CustomFlowOutcome(
            warnings=[WarningEntry(host=host, task="Dependency check",
                                   warning="skipped — a dependency failed earlier this run")]
        )

    outcome = CustomFlowOutcome()
    try:
        # --- detect ---------------------------------------------------------
        ver_before = ""
        if config.version_command.strip():
            res = executor.run_shell(config.version_command, changed_when=False)
            ver_before = res.stdout.strip()

        need_latest = dry_run or config.update_only_if_outdated
        latest = _detect_latest(config, executor, need_latest=need_latest)
        outdated = is_outdated(ver_before, latest) if config.update_only_if_outdated else True

        if config.update_only_if_outdated and not dry_run and latest == "":
            outcome.warnings.append(WarningEntry(
                host=host, task="Detect latest version",
                warning="could not resolve latest version; updating anyway "
                        "(update_only_if_outdated fail-open)"))

        # --- dry-run: report only ------------------------------------------
        if dry_run:
            status = custom_status(
                dry_run=True,
                ver_before=ver_before or None,
                latest_ver=latest or None,
            )
            outcome.record = CustomRecord(host=host, name=name, app=status)
            return outcome

        # --- outdated gate: skip update when current ------------------------
        if config.update_only_if_outdated and not outdated:
            status = custom_status(update_only_if_outdated=True, is_outdated=False)
            # 'OK (up to date)' is not reportable → no record (matches noop).
            if custom_should_report(status, dry_run=False):
                outcome.record = CustomRecord(host=host, name=name, app=status)
            return outcome

        # --- backup ---------------------------------------------------------
        if config.backup_command.strip():
            executor.run_shell(config.backup_command)

        if snap_enabled:
            snap_res = snapshot_with_retry(
                executor, config.pve_vmid, snap_state="present",
                retries=snapshot_retries, delay=snapshot_retry_delay,
                **(api_params or {}),
            )
            snap_taken = snap_res.changed
            if not snap_taken:
                snapshot_failed = True
                outcome.warnings.append(WarningEntry(
                    host=host, task=f"Snapshot {config.pve_vmid}",
                    warning=snapshot_failure_warning(snap_res),
                ))

        # --- update (per-step, with Python interpolation) -------------------
        run_steps(config.update_steps, executor, context=when_context)

        ver_after = ""
        if config.version_command.strip():
            res = executor.run_shell(config.version_command, changed_when=False)
            ver_after = res.stdout.strip()

        # --- change detection ----------------------------------------------
        changed_cmd_rc: Optional[int] = None
        if config.changed_when.type == "command" and config.changed_when.command.strip():
            res = executor.run_shell(config.changed_when.command, changed_when=False, ignore_errors=True)
            changed_cmd_rc = res.rc

        changed = custom_changed(
            changed_when_type=config.changed_when.type,
            ver_before=ver_before,
            ver_after=ver_after,
            changed_cmd_rc=changed_cmd_rc,
        )
        outcome.changed = changed

        # --- reboot ---------------------------------------------------------
        reboot_done = False
        if changed and config.reboot and allow_reboot:
            executor.reboot()
            reboot_done = True

        # --- health check (only when something changed) ---------------------
        if changed and config.health_check.type != "none":
            _run_health_check(config, executor, kuma_url=kuma_url,
                              retries=kuma_retries, delay=kuma_delay)

        # --- report ---------------------------------------------------------
        status = custom_status(
            changed_when_type=config.changed_when.type,
            ver_before=ver_before,
            ver_after=ver_after,
            changed_cmd_rc=changed_cmd_rc,
            reboot_done=reboot_done,
        )
        if custom_should_report(status, dry_run=False):
            outcome.record = CustomRecord(host=host, name=name, app=status)
        return outcome

    except Exception as exc:  # noqa: BLE001 - mirror Ansible rescue catch-all (StepError/HealthCheckError/…)
        # --- rescue: rollback + record FAILED -------------------------------
        # Snapshot rollback when one was confirmed taken (pct/qm on the node,
        # rollback_done only once the guest is confirmed running again);
        # otherwise the legacy rollback_command fallback (errors ignored).
        if snap_taken and node_executor is not None:
            pve_cmd = "pct" if config.pve_type == "lxc" else "qm"
            try:
                rb = node_executor.run_shell(
                    f"{pve_cmd} rollback {config.pve_vmid} BEFORE_UPDATE_AUTO",
                    ignore_errors=True,
                )
                if not rb.failed:
                    # Poll until the guest is running again (up to 12 × 10 s)
                    for _ in range(12):
                        _sleep(10)
                        chk = node_executor.run_shell(
                            f"{pve_cmd} status {config.pve_vmid}",
                            changed_when=False, ignore_errors=True,
                        )
                        if "running" in chk.stdout:
                            rollback_done = True
                            break
            except Exception:  # noqa: BLE001 - rollback errors are ignored
                pass
        elif config.rollback_command.strip():
            try:
                executor.run_shell(config.rollback_command, ignore_errors=True)
            except Exception:  # noqa: BLE001 - rollback errors are ignored
                pass
        failed_task = getattr(exc, "step_name", type(exc).__name__)
        rescue_str = custom_rescue_status(
            rollback_done=rollback_done, snapshot_failed=snapshot_failed
        )
        outcome.failed = True
        outcome.record = CustomRecord(host=host, name=name, app=rescue_str)
        outcome.error = ErrorEntry(host=host, task=str(failed_task), error=str(exc)[:300])
        return outcome

    finally:
        # --- always: delete the snapshot we created -------------------------
        if snap_taken:
            snapshot_with_retry(
                executor, config.pve_vmid, snap_state="absent",
                retries=snapshot_retries, delay=snapshot_retry_delay,
                **(api_params or {}),
            )
