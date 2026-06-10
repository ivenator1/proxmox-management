"""
Plain-Python parity tests for the Phase-3 LXC status trees and parsing helpers.

Each test section mirrors its Jinja counterpart case-for-case:
  lxc_app_status()       ← test_report_tmp_app.py   (18 + 5 rescue cases)
  lxc_os_status()        ← test_report_tmp_os.py    (13 cases)
  lxc_should_report()    ← test_report_when_condition.py (10 cases)
  lxc_dry_run_status()   ← test_dry_check_status.py  (9 cases)
  parse_ct_script()      ← test_detect_regex.py      (11 + 6 scale cases)
  parse_pct_config()     ← test_introspect_regex.py  (6 cases)
  parse_pct_status()     ← test_introspect_regex.py  (4 cases)
  lxc_os_changed()       ← lxc_update/tasks/update.yml changed_when
  lxc_os_pkg_count()     ← regex_search('(\\d+ upgraded)') in report.yml
  dpkg_hash_differs()    ← dpkg hash comparison in update.yml
"""


from proxmox_fleet.changes import dpkg_hash_differs, lxc_os_changed, lxc_os_pkg_count
from proxmox_fleet.lxc_parse import parse_ct_script, parse_pct_config, parse_pct_status
from proxmox_fleet.status import (
    lxc_app_did_update,
    lxc_app_status,
    lxc_dry_run_status,
    lxc_os_status,
    lxc_rescue_app_status,
    lxc_should_report,
)

# ---------------------------------------------------------------------------
# lxc_app_status — mirrors test_report_tmp_app.py
# ---------------------------------------------------------------------------


def test_template_container():
    assert lxc_app_status(is_template=True) == "TEMPLATE"


def test_never_started():
    assert lxc_app_status(is_running=False) == "NEVER STARTED"


def test_dry_run_with_status():
    assert lxc_app_status(dry_run=True, dry_run_status="v1.9 → v2.0") == "v1.9 → v2.0"


def test_dry_run_default_fallback():
    assert lxc_app_status(dry_run=True) == "dry-run"


def test_no_update_script():
    assert lxc_app_status(no_update_script=True) == "NO SCRIPT"


def test_app_failed():
    assert lxc_app_status(app_failed=True) == "FAILED"


def test_app_excluded_from_app_update():
    assert lxc_app_status(excluded=True) == "SKIPPED"


def test_app_excluded_id_no_match():
    assert lxc_app_status(excluded=False) == "OK"


def test_no_update_script_wins_over_excluded():
    # Nothing to skip when the container has no app-update script at all.
    assert lxc_app_status(no_update_script=True, excluded=True) == "NO SCRIPT"


def test_excluded_wins_over_app_failed():
    # The step never ran, so it can't have failed.
    assert lxc_app_status(excluded=True, app_failed=True) == "SKIPPED"


def test_version_updated():
    assert (
        lxc_app_status(app_changed=True, ver_before="v4.0", ver_after="v4.1")
        == "Updated: v4.0 → v4.1"
    )


def test_version_v_prefix_stripped_equal():
    # ver_before='v4.0', ver_after='4.0' — stripping 'v' makes them equal → OK
    assert lxc_app_status(app_changed=True, ver_before="v4.0", ver_after="4.0") == "OK"


def test_dpkg_hash_differs():
    assert (
        lxc_app_status(app_changed=True, dpkg_before="abc123", dpkg_after="def456")
        == "UPDATED"
    )


def test_dpkg_hash_matches():
    assert (
        lxc_app_status(app_changed=True, dpkg_before="abc123", dpkg_after="abc123") == "OK"
    )


def test_dpkg_hash_with_whitespace():
    # Hashes may come back with surrounding whitespace; strip normalises them.
    assert (
        lxc_app_status(app_changed=True, dpkg_before="  abc123  ", dpkg_after="abc123") == "OK"
    )


