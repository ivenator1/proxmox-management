"""Tests for proxmox_fleet.pkg_detail — the exact OS package parsers (PR1).

parse_upgraded() turns each package manager's upgrade stdout into
[{"name", "from", "to"}] with realistic fixtures per manager — including the
apk "(i/n)" line prefix that previously made vm_pkg_count count 0 — plus
garbage-input tolerance. pkg_mgr_for_ostype() is the single home for the
container ostype → manager map (previously duplicated in scan.py).
"""

from proxmox_fleet.pkg_detail import parse_upgraded, pkg_mgr_for_ostype


def _names(detail):
    return [d["name"] for d in detail]


# --- apt real-run ------------------------------------------------------------ #

APT_REAL = """\
Reading package lists... Done
Building dependency tree... Done
Reading state information... Done
Calculating upgrade... Done
Unpacking libssl3:amd64 (3.0.13-1~deb12u1) over (3.0.11-1~deb12u2) ...
Unpacking curl (8.5.0-2) over (8.5.0-1) ...
Unpacking python3-minimal (3.11.9-1) ...
Setting up libssl3:amd64 (3.0.13-1~deb12u1) ...
Setting up curl (8.5.0-2) ...
3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
"""


def test_apt_real_parses_over_lines():
    detail = parse_upgraded(APT_REAL, "apt")
    assert _names(detail) == ["libssl3", "curl", "python3-minimal"]
    assert detail[0] == {"name": "libssl3", "from": "3.0.11-1~deb12u2", "to": "3.0.13-1~deb12u1"}
    assert detail[1] == {"name": "curl", "from": "8.5.0-1", "to": "8.5.0-2"}
    assert detail[2] == {"name": "python3-minimal", "from": "", "to": "3.11.9-1"}


def test_apt_real_new_install_has_empty_from():
    detail = parse_upgraded("Unpacking newpkg (1.2.3) ...\n", "apt")
    assert detail == [{"name": "newpkg", "from": "", "to": "1.2.3"}]


def test_apt_real_setting_up_lines_ignored():
    """Setting up / Preparing lines must not be mistaken for package upgrades."""
    detail = parse_upgraded(
        "Preparing to unpack ...\nUnpacking foo (1.0) over (0.9) ...\nSetting up foo (1.0) ...\n", "apt"
    )
    assert _names(detail) == ["foo"]


# --- apt simulate ------------------------------------------------------------ #

APT_SIM = """\
Inst libssl3:amd64 [3.0.11-1~deb12u2] (3.0.13-1~deb12u1 Debian:12-security/stable-security [amd64])
Inst curl [8.5.0-1] (8.5.0-2 Debian:12-security/stable-security [amd64])
Inst newpkg (1.2.3 Debian:12/stable [amd64])
"""


def test_apt_simulate_with_and_without_old():
    detail = parse_upgraded(APT_SIM, "apt")
    assert detail == [
        {"name": "libssl3", "from": "3.0.11-1~deb12u2", "to": "3.0.13-1~deb12u1"},
        {"name": "curl", "from": "8.5.0-1", "to": "8.5.0-2"},
        {"name": "newpkg", "from": "", "to": "1.2.3"},
    ]


# --- dnf real ---------------------------------------------------------------- #

DNF_REAL = """\
Upgraded:
  curl-8.5.0-1.fc39.x86_64  openssl-libs-3.1.1-2.fc39.x86_64
  bind-9.18.19-1.fc39.x86_64
Complete!
"""


def test_dnf_real_parses_multicolumn_tokens():
    detail = parse_upgraded(DNF_REAL, "dnf")
    assert detail == [
        {"name": "curl", "from": "", "to": "8.5.0-1.fc39"},
        {"name": "openssl-libs", "from": "", "to": "3.1.1-2.fc39"},
        {"name": "bind", "from": "", "to": "9.18.19-1.fc39"},
    ]


def test_dnf_real_ignores_installed_section():
    """Only the Upgraded: block parses — Installed:/Removed: sections are not
    counted as upgrades."""
    stdout = "Installed:\n  vim-enhanced-8.2.100-1.fc39.x86_64\n\nUpgraded:\n  curl-8.5.0-1.fc39.x86_64\n\nComplete!\n"
    detail = parse_upgraded(stdout, "dnf")
    assert _names(detail) == ["curl"]


def test_dnf_real_handles_hyphenated_names_and_epoch_versions():
    """Canonical name-version-release.arch NEVRAs preserve names and epochs."""
    stdout = "Upgraded:\n  python3-libs-3.12.1-1.fc39.x86_64\n  NetworkManager-1:1.36.0-3.el9_0.x86_64\nComplete!\n"
    detail = parse_upgraded(stdout, "dnf")
    assert detail == [
        {"name": "python3-libs", "from": "", "to": "3.12.1-1.fc39"},
        {"name": "NetworkManager", "from": "", "to": "1:1.36.0-3.el9_0"},
    ]


