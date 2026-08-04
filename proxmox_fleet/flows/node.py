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

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from proxmox_fleet import http as http_mod
from proxmox_fleet.changes import pkg_changed as _pkg_changed
from proxmox_fleet.changes import vm_pkg_count as _vm_pkg_count
from proxmox_fleet.cluster import DEFAULT_CLUSTER, split_qualified
from proxmox_fleet.executor import Executor
from proxmox_fleet.flows._pkg import upgrade_cmd
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import ErrorEntry, NodeRecord, WarningEntry
from proxmox_fleet.orchestration import retry
from proxmox_fleet.pkg_detail import parse_upgraded
from proxmox_fleet.status import manager_status, node_status

# Nodes are always Debian/apt — no pkg_mgr detection step needed; the apt
# upgrade command (incl. LC_ALL=C locale pin) is shared with the other flows.

@dataclass
class PostUpgradeAssessment:
    """Classified result of the read-only node diagnostic primitive."""

    reboot_reasons: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks: Optional[Dict[str, Any]] = None


def _fact_int(facts: Mapping[str, Any], key: str) -> int:
    if key not in facts:
        raise ValueError(f"missing diagnostic fact: {key}")
    try:
        return int(facts[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid diagnostic fact: {key}") from exc


def _fact_bool(facts: Mapping[str, Any], key: str) -> bool:
    if key not in facts:
        raise ValueError(f"missing diagnostic fact: {key}")
    value = facts[key]
    if isinstance(value, bool):
        return value
    normalised = str(value).strip().lower()
    if normalised in {"true", "1", "yes"}:
        return True
    if normalised in {"false", "0", "no"}:
        return False
    raise ValueError(f"invalid diagnostic fact: {key}")


def _fact_text(facts: Mapping[str, Any], key: str) -> str:
    if key not in facts:
        raise ValueError(f"missing diagnostic fact: {key}")
    return str(facts[key] or "").strip()


def _nvidia_dkms_ready(status: str, running_kernel: str) -> bool:
    """Match an NVIDIA-family DKMS entry for this exact kernel in installed state."""
    pattern = re.compile(
        r"^\s*nvidia(?:[-_][A-Za-z0-9_.-]+)?/[^,]+,\s*"
        + re.escape(running_kernel)
        + r"(?:,\s*[^:]+)?:\s*installed\s*$",
        re.IGNORECASE,
    )
    return any(pattern.match(line) is not None for line in status.splitlines())


def classify_post_upgrade(
    facts: Mapping[str, Any],
    *,
    nvidia_host: bool,
    after_reboot: bool = False,
) -> PostUpgradeAssessment:
    """Validate primitive facts and classify reboot reasons and NVIDIA health."""
    result = PostUpgradeAssessment()
    try:
        if _fact_int(facts, "diagnostics_version") != 1:
            raise ValueError("unsupported node diagnostic response version")

        running_rc = _fact_int(facts, "running_kernel_rc")
        latest_rc = _fact_int(facts, "latest_kernel_rc")
        running = _fact_text(facts, "running_kernel")
        latest = _fact_text(facts, "latest_kernel")
        if running_rc != 0 or not running:
            result.errors.append("could not determine running kernel")
        if latest_rc != 0 or not latest:
            result.errors.append("could not determine latest installed kernel")

        if _fact_bool(facts, "reboot_required_exists"):
            packages = str(facts.get("reboot_required_packages", "") or "").splitlines()
            packages = [package.strip() for package in packages if package.strip()]
            suffix = f" ({', '.join(packages)})" if packages else ""
            result.reboot_reasons.append(f"reboot-required marker present{suffix}")
        if running and latest and running != latest:
            result.reboot_reasons.append(f"kernel update: {running} → {latest}")

        if not nvidia_host:
            return result
        if not _fact_bool(facts, "nvidia_checked"):
            result.errors.append("NVIDIA diagnostics were skipped on an NVIDIA host")
            return result

        installed_rc = _fact_int(facts, "nvidia_installed_rc")
        loaded_rc = _fact_int(facts, "nvidia_loaded_rc")
        dkms_rc = _fact_int(facts, "nvidia_dkms_rc")
        smi_rc = _fact_int(facts, "nvidia_smi_rc")
        installed = _fact_text(facts, "nvidia_installed")
        loaded = _fact_text(facts, "nvidia_loaded")
        dkms_status = _fact_text(facts, "nvidia_dkms_status")
        installed_ok = installed_rc == 0 and bool(installed)
        loaded_ok = loaded_rc == 0 and bool(loaded)
        dkms_ready = dkms_rc == 0 and bool(running) and _nvidia_dkms_ready(dkms_status, running)
        smi_ok = smi_rc == 0
        mismatch = installed_ok and loaded_ok and installed != loaded

        result.checks = {
            "running_kernel": running,
            "nvidia_loaded": loaded or None,
            "nvidia_installed": installed or None,
            "nvidia_dkms_ready": dkms_ready,
            "nvidia_smi_ok": smi_ok,
        }
        if not installed_ok:
            result.errors.append("NVIDIA driver module not found (modinfo failed)")
        if not loaded_ok:
            result.errors.append("NVIDIA module is not loaded")
        if not dkms_ready:
            result.errors.append(f"NVIDIA DKMS module is not installed for kernel {running or '?'}")
        if mismatch:
            message = f"NVIDIA module mismatch: loaded {loaded}, installed {installed}"
            if after_reboot:
                result.errors.append(f"{message} after reboot")
            else:
                result.reboot_reasons.append(message)
        if not smi_ok:
            if after_reboot:
                result.errors.append("nvidia-smi failed after reboot")
            elif not mismatch and installed_ok and loaded_ok and dkms_ready:
                result.warnings.append("nvidia-smi failed after update")
        return result
    except ValueError as exc:
        result.errors.append(str(exc))
        return result


@dataclass
class NodeFlowOutcome:
    """Everything the driver needs to fold this node/manager into the FleetState."""

    record: Optional[NodeRecord] = None
    changed: bool = False
    failed: bool = False
    error: Optional[ErrorEntry] = None
    warnings: List[WarningEntry] = field(default_factory=list)


def run_node_update(
    node: str,
    executor: Executor,
    settings: GlobalSettings,
    *,
    dry_run: bool = False,
    cluster: str = DEFAULT_CLUSTER,
    nvidia_host: bool = False,
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
        nvidia_host: True when this node is flagged ``nvidia_host=true`` in the
                  inventory — the read-only NVIDIA post-upgrade diagnostics run
                  and their classification drives reboot reasons / hard failures.
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
                raise RuntimeError(f"apt dist-upgrade failed (rc={res.rc}): {res.stderr or res.stdout}")
            return res.stdout

        apt_stdout = retry(_apt, retries=settings.node_apt_retries, delay=settings.node_apt_retry_delay, sleep=_sleep)
        apt_changed = _pkg_changed(apt_stdout, "apt")

        # ------------------------------------------------------------------
        # Post-upgrade diagnostics (single primitive: running/latest kernel,
        # Debian reboot-required marker, optional NVIDIA probes). The probes
        # are read-only (check_mode: false) so they run even under --check;
        # Python owns every classification and reboot decision.
        # ------------------------------------------------------------------
        post = executor.node_post_upgrade(nvidia_host=nvidia_host)
        if post.failed or post.rc != 0:
            raise RuntimeError(f"post-upgrade diagnostics failed: {post.stderr or post.stdout}")
        assessment = classify_post_upgrade(post.facts, nvidia_host=nvidia_host)
        reboot_reasons = assessment.reboot_reasons
        hard_errors = assessment.errors
        outcome.warnings = [
            WarningEntry(host=node, task="NVIDIA post-upgrade check", warning=warning)
            for warning in assessment.warnings
        ]
        checks: Optional[Dict[str, Any]] = assessment.checks
        reboot_needed = bool(reboot_reasons)
        reboot_disabled = dry_run or not settings.node_auto_reboot

        # ------------------------------------------------------------------
        # Reboot only when policy and manager-LXC safety allow it. A known
        # diagnostic/DKMS failure prevents rebooting into a broken driver.
        # ------------------------------------------------------------------
        rebooted = False
        if reboot_needed and not is_manager and not reboot_disabled and not hard_errors:
            reboot_result = executor.reboot(timeout=900)
            if reboot_result.failed or reboot_result.rc != 0:
                raise RuntimeError(
                    f"node reboot failed (rc={reboot_result.rc}): "
                    f"{reboot_result.stderr or reboot_result.stdout}"
                )
            rebooted = True
            if settings.apt_proxy_ip:
                http_mod.wait_for_port(
                    settings.apt_proxy_ip,
                    settings.apt_proxy_port,
                    timeout=settings.node_reboot_port_wait_timeout,
                )
            _sleep(15)

            if nvidia_host:
                post_reboot = executor.node_post_upgrade(nvidia_host=True)
                if post_reboot.failed or post_reboot.rc != 0:
                    raise RuntimeError(
                        f"NVIDIA post-reboot check failed: {post_reboot.stderr or post_reboot.stdout}"
                    )
                after = classify_post_upgrade(
                    post_reboot.facts,
                    nvidia_host=True,
                    after_reboot=True,
                )
                hard_errors.extend(after.errors)
                checks = {
                    "pre_reboot": assessment.checks or {},
                    "post_reboot": after.checks or {},
                }
                if after.errors:
                    outcome.error = ErrorEntry(
                        host=node,
                        task="NVIDIA post-reboot check",
                        error="; ".join(after.errors)[:300],
                    )

        # ------------------------------------------------------------------
        # Report (nodes always get a record — no idle suppression)
        # ------------------------------------------------------------------
        if hard_errors:
            status = "FAILED"
            outcome.failed = True
            if outcome.error is None:
                outcome.error = ErrorEntry(
                    host=node,
                    task="NVIDIA post-upgrade check" if nvidia_host else "post-upgrade diagnostics",
                    error="; ".join(hard_errors)[:300],
                )
        else:
            status = node_status(
                apt_changed,
                reboot_needed,
                rebooted,
                is_manager,
                reboot_disabled=reboot_disabled,
            )

        outcome.changed = apt_changed or rebooted or reboot_needed
        pkg_count = _vm_pkg_count(apt_stdout, "apt") if (apt_changed and not dry_run) else None
        packages = (parse_upgraded(apt_stdout, "apt") or None) if apt_changed else None
        outcome.record = NodeRecord(
            node=node,
            status=status,
            pkg_count=pkg_count,
            packages=packages,
            dry_run=dry_run or None,
            reboot_reasons=reboot_reasons or None,
            checks=checks,
        )
        return outcome

    except Exception as exc:  # noqa: BLE001 - mirror Ansible rescue catch-all
        failed_task = getattr(exc, "step_name", type(exc).__name__)
        outcome.failed = True
        outcome.record = NodeRecord(node=node, status="FAILED")
        outcome.error = ErrorEntry(host=node, task=str(failed_task), error=str(exc)[:300])
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
        pkg_count = _vm_pkg_count(res.stdout, "apt") if (apt_changed and not dry_run) else None
        packages = (parse_upgraded(res.stdout, "apt") or None) if apt_changed else None
        outcome.record = NodeRecord(
            node="Ansible-Manager",
            status=status,
            pkg_count=pkg_count,
            packages=packages,
            dry_run=dry_run or None,
        )
        return outcome

    except Exception as exc:  # noqa: BLE001
        failed_task = getattr(exc, "step_name", type(exc).__name__)
        outcome.failed = True
        outcome.record = NodeRecord(node="Ansible-Manager", status="FAILED")
        outcome.error = ErrorEntry(host="Ansible-Manager", task=str(failed_task), error=str(exc)[:300])
        return outcome
