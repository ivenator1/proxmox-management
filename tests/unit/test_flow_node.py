"""Tests for proxmox_fleet.flows.node — Phase 2 (node OS update) and
Phase 3 (manager self-update) control flows.

Uses ScriptedNodeExecutor (no real Ansible) and monkeypatches http.wait_for_port.
Covers: normal update, reboot, manager-host skip, idle, rescue, dry-run,
apt retry, proxy wait, manager update variants.
"""

import pytest

from proxmox_fleet import http as http_mod
from proxmox_fleet.executor import Executor
from proxmox_fleet.flows.node import run_manager_update, run_node_update
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout="", changed=True, rc=0, facts=None):
    return PrimitiveResult(rc=rc, changed=changed, stdout=stdout, failed=False, facts=facts or {})


def _fail(rc=1, stderr="boom"):
    return PrimitiveResult(rc=rc, failed=True, stderr=stderr)


APT_UPGRADED = "3 upgraded, 0 newly installed, 0 to remove.\n"
APT_NOOP = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
# apt real-run output with per-package Unpacking lines (PR1 packages detail)
APT_REAL_DETAIL = (
    "Unpacking libssl3:amd64 (3.0.13-1~deb12u1) over (3.0.11-1~deb12u2) ...\n"
    "Unpacking curl (8.5.0-2) over (8.5.0-1) ...\n"
    "2 upgraded, 0 newly installed, 0 to remove.\n"
)

# pct list echo $? responses: "0" = manager host, "1" = not manager host
NOT_MANAGER = _ok(stdout="1\n", changed=False)
IS_MANAGER = _ok(stdout="0\n", changed=False)


def _post_result(
    *,
    reboot_required=False,
    running="6.8.12-8-pve",
    latest="6.8.12-8-pve",
    nvidia=False,
    installed="550.90.07",
    loaded="550.90.07",
    dkms_ready=True,
    smi_rc=0,
    installed_rc=0,
    loaded_rc=0,
):
    dkms = f"nvidia-current/{installed}, {running}, x86_64: installed" if dkms_ready else ""
    return _ok(
        changed=False,
        facts={
            "diagnostics_version": 1,
            "running_kernel_rc": 0,
            "running_kernel": running,
            "latest_kernel_rc": 0,
            "latest_kernel": latest,
            "reboot_required_exists": reboot_required,
            "reboot_required_packages": "pve-kernel" if reboot_required else "",
            "nvidia_checked": nvidia,
            "nvidia_installed_rc": installed_rc if nvidia else 1,
            "nvidia_installed": installed if nvidia and installed_rc == 0 else "",
            "nvidia_loaded_rc": loaded_rc if nvidia else 1,
            "nvidia_loaded": loaded if nvidia and loaded_rc == 0 else "",
            "nvidia_dkms_rc": 0 if nvidia and dkms_ready else 1,
            "nvidia_dkms_status": dkms,
            "nvidia_smi_rc": smi_rc if nvidia else 1,
        },
    )


REBOOT_NEEDED = _post_result(reboot_required=True)
NO_REBOOT = _post_result()


def _settings(**kwargs) -> GlobalSettings:
    return GlobalSettings.model_validate(
        {
            "manager_lxc_id": "121",
            "node_auto_reboot": True,
            **kwargs,
        }
    )


def _no_sleep(seconds: float) -> None:
    pass


# ---------------------------------------------------------------------------
# Scripted fake executor — node-bound
# ---------------------------------------------------------------------------


