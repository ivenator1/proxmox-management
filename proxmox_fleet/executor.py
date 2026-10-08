"""The Executor boundary — the only thing the flows use to make Ansible *do* work.

A flow is bound to one host and calls ``run_shell`` (on the target),
``run_local`` (on the manager, for the few delegate_to: localhost commands), and
``reboot`` (the target). The real implementation invokes the run_shell /
reboot_host primitives via ansible-runner; tests supply a fake.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from proxmox_fleet.alloy import AlloyExecutor
from proxmox_fleet.runner import PrimitiveResult, invoke_primitive

# Node-side stdlib helper staged by the housekeeping primitives. Kept next to
# this module so the source travels with the package; the primitives copy its
# exact contents into a root-only node temp dir and run it with python3.
_HELPER_PATH = Path(__file__).with_name("housekeeping_io.py")


def housekeeping_helper_source() -> str:
    """Read the node-side housekeeping helper source to stage per operation."""
    return _HELPER_PATH.read_text(encoding="utf-8")


def _housekeeping_extravars(lxc_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Build the transport extravars shared by all housekeeping primitives.

    The request carries no policy: it is the exact manager-built JSON object of
    file wire records (and capture_id where applicable). It is serialized
    compactly and staged as a file so no log bodies or request text can leak
    into runner facts/stdout.
    """
    return {
        "lxc_id": lxc_id,
        "housekeeping_helper_content": housekeeping_helper_source(),
        "housekeeping_request_json": json.dumps(request, separators=(",", ":")),
    }


class SnapshotExecutor(Protocol):
    """Narrow capability used by snapshot retry policy."""

    host: str

    def snapshot(
        self,
        vmid: str,
        *,
        snap_state: str,
        api_host: str,
        api_user: str,
        api_token_id: str,
        api_token_secret: str,
        timeout: int = 600,
        api_timeout: int = 30,
    ) -> PrimitiveResult:
        ...