def test_no_hash_data_fallback():
    # app changed, no ver files, no dpkg hash → 'UPDATED'
    assert lxc_app_status(app_changed=True) == "UPDATED"


def test_app_not_changed():
    assert lxc_app_status(app_changed=False) == "OK"


# ---------------------------------------------------------------------------
# lxc_rescue_app_status — mirrors RESCUE_APP_EXPR in test_report_tmp_app.py
# ---------------------------------------------------------------------------


def test_rollback_performed():
    assert lxc_rescue_app_status(rollback_done=True) == "FAILED + ROLLED BACK"


def test_rollback_performed_wins_over_snapshot_failed():
    assert (
        lxc_rescue_app_status(rollback_done=True, snapshot_failed=True)
        == "FAILED + ROLLED BACK"
    )


def test_snapshot_failed_no_rollback():
    assert lxc_rescue_app_status(rollback_done=False, snapshot_failed=True) == "FAILED (NO SNAPSHOT)"


def test_failed_plain_when_snapshot_ok_but_no_rollback():
    assert lxc_rescue_app_status(rollback_done=False, snapshot_failed=False) == "FAILED"


def test_rescue_app_default():
    assert lxc_rescue_app_status() == "FAILED"


# ---------------------------------------------------------------------------
# lxc_os_status — mirrors test_report_tmp_os.py
# ---------------------------------------------------------------------------


def test_os_template_container():
    assert lxc_os_status(is_template=True) == ""


def test_os_not_running():
    assert lxc_os_status(is_running=False) == ""


def test_os_dry_run():
    assert lxc_os_status(dry_run=True) == ""


def test_os_excluded_from_os_update():
    assert lxc_os_status(excluded=True) == "SKIPPED"


def test_os_excluded_id_no_match():
    assert lxc_os_status(excluded=False) == "OK"


def test_os_failed():
    assert lxc_os_status(os_failed=True) == "FAILED"


def test_os_updated_with_reboot_and_pkg_count():
    assert lxc_os_status(os_changed=True, pkg_count=2, reboot_done=True) == "Updated (2 upgraded) & Rebooted"


def test_os_updated_with_reboot_no_pkg_count():
    assert lxc_os_status(os_changed=True, pkg_count=None, reboot_done=True) == "Updated & Rebooted"


def test_os_updated_no_reboot_with_pkg_count():
    assert lxc_os_status(os_changed=True, pkg_count=3) == "Updated (3 upgraded)"


def test_os_updated_no_reboot_no_pkg_count():
    assert lxc_os_status(os_changed=True, pkg_count=None) == "Updated"


def test_os_all_ok():
    assert lxc_os_status() == "OK"


def test_os_none_guard_empty_string():
    # lxc_os_status() returns '' for template/stopped/dry-run
    # (the Jinja empty branch stores None; the None-guard converts it to '')
    assert lxc_os_status(is_template=True) == ""


def test_os_none_guard_real_value():
    assert lxc_os_status(excluded=True) == "SKIPPED"


# ---------------------------------------------------------------------------
# lxc_should_report — mirrors test_report_when_condition.py
# ---------------------------------------------------------------------------


def test_report_both_ok_not_dry_run_is_suppressed():
    assert lxc_should_report("OK", "OK", dry_run=False) is False


def test_report_both_ok_dry_run_is_included():
    assert lxc_should_report("OK", "OK", dry_run=True) is True


def test_report_app_updated_is_included():
    assert lxc_should_report("Updated: v4.0 → v4.1", "OK", dry_run=False) is True


def test_report_app_updated_lower_is_included():
    assert lxc_should_report("UPDATED", "OK", dry_run=False) is True


def test_report_app_failed_is_included():
    assert lxc_should_report("FAILED", "OK", dry_run=False) is True


def test_report_app_no_script_expected_missing_is_suppressed():
    # os_only_lxc_list container (script_expected=False): idle NO SCRIPT is noise.
    assert lxc_should_report("NO SCRIPT", "OK", dry_run=False, script_expected=False) is False