class ScriptedNodeExecutor(Executor):
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

    def node_post_upgrade(self, *, nvidia_host=False):
        self.commands.append(f"node_post_upgrade nvidia_host={nvidia_host}")
        return self._resp("vmlinuz")

    def snapshot(self, vmid, *, snap_state, **kwargs):
        raise AssertionError("snapshot should never be called for node updates")

    def introspect(self, lxc_id):
        raise AssertionError("LXC methods should never be called for node updates")

    def vzdump(self, lxc_id, *, backup_storage, lxc_name):
        raise AssertionError("LXC methods should never be called for node updates")

    def lxc_os_update(self, lxc_id, *, os_update_cmd):
        raise AssertionError("LXC methods should never be called for node updates")

    def lxc_app_update(self, lxc_id, **kwargs):
        raise AssertionError("LXC methods should never be called for node updates")

    def post_update(self, lxc_id, **kwargs):
        raise AssertionError("LXC methods should never be called for node updates")

    def pct_rollback(self, lxc_id):
        raise AssertionError("LXC methods should never be called for node updates")

    def pct_start(self, lxc_id):
        raise AssertionError("LXC methods should never be called for node updates")

    def pct_stop(self, lxc_id):
        raise AssertionError("LXC methods should never be called for node updates")


# ---------------------------------------------------------------------------
# run_node_update tests
# ---------------------------------------------------------------------------


def test_normal_update_no_reboot():
    """apt upgrades packages, no reboot needed — UPDATED."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.changed is True
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.node == "pve-01"
    assert ex.reboots == 0


def test_normal_update_captures_pkg_count_and_packages(monkeypatch):
    """PR1: node success records carry pkg_count AND the exact apt package list."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_REAL_DETAIL)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.pkg_count == 2
    assert outcome.record.packages == [
        {"name": "libssl3", "from": "3.0.11-1~deb12u2", "to": "3.0.13-1~deb12u1"},
        {"name": "curl", "from": "8.5.0-1", "to": "8.5.0-2"},
    ]


def test_idle_node_has_no_packages():
    """PR1: an OK node record stays key-free (no packages/pkgs when nothing changed)."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert outcome.record is not None
    assert outcome.record.status == "OK"
    assert outcome.record.pkg_count is None
    assert outcome.record.packages is None


def test_update_with_reboot(monkeypatch):
    """apt upgrades, reboot needed, not manager — UPDATED & REBOOTED; wait_for_port called."""
    port_calls = []
    monkeypatch.setattr(http_mod, "wait_for_port", lambda h, p, **kw: port_calls.append((h, p)))

    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [REBOOT_NEEDED],
        }
    )
    outcome = run_node_update(
        "pve-01",
        ex,
        _settings(apt_proxy_ip="10.0.0.1", apt_proxy_port=3142),
        dry_run=False,
        _sleep=_no_sleep,
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

    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [REBOOT_NEEDED],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(apt_proxy_ip=""), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert port_calls == []


def test_manager_host_skip_reboot():
    """is_manager=True + reboot needed — UPDATED (MANUAL REBOOT REQ); no reboot call."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [IS_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [REBOOT_NEEDED],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert ex.reboots == 0
    assert outcome.changed is True


def test_no_changes_ok():
    """Nothing to upgrade — UPDATED is False, record still created (OK)."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "vmlinuz": [NO_REBOOT],
        }
    )
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
        "pve-01",
        ex,
        _settings(),
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

    ex = RetryNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update(
        "pve-01",
        ex,
        _settings(),
        dry_run=False,
        _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert outcome.record.status == "UPDATED"
    assert call_count[0] == 3


APT_SIM_DETAIL = (
    "Inst libssl3:amd64 [3.0.11-1~deb12u2] (3.0.13-1~deb12u1 Debian:12-security/stable-security [amd64])\n"
    "Inst curl [8.5.0-1] (8.5.0-2 Debian:12-security/stable-security [amd64])\n"
    "2 upgraded, 0 newly installed, 0 to remove.\n"
)


def test_dry_run_would_update():
    """Dry-run: apt -s shows pending upgrades — status reflects what would happen.

    PR1 roadmap: the node's simulated (would-update) output IS retained as
    package detail; pkg_count stays None so cumulative totals count only
    actual updates."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_SIM_DETAIL)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=True, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.packages == [
        {"name": "libssl3", "from": "3.0.11-1~deb12u2", "to": "3.0.13-1~deb12u1"},
        {"name": "curl", "from": "8.5.0-1", "to": "8.5.0-2"},
    ]
    assert outcome.record.pkg_count is None  # totals count real updates only
    assert ex.reboots == 0  # never reboot in dry-run


