"""
Unit test for the per-step command construction in roles/custom_update/tasks/run_step.yml
(the optional `timeout` wrapper).
"""
from tests.conftest import render

STEP_CMD = "{{ ('timeout ' ~ step.timeout ~ 's ' if step.timeout is defined else '') ~ step.command }}"


def test_no_timeout_runs_bare_command():
    assert render(STEP_CMD, step={'name': 'x', 'command': 'systemctl restart app'}) == 'systemctl restart app'


def test_timeout_wraps_command():
    assert render(STEP_CMD, step={'name': 'x', 'command': 'long-task', 'timeout': 120}) == 'timeout 120s long-task'
