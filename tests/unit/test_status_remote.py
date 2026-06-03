"""Parity tests for remote_status — derived from roles/remote_host_update/tasks/report.yml.

No existing Jinja shim exists for remote status; these tests are derived directly
from the YAML source and lock the Python implementation.
"""

from proxmox_fleet.status import remote_should_report, remote_status


# ---------------------------------------------------------------------------
# remote_status — same 5-branch tree as vm_status (no snapshot field)
# ---------------------------------------------------------------------------


def test_status_failed():
    assert remote_status(failed=True) == "FAILED"


def test_status_updated_and_rebooted():
    assert remote_status(pkg_changed=True, rebooted=True) == "UPDATED & REBOOTED"


def test_status_updated():
    assert remote_status(pkg_changed=True) == "UPDATED"


def test_status_ok():
    assert remote_status() == "OK"


def test_status_dry_run_would_update():
    assert remote_status(dry_run=True, pkg_changed=True) == "WOULD UPDATE"


def test_status_dry_run_ok():
    assert remote_status(dry_run=True) == "OK"


def test_status_failed_beats_dry_run():
    assert remote_status(dry_run=True, failed=True) == "FAILED"


def test_status_failed_beats_rebooted():
    assert remote_status(pkg_changed=True, rebooted=True, failed=True) == "FAILED"


# ---------------------------------------------------------------------------
# remote_should_report — mirrors report.yml `when:` gate
# ---------------------------------------------------------------------------


def test_should_report_on_changed():
    assert remote_should_report(pkg_changed=True, rebooted=False, failed=False) is True


def test_should_report_on_rebooted():
    assert remote_should_report(pkg_changed=False, rebooted=True, failed=False) is True


def test_should_report_on_failed():
    assert remote_should_report(pkg_changed=False, rebooted=False, failed=True) is True


def test_no_report_idle():
    assert remote_should_report(pkg_changed=False, rebooted=False, failed=False) is False