def test_dry_run_record_serializes_dry_run_true():
    """PR3: a node dry run persists dry_run=true in the record so the ledger
    never treats its simulated 'UPDATED' status as an applied update."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_SIM_DETAIL)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=True, _sleep=_no_sleep)

    assert outcome.record is not None
    assert outcome.record.dry_run is True
    assert outcome.record.model_dump()["dry_run"] is True


def test_real_run_record_omits_dry_run_key():
    """PR3: a real node run leaves dry_run out of the serialized record — the
    exact legacy byte shape."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=False, _sleep=_no_sleep)

    assert outcome.record is not None
    assert outcome.record.dry_run is None
    assert "dry_run" not in outcome.record.model_dump()


def test_dry_run_ok():
    """Dry-run: nothing pending — status OK."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), dry_run=True, _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record.status == "OK"
    assert ex.reboots == 0


def test_manager_lxc_id_empty_skips_check():
    """When manager_lxc_id is empty, is_manager check is skipped (no pct list call)."""
    ex = ScriptedNodeExecutor(
        script={
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update(
        "pve-01",
        ex,
        _settings(manager_lxc_id=""),
        dry_run=False,
        _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert outcome.record.status == "UPDATED"
    assert not any("pct list" in cmd for cmd in ex.commands)


def test_qualified_manager_id_skips_probe_on_other_cluster():
    """manager_lxc_id="alpha/121" + node cluster="beta" — probe NOT issued.

    A qualified manager id only ever refers to a container on its own
    cluster; running the probe against a different cluster's node risks
    matching an unrelated container with the same numeric id and wrongly
    suppressing that node's reboot.
    """
    ex = ScriptedNodeExecutor(
        script={
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [REBOOT_NEEDED],
        }
    )
    outcome = run_node_update(
        "pve-01",
        ex,
        _settings(manager_lxc_id="alpha/121"),
        dry_run=False,
        cluster="beta",
        _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert not any("pct list" in cmd for cmd in ex.commands)
    # not treated as manager — reboot proceeds normally
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert ex.reboots == 1


def test_qualified_manager_id_runs_probe_on_matching_cluster():
    """manager_lxc_id="alpha/121" + node cluster="alpha" — probe IS issued."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [IS_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [REBOOT_NEEDED],
        }
    )
    outcome = run_node_update(
        "pve-01",
        ex,
        _settings(manager_lxc_id="alpha/121"),
        dry_run=False,
        cluster="alpha",
        _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert any("pct list" in cmd for cmd in ex.commands)
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert ex.reboots == 0


def test_bare_manager_id_runs_probe_regardless_of_cluster():
    """manager_lxc_id="121" (bare) — probe IS issued no matter the node's cluster."""
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [NO_REBOOT],
        }
    )
    outcome = run_node_update(
        "pve-01",
        ex,
        _settings(manager_lxc_id="121"),
        dry_run=False,
        cluster="beta",
        _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert any("pct list" in cmd for cmd in ex.commands)
    assert outcome.record.status == "UPDATED"


def test_settle_sleep_called_after_reboot(monkeypatch):
    """The 15 s settle sleep is called after wait_for_port on reboot."""
    monkeypatch.setattr(http_mod, "wait_for_port", lambda *a, **kw: None)
    sleeps = []

    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [REBOOT_NEEDED],
        }
    )
    run_node_update(
        "pve-01",
        ex,
        _settings(apt_proxy_ip="10.0.0.1"),
        dry_run=False,
        _sleep=lambda s: sleeps.append(s),
    )

    assert 15 in sleeps or 15.0 in sleeps


# ---------------------------------------------------------------------------
# NVIDIA post-upgrade diagnostics
# ---------------------------------------------------------------------------