def test_report_app_no_script_unexpected_is_included():
    # A tagged community-script container missing its script is an anomaly —
    # surfaced even when the OS is idle (script_expected defaults to True).
    assert lxc_should_report("NO SCRIPT", "OK", dry_run=False) is True


def test_report_app_no_script_os_updated_is_included():
    assert lxc_should_report("NO SCRIPT", "Updated (3 upgraded)", dry_run=False,
                             script_expected=False) is True


def test_report_app_never_started_is_included():
    assert lxc_should_report("NEVER STARTED", "OK", dry_run=False) is True


def test_report_os_failed_is_included():
    assert lxc_should_report("OK", "FAILED", dry_run=False) is True


def test_report_os_updated_is_included():
    assert lxc_should_report("OK", "Updated (2 upgraded)", dry_run=False) is True


def test_report_os_skipped_is_suppressed():
    # SKIPPED does not match 'updated|failed' — idle containers suppressed
    assert lxc_should_report("OK", "SKIPPED", dry_run=False) is False


# ---------------------------------------------------------------------------
# lxc_dry_run_status — mirrors test_dry_check_status.py
# ---------------------------------------------------------------------------


def test_dry_no_gh_repo():
    assert lxc_dry_run_status(gh_repo="") == "no ver info"


def test_dry_gh_fetch_failed_404():
    assert lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=False) == "fetch failed"


def test_dry_gh_fetch_failed_connection_error():
    assert lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=False, latest_tag="") == "fetch failed"


def test_dry_installed_version_empty():
    assert (
        lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=True, installed_ver="", latest_tag="v2.0")
        == "unknown (latest: v2.0)"
    )


def test_dry_installed_version_empty_unknown_tag():
    assert (
        lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=True, installed_ver="", latest_tag="")
        == "unknown (latest: ?)"
    )


def test_dry_up_to_date():
    assert (
        lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=True, installed_ver="v2.0", latest_tag="v2.0")
        == "v2.0"
    )


def test_dry_up_to_date_v_prefix_stripped():
    # installed='v2.0', latest='2.0' — stripping 'v' makes them equal
    assert (
        lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=True, installed_ver="v2.0", latest_tag="2.0")
        == "v2.0"
    )


def test_dry_update_available():
    assert (
        lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=True, installed_ver="v1.9", latest_tag="v2.0")
        == "v1.9 → v2.0"
    )


def test_dry_update_available_with_whitespace_in_installed():
    assert (
        lxc_dry_run_status(gh_repo="owner/repo", fetch_ok=True, installed_ver=" v1.9 ", latest_tag="v2.0")
        == "v1.9 → v2.0"
    )


# ---------------------------------------------------------------------------
# parse_ct_script — mirrors test_detect_regex.py (resource extraction)
# ---------------------------------------------------------------------------

FULL_SCRIPT = """\
#!/usr/bin/env bash
APP="sonarr"
var_cpu="4"
var_ram="2048"
# other stuff
check_for_gh_release "Sonarr/Sonarr" "Sonarr/Sonarr"
pct set $CTID -cores 2
pct set $CTID -memory 1024
"""

NO_RESOURCE_SCRIPT = 'APP="app"\n'

PARTIAL_RESOURCE_SCRIPT = 'var_cpu="2"\n# no pct set lines\n'

NO_GH_RELEASE_SCRIPT = 'var_cpu="2"\npct set $CTID -cores 1\n'


def test_var_cpu_full_script():
    assert parse_ct_script(FULL_SCRIPT)["build_cpu"] == "4"


def test_var_cpu_no_resource_script():
    assert parse_ct_script(NO_RESOURCE_SCRIPT)["build_cpu"] == ""


def test_var_cpu_partial_script():
    assert parse_ct_script(PARTIAL_RESOURCE_SCRIPT)["build_cpu"] == "2"


def test_var_ram_full_script():
    assert parse_ct_script(FULL_SCRIPT)["build_ram"] == "2048"


