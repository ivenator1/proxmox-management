"""Plain-Python tests for the custom_update decision logic in
proxmox_fleet.status / proxmox_fleet.changes.

These mirror tests/unit/test_custom_report.py case-for-case to lock byte-parity
with the Jinja `tmp_custom` / `custom_changed` / `custom_is_outdated` expressions.
"""
from proxmox_fleet.changes import custom_changed, is_outdated, normalize_version
from proxmox_fleet.status import custom_should_report, custom_status

# --- custom_status (tmp_custom) -----------------------------------------------


def test_dry_run_with_versions():
    assert custom_status(dry_run=True, ver_before="1.0", latest_ver="1.1") == "dry-run: 1.0 → 1.1"


def test_dry_run_unknown_latest():
    assert custom_status(dry_run=True, ver_before="1.0") == "dry-run: 1.0 → unknown"


def test_changed_when_always():
    assert custom_status(changed_when_type="always") == "Updated"


def test_changed_when_always_with_reboot():
    assert custom_status(changed_when_type="always", reboot_done=True) == "Updated + Rebooted"


def test_changed_when_command_exit_zero():
    assert custom_status(changed_when_type="command", changed_cmd_rc=0) == "Updated"


def test_changed_when_command_exit_nonzero():
    assert custom_status(changed_when_type="command", changed_cmd_rc=1) == "OK"


def test_changed_when_command_with_reboot():
    assert custom_status(changed_when_type="command", changed_cmd_rc=0, reboot_done=True) == "Updated + Rebooted"


def test_version_changes():
    assert custom_status(ver_before="1.0", ver_after="1.1") == "Updated: 1.0 → 1.1"


def test_version_unchanged():
    assert custom_status(ver_before="1.0", ver_after="1.0") == "OK"


def test_version_changed_with_reboot():
    assert custom_status(ver_before="1.0", ver_after="1.1", reboot_done=True) == "Updated: 1.0 → 1.1 + Rebooted"


def test_no_version_data_fallback():
    assert custom_status() == "Updated"


def test_no_version_data_fallback_with_reboot():
    assert custom_status(reboot_done=True) == "Updated + Rebooted"


def test_up_to_date_when_outdated_gate_and_current():
    assert custom_status(update_only_if_outdated=True, is_outdated=False) == "OK (up to date)"


def test_outdated_gate_but_outdated_still_updates():
    assert custom_status(
        update_only_if_outdated=True, is_outdated=True, ver_before="1.0", ver_after="1.1"
    ) == "Updated: 1.0 → 1.1"


# --- custom_should_report (the when: gate) ------------------------------------


def test_should_report_on_dry_run():
    assert custom_should_report("dry-run: 1.0 → 1.1", dry_run=True) is True


def test_should_report_on_updated():
    assert custom_should_report("Updated: 1.0 → 1.1", dry_run=False) is True


def test_should_report_on_failed():
    assert custom_should_report("FAILED", dry_run=False) is True


def test_should_not_report_on_idle_ok():
    assert custom_should_report("OK", dry_run=False) is False
    assert custom_should_report("OK (up to date)", dry_run=False) is False


# --- custom_changed -----------------------------------------------------------


def test_changed_always_is_true():
    assert custom_changed(changed_when_type="always") is True


def test_changed_command_exit_zero_is_true():
    assert custom_changed(changed_when_type="command", changed_cmd_rc=0) is True


def test_changed_command_exit_nonzero_is_false():
    assert custom_changed(changed_when_type="command", changed_cmd_rc=3) is False


def test_changed_version_differs_is_true():
    assert custom_changed(ver_before="1.0", ver_after="1.1") is True


def test_changed_version_same_is_false():
    assert custom_changed(ver_before="1.0", ver_after="1.0") is False


def test_changed_no_version_data_defaults_true():
    assert custom_changed() is True


def test_changed_command_missing_result_falls_back_to_version():
    # type=command but no result yet → version logic → no versions → True (fallback)
    assert custom_changed(changed_when_type="command") is True


# --- is_outdated / normalize_version ------------------------------------------


def test_outdated_true_when_versions_differ():
    assert is_outdated("1.0", "1.1") is True


def test_outdated_false_when_equal():
    assert is_outdated("1.1", "1.1") is False


def test_outdated_false_when_equal_v_prefix_stripped():
    assert is_outdated("v1.1", "1.1") is False


def test_outdated_fail_open_when_latest_unresolved():
    assert is_outdated("1.0", "") is True


def test_normalize_version():
    assert normalize_version("v1.2.3") == "1.2.3"
    assert normalize_version("  1.2.3  ") == "1.2.3"
