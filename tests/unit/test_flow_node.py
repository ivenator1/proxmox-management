"""Tests for proxmox_fleet.flows.node — Phase 2 (node OS update) and
Phase 3 (manager self-update) control flows.

Uses ScriptedNodeExecutor (no real Ansible) and monkeypatches http.wait_for_port.
Covers: normal update, reboot, manager-host skip, idle, rescue, dry-run,
apt retry, proxy wait, manager update variants.
"""

from proxmox_fleet import http as http_mod
from proxmox_fleet.flows.node import run_manager_update, run_node_update
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

# pct list echo $? responses: "0" = manager host, "1" = not manager host
NOT_MANAGER = _ok(stdout="1\n", changed=False)
IS_MANAGER = _ok(stdout="0\n", changed=False)

REBOOT_NEEDED = _ok(stdout="reboot\n", changed=False)
NO_REBOOT = _ok(stdout="ok\n", changed=False)


def _settings(**kwargs) -> GlobalSettings:
    return GlobalSettings.model_validate({
        "manager_lxc_id": "121",
        "node_auto_reboot": True,
        **kwargs,
    })


def _no_sleep(seconds: float) -> None:
    pass


# ---------------------------------------------------------------------------
# Scripted fake executor — node-bound
# ---------------------------------------------------------------------------


class ScriptedNodeExecutor:
    host = "pve-01"

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

    def snapshot(self, vmid, *, snap_state, **kwargs):
        raise AssertionError("snapshot should never be called for node updates")


# ---------------------------------------------------------------------------
# run_node_update tests
# ---------------------------------------------------------------------------


def test_normal_update_no_reboot():
    """apt upgrades packages, no reboot needed — UPDATED."""
    ex = ScriptedNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "vmlinuz": [NO_REBOOT],
    })
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.changed is True
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.node == "pve-01"
    assert ex.reboots == 0


def test_update_with_reboot(monkeypatch):
    """apt upgrades, reboot needed, not manager — UPDATED & REBOOTED; wait_for_port called."""
    port_calls = []
    monkeypatch.setattr(http_mod, "wait_for_port", lambda h, p, **kw: port_calls.append((h, p)))

    ex = ScriptedNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "vmlinuz": [REBOOT_NEEDED],
    })
    outcome = run_node_update(
        "pve-01", ex,
        _settings(apt_proxy_ip="10.0.0.1", apt_proxy_port=3142),
        dry_run=False, _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert ex.reboots == 1
    assert port_calls == [("10.0.0.1", 3142)]


def test_reboot_no_proxy_when_ip_empty(monkeypatch):
    """If apt_proxy_ip is empty, wait_for_port is NOT called after reboot."""
    port_calls = []
    monkeypatch.setattr(http_mod, "wait_for_port", lambda h, p, **kw: port_calls.append((h, p)))

    ex = ScriptedNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "vmlinuz": [REBOOT_NEEDED],
    })
    outcome = run_node_update("pve-01", ex, _settings(apt_proxy_ip=""), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert port_calls == []


def test_manager_host_skip_reboot():
    """is_manager=True + reboot needed — UPDATED (MANUAL REBOOT REQ); no reboot call."""
    ex = ScriptedNodeExecutor(script={
        "pct list": [IS_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "vmlinuz": [REBOOT_NEEDED],
    })
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert ex.reboots == 0
    assert outcome.changed is True


def test_no_changes_ok():
    """Nothing to upgrade — UPDATED is False, record still created (OK)."""
    ex = ScriptedNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "vmlinuz": [NO_REBOOT],
    })
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.changed is False
    assert outcome.record is not None  # nodes always get a record
    assert outcome.record.status == "OK"


def test_rescue_on_apt_failure():
    """apt fails on all retries → rescue → FAILED record with ErrorEntry."""
    sleeps = []

    ex = ScriptedNodeExecutor(
        script={"pct list": [NOT_MANAGER]},
        default=_fail(),  # all apt calls fail
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(),
        dry_run=False,
        _sleep=lambda s: sleeps.append(s),
    )

    assert outcome.failed is True
    assert outcome.record is not None
    assert outcome.record.status == "FAILED"
    assert outcome.error is not None
    assert "pve-01" in outcome.error.host


