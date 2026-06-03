"""Tests for proxmox_fleet.flows.remote — the remote_host_update control flow.

Uses ScriptedRemoteExecutor (no real Ansible) and monkeypatches http for Kuma.
Covers: apt/dnf/apk detection, normal update, reboot, rescue, dry-run,
pre_update_cmd, idle suppression, Kuma gating.
"""

from proxmox_fleet import http as http_mod
from proxmox_fleet.flows.remote import run_remote_update
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout="", changed=True, rc=0):
    return PrimitiveResult(rc=rc, changed=changed, stdout=stdout, failed=False)


def _fail(rc=1, stderr="boom"):
    return PrimitiveResult(rc=rc, failed=True, stderr=stderr)


APT_UPGRADED = "3 upgraded, 0 newly installed, 0 to remove.\n"
APT_NOOP = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
DNF_UPGRADED = "Upgraded: foo-1.0\nComplete!\n"
DNF_NOOP = "Nothing to do!\n"
APK_UPGRADED = "(1 upgraded, 0 installed, 0 removed)\n"
APK_NOOP = "(0 upgraded, 0 installed, 0 removed)\n"

PKG_DETECT_APT = "/usr/bin/apt-get\napt\n"
PKG_DETECT_DNF = "/usr/bin/dnf\ndnf\n"
PKG_DETECT_APK = "/sbin/apk\napk\n"


def _settings(**kwargs) -> GlobalSettings:
    return GlobalSettings.model_validate({"remote_auto_reboot": True, **kwargs})


# ---------------------------------------------------------------------------
# Scripted fake executor
# ---------------------------------------------------------------------------


class ScriptedRemoteExecutor:
    host = "my-server"

    def __init__(self, script=None, default=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands = []
        self.reboots = 0

    def _resp(self, command):
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command, **opts):
        self.commands.append(command)
        return self._resp(command)

    def run_local(self, command):
        return self._resp(command)

    def reboot(self, *, timeout=600):
        self.reboots += 1
        return _ok()

    def snapshot(self, vmid, *, snap_state, **api_params):
        raise AssertionError("snapshot should never be called for remote hosts")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_normal_update_apt():
    """apt packages upgraded — UPDATED."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=False)

    assert not outcome.failed
    assert outcome.changed is True
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.host == "my-server"


def test_normal_update_dnf():
    """dnf packages upgraded — UPDATED."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_DNF)],
        "dnf upgrade": [_ok(stdout=DNF_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=False)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"


def test_normal_update_apk():
    """apk packages upgraded — UPDATED."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APK)],
        "apk": [_ok(stdout=APK_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=False)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"


def test_update_with_reboot():
    """Packages upgraded AND reboot-required — UPDATED & REBOOTED."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=0)],
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=False)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert ex.reboots == 1


def test_idle_nothing_to_upgrade():
    """Nothing upgraded — record suppressed."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=False)

    assert not outcome.failed
    assert outcome.changed is False
    assert outcome.record is None


def test_dry_run_would_update():
    """Dry-run with pending upgrades — WOULD UPDATE."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "apt-get -s": [_ok(stdout=APT_UPGRADED)],  # simulate: -s comes before dist-upgrade
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=True)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "WOULD UPDATE"


def test_dry_run_ok():
    """Dry-run with nothing pending — record suppressed."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade -s": [_ok(stdout=APT_NOOP)],
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=True)

    assert not outcome.failed
    assert outcome.record is None


def test_rescue_on_update_failure():
    """Upgrade command raises → rescue → plain FAILED (no snapshot for remote)."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_fail()],
    })
    outcome = run_remote_update("my-server", ex, _settings(), dry_run=False)

    assert outcome.failed is True
    assert outcome.record is not None
    assert outcome.record.status == "FAILED"
    assert outcome.error is not None


def test_pre_update_cmd_runs():
    """pre_update_cmd is executed before the upgrade."""
    ex = ScriptedRemoteExecutor(script={
        "my-pre-cmd": [_ok()],
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    run_remote_update("my-server", ex, _settings(), dry_run=False, pre_update_cmd="my-pre-cmd")
    assert any("my-pre-cmd" in cmd for cmd in ex.commands)


def test_pre_update_cmd_skipped_in_dry_run():
    """pre_update_cmd is NOT executed in dry-run mode."""
    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    run_remote_update("my-server", ex, _settings(), dry_run=True, pre_update_cmd="my-pre-cmd")
    assert not any("my-pre-cmd" in cmd for cmd in ex.commands)


def test_kuma_called_on_change(monkeypatch):
    """Kuma poll_until is called when packages changed."""
    polled = []
    monkeypatch.setattr(http_mod, "poll_until", lambda *a, **kw: polled.append(True))
    monkeypatch.setattr(http_mod, "get_json", lambda _: {})

    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    settings = _settings(
        remote_kuma_map={"my-server": "5"},
        kuma_url="http://kuma.local",
        kuma_slug="fleet",
        kuma_health_check_retries=1,
        kuma_health_check_delay=0,
    )
    run_remote_update("my-server", ex, settings, dry_run=False)
    assert len(polled) == 1


def test_kuma_not_called_when_idle(monkeypatch):
    """Kuma poll_until is NOT called when nothing changed."""
    polled = []
    monkeypatch.setattr(http_mod, "poll_until", lambda *a, **kw: polled.append(True))

    ex = ScriptedRemoteExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    settings = _settings(
        remote_kuma_map={"my-server": "5"},
        kuma_url="http://kuma.local",
        kuma_slug="fleet",
    )
    run_remote_update("my-server", ex, settings, dry_run=False)
    assert polled == []
