"""custom_update flow — the Python decomposition of roles/custom_update/tasks/*.

Owns the full control flow that used to live in main.yml's block/rescue/always:
  load (done by caller) -> detect -> backup -> update (steps) -> change-detect ->
  reboot -> health-check -> report, with dependency-skip gating and a rescue path
  that runs rollback_command and records FAILED.

All decisions are here in Python; the Executor + http module do the actual work.
Status strings come from proxmox_fleet.status (byte-parity with the old Jinja).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from proxmox_fleet import http as http_mod
from proxmox_fleet.changes import custom_changed, is_outdated
from proxmox_fleet.executor import Executor
from proxmox_fleet.models.config import CustomConfig
from proxmox_fleet.models.state import CustomRecord, ErrorEntry, WarningEntry
from proxmox_fleet.status import custom_should_report, custom_status
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

        def healthy(payload: Any) -> bool:
            beats = (payload or {}).get("heartbeatList", {}).get(str(hc.kuma_monitor_id), [])
            return bool(beats) and beats[-1].get("status") == 1

        try:
            http_mod.poll_until(lambda: http_mod.get_json(url), healthy, retries=retries, delay=delay)
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
) -> CustomFlowOutcome:
    """Run the whole custom_update flow for one host. Never raises — failures are
    captured into the outcome (FAILED record + error entry), mirroring rescue."""

    name = config.name or host

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
        if config.rollback_command.strip():
            try:
                executor.run_shell(config.rollback_command, ignore_errors=True)
            except Exception:  # noqa: BLE001 - rollback errors are ignored
                pass
        failed_task = getattr(exc, "step_name", type(exc).__name__)
        outcome.failed = True
        outcome.record = CustomRecord(host=host, name=name, app="FAILED")
        outcome.error = ErrorEntry(host=host, task=str(failed_task), error=str(exc)[:300])
        return outcome