def test_dnf_real_skips_separator_rows():
    """Decorative ``---`` rows inside the Upgraded: block must not become
    phantom packages (robustness regression)."""
    stdout = "Upgraded:\n  ---\n  --- ---\n  curl-8.5.0-1.fc39.x86_64\nComplete!\n"
    detail = parse_upgraded(stdout, "dnf")
    assert _names(detail) == ["curl"]


def test_dnf_assumeno_skips_separator_rows():
    stdout = "Upgrading:\n  ---\nTransaction Summary\nUpgrade  1 Packages\n"
    assert parse_upgraded(stdout, "dnf") == []


# --- dnf assumeno (dry-run) -------------------------------------------------- #

DNF_ASSUMENO = """\
Last metadata expiration check: 0:05:26 ago.
Dependencies resolved.

Upgrading:
  Package       Arch     Version          Repository  Size
  curl          x86_64   8.5.0-1.fc39     fedora      321 k
  openssl-libs  x86_64   3.1.1-2.fc39     fedora      1.2 M

Transaction Summary
Upgrade  2 Packages
"""


def test_dnf_assumeno_table_parses():
    detail = parse_upgraded(DNF_ASSUMENO, "dnf")
    assert detail == [
        {"name": "curl", "from": "", "to": "8.5.0-1.fc39"},
        {"name": "openssl-libs", "from": "", "to": "3.1.1-2.fc39"},
    ]


# --- apk --------------------------------------------------------------------- #

APK_UPGRADE = """\
(1/2) Upgrading musl (1.2.4-r0 -> 1.2.5-r0)
(2/2) Upgrading busybox (1.36.1-r0 -> 1.36.1-r1)
OK: 412 MiB in 158 packages
"""


def test_apk_parses_numbered_prefix():
    """The (i/n) prefix is the bug-#1 case: old vm_pkg_count regex missed it."""
    detail = parse_upgraded(APK_UPGRADE, "apk")
    assert detail == [
        {"name": "musl", "from": "1.2.4-r0", "to": "1.2.5-r0"},
        {"name": "busybox", "from": "1.36.1-r0", "to": "1.36.1-r1"},
    ]


def test_apk_simulate_without_prefix_parses():
    detail = parse_upgraded("Upgrading musl (1.2.4-r0 -> 1.2.5-r0)\n", "apk")
    assert detail == [{"name": "musl", "from": "1.2.4-r0", "to": "1.2.5-r0"}]


def test_apk_summary_line_only_is_empty():
    assert parse_upgraded("OK: 412 MiB in 158 packages\n", "apk") == []


# --- de-dup / garbage -------------------------------------------------------- #


def test_dedupe_preserves_order():
    stdout = (
        "Unpacking curl (8.5.0-2) over (8.5.0-1) ...\n"
        "Unpacking curl (8.5.0-2) over (8.5.0-1) ...\n"
        "Unpacking zlib (1.3-1) over (1.2.13-1) ...\n"
    )
    detail = parse_upgraded(stdout, "apt")
    assert _names(detail) == ["curl", "zlib"]
    assert len(detail) == 2


def test_garbage_input_yields_empty_list():
    assert parse_upgraded("total garbage that means nothing\n", "apt") == []
    assert parse_upgraded("fatal: cannot find repo\n", "dnf") == []
    assert parse_upgraded("cp: cannot stat 'x'\n", "apk") == []


def test_empty_stdout():
    for mgr in ("apt", "dnf", "apk"):
        assert parse_upgraded("", mgr) == []


def test_unknown_package_manager():
    assert parse_upgraded("Upgrading anything (1 -> 2)\n", "pacman") == []


def test_apt_noop_summary_empty():
    assert parse_upgraded("0 upgraded, 0 newly installed, 0 to remove.\n", "apt") == []


# --- pkg_mgr_for_ostype ------------------------------------------------------ #


def test_pkg_mgr_for_ostype_map():
    assert pkg_mgr_for_ostype("debian") == "apt"
    assert pkg_mgr_for_ostype("ubuntu") == "apt"
    assert pkg_mgr_for_ostype("devuan") == "apt"
    assert pkg_mgr_for_ostype("alpine") == "apk"


def test_pkg_mgr_for_ostype_fallback():
    # fedora/arch and unknown OS types fall back to dnf (scan's previous
    # _OSTYPE_PKG_MGR.get(os_type, "dnf") behaviour).
    assert pkg_mgr_for_ostype("fedora") == "dnf"
    assert pkg_mgr_for_ostype("arch") == "dnf"
    assert pkg_mgr_for_ostype("totally-unknown") == "dnf"