def test_apt_retry_succeeds_on_third_attempt():
    """apt fails twice then succeeds — outcome is UPDATED."""
    call_count = [0]

    class RetryNodeExecutor(ScriptedNodeExecutor):
        def run_shell(self, command, **opts):
            self.commands.append(command)
            if "dist-upgrade" in command:
                call_count[0] += 1
                if call_count[0] < 3:
                    return _fail()
                return _ok(stdout=APT_UPGRADED)
            return self._resp(command)

    ex = RetryNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "vmlinuz": [NO_REBOOT],
    })
    outcome = run_node_update(
        "pve-01", ex, _settings(),
        dry_run=False,
        _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert outcome.record.status == "UPDATED"
    assert call_count[0] == 3


def test_dry_run_would_update():
    """Dry-run: apt -s shows pending upgrades — status reflects what would happen."""
    ex = ScriptedNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "vmlinuz": [NO_REBOOT],
    })
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=True, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert ex.reboots == 0  # never reboot in dry-run


def test_dry_run_ok():
    """Dry-run: nothing pending — status OK."""
    ex = ScriptedNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "vmlinuz": [NO_REBOOT],
    })
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=True, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record.status == "OK"
    assert ex.reboots == 0


def test_manager_lxc_id_empty_skips_check():
    """When manager_lxc_id is empty, is_manager check is skipped (no pct list call)."""
    ex = ScriptedNodeExecutor(script={
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "vmlinuz": [NO_REBOOT],
    })
    outcome = run_node_update(
        "pve-01", ex, _settings(manager_lxc_id=""),
        dry_run=False, _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert outcome.record.status == "UPDATED"
    assert not any("pct list" in cmd for cmd in ex.commands)


def test_settle_sleep_called_after_reboot(monkeypatch):
    """The 15 s settle sleep is called after wait_for_port on reboot."""
    monkeypatch.setattr(http_mod, "wait_for_port", lambda *a, **kw: None)
    sleeps = []

    ex = ScriptedNodeExecutor(script={
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "vmlinuz": [REBOOT_NEEDED],
    })
    run_node_update(
        "pve-01", ex,
        _settings(apt_proxy_ip="10.0.0.1"),
        dry_run=False,
        _sleep=lambda s: sleeps.append(s),
    )

    assert 15 in sleeps or 15.0 in sleeps


# ---------------------------------------------------------------------------
# run_manager_update tests
# ---------------------------------------------------------------------------


def test_manager_update_changed():
    """apt upgrades packages on manager — UPDATED."""
    ex = ScriptedNodeExecutor(script={
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings())

    assert not outcome.failed
    assert outcome.changed is True
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.node == "Ansible-Manager"
    assert ex.reboots == 0


def test_manager_update_reboot_required():
    """apt upgrades packages AND reboot-required — UPDATED (MANUAL REBOOT REQ)."""
    ex = ScriptedNodeExecutor(script={
        "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
        "reboot-required": [_ok(stdout="reboot\n", changed=False)],
    })
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings())

    assert not outcome.failed
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert ex.reboots == 0


def test_manager_update_ok():
    """Nothing to upgrade on manager — OK."""
    ex = ScriptedNodeExecutor(script={
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "reboot-required": [_ok(rc=1, changed=False)],
    })
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings())

    assert not outcome.failed
    assert outcome.changed is False
    assert outcome.record.status == "OK"


def test_manager_apt_failure_ignored():
    """apt fails on manager (ignore_errors) — no exception; apt_changed=False."""
    ex = ScriptedNodeExecutor(
        script={"reboot-required": [_ok(rc=1, changed=False)]},
        default=_fail(),
    )
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings())

    # apt failed but we ignore it; reboot check still runs
    assert not outcome.failed
    assert outcome.changed is False
    # status depends on reboot check result
    assert outcome.record is not None


def test_manager_dry_run():
    """Dry-run manager update: apt -s used, no changes recorded."""
    commands = []

    class CaptureExecutor(ScriptedNodeExecutor):
        def run_shell(self, command, **opts):
            commands.append(command)
            if "dist-upgrade" in command:
                return _ok(stdout=APT_NOOP)
            return _ok(rc=1, changed=False)

    ex = CaptureExecutor()
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings(), dry_run=True)

    assert not outcome.failed
    assert any("-s" in cmd for cmd in commands), "dry-run should use apt-get -s"
    assert ex.reboots == 0
