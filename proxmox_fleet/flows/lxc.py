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
from proxmox_fleet.changes import lxc_os_changed, lxc_os_pkg_count
from proxmox_fleet.executor import Executor, snapshot_with_retry
from proxmox_fleet.flows._pkg import kuma_healthy
from proxmox_fleet.lxc_parse import parse_ct_script, parse_pct_config, parse_pct_status, script_name_from_update
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import ErrorEntry, LxcRecord, WarningEntry
from proxmox_fleet.status import (
    lxc_app_did_update,
    lxc_app_status,
    lxc_dry_run_status,
    lxc_os_status,
    lxc_rescue_app_status,
    lxc_should_report,
)


def _vprint(node: str, lxc_id: str, name: str, msg: str) -> None:
    print(f"  [{node}/{lxc_id} {name}] {msg}")


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
    """Build the OS package upgrade command for this container.

    ``LC_ALL=C`` pins the locale so lxc_os_changed() sees canonical apt/apk/dnf
    summary lines regardless of the container's configured locale.
    """
    if lxc_os in ("debian", "ubuntu", "devuan"):
        # DEBIAN_FRONTEND=noninteractive: -y alone does not suppress debconf
        # prompts, which would stall an unattended pct exec forever.
        apt_env = "LC_ALL=C DEBIAN_FRONTEND=noninteractive"
        return (
            f"pct exec {lxc_id} -- bash -c "
            f"'{apt_env} apt-get update && {apt_env} apt-get -y dist-upgrade'"
        )
    if lxc_os == "alpine":
        return f"pct exec {lxc_id} -- ash -c 'LC_ALL=C apk -U upgrade'"
    # fedora / arch / other
    return (
        f"pct exec {lxc_id} -- bash -c "
        f"'LC_ALL=C dnf -y upgrade || LC_ALL=C pacman -Syyu --noconfirm'"
    )


def _dpkg_hash_cmd(lxc_id: str, lxc_os: str) -> str:
    """Build the command to capture a hash of the installed package set.

    ``LC_ALL=C`` makes the ``sort`` order locale-independent so the hash is
    stable across runs (and across hosts with different locales).
    """
    if lxc_os == "alpine":
        return f"pct exec {lxc_id} -- ash -c 'LC_ALL=C apk info -v 2>/dev/null | LC_ALL=C sort | md5sum'"
    if lxc_os in ("debian", "ubuntu", "devuan"):
        return f"pct exec {lxc_id} -- bash -c 'LC_ALL=C dpkg-query -W 2>/dev/null | LC_ALL=C sort | md5sum'"
    return ""  # non-apt OS: skip hash


def _read_version(executor: Executor, lxc_id: str, script_name: str, os_type: str) -> str:
    """Read the installed app version from ~/.scriptname inside the container.

    Uses the OS-appropriate shell (``ash`` on Alpine, else ``bash``) — Alpine
    containers frequently ship no bash, so a hardcoded bash read returns empty.
    """
    safe = script_name.lower().replace("-", "")
    shell = _build_shell(os_type)
    cmd = f"pct exec {lxc_id} -- {shell} -c 'cat ~/.{safe} 2>/dev/null || echo \"\"'"
    res = executor.run_shell(cmd, changed_when=False)
    return res.stdout.strip()


