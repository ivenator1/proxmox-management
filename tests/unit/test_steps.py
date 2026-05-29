"""Tests for proxmox_fleet.steps — per-step execution + the eager-templating fix."""
import pytest

from proxmox_fleet.models.config import UpdateStep
from proxmox_fleet.runner import PrimitiveResult
from proxmox_fleet.steps import (
    StepError,
    evaluate_when,
    interpolate_command,
    run_steps,
    wrap_timeout,
)


def _ok(stdout="", changed=True, rc=0):
    return PrimitiveResult(rc=rc, changed=changed, stdout=stdout, failed=False)


def _fail(rc=1, stderr="boom"):
    return PrimitiveResult(rc=rc, failed=True, stderr=stderr)


class RecordingExecutor:
    """Records run_shell calls; returns queued results matched by command substring."""

    host = "testhost"

    def __init__(self, script=None, default=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands = []
        self.opts = []

    def _resp(self, command):
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command, **opts):
        self.commands.append(command)
        self.opts.append(opts)
        return self._resp(command)

    def run_local(self, command):
        self.commands.append(command)
        return self._resp(command)

    def reboot(self, *, timeout=600):
        return _ok()


# --- interpolation -------------------------------------------------------------

def test_interpolate_resolves_steps_ref():
    assert interpolate_command("install {{ steps.ver }}", {"ver": "1.2.3"}) == "install 1.2.3"


def test_interpolate_bracket_syntax():
    assert interpolate_command("x {{ steps['ver'] }} y", {"ver": "9"}) == "x 9 y"


def test_interpolate_leaves_other_jinja_untouched():
    # vault/host vars must survive for Ansible to template at execution time.
    out = interpolate_command("deploy --token {{ vault_token }} {{ steps.v }}", {"v": "2"})
    assert out == "deploy --token {{ vault_token }} 2"


def test_interpolate_unknown_step_raises():
    with pytest.raises(KeyError):
        interpolate_command("use {{ steps.missing }}", {})


def test_wrap_timeout():
    assert wrap_timeout("long-task", 120) == "timeout 120s long-task"
    assert wrap_timeout("bare", None) == "bare"


# --- when ----------------------------------------------------------------------

def test_evaluate_when_none_and_bool():
    assert evaluate_when(None, {}) is True
    assert evaluate_when(True, {}) is True
    assert evaluate_when(False, {}) is False


def test_evaluate_when_expression():
    assert evaluate_when("steps.ver == '1.1'", {"steps": {"ver": "1.1"}}) is True
    assert evaluate_when("steps.ver == '1.1'", {"steps": {"ver": "1.0"}}) is False


def test_evaluate_when_undefined_is_false():
    assert evaluate_when("ansible_facts.os_family == 'Debian'", {}) is False


def test_evaluate_when_list_anded():
    assert evaluate_when(["1 == 1", "2 == 2"], {}) is True
    assert evaluate_when(["1 == 1", "2 == 3"], {}) is False


# --- run_steps -----------------------------------------------------------------

def test_run_steps_registers_and_interpolates():
    """THE eager-templating fix: a later step consumes an earlier step's stdout."""
    ex = RecordingExecutor(script={"get-version": [_ok(stdout="4.5.6")]})
    steps = [
        UpdateStep(name="read", command="get-version", register="ver"),
        UpdateStep(name="install", command="install {{ steps.ver }}"),
    ]
    results = run_steps(steps, ex)
    assert results["ver"] == "4.5.6"
    assert ex.commands == ["get-version", "install 4.5.6"]


def test_run_steps_skips_on_when_false():
    ex = RecordingExecutor()
    steps = [
        UpdateStep(name="reg", command="echo hi", register="x"),
        UpdateStep(name="cond", command="should-skip", when="steps.x == 'nope'"),
    ]
    run_steps(steps, ex)
    assert "should-skip" not in ex.commands


def test_run_steps_raises_step_error_on_failure():
    ex = RecordingExecutor(script={"bad": [_fail()]})
    steps = [UpdateStep(name="bad-step", command="bad")]
    with pytest.raises(StepError) as exc:
        run_steps(steps, ex)
    assert exc.value.step_name == "bad-step"


def test_run_steps_ignore_errors_continues():
    ex = RecordingExecutor(script={"bad": [_fail()]})
    steps = [
        UpdateStep(name="bad", command="bad", ignore_errors=True),
        UpdateStep(name="next", command="ok"),
    ]
    run_steps(steps, ex)  # no raise
    assert "ok" in ex.commands


def test_run_steps_retries_until_success():
    ex = RecordingExecutor(script={"flaky": [_fail(), _ok(stdout="done")]})
    steps = [UpdateStep(name="flaky", command="flaky", retries=2, delay=0, register="r")]
    results = run_steps(steps, ex)
    assert results["r"] == "done"
    assert ex.commands.count("flaky") == 2


def test_run_steps_wraps_timeout():
    ex = RecordingExecutor()
    run_steps([UpdateStep(name="s", command="task", timeout=30)], ex)
    assert ex.commands == ["timeout 30s task"]
