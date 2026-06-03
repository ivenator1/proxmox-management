"""Tests for proxmox_fleet.flows.vm — the vm_update control flow.

Uses ScriptedVmExecutor (no real Ansible) and monkeypatches http for Kuma.
Covers: normal update, update with reboot, snapshot warning, rescue/rollback,
rescue without snapshot, dry-run, idle (nothing to upgrade), correct executor
binding (qm commands must go to node_executor, not the VM executor).
"""

from proxmox_fleet import http as http_mod
from proxmox_fleet.flows.vm import run_vm_update
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
    snapshot scripting is separate. Records all commands for assertion.
    """

    host = "my-vm"

    def __init__(self, host="my-vm", script=None, default=None, snap_changed=True):
        self.host = host
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


def _vm_ex(**script_kwargs):
    """VM executor: handles package manager detection, upgrade, reboot check."""
    return ScriptedVmExecutor(host="my-vm", script=script_kwargs)


def _node_ex(**script_kwargs):
    """Node executor: handles qm rollback, qm status."""
    return ScriptedVmExecutor(host="pve-01", script=script_kwargs)


def _call(node_ex, vm_ex=None, settings=None, **kwargs):
    """Helper: call run_vm_update with separate vm/node executors."""
    if vm_ex is None:
        vm_ex = _vm_ex()
    return run_vm_update(
        "pve-01", "200", "my-vm", vm_ex, node_ex,
        settings or _settings(),
        dry_run=kwargs.pop("dry_run", False),
        api_host=kwargs.pop("api_host", "1.2.3.4"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_normal_update_apt():
    """Packages upgraded, no reboot required — UPDATED reported."""
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    node_ex = _node_ex()

    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, node_ex, _settings(),
                            dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.changed is True
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.vmid == "200"
    assert outcome.record.node == "pve-01"
    # Snapshot created and deleted via the VM executor (snapshot() is a separate method)
    assert "200" in vm_ex.snapshots_created
    assert "200" in vm_ex.snapshots_deleted
    # No qm commands went to the VM executor
    assert not any("qm" in c for c in vm_ex.commands)


def test_update_with_reboot():
    """Packages upgraded AND reboot-required flag set — UPDATED & REBOOTED."""
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=0)],
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), _settings(),
                            dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert vm_ex.reboots == 1


def test_idle_nothing_to_upgrade():
    """Nothing upgraded — no record appended (idle suppressed)."""
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), _settings(),
                            dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.changed is False
    assert outcome.record is None


def test_dry_run_would_update():
    """Dry-run with pending upgrades — WOULD UPDATE."""
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "apt-get -s": [_ok(stdout=APT_UPGRADED)],  # -s comes before dist-upgrade
    })
    settings = _settings(vm_backup_strategy="none")
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), settings,
                            dry_run=True, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "WOULD UPDATE"
    assert vm_ex.snapshots_created == []  # no snapshot in dry-run


def test_dry_run_ok():
    """Dry-run with nothing pending — record suppressed."""
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "apt-get -s": [_ok(stdout=APT_NOOP)],
    })
    settings = _settings(vm_backup_strategy="none")
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), settings,
                            dry_run=True, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is None


def test_snapshot_failure_warns_continues():
    """Snapshot API returns changed=False → warning appended, update still proceeds."""
    vm_ex = ScriptedVmExecutor(host="my-vm", snap_changed=False, script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), _settings(),
                            dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert len(outcome.warnings) == 1
    assert "snapshot failed" in outcome.warnings[0].warning


def test_rescue_rollback_on_update_failure():
    """Upgrade fails → rescue → qm rollback on NODE executor → FAILED + ROLLED BACK.

    Key assertion: qm commands go to node_ex, never to vm_ex.
    """
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_fail()],
    })
    node_ex = _node_ex(**{
        "qm rollback": [_ok()],
        "qm status": [_ok(stdout="status: running")],
    })

    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, node_ex, _settings(),
                            dry_run=False, api_host="1.2.3.4")

    assert outcome.failed is True
    assert outcome.record is not None
    assert outcome.record.status == "FAILED + ROLLED BACK"
    assert outcome.error is not None
    # qm commands went to node_ex, not vm_ex
    assert any("qm rollback" in c for c in node_ex.commands)
    assert any("qm status" in c for c in node_ex.commands)
    assert not any("qm" in c for c in vm_ex.commands)
    # Snapshot cleaned up in finally
    assert "200" in vm_ex.snapshots_deleted


def test_rescue_rollback_not_done_when_qm_fails():
    """qm rollback command itself fails → rollback_done stays False → plain FAILED."""
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_fail()],
    })
    node_ex = _node_ex(**{
        "qm rollback": [_fail()],  # rollback command fails
    })

    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, node_ex, _settings(),
                            dry_run=False, api_host="1.2.3.4")

    assert outcome.failed is True
    assert outcome.record is not None
    # Rollback command failed — can't claim ROLLED BACK
    assert outcome.record.status == "FAILED"


def test_rescue_no_snapshot_strategy_none():
    """backup_strategy=none → no snapshot → rescue records plain FAILED."""
    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_fail()],
    })
    settings = _settings(vm_backup_strategy="none")
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), settings,
                            dry_run=False, api_host="1.2.3.4")

    assert outcome.failed is True
    assert outcome.record.status == "FAILED"
    assert vm_ex.snapshots_created == []
    assert vm_ex.snapshots_deleted == []
    # qm rollback never called when no snapshot was taken
    assert not any("qm rollback" in c for c in _node_ex().commands)


def test_rescue_snapshot_failed_no_rollback():
    """Snapshot API failed (changed=False) → rescue records FAILED (NO SNAPSHOT)."""
    vm_ex = ScriptedVmExecutor(host="my-vm", snap_changed=False, script={
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_fail()],
    })
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), _settings(),
                            dry_run=False, api_host="1.2.3.4")

    assert outcome.failed is True
    assert outcome.record.status == "FAILED (NO SNAPSHOT)"


def test_kuma_health_check_called_on_change(monkeypatch):
    """Kuma poll_until is called when something changed and kuma_map is set."""
    polled = []
    monkeypatch.setattr(http_mod, "poll_until", lambda *a, **kw: polled.append(True))
    monkeypatch.setattr(http_mod, "get_json", lambda _: {"heartbeatList": {"1": [{"status": 1}]}})

    vm_ex = _vm_ex(**{
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
    outcome = run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), settings,
                            dry_run=False, api_host="1.2.3.4")

    assert not outcome.failed
    assert len(polled) == 1


def test_kuma_not_called_when_idle(monkeypatch):
    """Kuma poll_until is NOT called when nothing changed."""
    polled = []
    monkeypatch.setattr(http_mod, "poll_until", lambda *a, **kw: polled.append(True))

    vm_ex = _vm_ex(**{
        "which apt-get": [_ok(stdout=PKG_DETECT_APT)],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
    })
    settings = _settings(
        vm_kuma_map={"my-vm": "1"},
        kuma_url="http://kuma.local",
        kuma_slug="fleet",
    )
    run_vm_update("pve-01", "200", "my-vm", vm_ex, _node_ex(), settings,
                  dry_run=False, api_host="1.2.3.4")
    assert polled == []
