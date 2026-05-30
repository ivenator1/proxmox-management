"""Thin wrapper around ansible-runner: invoke ONE execution primitive and parse
its result into a typed ``PrimitiveResult``.

This is the only place Python talks to Ansible. Primitives are the small task
files under ``ansible/primitives/`` — each performs exactly one remote/privileged
action and makes no decisions. ``ansible_runner`` is imported lazily so that unit
tests and type-checking do not require it installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PrimitiveResult:
    """The structured outcome of one primitive invocation."""

    rc: int
    changed: bool = False
    failed: bool = False
    stdout: str = ""
    stderr: str = ""
    # Facts the primitive set via ``set_stats`` (data=..., aggregate=no) — harvested
    # from the run events. Used to return parsed values (e.g. pct config output).
    facts: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and self.rc == 0


def _harvest(runner: Any) -> PrimitiveResult:
    facts: Dict[str, Any] = {}
    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    changed = False
    failed = False
    events: List[Dict[str, Any]] = []

    for event in runner.events:
        events.append(event)
        data = event.get("event_data", {}) or {}
        res = data.get("res", {}) or {}
        if event.get("event") == "runner_on_ok" and isinstance(res, dict):
            changed = changed or bool(res.get("changed", False))
            # ansible.builtin.set_stats surfaces under res['ansible_stats']['data'].
            stats = res.get("ansible_stats")
            if isinstance(stats, dict) and isinstance(stats.get("data"), dict):
                facts.update(stats["data"])
            if isinstance(res.get("stdout"), str):
                stdout_chunks.append(res["stdout"])
        if event.get("event") in ("runner_on_failed", "runner_on_unreachable"):
            failed = True
            if isinstance(res.get("stderr"), str):
                stderr_chunks.append(res["stderr"])
            if isinstance(res.get("msg"), str):
                stderr_chunks.append(res["msg"])

    rc = getattr(runner, "rc", 1)
    status = getattr(runner, "status", "")
    failed = failed or status == "failed" or rc != 0
    return PrimitiveResult(
        rc=rc,
        changed=changed,
        failed=failed,
        stdout="\n".join(stdout_chunks),
        stderr="\n".join(stderr_chunks),
        facts=facts,
        events=events,
    )


def invoke_primitive(
    primitive: str,
    *,
    inventory: str = "hosts.ini",
    host_pattern: Optional[str] = None,
    extravars: Optional[Dict[str, Any]] = None,
    private_data_dir: Optional[str] = None,
    project_dir: Optional[str] = None,
    check: bool = False,
    quiet: bool = True,
) -> PrimitiveResult:
    """Run ``ansible/primitives/<primitive>.yml`` and return a typed result.

    ``extravars`` flows in as ``-e`` vars (target host, params). ``host_pattern``
    limits the play to one host/group.

    ``project_dir`` sets the CWD for the ansible-playbook subprocess so that
    the relative path ``ansible/primitives/<primitive>.yml`` resolves correctly.
    Defaults to the current working directory (callers must ensure CWD is the
    project root — the fleet-update CLI and mol_run_flow.py both guarantee this).
    """
    import ansible_runner  # lazy: only needed when actually executing

    evars = dict(extravars or {})
    if host_pattern is not None:
        evars.setdefault("target_hosts", host_pattern)
    cmdline = "--check" if check else None

    runner = ansible_runner.run(
        playbook=f"ansible/primitives/{primitive}.yml",
        inventory=inventory,
        extravars=evars,
        private_data_dir=private_data_dir,
        project_dir=project_dir if project_dir is not None else os.getcwd(),
        cmdline=cmdline,
        quiet=quiet,
    )
    return _harvest(runner)
