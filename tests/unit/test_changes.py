"""Tests for proxmox_fleet.changes — change-detection pure functions."""
from proxmox_fleet.changes import (
    custom_changed,
    dpkg_hash_differs,
    is_outdated,
    lxc_os_changed,
    lxc_os_pkg_count,
    normalize_version,
    pkg_changed,
    vm_pkg_count,
)


# --- normalize_version ---

def test_normalize_version_strips_leading_lowercase_v():
    assert normalize_version("v1.2.3") == "1.2.3"


def test_normalize_version_does_not_strip_uppercase_v():
    assert normalize_version("V1.2.3") == "V1.2.3"


def test_normalize_version_empty_string():
    assert normalize_version("") == ""


def test_normalize_version_none_like():
    assert normalize_version(None) == ""  # type: ignore[arg-type]


def test_normalize_version_strips_whitespace():
    assert normalize_version("  v2.0  ") == "2.0"


def test_normalize_version_no_v_prefix():
    assert normalize_version("1.2.3") == "1.2.3"


# --- is_outdated ---

def test_is_outdated_empty_latest_is_true():
    assert is_outdated("1.0", "") is True


def test_is_outdated_blank_latest_is_true():
    assert is_outdated("1.0", "   ") is True


def test_is_outdated_equal_versions_false():
    assert is_outdated("1.2.3", "1.2.3") is False


def test_is_outdated_v_prefix_normalised():
    assert is_outdated("v1.2.3", "1.2.3") is False


def test_is_outdated_different_versions_true():
    assert is_outdated("1.0", "1.1") is True


# --- lxc_os_changed ---

def test_lxc_os_changed_apt_noop_false():
    assert lxc_os_changed("0 upgraded, 0 newly installed, 0 to remove") is False


def test_lxc_os_changed_apt_with_upgrades():
    assert lxc_os_changed("3 upgraded, 0 newly installed") is True


def test_lxc_os_changed_empty_false():
    assert lxc_os_changed("") is False


def test_lxc_os_changed_apk_upgrading_keyword():
    assert lxc_os_changed("Upgrading musl (1.1.24-r3 -> 1.2.4-r2)") is True


def test_lxc_os_changed_dnf_upgrading_keyword():
    assert lxc_os_changed("Upgrading: bash.x86_64") is True


def test_lxc_os_changed_keyword_with_noop_phrase_false():
    # "Setting up" keyword present but apt noop phrase also present — noop wins
    assert lxc_os_changed("Setting up fake 0 upgraded, 0 newly installed") is False


def test_lxc_os_changed_setting_up_without_noop():
    assert lxc_os_changed("Setting up libssl (1.2.3)") is True


# --- lxc_os_pkg_count ---

def test_lxc_os_pkg_count_parses_n():
    assert lxc_os_pkg_count("3 upgraded, 0 newly installed") == 3


def test_lxc_os_pkg_count_zero():
    assert lxc_os_pkg_count("0 upgraded, 0 newly installed") == 0


def test_lxc_os_pkg_count_no_match_none():
    assert lxc_os_pkg_count("Upgrading musl") is None


def test_lxc_os_pkg_count_empty_none():
    assert lxc_os_pkg_count("") is None


# --- dpkg_hash_differs ---

def test_dpkg_hash_differs_when_different():
    assert dpkg_hash_differs("abc123", "def456") is True


def test_dpkg_hash_differs_when_equal():
    assert dpkg_hash_differs("abc123", "abc123") is False


def test_dpkg_hash_differs_empty_before_false():
    assert dpkg_hash_differs("", "def456") is False


def test_dpkg_hash_differs_empty_after_false():
    assert dpkg_hash_differs("abc123", "") is False


def test_dpkg_hash_differs_both_empty_false():
    assert dpkg_hash_differs("", "") is False


def test_dpkg_hash_differs_strips_whitespace():
    assert dpkg_hash_differs(" abc123 ", "abc123") is False


# --- vm_pkg_count ---

def test_vm_pkg_count_apt():
    assert vm_pkg_count("2 upgraded, 0 newly installed", "apt") == 2


def test_vm_pkg_count_apt_zero_none():
    assert vm_pkg_count("0 upgraded, 0 newly installed", "apt") == 0


def test_vm_pkg_count_dnf():
    assert vm_pkg_count("Upgrade  3 Packages", "dnf") == 3


def test_vm_pkg_count_dnf_case_insensitive():
    assert vm_pkg_count("upgrade 1 package", "dnf") == 1


def test_vm_pkg_count_apk_multiple_lines():
    stdout = (
        "(1/2) Upgrading musl (1.2.4-r0 -> 1.2.5-r0)\n"
        "(2/2) Upgrading busybox (1.36.1-r0 -> 1.36.1-r1)\n"
    )
    assert vm_pkg_count(stdout, "apk") == 2


def test_vm_pkg_count_apk_none_when_no_upgrade_lines():
    assert vm_pkg_count("OK: 100 MiB in 50 packages", "apk") is None


def test_vm_pkg_count_unknown_manager_none():
    assert vm_pkg_count("some output", "pacman") is None


def test_vm_pkg_count_empty_none():
    assert vm_pkg_count("", "apt") is None


# --- pkg_changed ---

def test_pkg_changed_apt_noop_false():
    assert pkg_changed("0 upgraded, 0 newly installed", "apt") is False


def test_pkg_changed_apt_upgrades_true():
    assert pkg_changed("3 upgraded", "apt") is True


def test_pkg_changed_dnf_nothing_to_do_false():
    assert pkg_changed("Nothing to do.", "dnf") is False


def test_pkg_changed_dnf_upgraded_true():
    assert pkg_changed("Complete!", "dnf") is True


def test_pkg_changed_apk_noop_false():
    assert pkg_changed("0 upgraded, 0 installed, 0 removed", "apk") is False


def test_pkg_changed_apk_upgraded_true():
    assert pkg_changed("Upgrading musl", "apk") is True


def test_pkg_changed_unknown_manager_with_output_true():
    assert pkg_changed("some output", "pacman") is True


def test_pkg_changed_empty_false():
    assert pkg_changed("", "apt") is False


# --- custom_changed ---

def test_custom_changed_always_true():
    assert custom_changed(changed_when_type="always") is True


def test_custom_changed_command_rc0_true():
    assert custom_changed(changed_when_type="command", changed_cmd_rc=0) is True


def test_custom_changed_command_rc1_false():
    assert custom_changed(changed_when_type="command", changed_cmd_rc=1) is False


def test_custom_changed_command_skipped_falls_back_to_version():
    # when skipped, falls back to version logic (empty before/after → True)
    assert custom_changed(
        changed_when_type="command", changed_cmd_rc=1, changed_cmd_skipped=True
    ) is True


def test_custom_changed_version_differ_true():
    assert custom_changed(
        changed_when_type="version", ver_before="1.0", ver_after="1.1"
    ) is True


def test_custom_changed_version_same_false():
    assert custom_changed(
        changed_when_type="version", ver_before="1.0", ver_after="1.0"
    ) is False


def test_custom_changed_version_empty_before_true():
    assert custom_changed(changed_when_type="version", ver_before="", ver_after="1.1") is True


def test_custom_changed_version_empty_after_true():
    assert custom_changed(changed_when_type="version", ver_before="1.0", ver_after="") is True
