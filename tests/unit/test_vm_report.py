"""
Unit tests for the VM status expressions:
- the success-path status in roles/vm_update/tasks/report.yml
- the rescue-path status (incl. rollback / no-snapshot) in roles/vm_update/tasks/main.yml
"""
import pytest
from tests.conftest import render, make_result

# Success-path status from report.yml
VM_STATUS = """\
{%- if vm_pkg_res is failed -%}FAILED
{%- elif vm_dry_run | default(false) -%}{% if vm_pkg_res.changed | default(false) %}WOULD UPDATE{% else %}OK{% endif %}
{%- elif vm_reboot_action is defined and vm_reboot_action.changed -%}UPDATED & REBOOTED
{%- elif vm_pkg_res.changed | default(false) -%}UPDATED
{%- else -%}OK{%- endif -%}"""

# Rescue-path status from main.yml
VM_RESCUE_STATUS = (
    "{{ 'FAILED + ROLLED BACK' if (vm_rollback_done | default(false)) "
    "else ('FAILED (NO SNAPSHOT)' if (vm_snapshot_failed | default(false)) else 'FAILED') }}"
)


def s(**ctx):
    return render(VM_STATUS, **ctx)


def test_status_failed():
    assert s(vm_pkg_res=make_result(failed=True)) == 'FAILED'


def test_status_updated_and_rebooted():
    assert s(vm_pkg_res=make_result(changed=True),
             vm_reboot_action=make_result(changed=True)) == 'UPDATED & REBOOTED'


def test_status_updated():
    assert s(vm_pkg_res=make_result(changed=True)) == 'UPDATED'


def test_status_ok():
    assert s(vm_pkg_res=make_result(changed=False)) == 'OK'


def test_status_dry_run_would_update():
    assert s(vm_dry_run=True, vm_pkg_res=make_result(changed=True)) == 'WOULD UPDATE'


def test_status_dry_run_ok():
    assert s(vm_dry_run=True, vm_pkg_res=make_result(changed=False)) == 'OK'


def test_status_failed_beats_dry_run():
    assert s(vm_dry_run=True, vm_pkg_res=make_result(failed=True)) == 'FAILED'


# --- rescue status ---

def test_rescue_rolled_back():
    assert render(VM_RESCUE_STATUS, vm_rollback_done=True) == 'FAILED + ROLLED BACK'


def test_rescue_no_snapshot():
    assert render(VM_RESCUE_STATUS, vm_rollback_done=False, vm_snapshot_failed=True) == 'FAILED (NO SNAPSHOT)'


def test_rescue_plain_failed():
    assert render(VM_RESCUE_STATUS, vm_rollback_done=False, vm_snapshot_failed=False) == 'FAILED'


def test_rescue_default():
    assert render(VM_RESCUE_STATUS) == 'FAILED'