def test_nvidia_matching_versions_are_healthy():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [_post_result(nvidia=True)],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.checks == {
        "running_kernel": "6.8.12-8-pve",
        "nvidia_loaded": "550.90.07",
        "nvidia_installed": "550.90.07",
        "nvidia_dkms_ready": True,
        "nvidia_smi_ok": True,
    }
    assert ex.reboots == 0


def test_nvidia_mismatch_triggers_normal_auto_reboot():
    before = _post_result(nvidia=True, loaded="550.54.14", installed="550.90.07")
    after = _post_result(nvidia=True, loaded="550.90.07", installed="550.90.07")
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [before, after],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED & REBOOTED"
    assert outcome.record.reboot_reasons == [
        "NVIDIA module mismatch: loaded 550.54.14, installed 550.90.07"
    ]
    assert outcome.record.checks is not None
    assert outcome.record.checks["pre_reboot"]["nvidia_loaded"] == "550.54.14"
    assert outcome.record.checks["post_reboot"]["nvidia_loaded"] == "550.90.07"
    assert ex.reboots == 1


def test_nvidia_mismatch_manual_policy_does_not_reboot():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "vmlinuz": [_post_result(nvidia=True, loaded="550.54.14")],
        }
    )
    outcome = run_node_update(
        "pve-01",
        ex,
        _settings(node_auto_reboot=False),
        nvidia_host=True,
        _sleep=_no_sleep,
    )

    assert not outcome.failed
    assert outcome.changed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert ex.reboots == 0


def test_nvidia_mismatch_on_manager_host_remains_manual():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [IS_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [_post_result(nvidia=True, loaded="550.54.14")],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert ex.reboots == 0


def test_nvidia_missing_modinfo_is_hard_failure_without_reboot():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [_post_result(nvidia=True, installed_rc=1)],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert outcome.failed
    assert outcome.error is not None
    assert "modinfo" in outcome.error.error
    assert ex.reboots == 0


def test_nvidia_dkms_entry_for_other_kernel_is_rejected():
    result = _post_result(nvidia=True)
    result.facts["nvidia_dkms_status"] = (
        "nvidia-current/550.90.07, 6.8.12-7-pve, x86_64: installed"
    )
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [result],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert outcome.failed
    assert outcome.error is not None
    assert "6.8.12-8-pve" in outcome.error.error
    assert ex.reboots == 0


def test_nvidia_missing_dkms_is_hard_failure_without_reboot():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [_post_result(nvidia=True, loaded="550.54.14", dkms_ready=False)],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert outcome.failed
    assert outcome.error is not None
    assert "DKMS" in outcome.error.error
    assert ex.reboots == 0


def test_nvidia_smi_failure_during_mismatch_is_attributed_to_reboot():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [
                _post_result(nvidia=True, loaded="550.54.14", smi_rc=1),
                _post_result(nvidia=True),
            ],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert not outcome.failed
    assert outcome.warnings == []
    assert ex.reboots == 1


def test_standalone_nvidia_smi_failure_is_warning():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "vmlinuz": [_post_result(nvidia=True, smi_rc=1)],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert not outcome.failed
    assert len(outcome.warnings) == 1
    assert "nvidia-smi" in outcome.warnings[0].warning
    assert ex.reboots == 0


@pytest.mark.parametrize(
    "after",
    [
        _post_result(nvidia=True, loaded="550.54.14"),
        _post_result(nvidia=True, dkms_ready=False),
        _post_result(nvidia=True, smi_rc=1),
    ],
)
def test_nvidia_post_reboot_validation_failure_marks_node_failed(after):
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [_post_result(nvidia=True, loaded="550.54.14"), after],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), nvidia_host=True, _sleep=_no_sleep
    )

    assert outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "FAILED"
    assert outcome.error is not None
    assert outcome.error.task == "NVIDIA post-reboot check"


def test_failed_reboot_is_not_reported_as_success():
    class FailedRebootExecutor(ScriptedNodeExecutor):
        def reboot(self, *, timeout=600):
            self.reboots += 1
            return _fail(stderr="reboot timed out")

    ex = FailedRebootExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [REBOOT_NEEDED],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), _sleep=_no_sleep)

    assert outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "FAILED"
    assert outcome.error is not None
    assert "reboot failed" in outcome.error.error