def test_var_ram_no_resource_script():
    assert parse_ct_script(NO_RESOURCE_SCRIPT)["build_ram"] == ""


def test_run_cpu_full_script():
    assert parse_ct_script(FULL_SCRIPT)["run_cpu"] == "2"


def test_run_cpu_partial_script():
    assert parse_ct_script(PARTIAL_RESOURCE_SCRIPT)["run_cpu"] == ""


def test_run_ram_full_script():
    assert parse_ct_script(FULL_SCRIPT)["run_ram"] == "1024"


def test_run_ram_no_resource_script():
    assert parse_ct_script(NO_RESOURCE_SCRIPT)["run_ram"] == ""


def test_gh_repo_extraction():
    assert parse_ct_script(FULL_SCRIPT)["gh_repo"] == "Sonarr/Sonarr"


def test_gh_repo_absent():
    assert parse_ct_script(NO_GH_RELEASE_SCRIPT)["gh_repo"] == ""


# needs_resource_scale — mirrors NEEDS_SCALE in test_detect_regex.py


def test_needs_scale_build_greater_than_run():
    assert parse_ct_script("var_cpu=\"4\"\npct set $CTID -cores 2\n")["needs_resource_scale"] is True


def test_needs_scale_build_equal_to_run():
    assert parse_ct_script("var_cpu=\"2\"\npct set $CTID -cores 2\n")["needs_resource_scale"] is False


def test_needs_scale_build_less_than_run():
    assert parse_ct_script("var_cpu=\"2\"\npct set $CTID -cores 4\n")["needs_resource_scale"] is False


def test_needs_scale_empty_build_cpu():
    assert parse_ct_script("pct set $CTID -cores 2\n")["needs_resource_scale"] is False


def test_needs_scale_empty_run_cpu():
    assert parse_ct_script("var_cpu=\"4\"\n")["needs_resource_scale"] is False


def test_needs_scale_both_empty():
    assert parse_ct_script("")["needs_resource_scale"] is False


# ---------------------------------------------------------------------------
# parse_pct_config / parse_pct_status — mirrors test_introspect_regex.py
# ---------------------------------------------------------------------------

SAMPLE_CONFIG = """\
arch: amd64
cores: 2
hostname: sonarr
memory: 2048
ostype: ubuntu
rootfs: local:101/vm-101-disk-0.raw,size=10G
tags: community-script
"""

TEMPLATE_CONFIG = SAMPLE_CONFIG + "template: 1\n"

CONFIG_NO_HOSTNAME = """\
arch: amd64
ostype: debian
cores: 1
"""

CONFIG_NO_OSTYPE = """\
arch: amd64
hostname: myapp
cores: 1
"""

STATUS_RUNNING = "status: running"
STATUS_STOPPED = "status: stopped"


def test_lxc_name_extracted():
    assert parse_pct_config(SAMPLE_CONFIG)["name"] == "sonarr"


def test_lxc_name_missing_falls_back_to_unknown():
    assert parse_pct_config(CONFIG_NO_HOSTNAME)["name"] == "Unknown"


def test_lxc_os_extracted():
    assert parse_pct_config(SAMPLE_CONFIG)["os_type"] == "ubuntu"


def test_lxc_os_missing_falls_back_to_debian():
    assert parse_pct_config(CONFIG_NO_OSTYPE)["os_type"] == "debian"


def test_is_template_false():
    assert parse_pct_config(SAMPLE_CONFIG)["is_template"] is False


def test_is_template_true():
    assert parse_pct_config(TEMPLATE_CONFIG)["is_template"] is True


def test_is_running_true():
    assert parse_pct_status(STATUS_RUNNING)["is_running"] is True


def test_is_running_false_when_stopped():
    assert parse_pct_status(STATUS_STOPPED)["is_running"] is False


def test_was_stopped_true():
    assert parse_pct_status(STATUS_STOPPED)["was_stopped"] is True


