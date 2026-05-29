"""Run ``update_steps`` in Python — the decomposition of
roles/custom_update/tasks/run_step.yml + update.yml's loop.

THE EAGER-TEMPLATING FIX
------------------------
Ansible rendered step ``command`` strings eagerly (during validation/combine/loop
expansion), so a later step could not interpolate an earlier step's ``register``
output into its ``command`` — the value did not exist yet. Here we render in
**Python, at the moment we run each step, with the real prior results present**.
A command may reference an earlier step's stdout as ``{{ steps.NAME }}``; we
resolve those (and only those) before invoking the run_shell primitive.

Every *other* ``{{ ... }}`` token is left untouched so Ansible still resolves
host/vault vars when run_shell executes the command (e.g. ``{{ vault_token }}``).
Per-step ``when`` is evaluated here too (Jinja over the real context — not eager,
because the values exist by the time we evaluate).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Protocol

import jinja2

from proxmox_fleet.models.config import UpdateStep
from proxmox_fleet.runner import PrimitiveResult

# Matches {{ steps.NAME }} / {{ steps['NAME'] }} / {{ steps["NAME"] }} (with whitespace).
_STEPS_REF = re.compile(
    r"\{\{\s*steps(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*['\"]([^'\"]+)['\"]\s*\])\s*\}\}"
)


class StepExecutor(Protocol):
    """The subset of the Executor used by run_steps (the run_shell primitive)."""

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


class StepError(RuntimeError):
    """A step failed (non-zero rc and not ignore_errors). Triggers the flow's rescue."""

    def __init__(self, step_name: str, result: PrimitiveResult) -> None:
        super().__init__(f"step {step_name!r} failed (rc={result.rc})")
        self.step_name = step_name
        self.result = result


def interpolate_command(command: str, step_results: Dict[str, str]) -> str:
    """Resolve ``{{ steps.NAME }}`` references from prior step stdout. Leaves all
    other ``{{ ... }}`` untouched for Ansible to template at execution time."""

    def repl(match: "re.Match[str]") -> str:
        key = match.group(1) or match.group(2)
        if key not in step_results:
            raise KeyError(f"command references unknown prior step result: steps.{key}")
        return step_results[key]

    return _STEPS_REF.sub(repl, command)


def wrap_timeout(command: str, timeout: Optional[int]) -> str:
    """Prepend ``timeout Ns`` when a step sets a timeout (mirrors run_step.yml)."""
    return f"timeout {timeout}s {command}" if timeout else command


def evaluate_when(when: Any, context: Dict[str, Any]) -> bool:
    """Evaluate a per-step ``when``. None → run; bool → as-is; str/list → Jinja
    expression(s) ANDed together, rendered against the real context."""
    if when is None:
        return True
    if isinstance(when, bool):
        return when
    conditions = when if isinstance(when, (list, tuple)) else [when]
    env = jinja2.Environment(undefined=jinja2.Undefined)
    for cond in conditions:
        try:
            value = env.compile_expression(str(cond))(**context)
        except jinja2.UndefinedError:
            return False
        if not bool(value):
            return False
    return True


def run_steps(
    steps: List[UpdateStep],
    executor: StepExecutor,
    *,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Run each step in order. Returns the map of ``register`` name → trimmed stdout.

    Raises StepError on the first failing, non-ignored step (the flow's rescue
    block converts that into a FAILED record + rollback).
    """
    results: Dict[str, str] = {}
    extra = dict(context or {})

    for step in steps:
        if not evaluate_when(step.when, {"steps": results, **extra}):
            continue

        command = wrap_timeout(interpolate_command(step.command, results), step.timeout)

        attempts = max(step.retries, 1)  # Ansible 'retries' is total attempts (default 1)
        res: PrimitiveResult
        for attempt in range(attempts):
            res = executor.run_shell(
                command,
                become=step.become,
                chdir=step.chdir,
                environment=step.environment,
                changed_when=step.changed_when if step.changed_when is not None else True,
                ignore_errors=step.ignore_errors,
            )
            if res.ok or step.ignore_errors:
                break

        if not res.ok and not step.ignore_errors:
            raise StepError(step.name, res)

        if step.register_var:
            results[step.register_var] = res.stdout.strip()

    return results