def test_kernel_mismatch_still_triggers_reboot():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "vmlinuz": [_post_result(latest="6.8.12-9-pve")],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), _sleep=_no_sleep)

    assert not outcome.failed
    assert outcome.record is not None
    assert "kernel update" in outcome.record.reboot_reasons[0]
    assert ex.reboots == 1


def test_dry_run_gathers_nvidia_diagnostics_but_never_reboots():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "vmlinuz": [_post_result(nvidia=True, loaded="550.54.14")],
        }
    )
    outcome = run_node_update(
        "pve-01", ex, _settings(), dry_run=True, nvidia_host=True, _sleep=_no_sleep
    )

    assert not outcome.failed
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert outcome.record.checks is not None
    assert ex.reboots == 0


def test_malformed_post_upgrade_facts_fail_safely():
    ex = ScriptedNodeExecutor(
        script={
            "pct list": [NOT_MANAGER],
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "vmlinuz": [_ok(changed=False, facts={})],
        }
    )
    outcome = run_node_update("pve-01", ex, _settings(), _sleep=_no_sleep)

    assert outcome.failed
    assert outcome.error is not None
    assert "diagnostic" in outcome.error.error


# ---------------------------------------------------------------------------
# run_manager_update tests
# ---------------------------------------------------------------------------


def test_manager_update_changed():
    """apt upgrades packages on manager — UPDATED, with exact packages (PR1)."""
    ex = ScriptedNodeExecutor(
        script={
            "dist-upgrade": [_ok(stdout=APT_REAL_DETAIL)],
            "reboot-required": [_ok(rc=1, changed=False)],
        }
    )
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings())

    assert not outcome.failed
    assert outcome.changed is True
    assert outcome.record is not None
    assert outcome.record.status == "UPDATED"
    assert outcome.record.node == "Ansible-Manager"
    assert outcome.record.pkg_count == 2
    assert outcome.record.packages == [
        {"name": "libssl3", "from": "3.0.11-1~deb12u2", "to": "3.0.13-1~deb12u1"},
        {"name": "curl", "from": "8.5.0-1", "to": "8.5.0-2"},
    ]
    assert ex.reboots == 0


def test_manager_update_reboot_required():
    """apt upgrades packages AND reboot-required — UPDATED (MANUAL REBOOT REQ)."""
    ex = ScriptedNodeExecutor(
        script={
            "dist-upgrade": [_ok(stdout=APT_UPGRADED)],
            "reboot-required": [_ok(stdout="reboot\n", changed=False)],
        }
    )
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings())

    assert not outcome.failed
    assert outcome.record.status == "UPDATED (MANUAL REBOOT REQ)"
    assert ex.reboots == 0


def test_manager_update_ok():
    """Nothing to upgrade on manager — OK."""
    ex = ScriptedNodeExecutor(
        script={
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(rc=1, changed=False)],
        }
    )
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


def test_manager_dry_run_serializes_dry_run_true():
    """PR3: manager dry runs persist dry_run=true — manager status strings have
    no simulation variant, so the flag is what keeps the ledger honest."""
    ex = ScriptedNodeExecutor(
        script={
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(rc=1, changed=False)],
        }
    )
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings(), dry_run=True)

    assert outcome.record is not None
    assert outcome.record.dry_run is True
    assert outcome.record.model_dump()["dry_run"] is True


def test_manager_real_run_omits_dry_run_key():
    """PR3: a real manager run leaves dry_run out of the serialized record."""
    ex = ScriptedNodeExecutor(
        script={
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(rc=1, changed=False)],
        }
    )
    ex.host = "localhost"
    outcome = run_manager_update(ex, _settings(), dry_run=False)

    assert outcome.record is not None
    assert outcome.record.dry_run is None
    assert "dry_run" not in outcome.record.model_dump()