class Executor(AlloyExecutor, Protocol):
    """Everything a flow may ask a bound host to do.

    Inherits :class:`AlloyExecutor` so the typed logging/housekeeping policy can
    require both Alloy compliance and the bounded maintenance transport from one
    interface. ``RunnerExecutor`` implements both; missing capabilities are a
    typing error, never a silent ``getattr``/``Any`` fallback.
    """

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

    def node_post_upgrade(
        self,
        *,
        nvidia_host: bool = False,
        after_reboot: bool = False,
    ) -> PrimitiveResult:
        """Run the read-only node post-upgrade diagnostics in one subprocess.

        Returns running, latest-installed and configured target kernel facts,
        reboot-required metadata, and optional NVIDIA loaded/target-module,
        DKMS and device-health probes. ``after_reboot`` makes NVIDIA validation
        target the running kernel. All tasks run under check mode; Python owns
        classification and reboot policy.
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
        timeout: int = 600,
        api_timeout: int = 30,
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
        script_stdout, df_stdout, boot_df_stdout, os_release_stdout. The disk and
        OS facts are empty for a container that was not running (introspect
        precedes the flow's pct_start).
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
        lxc_bypass_storage_guard: bool = False,
    ) -> PrimitiveResult:
        """Run the community-scripts /usr/bin/update via the lxc_app_update primitive.

        Handles resource scaling and a Python-approved high-utilization storage
        guard bypass internally.
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

    def housekeeping_probe(self, lxc_id: str) -> PrimitiveResult:
        """Read-only housekeeping facts for one container via its node.

        Returns facts keys ``guest``, ``disk``, ``journal_bytes``, ``profiles``,
        ``files``, ``cache_paths``, ``busy_tools``, ``binaries``,
        ``log_access_ready`` and ``policy_sha256``. The primitive runs under
        check mode too; it performs no guest writes.
        """
        ...

    def housekeeping_capture(
        self, lxc_id: str, *, files: List[Dict[str, Any]], capture_id: str
    ) -> PrimitiveResult:
        """Freeze initial file high-water prefixes into the node root-only spool.

        ``files`` are exact manager-built wire records; ``capture_id`` encodes
        the sharded spool namespace. Returns facts ``capture_id`` and ``files``
        (wire records plus ``sha256`` and ``blob_path``). The persistent spool is
        never cleaned up here — only the transient helper/request stage is.
        """
        ...

    def housekeeping_snapshot(
        self, lxc_id: str, *, files: List[Dict[str, Any]], destination: str
    ) -> PrimitiveResult:
        """Tar requested byte ranges on the node and fetch them to the manager.

        ``destination`` is the exact manager-side tar path. Returns facts
        ``files`` (observed metadata); raw bytes are never placed in facts or
        stdout and a failed fetch fails the primitive closed.
        """
        ...

    def housekeeping_apply(self, lxc_id: str, *, command: str) -> PrimitiveResult:
        """Action transport only: run *command* in the container via ``pct exec``.

        Quoting is shlex-safe and ``lxc_id`` must be numeric; the manager
        constructs every command and controls dry-run, so no policy lives here.
        The real container exit status is preserved.
        """
        ...

    def housekeeping_prune(
        self,
        lxc_id: str,
        *,
        files: List[Dict[str, Any]],
    ) -> PrimitiveResult:
        """Quarantine-then-unlink approved closed files inside the container.

        ``files`` carry exact identities/digests and persisted quarantine paths,
        including unfinished recovery intents. Returns per-file outcomes and
        measured reclaimed bytes.
        """
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

    def node_post_upgrade(
        self,
        *,
        nvidia_host: bool = False,
        after_reboot: bool = False,
    ) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "node_post_upgrade",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars={
                "nvidia_host": bool(nvidia_host),
                "after_reboot": bool(after_reboot),
            },
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
        timeout: int = 600,
        api_timeout: int = 30,
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
                "timeout": timeout,
                "api_timeout": api_timeout,
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
        lxc_bypass_storage_guard: bool = False,
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
                "lxc_bypass_storage_guard": lxc_bypass_storage_guard,
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

    def housekeeping_probe(self, lxc_id: str) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "lxc_housekeeping_probe",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars=_housekeeping_extravars(lxc_id, {}),
            check=self.check,
        ))

    def housekeeping_capture(
        self, lxc_id: str, *, files: List[Dict[str, Any]], capture_id: str
    ) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "lxc_log_capture",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars=_housekeeping_extravars(
                lxc_id, {"files": files, "capture_id": capture_id}
            ),
            check=self.check,
        ))

    def housekeeping_snapshot(
        self, lxc_id: str, *, files: List[Dict[str, Any]], destination: str
    ) -> PrimitiveResult:
        extravars = _housekeeping_extravars(lxc_id, {"files": files})
        extravars["housekeeping_manager_destination"] = destination
        return _merge_facts(invoke_primitive(
            "lxc_log_snapshot",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars=extravars,
            check=self.check,
        ))

    def housekeeping_apply(self, lxc_id: str, *, command: str) -> PrimitiveResult:
        # The ID is interpolated into a shell string (pct exec), so it MUST be a
        # bare container number; the command is shlex-quoted and passed as one
        # argv to `sh -c`. ignore_errors keeps run_shell's set_stats reachable so
        # the container's real exit status survives for the caller.
        container_id = str(lxc_id).strip()
        if not container_id.isdigit():
            return PrimitiveResult(
                rc=1,
                stdout="",
                stderr=f"housekeeping_apply refused non-numeric lxc_id: {lxc_id!r}",
                changed=False,
                failed=True,
            )
        wrapped = f"pct exec {container_id} -- sh -c {shlex.quote(command)}"
        return self.run_shell(wrapped, ignore_errors=True)

    def housekeeping_prune(
        self,
        lxc_id: str,
        *,
        files: List[Dict[str, Any]],
    ) -> PrimitiveResult:
        return _merge_facts(invoke_primitive(
            "lxc_log_prune",
            inventory=self.inventory,
            host_pattern=self.host,
            extravars=_housekeeping_extravars(lxc_id, {"files": files}),
            check=self.check,
        ))

    def alloy_probe(self, *, lxc_id: Optional[str] = None) -> PrimitiveResult:
        primitive = "alloy_lxc_probe" if lxc_id is not None else "alloy_vm_probe"
        extravars = {"lxc_id": lxc_id} if lxc_id is not None else {}
        return _merge_facts(invoke_primitive(
            primitive,
            inventory=self.inventory,
            host_pattern=self.host,
            extravars=extravars,
            # Probe tasks explicitly use check_mode: false; they are read-only and
            # must run during --check so Python can classify drift.
            check=self.check,
        ))

    def alloy_reconcile(
        self,
        *,
        lxc_id: Optional[str] = None,
        desired_content: str,
        install: bool,
        configure: bool,
        add_journal_group: bool,
        repair_service: bool,
    ) -> PrimitiveResult:
        primitive = "alloy_lxc_reconcile" if lxc_id is not None else "alloy_vm_reconcile"
        extravars: Dict[str, Any] = {
            "alloy_config_content": desired_content,
            "alloy_install": install,
            "alloy_configure": configure,
            "alloy_add_journal_group": add_journal_group,
            "alloy_repair_service": repair_service,
        }
        if lxc_id is not None:
            extravars["lxc_id"] = lxc_id
        return _merge_facts(invoke_primitive(
            primitive,
            inventory=self.inventory,
            host_pattern=self.host,
            extravars=extravars,
            check=self.check,
        ))


def _merge_facts(result: PrimitiveResult) -> PrimitiveResult:
    """Prefer the explicit set_stats facts the run_shell primitive returns."""
    facts = result.facts
    if "rc" in facts:
        try:
            result.rc = int(facts["rc"])
        except (TypeError, ValueError):
            # Preserve PrimitiveResult.rc when a malformed optional fact is
            # returned; the runner-level status remains authoritative.
            result.rc = int(result.rc)
    if "stdout" in facts:
        result.stdout = str(facts["stdout"])
    if "stderr" in facts:
        result.stderr = str(facts["stderr"])
    if "changed" in facts:
        result.changed = bool(facts["changed"])
    result.failed = result.failed or result.rc != 0
    return result


def snapshot_failure_warning(result: PrimitiveResult) -> str:
    """Build a useful non-fatal warning from a failed snapshot primitive."""
    base = "snapshot failed — automatic rollback unavailable for this update"
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    if not detail:
        return base
    return f"{base}: {detail[-400:]}"


def snapshot_with_retry(
    executor: SnapshotExecutor,
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
    last_result: Optional[PrimitiveResult] = None

    def attempt() -> PrimitiveResult:
        nonlocal last_result
        last_result = executor.snapshot(vmid, snap_state=snap_state, **api_params)
        return last_result

    try:
        return orchestration.retry(
            attempt,
            retries=retries,
            delay=delay,
            until=predicate,
            exceptions=(),
            sleep=_sleep,
        )
    except Exception as exc:  # noqa: BLE001 - snapshot failures remain non-fatal warnings
        if last_result is not None:
            return last_result
        return PrimitiveResult(
            rc=1,
            stdout="",
            stderr=str(exc),
            changed=False,
            failed=True,
            facts={},
        )