def _discover_lxcs(executor: Executor, settings: GlobalSettings) -> List[str]:
    """List container VMIDs on the node that carry any of the configured fleet tags,
    plus any IDs explicitly listed in os_only_lxc_list (OS updates only — they lack
    /usr/bin/update, so the flow naturally reports app="NO SCRIPT")."""
    tag_regex = "|".join(settings.lxc_tags)
    extra_ids = [str(x) for x in settings.os_only_lxc_list]
    extra_clause = ""
    if extra_ids:
        extra_regex = "|".join(extra_ids)
        extra_clause = f' || echo "$vmid" | grep -qxE \'({extra_regex})\''
    # Use `while read vmid rest` instead of awk to extract the first field —
    # awk's $1 gets expanded through extravars/Jinja2/shell quoting layers.
    # if/fi ensures the loop always exits 0 (grep non-match would exit 1 and
    # cause _harvest() to discard stdout as a failed task).
    cmd = (
        f"pct list | tail -n +2 | while read vmid rest; do "
        f"if pct config \"$vmid\" 2>/dev/null | grep -qE '^tags:.*({tag_regex})'{extra_clause}; "
        f"then echo \"$vmid\"; fi; done"
    )
    res = executor.run_shell(cmd, changed_when=False)
    if res.failed:
        raise RuntimeError(f"discover_lxcs shell failed (rc={res.rc}): {res.stderr or '(no stderr)'}")
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
    introspect_res = executor.introspect(lxc_id)
    config_stdout = str(introspect_res.facts.get("config_stdout", ""))
    pct_info = parse_pct_config(config_stdout)
    name: str = pct_info["name"]
    lxc_os: str = pct_info["os_type"]
    is_template: bool = pct_info["is_template"]

    if is_template:
        return LxcFlowOutcome()  # skipped, no record

    status_stdout = str(introspect_res.facts.get("status_stdout", ""))
    status_info = parse_pct_status(status_stdout)
    is_running: bool = status_info["is_running"]
    was_stopped: bool = status_info["was_stopped"]

    if was_stopped:
        executor.pct_start(lxc_id)
        time.sleep(5)
        # Re-read status after start (conditional — can't batch with introspect)
        status_res2 = executor.run_shell(f"pct status {lxc_id}", changed_when=False)
        is_running = parse_pct_status(status_res2.stdout)["is_running"]

    # ------------------------------------------------------------------
    # State tracked across try/finally
    # ------------------------------------------------------------------
    snap_taken = False
    snapshot_failed = False
    rollback_done = False
    outcome = LxcFlowOutcome()

    api_params: Dict[str, Any] = {
        "api_host": api_host,
        "api_user": settings.pve_api_user,
        "api_token_id": settings.pve_api_token_id,
        "api_token_secret": settings.pve_api_token_secret,
    }

    try:
        # ------------------------------------------------------------------
        # Detect — parse the update script pulled by introspect
        # ------------------------------------------------------------------
        ct_script_name: Optional[str] = None
        ct_info: Dict[str, Any] = {}

        pull_rc_val = introspect_res.facts.get("pull_rc", 1)
        lxc_no_update_script = int(pull_rc_val) != 0
        app_excluded = lxc_id in {str(x) for x in settings.app_update_exclude_list}

        if settings.lxc_verbose:
            _vprint(node, lxc_id, name, f"script={'NONE (pull failed)' if lxc_no_update_script else 'found'}")

        if not lxc_no_update_script:
            update_content = str(introspect_res.facts.get("script_stdout", ""))
            ct_script_name_raw = script_name_from_update(update_content) or ""

            if settings.lxc_verbose:
                _vprint(node, lxc_id, name, f"ct_script={ct_script_name_raw or 'NOT FOUND'}")

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
            if app_excluded:
                dry_status = "SKIPPED"
            else:
                gh_repo = ct_info.get("gh_repo", "")
                installed_ver = ""
                latest_tag = ""
                fetch_ok = True

                if ct_script_name and gh_repo:
                    installed_ver = _read_version(executor, lxc_id, ct_script_name, lxc_os)
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
            executor.vzdump(lxc_id, backup_storage=settings.lxc_backup_storage, lxc_name=name)

        if strategy in ("snapshot", "both") and lxc_id not in {str(x) for x in settings.snapshot_exclude_list}:
            snap_res = snapshot_with_retry(executor, lxc_id, snap_state="present",
                                           retries=settings.snapshot_retries,
                                           delay=settings.snapshot_retry_delay,
                                           **api_params)
            snap_taken = snap_res.changed
            if settings.lxc_verbose:
                _vprint(node, lxc_id, name, f"snapshot={'taken' if snap_taken else 'FAILED'}")
            if not snap_taken:
                outcome.warnings.append(WarningEntry(
                    host=lxc_id, task="Create snapshot",
                    warning="snapshot failed — automatic rollback unavailable for this update",
                ))
                snapshot_failed = True

        # ------------------------------------------------------------------
        # Update
        # ------------------------------------------------------------------
        # 1. Read pre-update version
        ver_before = ""
        if ct_script_name and not lxc_no_update_script and not app_excluded:
            ver_before = _read_version(executor, lxc_id, ct_script_name, lxc_os)

        # 2. OS update (skip if excluded)
        os_res_stdout = ""
        os_failed = False
        if lxc_id not in {str(x) for x in settings.os_update_exclude_list}:
            os_cmd = _os_update_cmd(lxc_id, lxc_os)
            os_res = executor.lxc_os_update(lxc_id, os_update_cmd=os_cmd)
            os_res_stdout = os_res.stdout
            os_failed = os_res.failed
            if settings.lxc_verbose:
                summary = (os_res.stdout or "").strip().replace("\n", " ")[:120]
                _vprint(node, lxc_id, name, f"os_update: {'FAILED' if os_failed else 'ok'}  {summary}")

        # 3. dpkg hash before app update
        dpkg_before = ""
        if not lxc_no_update_script and not app_excluded:
            hash_cmd = _dpkg_hash_cmd(lxc_id, lxc_os)
            if hash_cmd:
                dpkg_res = executor.run_shell(hash_cmd, changed_when=False)
                dpkg_before = dpkg_res.stdout.strip()

        # 4-5. App update with resource scaling (primitive handles scale up/down)
        needs_scale = ct_info.get("needs_resource_scale", False)
        build_cpu = ct_info.get("build_cpu", "")
        build_ram = ct_info.get("build_ram", "")
        run_cpu = ct_info.get("run_cpu", "")
        run_ram = ct_info.get("run_ram", "")

        app_failed = False
        app_changed = False
        shell = _build_shell(lxc_os)

        if not lxc_no_update_script and not app_excluded:
            app_res = executor.lxc_app_update(
                lxc_id,
                lxc_shell=shell,
                lxc_unattended=settings.lxc_unattended,
                lxc_needs_scale=bool(needs_scale),
                lxc_build_cpu=str(build_cpu),
                lxc_build_ram=str(build_ram),
                lxc_run_cpu=str(run_cpu),
                lxc_run_ram=str(run_ram),
            )
            app_failed = app_res.failed
            app_changed = not app_res.failed  # tentative; overridden below by version/hash
            if settings.lxc_verbose:
                summary = (app_res.stdout or "").strip().replace("\n", " ")[:120]
                _vprint(node, lxc_id, name, f"app_update: {'FAILED' if app_failed else 'ok'}  {summary}")

        # 6-7. dpkg hash after + version after (batched into one primitive)
        dpkg_after = ""
        ver_after = ""
        if not lxc_no_update_script and not app_excluded:
            hash_cmd = _dpkg_hash_cmd(lxc_id, lxc_os)
            safe_script_name = ct_script_name.lower().replace("-", "") if ct_script_name else ""
            post_res = executor.post_update(
                lxc_id,
                lxc_shell=shell,
                dpkg_hash_cmd=hash_cmd,
                lxc_script_name=safe_script_name,
            )
            dpkg_after = str(post_res.facts.get("dpkg_hash_after", "")).strip()
            ver_after = str(post_res.facts.get("version_after", "")).strip()
            if settings.lxc_verbose and ct_script_name:
                dpkg_diff = "dpkg=changed" if dpkg_before != dpkg_after else "dpkg=same"
                _vprint(node, lxc_id, name, f"ver: {ver_before!r} → {ver_after!r}  {dpkg_diff}")

        # 9. Wait for Proxmox task locks
        time.sleep(5)

        # 10. Reboot check — unconditional (parity with the old update.yml): an
        # OS update alone can install a kernel, and os_only_lxc_list containers
        # never have an update script, so gating on it would skip their reboots.
        reboot_done = False
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
            excluded=app_excluded,
            app_failed=app_failed,
            app_changed=app_changed,
            ver_before=ver_before,
            ver_after=ver_after,
            dpkg_before=dpkg_before,
            dpkg_after=dpkg_after,
        )
        os_changed = lxc_os_changed(os_res_stdout)
        # Structured decision (shared with lxc_app_status) — never re-parse the
        # rendered status string for control flow.
        app_did_update = lxc_app_did_update(
            no_update_script=lxc_no_update_script,
            excluded=app_excluded,
            app_failed=app_failed,
            app_changed=app_changed,
            ver_before=ver_before,
            ver_after=ver_after,
            dpkg_before=dpkg_before,
            dpkg_after=dpkg_after,
        )
        something_changed = app_did_update or os_changed

        kuma_id = str(settings.lxc_kuma_map.get(lxc_id, ""))
        if kuma_id and settings.kuma_url and something_changed:
            kuma_url = f"{settings.kuma_url}/api/status-page/heartbeat/{settings.kuma_slug}"
            try:
                http_mod.poll_until(
                    lambda: http_mod.get_json(kuma_url),
                    lambda p: kuma_healthy(p, monitor_id=kuma_id),
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
        script_expected = lxc_id not in {str(x) for x in settings.os_only_lxc_list}
        if lxc_should_report(app_status_str, os_status_str, dry_run=False,
                             script_expected=script_expected):
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
                executor.pct_rollback(lxc_id)
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
            snapshot_with_retry(executor, lxc_id, snap_state="absent",
                                retries=settings.snapshot_retries,
                                delay=settings.snapshot_retry_delay,
                                **api_params)

        if was_stopped and not rollback_done and not is_template:
            try:
                executor.pct_stop(lxc_id)
            except Exception:  # noqa: BLE001
                pass
