"""The Executor boundary — the only thing the flows use to make Ansible *do* work.

A flow is bound to one host and calls ``run_shell`` (on the target),
``run_local`` (on the manager, for the few delegate_to: localhost commands), and
``reboot`` (the target). The real implementation invokes the run_shell /
reboot_host primitives via ansible-runner; tests supply a fake.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Protocol

from proxmox_fleet.runner import PrimitiveResult, invoke_primitive


class Executor(Protocol):
    host: str

    def run_shell(
        self,
        command: str,
        *,
        become: bool = False,
        chdir: Optional[str] = None,
        environment: Optional[Dict[str, Any]] = None,
        changed_when: Any = True,
        ignore_errors: bool = False,
    ) -> PrimitiveResult:
        ...

    def run_local(self, command: str) -> PrimitiveResult:
        ...

    def reboot(self, *, timeout: int = 600) -> PrimitiveResult:
        ...

    def node_post_upgrade(self, *, nvidia_host: bool = False) -> PrimitiveResult:
        """Run the read-only node post-upgrade diagnostics in one subprocess.

        Returns facts: running_kernel, latest_kernel, reboot_required_exists,
        reboot_required_packages, and (when ``nvidia_host``) the NVIDIA probes:
        nvidia_checked, nvidia_installed(_rc), nvidia_loaded(_rc),
        nvidia_dkms(_rc/status), and nvidia_smi_rc. All tasks are
        check_mode: false, so the probes run even under ``--check`` — Python
        owns every classification and reboot decision.
        """
        ...

    def snapshot(
        self,
        vmid: str,
        *,
        snap_state: str,
        api_host: str,
        api_user: str,
        api_token_id: str,
        api_token_secret: str,
    ) -> PrimitiveResult:
        """Create (snap_state='present') or delete (snap_state='absent') a snapshot.

        Uses snapshot.yml which invokes community.proxmox.proxmox_snap on localhost.
        Works for both LXC containers and QEMU VMs (the Proxmox API is vmid-agnostic).
        api_host must be the node's ansible_host IP (not the inventory name).
        """
        ...

    def introspect(self, lxc_id: str) -> PrimitiveResult:
        """Read pct config, pct status, and update-script content in one subprocess.

        Returns facts: config_stdout, config_rc, status_stdout, pull_rc,
        script_stdout, df_stdout, os_release_stdout. The last two are empty for a
        container that was not running (introspect precedes the flow's pct_start).
        """
        ...

    def vzdump(self, lxc_id: str, *, backup_storage: str, lxc_name: str) -> PrimitiveResult:
        """Create a full vzdump backup. Failure is hard — callers do not ignore errors."""
        ...

    def lxc_os_update(self, lxc_id: str, *, os_update_cmd: str) -> PrimitiveResult:
        """Run OS package upgrade inside the container via the lxc_os_update primitive."""
        ...

    def lxc_app_update(
        self,
        lxc_id: str,
        *,
        lxc_shell: str = "bash",
        lxc_unattended: bool = True,
        lxc_needs_scale: bool = False,
        lxc_build_cpu: str = "",
        lxc_build_ram: str = "",
        lxc_run_cpu: str = "",
        lxc_run_ram: str = "",
    ) -> PrimitiveResult:
        """Run the community-scripts /usr/bin/update via the lxc_app_update primitive.

        Handles resource scaling (up before, down after) internally when lxc_needs_scale=True.
        """
        ...

    def post_update(
        self,
        lxc_id: str,
        *,
        lxc_shell: str = "bash",
        dpkg_hash_cmd: str = "",
        lxc_script_name: str = "",
    ) -> PrimitiveResult:
        """Read dpkg hash and version file after the update in one subprocess.

        Returns facts: dpkg_hash_after, version_after.
        """
        ...

    def pct_rollback(self, lxc_id: str) -> PrimitiveResult:
        """Roll back the container to BEFORE_UPDATE_AUTO via the rollback primitive."""
        ...

    def pct_start(self, lxc_id: str) -> PrimitiveResult:
        """Start a stopped container via the pct_start primitive."""
        ...

    def pct_stop(self, lxc_id: str) -> PrimitiveResult:
        """Stop a container via the pct_stop primitive."""
        ...


class RunnerExecutor:
    """Executor backed by ansible-runner primitives, bound to a single host."""

    def __init__(self, host: str, *, inventory: str = "hosts.ini", check: bool = False) -> None:
        self.host = host
        self.inventory = inventory
        self.check = check

    def _shell(self, command: str, host_pattern: str, **opts: Any) -> PrimitiveResult:
        extravars: Dict[str, Any] = {"shell_command": command}
        if opts.get("become"):
            extravars["shell_become"] = True
        if opts.get("chdir"):
            extravars["shell_chdir"] = opts["chdir"]
        if opts.get("environment"):
            extravars["shell_environment"] = opts["environment"]
        if "changed_when" in opts and opts["changed_when"] is not None:
            extravars["shell_changed_when"] = opts["changed_when"]
        if opts.get("ignore_errors"):
            extravars["shell_ignore_errors"] = True
        result = invoke_primitive(
            "run_shell",
            inventory=self.inventory,
            host_pattern=host_pattern,
            extravars=extravars,
            check=self.check,
        )
        return _merge_facts(result)

    def run_shell(self, command: str, **opts: Any) -> PrimitiveResult:
        return self._shell(command, self.host, **opts)

    def run_local(self, command: str) -> PrimitiveResult:
        return self._shell(command, "localhost")

    def reboot(self, *, timeout: int = 600) -> PrimitiveResult:
        return invoke_primitive(
            "reboot_host",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={"reboot_timeout": timeout},
            check=self.check,
        )

    def node_post_upgrade(self, *, nvidia_host: bool = False) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "node_post_upgrade",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={"nvidia_host": bool(nvidia_host)},
            check=self.check,
        ))

    def snapshot(
        self,
        vmid: str,
        *,
        snap_state: str,
        api_host: str,
        api_user: str,
        api_token_id: str,
        api_token_secret: str,
    ) -> PrimitiveResult:
        result = invoke_primitive(
            "snapshot",
            inventory=self.inventory,
            extravars={
                "vmid": vmid,
                "snap_state": snap_state,
                "api_host": api_host,
                "api_user": api_user,
                "api_token_id": api_token_id,
                "api_token_secret": api_token_secret,
            },
            check=self.check,
        )
        facts = result.facts
        if "changed" in facts:
            result.changed = bool(facts["changed"])
        if "failed" in facts:
            result.failed = bool(facts["failed"])
        return result

    def introspect(self, lxc_id: str) -> PrimitiveResult:
        return invoke_primitive(
            "lxc_introspect",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={"lxc_id": lxc_id},
            check=self.check,
        )

    def vzdump(self, lxc_id: str, *, backup_storage: str, lxc_name: str) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "vzdump",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={
                "lxc_id": lxc_id,
                "backup_storage": backup_storage,
                "lxc_name": lxc_name,
            },
            check=self.check,
        ))

    def lxc_os_update(self, lxc_id: str, *, os_update_cmd: str) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "lxc_os_update",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={"lxc_id": lxc_id, "os_update_cmd": os_update_cmd},
            check=self.check,
        ))

    def lxc_app_update(
        self,
        lxc_id: str,
        *,
        lxc_shell: str = "bash",
        lxc_unattended: bool = True,
        lxc_needs_scale: bool = False,
        lxc_build_cpu: str = "",
        lxc_build_ram: str = "",
        lxc_run_cpu: str = "",
        lxc_run_ram: str = "",
    ) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "lxc_app_update",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={
                "lxc_id": lxc_id,
                "lxc_shell": lxc_shell,
                "lxc_unattended": lxc_unattended,
                "lxc_needs_scale": lxc_needs_scale,
                "lxc_build_cpu": lxc_build_cpu,
                "lxc_build_ram": lxc_build_ram,
                "lxc_run_cpu": lxc_run_cpu,
                "lxc_run_ram": lxc_run_ram,
            },
            check=self.check,
        ))

    def post_update(
        self,
        lxc_id: str,
        *,
        lxc_shell: str = "bash",
        dpkg_hash_cmd: str = "",
        lxc_script_name: str = "",
    ) -> PrimitiveResult:
        return invoke_primitive(
            "lxc_post_update",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={
                "lxc_id": lxc_id,
                "lxc_shell": lxc_shell,
                "dpkg_hash_cmd": dpkg_hash_cmd,
                "lxc_script_name": lxc_script_name,
            },
            check=self.check,
        )

    def pct_rollback(self, lxc_id: str) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "rollback",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={"lxc_id": lxc_id},
            check=self.check,
        ))

    def pct_start(self, lxc_id: str) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "pct_start",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={"lxc_id": lxc_id},
            check=self.check,
        ))

    def pct_stop(self, lxc_id: str) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "pct_stop",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={"lxc_id": lxc_id},
            check=self.check,
        ))


def _merge_facts(result: PrimitiveResult) -> PrimitiveResult:
    """Prefer the explicit set_stats facts the run_shell primitive returns."""
    facts = result.facts
    if "rc" in facts:
        try:
            result.rc = int(facts["rc"])
        except (TypeError, ValueError):
            pass
    if "stdout" in facts:
        result.stdout = str(facts["stdout"])
    if "stderr" in facts:
        result.stderr = str(facts["stderr"])
    if "changed" in facts:
        result.changed = bool(facts["changed"])
    result.failed = result.failed or result.rc != 0
    return result


def snapshot_with_retry(
    executor: Executor,
    vmid: str,
    *,
    snap_state: str,
    retries: int = 3,
    delay: float = 15.0,
    _sleep: Callable[[float], None] = time.sleep,
    **api_params: Any,
) -> PrimitiveResult:
    """Retry executor.snapshot() to handle transient PVE task locks ('CT is locked').

    Uses `until=changed` for create (present) and `until=not failed` for delete
    (absent). Returns a failed PrimitiveResult after exhausting all retries rather
    than raising, so callers can apply their existing warning/fallback logic.
    """
    from proxmox_fleet import orchestration

    predicate = (lambda r: r.changed) if snap_state == "present" else (lambda r: not r.failed)
    try:
        return orchestration.retry(
            lambda: executor.snapshot(vmid, snap_state=snap_state, **api_params),
            retries=retries,
            delay=delay,
            until=predicate,
            exceptions=(),
            sleep=_sleep,
        )
    except Exception:  # noqa: BLE001 - contract: a failed snapshot is a warning, never a raise
        return PrimitiveResult(rc=1, stdout="", stderr="", changed=False, failed=True, facts={})
