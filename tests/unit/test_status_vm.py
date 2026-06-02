"""Parity tests for vm_status / vm_rescue_status — mirrors test_vm_report.py case-for-case.

test_vm_report.py tests the Jinja expression directly; this file tests the
Python function that replaces it. Both must agree on every case.
"""
import pytest

from proxmox_fleet.status import vm_rescue_status, vm_should_report, vm_status


# ---------------------------------------------------------------------------
# vm_status — mirrors VM_STATUS tree in test_vm_report.py
# ---------------------------------------------------------------------------


def test_status_failed():
    assert vm_status(failed=True) == "FAILED"


def test_status_updated_and_rebooted():
    assert vm_status(pkg_changed=True, rebooted=True) == "UPDATED & REBOOTED"


def test_status_updated():
    assert vm_status(pkg_changed=True) == "UPDATED"


def test_status_ok():
    assert vm_status() == "OK"


def test_status_dry_run_would_update():
    assert vm_status(dry_run=True, pkg_changed=True) == "WOULD UPDATE"


def test_status_dry_run_ok():
    assert vm_status(dry_run=True) == "OK"


def test_status_failed_beats_dry_run():
    assert vm_status(dry_run=True, failed=True) == "FAILED"


def test_status_failed_beats_rebooted():
    assert vm_status(pkg_changed=True, rebooted=True, failed=True) == "FAILED"


# ---------------------------------------------------------------------------
# vm_rescue_status — mirrors VM_RESCUE_STATUS tree in test_vm_report.py
# ---------------------------------------------------------------------------


def test_rescue_rolled_back():
    assert vm_rescue_status(rollback_done=True) == "FAILED + ROLLED BACK"


def test_rescue_no_snapshot():
    assert vm_rescue_status(snapshot_failed=True) == "FAILED (NO SNAPSHOT)"


def test_rescue_plain_failed():
    assert vm_rescue_status() == "FAILED"


def test_rescue_rollback_done_beats_no_snapshot():
    # If rollback happened, a snapshot existed — ROLLED BACK is the accurate description.
    assert vm_rescue_status(rollback_done=True, snapshot_failed=True) == "FAILED + ROLLED BACK"


# ---------------------------------------------------------------------------
# vm_should_report — mirrors report.yml `when:` gate
# ---------------------------------------------------------------------------


def test_should_report_on_changed():
    assert vm_should_report(pkg_changed=True, rebooted=False, failed=False) is True


def test_should_report_on_rebooted():
    assert vm_should_report(pkg_changed=False, rebooted=True, failed=False) is True


def test_should_report_on_failed():
    assert vm_should_report(pkg_changed=False, rebooted=False, failed=True) is True


def test_no_report_idle():
    assert vm_should_report(pkg_changed=False, rebooted=False, failed=False) is False
