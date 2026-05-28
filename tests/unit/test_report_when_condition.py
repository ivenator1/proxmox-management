"""
Unit tests for the `when:` gate on the "Append LXC record" task in
roles/lxc_update/tasks/report.yml (lines 57-60).

Idle containers (tmp_app='OK', tmp_os='OK', lxc_dry_run=False) must NOT
produce a fleet record — they are silently filtered out.
"""
from tests.conftest import render

# Mirrors the `when:` condition exactly (> YAML block folds newlines to spaces)
WHEN_CONDITION = (
    "{% if lxc_dry_run | bool or "
    "(tmp_app | trim | lower) is search('updated|failed|no script|never started') or "
    "(tmp_os | trim | lower) is search('updated|failed') %}"
    "True{% else %}False{% endif %}"
)


def when(tmp_app='OK', tmp_os='OK', lxc_dry_run=False):
    result = render(WHEN_CONDITION, tmp_app=tmp_app, tmp_os=tmp_os, lxc_dry_run=lxc_dry_run)
    return result == 'True'


def test_both_ok_not_dry_run_is_suppressed():
    assert when('OK', 'OK', False) is False


def test_both_ok_dry_run_is_included():
    assert when('OK', 'OK', True) is True


def test_app_updated_is_included():
    assert when('Updated: v4.0 → v4.1', 'OK') is True


def test_app_updated_lower_is_included():
    assert when('UPDATED', 'OK') is True


def test_app_failed_is_included():
    assert when('FAILED', 'OK') is True


def test_app_no_script_is_included():
    assert when('NO SCRIPT', 'OK') is True


def test_app_never_started_is_included():
    assert when('NEVER STARTED', 'OK') is True


def test_os_failed_is_included():
    assert when('OK', 'FAILED') is True


def test_os_updated_is_included():
    assert when('OK', 'Updated (2 upgraded)') is True


def test_os_skipped_is_suppressed():
    # SKIPPED does not match 'updated|failed' — skipped containers are idle
    assert when('OK', 'SKIPPED') is False