def test_was_stopped_false_when_running():
    assert parse_pct_status(STATUS_RUNNING)["was_stopped"] is False


# ---------------------------------------------------------------------------
# lxc_os_changed / lxc_os_pkg_count — from update.yml changed_when logic
# ---------------------------------------------------------------------------


def test_os_changed_when_packages_upgraded():
    assert lxc_os_changed("2 upgraded, 0 newly installed, 0 to remove") is True


def test_os_not_changed_when_zero_upgraded():
    assert lxc_os_changed("0 upgraded, 0 newly installed, 0 to remove") is False


def test_os_not_changed_on_empty_output():
    assert lxc_os_changed("") is False


def test_os_changed_on_alpine_upgrade():
    assert lxc_os_changed("Upgrading busybox (1.35.0-r15 -> 1.36.1-r1)") is True


def test_pkg_count_extracted():
    assert lxc_os_pkg_count("3 upgraded, 0 newly installed") == 3


def test_pkg_count_none_on_no_match():
    assert lxc_os_pkg_count("OK, your system is up to date") is None


def test_pkg_count_none_on_empty():
    assert lxc_os_pkg_count("") is None


# ---------------------------------------------------------------------------
# dpkg_hash_differs
# ---------------------------------------------------------------------------


def test_dpkg_hash_differs_true():
    assert dpkg_hash_differs("abc123", "def456") is True


def test_dpkg_hash_differs_false_same():
    assert dpkg_hash_differs("abc123", "abc123") is False


def test_dpkg_hash_differs_false_with_whitespace():
    assert dpkg_hash_differs("  abc123  ", "abc123") is False


def test_dpkg_hash_differs_false_empty_before():
    assert dpkg_hash_differs("", "abc123") is False


def test_dpkg_hash_differs_false_empty_after():
    assert dpkg_hash_differs("abc123", "") is False


def test_dpkg_hash_differs_false_both_empty():
    assert dpkg_hash_differs("", "") is False


# ---------------------------------------------------------------------------
# lxc_app_did_update — structured decision shared with the health-check gate
# ---------------------------------------------------------------------------


def test_did_update_version_differs():
    assert lxc_app_did_update(app_changed=True, ver_before="1.0", ver_after="1.1") is True


def test_did_update_version_same_v_prefix_stripped():
    assert lxc_app_did_update(app_changed=True, ver_before="v1.0", ver_after="1.0") is False


def test_did_update_dpkg_hash_differs():
    assert lxc_app_did_update(app_changed=True, dpkg_before="aaa", dpkg_after="bbb") is True


def test_did_update_dpkg_hash_same():
    assert lxc_app_did_update(app_changed=True, dpkg_before="aaa", dpkg_after="aaa") is False


def test_did_update_no_data_fallback_true():
    assert lxc_app_did_update(app_changed=True) is True


def test_did_update_false_when_not_changed_failed_excluded_or_no_script():
    assert lxc_app_did_update(app_changed=False) is False
    assert lxc_app_did_update(app_changed=True, app_failed=True) is False
    assert lxc_app_did_update(app_changed=True, excluded=True) is False
    assert lxc_app_did_update(app_changed=True, no_update_script=True) is False


def test_did_update_agrees_with_status_string():
    """lxc_app_status must say 'Updated'/'UPDATED' exactly when did_update is True
    (the flow's health-check gate relies on this equivalence)."""
    cases = [
        dict(ver_before="1.0", ver_after="1.1"),
        dict(ver_before="1.0", ver_after="1.0"),
        dict(dpkg_before="aaa", dpkg_after="bbb"),
        dict(dpkg_before="aaa", dpkg_after="aaa"),
        dict(),
    ]
    for kwargs in cases:
        status = lxc_app_status(app_changed=True, **kwargs)
        did = lxc_app_did_update(app_changed=True, **kwargs)
        assert ("updated" in status.lower()) == did, (kwargs, status, did)
