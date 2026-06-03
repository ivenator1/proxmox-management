"""Tests for proxmox_fleet.flows._pkg — shared package-manager helpers."""
import pytest

from proxmox_fleet.flows._pkg import detect_pkg_mgr, kuma_healthy, upgrade_cmd
from proxmox_fleet.runner import PrimitiveResult


class _FakeExec:
    """Returns a fixed stdout for the detection command."""

    host = "h"

    def __init__(self, stdout: str):
        self._stdout = stdout
        self.commands = []

    def run_shell(self, command, **opts):
        self.commands.append(command)
        return PrimitiveResult(rc=0, changed=False, stdout=self._stdout, failed=False)


# --- detect_pkg_mgr ------------------------------------------------------------

@pytest.mark.parametrize("stdout,expected", [
    ("/usr/bin/apt-get\napt\n", "apt"),
    ("/usr/bin/dnf\ndnf\n", "dnf"),
    ("/sbin/apk\napk\n", "apk"),
    ("unknown\n", "apt"),   # inconclusive → Debian-family fallback
    ("", "apt"),            # no output → fallback
])
def test_detect_pkg_mgr(stdout, expected):
    assert detect_pkg_mgr(_FakeExec(stdout)) == expected


def test_detect_pkg_mgr_is_read_only():
    ex = _FakeExec("apt\n")
    detect_pkg_mgr(ex)
    # exactly one detection shell, issued with changed_when=False / ignore_errors
    assert len(ex.commands) == 1


# --- upgrade_cmd ---------------------------------------------------------------

@pytest.mark.parametrize("pkg_mgr", ["apt", "dnf", "apk"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_upgrade_cmd_pins_locale(pkg_mgr, dry_run):
    """Every branch must pin LC_ALL=C so change detection sees English summaries."""
    cmd = upgrade_cmd(pkg_mgr, dry_run=dry_run)
    assert "LC_ALL=C" in cmd


def test_upgrade_cmd_apt_real_vs_dry():
    assert "-s dist-upgrade" in upgrade_cmd("apt", dry_run=True)
    assert "dist-upgrade -y --autoremove" in upgrade_cmd("apt", dry_run=False)


def test_upgrade_cmd_dnf_real_vs_dry():
    assert upgrade_cmd("dnf", dry_run=True) == "LC_ALL=C dnf upgrade --assumeno"
    assert upgrade_cmd("dnf", dry_run=False) == "LC_ALL=C dnf upgrade -y"


def test_upgrade_cmd_apk_real_vs_dry():
    assert upgrade_cmd("apk", dry_run=True) == "LC_ALL=C apk -s upgrade"
    assert upgrade_cmd("apk", dry_run=False) == "LC_ALL=C apk -U upgrade"


def test_upgrade_cmd_unknown_raises():
    with pytest.raises(RuntimeError):
        upgrade_cmd("zypper", dry_run=False)


# --- kuma_healthy --------------------------------------------------------------

def test_kuma_healthy_true_on_last_beat_up():
    payload = {"heartbeatList": {"5": [{"status": 0}, {"status": 1}]}}
    assert kuma_healthy(payload, monitor_id="5") is True


def test_kuma_healthy_false_on_last_beat_down():
    payload = {"heartbeatList": {"5": [{"status": 1}, {"status": 0}]}}
    assert kuma_healthy(payload, monitor_id="5") is False


def test_kuma_healthy_false_on_missing_or_empty():
    assert kuma_healthy({"heartbeatList": {}}, monitor_id="5") is False
    assert kuma_healthy(None, monitor_id="5") is False
    assert kuma_healthy({"heartbeatList": {"5": []}}, monitor_id="5") is False
