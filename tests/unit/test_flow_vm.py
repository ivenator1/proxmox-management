"""Tests for proxmox_fleet.flows.vm — the vm_update control flow.

Uses ScriptedVmExecutor (no real Ansible) and monkeypatches http for Kuma.
Covers: normal update, update with reboot, snapshot warning, rescue/rollback,
rescue without snapshot, dry-run, idle (nothing to upgrade).
"""
import pytest

from proxmox_fleet import http as http_mod
from proxmox_fleet.flows.vm import VmFlowOutcome, run_vm_update
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout="", changed=True, rc=0):
    return PrimitiveResult(rc=rc, changed=changed, stdout=stdout, failed=False)


def _fail(rc=1, stderr="boom"):
    return PrimitiveResult(rc=rc, failed=True, stderr=stderr)


# apt upgrade stdout — packages were upgraded
APT_UPGRADED = "3 upgraded, 0 newly installed, 0 to remove.\nSetting up foo (1.2) ...\n"
# apt upgrade stdout — nothing to upgrade
APT_NOOP = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
# pkg-manager detection stdout
PKG_DETECT_APT = "/usr/bin/apt-get\napt\n"
PKG_DETECT_DNF = "/usr/bin/dnf\ndnf\n"


def _settings(**kwargs) -> GlobalSettings:
    return GlobalSettings.model_validate({
        "vm_auto_reboot": True,
        "vm_backup_strategy": "snapshot",
        "pve_api_user": "root@pam",
        "pve_api_token_id": "test",
        "pve_api_token_secret": "secret",
        **kwargs,
    })


# ---------------------------------------------------------------------------
# Scripted fake executor
# ---------------------------------------------------------------------------


class ScriptedVmExecutor:
    """Fake executor for vm flow tests.

    run_shell responses keyed by command substring; first match wins.
    snapshot scripting is separate.
    """

    host = "my-vm"

    def __init__(self, script=None, default=None, snap_changed=True):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands = []
        self.reboots = 0
        self.snap_changed = snap_changed
        self.snapshots_created = []
        self.snapshots_deleted = []

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
        if snap_state == "present":
            self.snapshots_created.append(vmid)
        else:
            self.snapshots_deleted.append(vmid)
        return _ok(changed=self.snap_changed)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_normal_update_apt():
    """Packages upgraded, no reboot required — UPDATED reported."""
    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],  # no reboot needed
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, _settings(), dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.changed is True
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.vmid == "200"
    assert outcome.record.node == "pve-01"
    # Snapshot created and deleted (always block)
    assert "200" in ex.snapshots_created
    assert "200" in ex.snapshots_deleted


def test_update_with_reboot():
    """Packages upgraded AND reboot-required flag set — UPDATED & REBOOTED."""
    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=0)],  # reboot needed
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, _settings(), dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert ex.reboots == 1


def test_idle_nothing_to_upgrade():
    """Nothing upgraded — no record appended (idle suppressed)."""
    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, _settings(), dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.changed is False
    assert outcome.record is None  # idle: nothing to report


def test_dry_run_would_update():
    """Dry-run with pending upgrades — WOULD UPDATE."""
    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "apt-get -s": [_ok(stdout=APT_UPGRADED)],  # simulate: -s comes before dist-upgrade
    })
    settings = _settings(vm_backup_strategy="none")
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, settings, dry_run=True, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "WOULD UPDATE"
    # No snapshots taken in dry-run
    assert ex.snapshots_created == []


def test_dry_run_ok():
    """Dry-run with nothing to upgrade — OK."""
    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade -s": [_ok(stdout=APT_NOOP)],
    })
    settings = _settings(vm_backup_strategy="none")
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, settings, dry_run=True, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is None  # idle in dry-run suppressed


def test_snapshot_failure_warns_continues():
    """Snapshot API returns changed=False → warning appended, update still proceeds."""
    ex = ScriptedVmExecutor(
        script={
            "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "reboot-required": [_ok(rc=1, changed=False)],
        },
        snap_changed=False,  # snapshot fails
    )
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, _settings(), dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert len(outcome.warnings) == 1
    assert "snapshot failed" in outcome.warnings[0].warning


def test_rescue_rollback_on_update_failure():
    """Update command raises → rescue fires → qm rollback called → FAILED + ROLLED BACK."""
    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_fail()],
        "qm rollback": [_ok()],
        "qm status": [_ok(stdout="status: running")],
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, _settings(), dry_run=False, api_host="1.2.3.4")

    assert outcome.failed is True
    assert outcome.record is not None
    assert outcome.record.status == "FAILED + ROLLED BACK"
    assert outcome.error is not None
    # Snapshot still cleaned up in finally
    assert "200" in ex.snapshots_deleted


def test_rescue_no_snapshot_strategy_none():
    """backup_strategy=none → no snapshot taken → rescue records plain FAILED."""
    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_fail()],
    })
    settings = _settings(vm_backup_strategy="none")
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, settings, dry_run=False, api_host="1.2.3.4")

    assert outcome.failed is True
    assert outcome.record is not None
    assert outcome.record.status == "FAILED"  # plain (no snapshot, no rollback)
    assert ex.snapshots_created == []
    assert ex.snapshots_deleted == []


def test_rescue_snapshot_failed_no_rollback():
    """Snapshot API failed (changed=False, strategy=snapshot) → rescue records FAILED (NO SNAPSHOT)."""
    ex = ScriptedVmExecutor(
        script={
            "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
            "dist-upgrade": [_fail()],
        },
        snap_changed=False,
    )
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, _settings(), dry_run=False, api_host="1.2.3.4")

    assert outcome.failed is True
    assert outcome.record is not None
    assert outcome.record.status == "FAILED (NO SNAPSHOT)"


def test_kuma_health_check_called_on_change(monkeypatch):
    """Kuma poll_until is called when something changed and kuma_map is set."""
    polled = []

    def fake_poll(fetch_fn, pred, **kwargs):
        polled.append(True)

    monkeypatch.setattr(http_mod, "poll_until", fake_poll)
    monkeypatch.setattr(http_mod, "get_json", lambda _: {"heartbeatList": {"1": [{"status": 1}]}})

    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    settings = _settings(
        vm_kuma_map={"my-vm": "1"},
        kuma_url="http://kuma.local",
        kuma_slug="fleet",
        kuma_health_check_retries=1,
        kuma_health_check_delay=0,
    )
    outcome = run_vm_update("pve-01", "200", "my-vm", ex, settings, dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert len(polled) == 1


def test_kuma_not_called_when_idle(monkeypatch):
    """Kuma poll_until is NOT called when nothing changed."""
    polled = []

    def fake_poll(fetch_fn, pred, **kwargs):
        polled.append(True)

    monkeypatch.setattr(http_mod, "poll_until", fake_poll)

    ex = ScriptedVmExecutor(script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    settings = _settings(
        vm_kuma_map={"my-vm": "1"},
        kuma_url="http://kuma.local",
        kuma_slug="fleet",
    )
    run_vm_update("pve-01", "200", "my-vm", ex, settings, dry_run=False, api_host="1.2.3.4")
    assert polled == []
