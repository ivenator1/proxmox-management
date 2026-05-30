"""lxc_update flow — the Python decomposition of roles/lxc_update/tasks/*.

Owns the full per-container control flow that used to live in main.yml's
block/rescue/always:

  introspect (fail-loud) → try: detect → [dry_check] → backup → update →
  health → report / except: rescue (rollback) / finally: cleanup

All decisions are here in Python; the Executor + http module do the actual work.
Status strings come from proxmox_fleet.status (byte-parity with the old Jinja).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from proxmox_fleet import http as http_mod
from proxmox_fleet.changes import dpkg_hash_differs, lxc_os_changed, lxc_os_pkg_count
from proxmox_fleet.executor import Executor
from proxmox_fleet.lxc_parse import parse_ct_script, parse_pct_config, parse_pct_status, script_name_from_update
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import ErrorEntry, LxcRecord, WarningEntry
from proxmox_fleet.status import (
    lxc_app_status,
    lxc_dry_run_status,
    lxc_os_status,
    lxc_rescue_app_status,
    lxc_should_report,
)


class HealthCheckError(RuntimeError):
    """Kuma health check timed out — triggers rescue/rollback."""


@dataclass
class LxcFlowOutcome:
    """Everything the driver needs to fold this container into the FleetState."""

    record: Optional[LxcRecord] = None
    changed: bool = False
    failed: bool = False
    error: Optional[ErrorEntry] = None
    warnings: List[WarningEntry] = field(default_factory=list)

    @property
    def should_report(self) -> bool:
        return self.record is not None


def _build_shell(lxc_os: str) -> str:
    """Return the correct shell for this OS type inside the container."""
    return "ash" if lxc_os == "alpine" else "bash"


def _os_update_cmd(lxc_id: str, lxc_os: str) -> str:
    """Build the OS package upgrade command for this container."""
    if lxc_os in ("debian", "ubuntu", "devuan"):
        return (
            f"pct exec {lxc_id} -- bash -c "
            f"'apt-get update && apt-get -y dist-upgrade'"
        )
    if lxc_os == "alpine":
        return f"pct exec {lxc_id} -- ash -c 'apk -U upgrade'"
    # fedora / arch / other
    return (
        f"pct exec {lxc_id} -- bash -c "
        f"'dnf -y upgrade || pacman -Syyu --noconfirm'"
    )


def _dpkg_hash_cmd(lxc_id: str, lxc_os: str) -> str:
    """Build the command to capture a hash of the installed package set."""
    if lxc_os == "alpine":
        return f"pct exec {lxc_id} -- ash -c 'apk info -v 2>/dev/null | sort | md5sum'"
    if lxc_os in ("debian", "ubuntu", "devuan"):
        return f"pct exec {lxc_id} -- bash -c 'dpkg-query -W 2>/dev/null | sort | md5sum'"
    return ""  # non-apt OS: skip hash


def _read_version(executor: Executor, lxc_id: str, script_name: str) -> str:
    """Read the installed app version from ~/.scriptname inside the container."""
    safe = script_name.lower().replace("-", "")
    shell = _build_shell("")  # default bash; we don't have os_type here but script_name is fine
    cmd = f"pct exec {lxc_id} -- {shell} -c 'cat ~/.{safe} 2>/dev/null || echo \"\"'"
    res = executor.run_shell(cmd, changed_when=False)
    return res.stdout.strip()


def _kuma_healthy(payload: Any, *, monitor_id: str) -> bool:
    """Predicate for poll_until: check if the Kuma monitor shows status=1."""
    beats = (payload or {}).get("heartbeatList", {}).get(monitor_id, [])
    return bool(beats) and beats[-1].get("status") == 1


def _discover_lxcs(executor: Executor, settings: GlobalSettings) -> List[str]:
    """List container VMIDs on the node that carry any of the configured fleet tags."""
    tag_regex = "|".join(settings.lxc_tags)
    cmd = (
        f"pct list | awk 'NR>1 {{print $1}}' | while read id; do "
        f"pct config \"$id\" 2>/dev/null "
        f"| grep -qE '^tags:.*({tag_regex})' && echo \"$id\"; done"
    )
    res = executor.run_shell(cmd, changed_when=False)
    raw_ids = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    exclude = {str(x) for x in settings.exclude_list}
    return [lxc_id for lxc_id in raw_ids if lxc_id not in exclude]


def run_lxc_update(
    node: str,
    lxc_id: str,
    executor: Executor,
    settings: GlobalSettings,
    *,
    dry_run: bool = False,
    api_host: str = "",
) -> LxcFlowOutcome:
    """Run the whole lxc_update flow for one container. Never raises — failures
    are captured into the outcome (FAILED record + error entry), mirroring rescue.

    Args:
        node:      Proxmox node inventory hostname (used in LxcRecord.node).
        lxc_id:    Container VMID string (e.g. "101").
        executor:  Bound to the Proxmox node; uses run_shell for pct commands.
        settings:  GlobalSettings from vars.yml.
        dry_run:   When True, detect phase runs (version compare) but no updates.
        api_host:  The node's ansible_host IP for the Proxmox API (snapshot calls).
                   Required when lxc_backup_strategy includes 'snapshot'.
    """

    # ------------------------------------------------------------------
    # Introspect — OUTSIDE try/except (fail loud like Ansible block does).
    # Templates and introspect failures abort without hitting rescue.
    # ------------------------------------------------------------------
    config_res = executor.run_shell(f"pct config {lxc_id}", changed_when=False)
    pct_info = parse_pct_config(config_res.stdout)
    name: str = pct_info["name"]
    lxc_os: str = pct_info["os_type"]
    is_template: bool = pct_info["is_template"]

    if is_template:
        return LxcFlowOutcome()  # skipped, no record

    status_res = executor.run_shell(f"pct status {lxc_id}", changed_when=False)
    status_info = parse_pct_status(status_res.stdout)
    is_running: bool = status_info["is_running"]
    was_stopped: bool = status_info["was_stopped"]

    if was_stopped:
        executor.run_shell(f"pct start {lxc_id}")
        time.sleep(5)
        # Re-read status after start
        status_res2 = executor.run_shell(f"pct status {lxc_id}", changed_when=False)
        is_running = parse_pct_status(status_res2.stdout)["is_running"]

    # ------------------------------------------------------------------
    # State tracked across try/finally
    # ------------------------------------------------------------------
    snap_taken = False
    snapshot_failed = False
    rollback_done = False
    outcome = LxcFlowOutcome()

    api_params: Dict[str, str] = {
        "api_host": api_host,
        "api_user": settings.pve_api_user,
        "api_token_id": settings.pve_api_token_id,
        "api_token_secret": settings.pve_api_token_secret,
    }

    try:
        # ------------------------------------------------------------------
        # Detect — pull /usr/bin/update and parse the ct script
        # ------------------------------------------------------------------
        ct_script_name: Optional[str] = None
        ct_info: Dict[str, Any] = {}

        pull_dst = f"/tmp/ansible_update_{lxc_id}"
        pull_res = executor.run_shell(
            f"pct pull {lxc_id} /usr/bin/update {pull_dst}", changed_when=False
        )
        lxc_no_update_script = pull_res.rc != 0

        if not lxc_no_update_script:
            grep_res = executor.run_shell(
                f"grep -oP 'ct/\\K[^.]+(?=\\.sh)' {pull_dst} | head -1",
                changed_when=False,
            )
            ct_script_name_raw = grep_res.stdout.strip()
            executor.run_shell(f"rm -f {pull_dst}", changed_when=False)

            if ct_script_name_raw:
                ct_script_name = ct_script_name_raw
                # Fetch the ct script from GitHub (local manager has outbound HTTPS)
                gh_url = (
                    f"https://raw.githubusercontent.com/community-scripts/ProxmoxVE"
                    f"/main/ct/{ct_script_name}.sh"
                )
                try:
                    content = http_mod.request(gh_url).body
                    ct_info = parse_ct_script(content)
                except Exception:  # noqa: BLE001 - fail-open like detect.yml failed_when: false
                    ct_info = parse_ct_script("")
            else:
                lxc_no_update_script = True

        # ------------------------------------------------------------------
        # Dry-check — version compare only, no mutations
        # ------------------------------------------------------------------
        if dry_run:
            gh_repo = ct_info.get("gh_repo", "")
            installed_ver = ""
            latest_tag = ""
            fetch_ok = True

            if ct_script_name and gh_repo:
                installed_ver = _read_version(executor, lxc_id, ct_script_name)
                try:
                    data = http_mod.get_json(
                        f"https://api.github.com/repos/{gh_repo}/releases/latest",
                        headers={"Accept": "application/vnd.github+json"},
                    )
                    latest_tag = str((data or {}).get("tag_name", "")).strip()
                except Exception:  # noqa: BLE001
                    fetch_ok = False

            dry_status = lxc_dry_run_status(
                gh_repo=gh_repo,
                fetch_ok=fetch_ok,
                installed_ver=installed_ver,
                latest_tag=latest_tag,
            )
            outcome.record = LxcRecord(
                node=node, name=name, id=lxc_id,
                app=dry_status, os="", snap=False,
            )
            return outcome

        # ------------------------------------------------------------------
        # Backup
        # ------------------------------------------------------------------
        strategy = settings.lxc_backup_strategy
        if strategy in ("vzdump", "both"):
            vzdump_cmd = (
                f"vzdump {lxc_id} --compress zstd --storage {settings.lxc_backup_storage}"
                f' --notes-template "{name} - ansible fleet-update"'
            )
            executor.run_shell(vzdump_cmd)  # fail hard — no ignore_errors

        if strategy in ("snapshot", "both") and lxc_id not in {str(x) for x in settings.snapshot_exclude_list}:
            snap_res = executor.snapshot(lxc_id, snap_state="present", **api_params)
            snap_taken = snap_res.changed
            if not snap_taken:
                outcome.warnings.append(WarningEntry(
                    host=lxc_id, task="Create snapshot",
                    warning="proxmox_snap returned changed=false — snapshot may have failed; "
                            "rollback will not be available",
                ))
                snapshot_failed = True

        # ------------------------------------------------------------------
        # Update
        # ------------------------------------------------------------------
        # 1. Read pre-update version
        ver_before = ""
        if ct_script_name and not lxc_no_update_script:
            ver_before = _read_version(executor, lxc_id, ct_script_name)

        # 2. OS update (skip if excluded)
        os_res_stdout = ""
        os_failed = False
        if lxc_id not in {str(x) for x in settings.os_update_exclude_list}:
            os_cmd = _os_update_cmd(lxc_id, lxc_os)
            os_res = executor.run_shell(os_cmd, ignore_errors=True)
            os_res_stdout = os_res.stdout
            os_failed = os_res.failed

        # 3. dpkg hash before app update
        dpkg_before = ""
        if not lxc_no_update_script:
            hash_cmd = _dpkg_hash_cmd(lxc_id, lxc_os)
            if hash_cmd:
                dpkg_res = executor.run_shell(hash_cmd, changed_when=False)
                dpkg_before = dpkg_res.stdout.strip()

        # 4. Scale up resources (if needed)
        needs_scale = ct_info.get("needs_resource_scale", False)
        build_cpu = ct_info.get("build_cpu", "")
        build_ram = ct_info.get("build_ram", "")
        run_cpu = ct_info.get("run_cpu", "")
        run_ram = ct_info.get("run_ram", "")

        if needs_scale and build_cpu and build_ram:
            executor.run_shell(
                f"pct set {lxc_id} --cores {build_cpu} --memory {build_ram}",
                changed_when=False,
            )

        # 5. App update
        app_res_stdout = ""
        app_failed = False
        app_changed = False

        if not lxc_no_update_script:
            shell = _build_shell(lxc_os)
            phs_silent = "export PHS_SILENT=1" if settings.lxc_unattended else ""
            app_cmd = (
                f"pct exec {lxc_id} -- {shell} -c '"
                f"mkdir -p /tmp/.nc; "
                f"printf \"#!/bin/sh\\n:\\n\" > /tmp/.nc/clear; "
                f"chmod +x /tmp/.nc/clear; "
                f"export PATH=/tmp/.nc:$PATH; "
                f"export TERM=dumb; "
                f"{phs_silent + '; ' if phs_silent else ''}"
                f"/usr/bin/update'"
            )
            app_res = executor.run_shell(app_cmd, ignore_errors=True)
            app_res_stdout = app_res.stdout
            app_failed = app_res.failed
            app_changed = not app_res.failed  # tentative; overridden below by version/hash

        # 6. dpkg hash after
        dpkg_after = ""
        if not lxc_no_update_script:
            hash_cmd = _dpkg_hash_cmd(lxc_id, lxc_os)
            if hash_cmd:
                dpkg_res2 = executor.run_shell(hash_cmd, changed_when=False)
                dpkg_after = dpkg_res2.stdout.strip()

        # 7. Read post-update version
        ver_after = ""
        if ct_script_name and not lxc_no_update_script:
            ver_after = _read_version(executor, lxc_id, ct_script_name)

        # 8. Scale down
        if needs_scale and run_cpu and run_ram:
            executor.run_shell(
                f"pct set {lxc_id} --cores {run_cpu} --memory {run_ram}",
                changed_when=False,
            )

        # 9. Wait for Proxmox task locks
        time.sleep(5)

        # 10. Reboot check
        reboot_done = False
        if not lxc_no_update_script:
            reboot_chk = executor.run_shell(
                f"pct exec {lxc_id} -- test -f /var/run/reboot-required",
                changed_when=False, ignore_errors=True,
            )
            if reboot_chk.rc == 0 and settings.lxc_auto_reboot:
                executor.run_shell(f"pct reboot {lxc_id}")
                reboot_done = True

        # ------------------------------------------------------------------
        # Health check (only when something changed)
        # ------------------------------------------------------------------
        app_status_str = lxc_app_status(
            is_template=is_template,
            is_running=is_running,
            dry_run=False,
            no_update_script=lxc_no_update_script,
            app_failed=app_failed,
            app_changed=app_changed,
            ver_before=ver_before,
            ver_after=ver_after,
            dpkg_before=dpkg_before,
            dpkg_after=dpkg_after,
        )
        os_changed = lxc_os_changed(os_res_stdout)
        something_changed = ("updated" in app_status_str.lower()) or os_changed

        kuma_id = str(settings.lxc_kuma_map.get(lxc_id, ""))
        if kuma_id and settings.kuma_url and something_changed:
            kuma_url = f"{settings.kuma_url}/api/status-page/heartbeat/{settings.kuma_slug}"
            try:
                http_mod.poll_until(
                    lambda: http_mod.get_json(kuma_url),
                    lambda p: _kuma_healthy(p, monitor_id=kuma_id),
                    retries=settings.kuma_health_check_retries,
                    delay=settings.kuma_health_check_delay,
                )
            except Exception as exc:  # noqa: BLE001
                raise HealthCheckError(f"Kuma health check failed for {lxc_id}: {exc}") from exc

        # ------------------------------------------------------------------
        # Report
        # ------------------------------------------------------------------
        pkg_count = lxc_os_pkg_count(os_res_stdout)
        os_status_str = lxc_os_status(
            is_template=is_template,
            is_running=is_running,
            dry_run=False,
            excluded=(lxc_id in {str(x) for x in settings.os_update_exclude_list}),
            os_failed=os_failed,
            os_changed=os_changed,
            pkg_count=pkg_count,
            reboot_done=reboot_done,
        )

        outcome.changed = something_changed
        if lxc_should_report(app_status_str, os_status_str, dry_run=False):
            outcome.record = LxcRecord(
                node=node, name=name, id=lxc_id,
                app=app_status_str, os=os_status_str, snap=snap_taken,
            )
        return outcome

    except Exception as exc:  # noqa: BLE001 - mirror Ansible rescue catch-all
        # ------------------------------------------------------------------
        # Rescue — rollback if snapshot was taken; record FAILED
        # ------------------------------------------------------------------
        failed_task = getattr(exc, "step_name", type(exc).__name__)

        if snap_taken:
            try:
                executor.run_shell(
                    f"pct rollback {lxc_id} BEFORE_UPDATE_AUTO",
                    ignore_errors=True,
                )
                # Poll until container is running again (up to 12 × 10s)
                for _ in range(12):
                    time.sleep(10)
                    chk = executor.run_shell(f"pct status {lxc_id}", changed_when=False)
                    if "status: running" in chk.stdout:
                        break
                rollback_done = True
            except Exception:  # noqa: BLE001 - rollback errors are ignored
                pass

        rescue_app = lxc_rescue_app_status(
            rollback_done=rollback_done, snapshot_failed=snapshot_failed
        )
        outcome.failed = True
        outcome.record = LxcRecord(
            node=node, name=name, id=lxc_id,
            app=rescue_app, os="FAILED", snap=False,
        )
        outcome.error = ErrorEntry(
            host=lxc_id, task=str(failed_task), error=str(exc)[:300]
        )
        outcome.warnings = list(outcome.warnings)
        return outcome

    finally:
        # ------------------------------------------------------------------
        # Always — delete snapshot; stop container if it was stopped before
        # ------------------------------------------------------------------
        if snap_taken:
            try:
                executor.snapshot(lxc_id, snap_state="absent", **api_params)
            except Exception:  # noqa: BLE001 - cleanup errors ignored
                pass

        if was_stopped and not rollback_done and not is_template:
            try:
                executor.run_shell(f"pct stop {lxc_id}", ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
