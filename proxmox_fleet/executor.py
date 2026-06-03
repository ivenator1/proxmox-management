"""The Executor boundary — the only thing the flows use to make Ansible *do* work.

A flow is bound to one host and calls ``run_shell`` (on the target),
``run_local`` (on the manager, for the few delegate_to: localhost commands), and
``reboot`` (the target). The real implementation invokes the run_shell /
reboot_host primitives via ansible-runner; tests supply a fake.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

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
