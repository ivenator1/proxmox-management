"""Tests for proxmox_fleet.driver — run_custom_phase() and helpers.

RunnerExecutor is monkeypatched with a ScriptedExecutor so no ansible-runner is
required for unit tests. The state JSON output is verified via dump_for_ansible().
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

import proxmox_fleet.driver as driver_mod
from proxmox_fleet.driver import (
    _deep_merge,
    _merge_state,
    run_custom_phase,
    run_fleet,
    run_node_phase,
    run_notify_phase,
)
from proxmox_fleet.flows.vm import VmFlowOutcome
from proxmox_fleet.inventory import VmSpec
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import FleetState
from proxmox_fleet.runner import PrimitiveResult


def _ok(stdout: str = "", changed: bool = True, facts: Optional[Dict[str, Any]] = None) -> PrimitiveResult:
    return PrimitiveResult(rc=0, changed=changed, stdout=stdout, facts=facts or {})


def _fail(rc: int = 1) -> PrimitiveResult:
    return PrimitiveResult(rc=rc, failed=True, stderr="boom")


class ScriptedExecutor:
    """Fake executor injected by tests."""

    def __init__(
        self,
        script: Optional[Dict[str, List[PrimitiveResult]]] = None,
        default: Optional[PrimitiveResult] = None,
    ) -> None:
        self.host = "test-host"
        self.script: Dict[str, List[PrimitiveResult]] = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands: List[str] = []
        self.reboots = 0

    def _resp(self, command: str) -> PrimitiveResult:
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command: str, **opts: Any) -> PrimitiveResult:
        self.commands.append(command)
        return self._resp(command)

    def run_local(self, command: str) -> PrimitiveResult:
        return _ok()

    def reboot(self, **kw: Any) -> PrimitiveResult:
        self.reboots += 1
        return _ok()

    def node_post_upgrade(self, *, nvidia_host: bool = False) -> PrimitiveResult:
        self.commands.append(f"node_post_upgrade nvidia_host={nvidia_host}")
        return self._resp("vmlinuz")


@pytest.fixture()
def inventory_path(tmp_path: Path) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text("[custom_hosts]\n")  # empty, populated per test
    return str(p)


def _write_inventory(tmp_path: Path, lines: str) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(f"[custom_hosts]\n{lines}\n")
    return str(p)


def _write_config(tmp_path: Path, name: str, data: dict) -> None:
    d = tmp_path / "configs"
    d.mkdir(exist_ok=True)
    (d / f"{name}.yml").write_text(yaml.dump(data))


def _settings(tmp_path: Path, **kw: Any) -> GlobalSettings:
    return GlobalSettings(configs_dir=str(tmp_path / "configs"), **kw)


# --- _deep_merge helper -------------------------------------------------------


def test_deep_merge_simple():
    assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


def test_deep_merge_override():
    assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_deep_merge_nested_dicts():
    base = {"hc": {"type": "none", "slug": "old"}}
    override = {"hc": {"type": "kuma"}}
    result = _deep_merge(base, override)
    assert result["hc"]["type"] == "kuma"
    assert result["hc"]["slug"] == "old"


def test_deep_merge_list_replaces():
    base = {"steps": [1, 2, 3]}
    override = {"steps": [4, 5]}
    assert _deep_merge(base, override)["steps"] == [4, 5]


# --- run_custom_phase — dep order abort ---------------------------------------


def test_dep_order_abort_on_problems(tmp_path, monkeypatch):
    inv = _write_inventory(
        tmp_path,
        "app-01 ansible_host=10.0.0.1 custom_config=app\ndb-01 ansible_host=10.0.0.2 custom_config=db",
    )
    # host_vars says app-01 depends_on db-01, but db-01 is AFTER app-01 → error.
    hv = tmp_path / "host_vars"
    hv.mkdir()
    (hv / "app-01.yml").write_text("depends_on:\n  - db-01\n")

    settings = GlobalSettings(
        configs_dir=str(tmp_path / "configs"),
        host_vars_dir=str(hv),
    )
    with pytest.raises(SystemExit) as exc:
        run_custom_phase(settings=settings, inventory_path=inv)
    assert exc.value.code == 1


# --- run_custom_phase — happy path -------------------------------------------


def test_normal_run_writes_state_json(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "version_command": "ver",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "changed_when": {"type": "version"},
            "health_check": {"type": "none"},
        },
    )
    settings = _settings(tmp_path)

    executor = ScriptedExecutor({"ver": [_ok("1.0"), _ok("1.1")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state_path = tmp_path / "out.json"
    state = run_custom_phase(
        settings=settings,
        inventory_path=inv,
        state_output_path=str(state_path),
    )

    assert state.changed is True
    assert state.failed is False
    assert len(state.custom) == 1
    assert "Updated" in state.custom[0].app

    # Verify JSON file exists with fleet_* keys.
    raw = json.loads(state_path.read_text())
    assert "fleet_custom_data" in raw
    assert raw["fleet_changed"] is True


def test_empty_custom_hosts_produces_empty_state(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "")  # empty group
    settings = _settings(tmp_path)
    state_path = tmp_path / "out.json"
    state = run_custom_phase(settings=settings, inventory_path=inv, state_output_path=str(state_path))
    assert state.custom == []
    assert state.changed is False


def test_dry_run_flag_propagated(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "version_command": "ver",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "changed_when": {"type": "version"},
            "health_check": {"type": "none"},
        },
    )
    settings = _settings(tmp_path, fleet_dry_run=True)

    executor = ScriptedExecutor({"ver": [_ok("1.0")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv, state_output_path=str(tmp_path / "out.json"))
    # dry-run: only version command ran, update step did not.
    assert "do-upgrade" not in executor.commands
    assert "dry-run" in state.custom[0].app


def test_extra_vars_fleet_dry_run_propagated(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "version_command": "ver",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "changed_when": {"type": "version"},
            "health_check": {"type": "none"},
        },
    )
    settings = _settings(tmp_path)

    executor = ScriptedExecutor({"ver": [_ok("1.0")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(
        settings=settings,
        inventory_path=inv,
        extra_vars={"fleet_dry_run": "true"},
        state_output_path=str(tmp_path / "out.json"),
    )
    assert "dry-run" in state.custom[0].app


def test_extra_vars_false_string_is_not_truthy(tmp_path, monkeypatch):
    """-e fleet_dry_run=false arrives as the string 'false' — it must NOT put
    the phase in dry-run (bool('false') is True; _truthy must not be)."""
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "version_command": "ver",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "changed_when": {"type": "version"},
            "health_check": {"type": "none"},
        },
    )
    settings = _settings(tmp_path)

    executor = ScriptedExecutor({"ver": [_ok("1.0"), _ok("1.1")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(
        settings=settings,
        inventory_path=inv,
        extra_vars={"fleet_dry_run": "false", "custom_dry_run": "no", "force_window": "0"},
        state_output_path=str(tmp_path / "out.json"),
    )
    # Real run: the update step executed, status is not dry-run.
    assert "do-upgrade" in executor.commands
    assert "dry-run" not in state.custom[0].app


def test_check_puts_custom_phase_in_dry_run(tmp_path, monkeypatch):
    """check=True must imply dry-run (run_shell primitives have check_mode: false,
    so without this the custom phase would execute real update steps)."""
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "version_command": "ver",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "changed_when": {"type": "version"},
            "health_check": {"type": "none"},
        },
    )
    settings = _settings(tmp_path)

    executor = ScriptedExecutor({"ver": [_ok("1.0")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(
        settings=settings, inventory_path=inv, check=True, state_output_path=str(tmp_path / "out.json")
    )
    assert "do-upgrade" not in executor.commands
    assert "dry-run" in state.custom[0].app


def test_failed_host_recorded_in_state(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "version_command": "ver",
            "update_steps": [{"name": "fail-step", "command": "bad-cmd"}],
            "changed_when": {"type": "version"},
            "health_check": {"type": "none"},
        },
    )
    settings = _settings(tmp_path)

    executor = ScriptedExecutor(
        {
            "ver": [_ok("1.0")],
            "bad-cmd": [_fail()],
        }
    )
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv, state_output_path=str(tmp_path / "out.json"))
    assert state.failed is True
    assert state.custom[0].app == "FAILED"
    assert len(state.errors) == 1


def test_window_skip_outside_window(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "health_check": {"type": "none"},
        },
    )
    # Set maintenance_window in host_vars.
    hv = tmp_path / "host_vars"
    hv.mkdir()
    (hv / "gitea.yml").write_text("maintenance_window:\n  days: [Sat]\n  start: '02:00'\n  end: '04:00'\n  tz: UTC\n")
    settings = GlobalSettings(
        configs_dir=str(tmp_path / "configs"),
        host_vars_dir=str(hv),
    )

    executor = ScriptedExecutor()
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    # Use a fixed "now" that is outside the Saturday window (it's a Monday).
    from datetime import datetime, timezone

    monday_noon = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # Monday
    monkeypatch.setattr(
        "proxmox_fleet.window.datetime",
        type(
            "FakeDT",
            (),
            {
                "now": staticmethod(lambda **kw: monday_noon),
            },
        ),
    )

    state = run_custom_phase(settings=settings, inventory_path=inv, state_output_path=str(tmp_path / "out.json"))
    # Host was skipped → no record, no commands run.
    assert state.custom == []
    assert executor.commands == []


# --- run_node_phase helpers ---------------------------------------------------

APT_UPGRADED = "3 upgraded, 0 newly installed, 0 to remove.\n"
APT_NOOP = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
NOT_MANAGER = _ok(stdout="1\n", changed=False)  # pct list grep → not manager
NO_REBOOT = _ok(
    changed=False,
    facts={
        "diagnostics_version": 1,
        "running_kernel_rc": 0,
        "running_kernel": "6.8.12-8-pve",
        "latest_kernel_rc": 0,
        "latest_kernel": "6.8.12-8-pve",
        "reboot_required_exists": False,
        "reboot_required_packages": "",
        "nvidia_checked": False,
    },
)


def _node_inventory(tmp_path: Path, node_lines: str) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(f"[proxmox_nodes]\n{node_lines}\n")
    return str(p)


def _node_settings(**kw) -> GlobalSettings:
    return GlobalSettings(manager_lxc_id="121", node_apt_retry_delay=0, **kw)


class ScriptedNodeExecutor(ScriptedExecutor):
    """ScriptedExecutor that also handles reboot() calls."""

    def snapshot(self, *a, **kw):
        raise AssertionError("snapshot should not be called for node phase")


def _make_node_executor(script=None):
    """Return an executor that answers the standard node-update command sequence."""
    base = {
        "pct list": [NOT_MANAGER],
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "vmlinuz": [NO_REBOOT],
    }
    if script:
        base.update(script)
    return ScriptedNodeExecutor(base)


# --- run_node_phase — happy path ---------------------------------------------


def test_node_phase_all_ok_writes_state_json(tmp_path, monkeypatch):
    """Two nodes, both idle → state JSON contains 2 node records + manager."""
    inv = _node_inventory(
        tmp_path,
        "pve-01 ansible_host=10.0.0.1\npve-02 ansible_host=10.0.0.2",
    )
    mgr_executor = ScriptedNodeExecutor(
        {
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(stdout="ok\n", changed=False)],
        }
    )

    def _fake_executor(host, **kw):
        ex = mgr_executor if host == "localhost" else _make_node_executor()
        ex.host = host
        return ex

    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    state_path = tmp_path / "out.json"
    state = run_node_phase(
        settings=_node_settings(),
        inventory_path=inv,
        check=False,
        state_output_path=str(state_path),
    )

    assert state.failed is False
    assert state.changed is False
    # 2 nodes + 1 manager
    assert len(state.node) == 3
    assert all(r.status == "OK" for r in state.node)
    assert state.node[-1].node == "Ansible-Manager"

    raw = json.loads(state_path.read_text())
    assert "fleet_node_data" in raw
    assert len(raw["fleet_node_data"]) == 3
    assert raw["fleet_changed"] is False


def test_node_phase_state_json_has_fleet_keys(tmp_path, monkeypatch):
    """dump_for_ansible() uses fleet_* key names, not short names."""
    inv = _node_inventory(tmp_path, "pve-01 ansible_host=10.0.0.1")
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: _make_node_executor())

    state_path = tmp_path / "out.json"
    run_node_phase(settings=_node_settings(), inventory_path=inv, check=False, state_output_path=str(state_path))

    raw = json.loads(state_path.read_text())
    for key in ("fleet_node_data", "fleet_changed", "fleet_failed", "fleet_error_log", "fleet_warning_log"):
        assert key in raw, f"missing key: {key}"


def test_node_phase_passes_nvidia_host_from_inventory(tmp_path, monkeypatch):
    from proxmox_fleet.flows.node import NodeFlowOutcome
    from proxmox_fleet.models.state import NodeRecord

    inv = _node_inventory(
        tmp_path,
        "pve-01 ansible_host=10.0.0.1 nvidia_host=true",
    )
    captured = {}

    def fake_update(node, executor, settings, **kwargs):
        captured.update(kwargs)
        return NodeFlowOutcome(record=NodeRecord(node=node, status="OK"))

    monkeypatch.setattr(driver_mod, "run_node_update", fake_update)
    state = run_node_phase(
        settings=_node_settings(),
        inventory_path=inv,
        include_manager=False,
        state_output_path=None,
    )

    assert captured["nvidia_host"] is True
    assert [record.node for record in state.node] == ["pve-01"]


def test_nvidia_node_failure_keeps_serial_abort_behavior(tmp_path, monkeypatch):
    from proxmox_fleet.flows.node import NodeFlowOutcome
    from proxmox_fleet.models.state import ErrorEntry, NodeRecord

    inv = _node_inventory(
        tmp_path,
        "pve-01 ansible_host=10.0.0.1 nvidia_host=true\n"
        "pve-02 ansible_host=10.0.0.2",
    )
    visited = []

    def fake_update(node, executor, settings, **kwargs):
        visited.append(node)
        if kwargs["nvidia_host"]:
            return NodeFlowOutcome(
                record=NodeRecord(node=node, status="FAILED"),
                failed=True,
                error=ErrorEntry(host=node, task="NVIDIA post-upgrade check", error="missing DKMS"),
            )
        return NodeFlowOutcome(record=NodeRecord(node=node, status="OK"))

    monkeypatch.setattr(driver_mod, "run_node_update", fake_update)
    state = run_node_phase(
        settings=_node_settings(),
        inventory_path=inv,
        include_manager=False,
        state_output_path=None,
    )

    assert state.failed
    assert visited == ["pve-01"]
    assert [record.node for record in state.node] == ["pve-01"]


def test_node_phase_dry_run_propagated(tmp_path, monkeypatch):
    """fleet_dry_run=True sends apt-get -s (not -y) to nodes."""
    inv = _node_inventory(tmp_path, "pve-01 ansible_host=10.0.0.1")
    executor = _make_node_executor()
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    run_node_phase(
        settings=_node_settings(fleet_dry_run=True),
        inventory_path=inv,
        check=False,
        state_output_path=str(tmp_path / "out.json"),
    )

    apt_cmds = [c for c in executor.commands if "dist-upgrade" in c]
    assert apt_cmds, "expected an apt command"
    assert all("-s" in c for c in apt_cmds), "dry-run should use apt-get -s"


# --- run_node_phase — failure / abort ----------------------------------------


def test_node_phase_failure_aborts_remaining_nodes(tmp_path, monkeypatch):
    """First node fails → second node is not processed; manager still runs."""
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip retry delays

    inv = _node_inventory(
        tmp_path,
        "pve-01 ansible_host=10.0.0.1\npve-02 ansible_host=10.0.0.2",
    )
    # default=_fail() so every apt attempt fails, exhausting all retries.
    fail_executor = ScriptedNodeExecutor({"pct list": [NOT_MANAGER]}, default=_fail())
    pve02_executor = _make_node_executor()
    mgr_executor = ScriptedNodeExecutor(
        {
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(stdout="ok\n", changed=False)],
        }
    )

    dispatch = {"pve-01": fail_executor, "pve-02": pve02_executor, "localhost": mgr_executor}

    def _fake(host, **kw):
        ex = dispatch.get(host, _make_node_executor())
        ex.host = host
        return ex

    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake)

    state = run_node_phase(
        settings=_node_settings(),
        inventory_path=inv,
        check=False,
        state_output_path=str(tmp_path / "out.json"),
    )

    assert state.failed is True
    assert len(state.errors) == 1

    node_names = [r.node for r in state.node]
    assert "pve-01" in node_names
    assert "pve-02" not in node_names  # aborted
    assert "Ansible-Manager" in node_names  # always runs

    # pve-02 executor was never called.
    assert pve02_executor.commands == []


def test_node_phase_failure_recorded_in_state(tmp_path, monkeypatch):
    """A failed node produces a FAILED record and sets fleet_failed."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    inv = _node_inventory(tmp_path, "pve-01 ansible_host=10.0.0.1")
    fail_executor = ScriptedNodeExecutor({"pct list": [NOT_MANAGER]}, default=_fail())
    mgr_executor = ScriptedNodeExecutor(
        {
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(stdout="ok\n", changed=False)],
        }
    )
    dispatch = {"pve-01": fail_executor, "localhost": mgr_executor}

    def _fake(host, **kw):
        ex = dispatch.get(host, _make_node_executor())
        ex.host = host
        return ex

    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake)

    state_path = tmp_path / "out.json"
    state = run_node_phase(
        settings=_node_settings(), inventory_path=inv, check=False, state_output_path=str(state_path)
    )

    assert state.failed is True
    failed = [r for r in state.node if r.node == "pve-01"]
    assert len(failed) == 1
    assert failed[0].status == "FAILED"

    raw = json.loads(state_path.read_text())
    assert raw["fleet_failed"] is True


def test_node_phase_empty_inventory_still_runs_manager(tmp_path, monkeypatch):
    """No [proxmox_nodes] entries → only manager record in state."""
    inv = _node_inventory(tmp_path, "")  # empty group
    mgr_executor = ScriptedNodeExecutor(
        {
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(stdout="ok\n", changed=False)],
        }
    )
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: mgr_executor)

    state = run_node_phase(
        settings=_node_settings(), inventory_path=inv, check=False, state_output_path=str(tmp_path / "out.json")
    )

    assert state.failed is False
    assert len(state.node) == 1
    assert state.node[0].node == "Ansible-Manager"


# --- run_custom_phase (existing) -----------------------------------------------


def test_dep_failed_propagates_to_next_host(tmp_path, monkeypatch):
    inv = _write_inventory(
        tmp_path,
        "db-01 ansible_host=10.0.0.1 custom_config=db\napp-01 ansible_host=10.0.0.2 custom_config=app",
    )
    # db fails; app depends_on db.
    hv = tmp_path / "host_vars"
    hv.mkdir()
    (hv / "app-01.yml").write_text("depends_on:\n  - db-01\n")

    _write_config(
        tmp_path,
        "db",
        {
            "name": "DB",
            "update_steps": [{"name": "fail-step", "command": "bad-db"}],
            "health_check": {"type": "none"},
        },
    )
    _write_config(
        tmp_path,
        "app",
        {
            "name": "App",
            "update_steps": [{"name": "upgrade", "command": "do-app"}],
            "health_check": {"type": "none"},
        },
    )

    settings = GlobalSettings(
        configs_dir=str(tmp_path / "configs"),
        host_vars_dir=str(hv),
    )

    executor = ScriptedExecutor({"bad-db": [_fail()]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv, state_output_path=str(tmp_path / "out.json"))
    assert state.failed is True
    # app-01 should have a warning (dep skip), not a FAILED record.
    assert any("dependency" in w.warning.lower() for w in state.warnings)
    assert "do-app" not in executor.commands


# --------------------------------------------------------------------------- #
# run_notify_phase — Phase 4 (briefing / history / notifiers)
# --------------------------------------------------------------------------- #


def _notify_state(**kw) -> FleetState:
    return FleetState.from_raw(kw)


def _patch_notifiers(monkeypatch):
    """Capture dispatch / ping_deadmans calls made by the driver."""
    calls: Dict[str, List[Any]] = {"dispatch": [], "ping": []}
    monkeypatch.setattr(
        "proxmox_fleet.driver.notifiers.dispatch",
        lambda nl, **kw: calls["dispatch"].append((nl, kw)),
    )
    monkeypatch.setattr(
        "proxmox_fleet.driver.notifiers.ping_deadmans",
        lambda url, **kw: calls["ping"].append((url, kw)),
    )
    return calls


def test_notify_phase_dispatches_and_writes_history(tmp_path, monkeypatch):
    calls = _patch_notifiers(monkeypatch)
    state = _notify_state(
        fleet_changed=True,
        fleet_node_data=[{"node": "pve-01", "status": "OK"}],
        fleet_lxc_data=[
            {"node": "pve-01", "name": "sonarr", "id": "101", "app": "Updated: v4.0 → v4.1", "os": "OK", "snap": True}
        ],
    )
    settings = GlobalSettings(
        discord_webhook="https://d/hook",
        fleet_history_dir=str(tmp_path),
        fleet_deadmans_url="https://hc.io/abc",
    )

    body = run_notify_phase(settings=settings, state=state)

    # dispatched once, to the synthesized discord notifier, with the rendered body
    assert len(calls["dispatch"]) == 1
    notifier_list, kw = calls["dispatch"][0]
    assert notifier_list == [{"type": "discord", "enabled": True, "webhook": "https://d/hook"}]
    assert kw["body"] == body
    assert kw["title"] == "✅ Briefing: All Systems Clear"
    assert kw["color"] == 3066993

    # dead-man pinged
    assert calls["ping"][0][0] == "https://hc.io/abc"
    assert calls["ping"][0][1]["failed"] is False

    # history written, carrying the exact briefing body
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["briefing"] == body
    assert "sonarr" in latest["briefing"]


def test_notify_phase_suppressed_when_idle_but_history_still_written(tmp_path, monkeypatch):
    calls = _patch_notifiers(monkeypatch)
    state = _notify_state()  # nothing changed/failed
    settings = GlobalSettings(discord_webhook="https://d/hook", fleet_history_dir=str(tmp_path))

    body = run_notify_phase(settings=settings, state=state)

    assert calls["dispatch"] == []  # should_notify is False
    # history still records the (empty) briefing
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert latest["briefing"] == body


def test_notify_phase_force_notify_overrides_idle(tmp_path, monkeypatch):
    calls = _patch_notifiers(monkeypatch)
    settings = GlobalSettings(discord_webhook="https://d/hook", fleet_history_dir=str(tmp_path), force_notify=True)
    run_notify_phase(settings=settings, state=_notify_state())
    assert len(calls["dispatch"]) == 1


def test_notify_phase_history_disabled(tmp_path, monkeypatch):
    _patch_notifiers(monkeypatch)
    settings = GlobalSettings(fleet_history_enabled=False, fleet_history_dir=str(tmp_path))
    run_notify_phase(settings=settings, state=_notify_state(fleet_changed=True))
    assert not (tmp_path / "latest.json").exists()


def test_notify_phase_failed_state_titles(tmp_path, monkeypatch):
    calls = _patch_notifiers(monkeypatch)
    settings = GlobalSettings(discord_webhook="https://d/hook", fleet_history_dir=str(tmp_path))
    run_notify_phase(settings=settings, state=_notify_state(fleet_failed=True))
    _, kw = calls["dispatch"][0]
    assert kw["title"] == "⚠️ Briefing: Failures Detected"
    assert kw["ntfy_title"] == "Fleet Update: Failures Detected"
    assert kw["color"] == 15158332
    assert calls["ping"][0][1]["failed"] is True


def test_notify_phase_passes_package_detail_keep_to_history(tmp_path, monkeypatch):
    """PR1: run_notify_phase threads fleet_package_detail_keep into
    history.write_history's keep_detail argument."""
    _patch_notifiers(monkeypatch)
    captured: Dict[str, Any] = {}
    monkeypatch.setattr(
        driver_mod.history,
        "write_history",
        lambda state, **kw: captured.update(kw),
    )
    settings = GlobalSettings(fleet_history_dir=str(tmp_path), fleet_package_detail_keep=3)
    body = run_notify_phase(settings=settings, state=_notify_state(fleet_changed=True))
    assert captured["keep_detail"] == 3
    assert captured["keep"] == settings.fleet_history_keep
    assert captured["history_dir"] == str(tmp_path)
    assert captured["briefing"] == body


def test_notify_phase_package_detail_keep_default_from_settings(tmp_path, monkeypatch):
    """PR1: with fleet_package_detail_keep unset, the settings default (7) still
    flows through — the driver must never hardcode its own value."""
    _patch_notifiers(monkeypatch)
    captured: Dict[str, Any] = {}
    monkeypatch.setattr(
        driver_mod.history,
        "write_history",
        lambda state, **kw: captured.update(kw),
    )
    run_notify_phase(settings=GlobalSettings(fleet_history_dir=str(tmp_path)), state=_notify_state(fleet_changed=True))
    assert captured["keep_detail"] == GlobalSettings().fleet_package_detail_keep


# --- _merge_state -------------------------------------------------------------


def test_merge_state_concatenates_and_or_joins():
    dst = FleetState.from_raw(
        {
            "fleet_lxc_data": [{"node": "n1", "name": "a", "id": "1", "app": "OK"}],
            "fleet_changed": True,
            "fleet_failed": False,
            "fleet_error_log": [{"host": "h1", "task": "t", "error": "e"}],
        }
    )
    src = FleetState.from_raw(
        {
            "fleet_lxc_data": [{"node": "n2", "name": "b", "id": "2", "app": "OK"}],
            "fleet_vm_data": [{"node": "n2", "vmid": "9", "name": "vm", "status": "OK"}],
            "fleet_changed": False,
            "fleet_failed": True,
            "fleet_warning_log": [{"host": "h2", "task": "u", "warning": "w"}],
        }
    )
    _merge_state(dst, src)
    assert [r.id for r in dst.lxc] == ["1", "2"]
    assert [r.vmid for r in dst.vm] == ["9"]
    assert dst.changed is True  # True or False
    assert dst.failed is True  # False or True
    assert len(dst.errors) == 1 and len(dst.warnings) == 1


# --- run_fleet orchestrator ---------------------------------------------------


def _stub_phases(monkeypatch, *, remote=None, custom=None, lxc=None, vm=None, node=None):
    def mk(state):
        def _fn(**kwargs):
            return state if state is not None else FleetState()

        return _fn

    monkeypatch.setattr(driver_mod, "run_remote_phase", mk(remote))
    monkeypatch.setattr(driver_mod, "run_custom_phase", mk(custom))
    monkeypatch.setattr(driver_mod, "run_lxc_phase", mk(lxc))
    monkeypatch.setattr(driver_mod, "run_vm_phase", mk(vm))
    monkeypatch.setattr(driver_mod, "run_node_phase", mk(node))


def test_run_fleet_merges_all_phases(monkeypatch):
    captured = {}

    def _notify(*, settings, state, check=False):
        captured["state"] = state
        return "body"

    monkeypatch.setattr(driver_mod, "run_notify_phase", _notify)
    _stub_phases(
        monkeypatch,
        remote=FleetState.from_raw({"fleet_remote_data": [{"host": "r", "status": "OK"}]}),
        node=FleetState.from_raw({"fleet_node_data": [{"node": "n", "status": "OK"}]}),
    )
    settings = GlobalSettings(apt_proxy_ip="")  # skip pre-flight
    rc = run_fleet(settings=settings, inventory_path="x", check=True)
    assert rc == 0
    assert len(captured["state"].remote) == 1
    assert len(captured["state"].node) == 1


def test_run_fleet_returns_1_on_failure(monkeypatch):
    monkeypatch.setattr(driver_mod, "run_notify_phase", lambda **kw: "body")
    _stub_phases(monkeypatch, lxc=FleetState.from_raw({"fleet_failed": True}))
    rc = run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x")
    assert rc == 1


def test_run_fleet_preflight_abort(monkeypatch):
    def _boom(host, port, **kw):
        raise TimeoutError("nope")

    monkeypatch.setattr(driver_mod.http, "wait_for_port", _boom)
    monkeypatch.setattr(driver_mod, "run_notify_phase", lambda **kw: "body")
    _stub_phases(monkeypatch)
    with pytest.raises(SystemExit):
        run_fleet(settings=GlobalSettings(apt_proxy_ip="10.0.0.1", apt_proxy_port=3142), inventory_path="x")


# --- run_fleet — --phases / --limit gating -------------------------------------


def _record_phases(monkeypatch):
    """Stub every phase helper, recording (phase, selected kwargs) per call."""
    calls: List[Any] = []

    def mk(name):
        def _fn(**kwargs):
            calls.append(
                (name, {k: kwargs.get(k) for k in ("limit", "include_nodes", "include_manager") if k in kwargs})
            )
            return FleetState()

        return _fn

    monkeypatch.setattr(driver_mod, "run_remote_phase", mk("remote"))
    monkeypatch.setattr(driver_mod, "run_custom_phase", mk("custom"))
    monkeypatch.setattr(driver_mod, "run_lxc_phase", mk("lxc"))
    monkeypatch.setattr(driver_mod, "run_vm_phase", mk("vm"))
    monkeypatch.setattr(driver_mod, "run_node_phase", mk("node"))
    monkeypatch.setattr(driver_mod, "run_notify_phase", lambda **kw: "body")
    return calls


def test_run_fleet_phases_none_runs_everything(monkeypatch):
    calls = _record_phases(monkeypatch)
    run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x")
    assert [c[0] for c in calls] == ["remote", "custom", "lxc", "vm", "node"]
    node_kwargs = calls[-1][1]
    assert node_kwargs["include_nodes"] is True
    assert node_kwargs["include_manager"] is True


def test_run_fleet_phases_subset(monkeypatch):
    calls = _record_phases(monkeypatch)
    run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x", phases={"lxc", "vm"})
    assert [c[0] for c in calls] == ["lxc", "vm"]


def test_run_fleet_phases_node_without_manager(monkeypatch):
    calls = _record_phases(monkeypatch)
    run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x", phases={"node"})
    assert [c[0] for c in calls] == ["node"]
    assert calls[0][1]["include_nodes"] is True
    assert calls[0][1]["include_manager"] is False


def test_run_fleet_phases_manager_only(monkeypatch):
    calls = _record_phases(monkeypatch)
    run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x", phases={"manager"})
    assert [c[0] for c in calls] == ["node"]
    assert calls[0][1]["include_nodes"] is False
    assert calls[0][1]["include_manager"] is True


def test_run_fleet_unknown_phase_exits(monkeypatch):
    _record_phases(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x", phases={"lxc", "bogus"})
    assert "bogus" in str(exc.value)


def test_run_fleet_limit_threaded_to_phases(monkeypatch):
    calls = _record_phases(monkeypatch)
    run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x", limit={"pve-01", "105"})
    assert all(c[1]["limit"] == {"pve-01", "105"} for c in calls)


def test_run_fleet_notify_runs_even_with_phase_subset(monkeypatch):
    notified = {}
    _stub_phases(monkeypatch)
    monkeypatch.setattr(driver_mod, "run_notify_phase", lambda **kw: notified.setdefault("called", True))
    run_fleet(settings=GlobalSettings(apt_proxy_ip=""), inventory_path="x", phases={"remote"})
    assert notified.get("called") is True


# --- per-phase limit filtering --------------------------------------------------


def test_custom_phase_limit_filters_hosts(tmp_path, monkeypatch):
    inv = _write_inventory(
        tmp_path,
        "gitea ansible_host=10.0.0.1 custom_config=gitea\nwiki ansible_host=10.0.0.2 custom_config=wiki",
    )
    for name in ("gitea", "wiki"):
        _write_config(
            tmp_path,
            name,
            {
                "name": name,
                "version_command": "ver",
                "update_steps": [{"name": "upgrade", "command": f"do-{name}"}],
                "changed_when": {"type": "version"},
                "health_check": {"type": "none"},
            },
        )
    settings = _settings(tmp_path)

    executor = ScriptedExecutor({"ver": [_ok("1.0"), _ok("1.1")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv, state_output_path=None, limit={"gitea"})
    assert [r.name for r in state.custom] == ["gitea"]
    assert "do-wiki" not in executor.commands


def test_node_phase_limit_filters_nodes_and_skips_manager(tmp_path, monkeypatch):
    inv = _node_inventory(
        tmp_path,
        "pve-01 ansible_host=10.0.0.1\npve-02 ansible_host=10.0.0.2",
    )
    pve02_executor = _make_node_executor()
    mgr_executor = ScriptedNodeExecutor(
        {
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(stdout="ok\n", changed=False)],
        }
    )
    dispatch = {"pve-02": pve02_executor, "localhost": mgr_executor}

    def _fake(host, **kw):
        return dispatch.get(host, _make_node_executor())

    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake)

    state = run_node_phase(
        settings=_node_settings(), inventory_path=inv, check=False, state_output_path=None, limit={"pve-01"}
    )

    node_names = [r.node for r in state.node]
    assert node_names == ["pve-01"]  # pve-02 filtered out
    assert pve02_executor.commands == []
    assert mgr_executor.commands == []  # manager not in limit → skipped


def test_node_phase_limit_manager_token_runs_manager(tmp_path, monkeypatch):
    inv = _node_inventory(tmp_path, "pve-01 ansible_host=10.0.0.1")
    mgr_executor = ScriptedNodeExecutor(
        {
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(stdout="ok\n", changed=False)],
        }
    )
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda host, **kw: mgr_executor)

    state = run_node_phase(
        settings=_node_settings(), inventory_path=inv, check=False, state_output_path=None, limit={"manager"}
    )

    # Nodes filtered out entirely; only the manager record remains.
    assert [r.node for r in state.node] == ["Ansible-Manager"]


def _include_flag_fixtures(tmp_path, monkeypatch):
    inv = _node_inventory(tmp_path, "pve-01 ansible_host=10.0.0.1")
    node_executor = _make_node_executor()
    mgr_executor = ScriptedNodeExecutor(
        {
            "dist-upgrade": [_ok(stdout=APT_NOOP)],
            "reboot-required": [_ok(stdout="ok\n", changed=False)],
        }
    )
    dispatch = {"pve-01": node_executor, "localhost": mgr_executor}
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor", lambda host, **kw: dispatch.get(host, _make_node_executor())
    )
    return inv, node_executor, mgr_executor


def test_node_phase_include_manager_false_skips_manager(tmp_path, monkeypatch):
    inv, _node_executor, mgr_executor = _include_flag_fixtures(tmp_path, monkeypatch)
    state = run_node_phase(
        settings=_node_settings(), inventory_path=inv, check=False, state_output_path=None, include_manager=False
    )
    assert [r.node for r in state.node] == ["pve-01"]
    assert mgr_executor.commands == []


def test_node_phase_include_nodes_false_runs_manager_only(tmp_path, monkeypatch):
    inv, node_executor, _mgr_executor = _include_flag_fixtures(tmp_path, monkeypatch)
    state = run_node_phase(
        settings=_node_settings(), inventory_path=inv, check=False, state_output_path=None, include_nodes=False
    )
    assert [r.node for r in state.node] == ["Ansible-Manager"]
    assert node_executor.commands == []


def test_remote_phase_limit_filters_hosts(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text("[remote_hosts]\nweb-01 ansible_host=10.0.0.1\nweb-02 ansible_host=10.0.0.2\n")

    ran: List[str] = []

    def _fake_remote_update(name, ex, settings, **kw):
        ran.append(name)
        from proxmox_fleet.flows.remote import RemoteFlowOutcome

        return RemoteFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_remote_update", _fake_remote_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    driver_mod.run_remote_phase(
        settings=GlobalSettings(), inventory_path=str(p), state_output_path=None, limit={"web-02"}
    )
    assert ran == ["web-02"]


def test_vm_phase_limit_matches_name_or_vmid(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\n"
        "[proxmox_vms]\n"
        "media-vm ansible_host=10.0.1.1 vmid=200 pve_node=pve-01\n"
        "db-vm ansible_host=10.0.1.2 vmid=201 pve_node=pve-01\n"
        "game-vm ansible_host=10.0.1.3 vmid=202 pve_node=pve-01\n"
    )

    ran: List[str] = []

    def _fake_vm_update(node_name, vmid, name, vm_ex, node_ex, settings, **kw):
        ran.append(name)
        from proxmox_fleet.flows.vm import VmFlowOutcome

        return VmFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_vm_update", _fake_vm_update)
    monkeypatch.setattr(driver_mod, "_discover_vm_locations", lambda *a, **kw: {})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    # Limit by inventory name AND by vmid — both must match.
    driver_mod.run_vm_phase(
        settings=GlobalSettings(), inventory_path=str(p), state_output_path=None, limit={"media-vm", "201"}
    )
    assert sorted(ran) == ["db-vm", "media-vm"]


def test_lxc_phase_limit_node_name_keeps_all_ids(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\npve-02 ansible_host=10.0.0.2\n")

    discovered: List[str] = []
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        discovered.append(ex.host)
        return ["101", "102"]

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(f"{node_name}/{lxc_id}")
        from proxmox_fleet.flows.lxc import LxcFlowOutcome

        return LxcFlowOutcome()

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=str(p), state_output_path=None, limit={"pve-01"})

    # Only pve-01 was discovered (pve-02 skipped before SSH); all its ids ran.
    assert discovered == ["pve-01"]
    assert sorted(ran) == ["pve-01/101", "pve-01/102"]


def test_lxc_phase_limit_by_container_id(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\npve-02 ansible_host=10.0.0.2\n")

    per_node_ids = {"pve-01": ["101", "102"], "pve-02": ["201"]}
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        return per_node_ids[ex.host]

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(f"{node_name}/{lxc_id}")
        from proxmox_fleet.flows.lxc import LxcFlowOutcome

        return LxcFlowOutcome()

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    # A bare id targets the container wherever it lives — both nodes get
    # discovered (we can't know which node holds 201 in advance).
    driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=str(p), state_output_path=None, limit={"201"})
    assert ran == ["pve-02/201"]


# --- run_custom_phase — PVE snapshot wiring (v2) --------------------------------


def _pve_custom_inventory(tmp_path: Path) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\npve-01 ansible_host=10.0.0.9\n"
        "[custom_hosts]\ngitea ansible_host=10.0.0.1 custom_config=gitea\n"
    )
    return str(p)


def test_custom_phase_passes_snapshot_wiring(tmp_path, monkeypatch):
    inv = _pve_custom_inventory(tmp_path)
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "health_check": {"type": "none"},
            "pve_vmid": 105,
            "pve_node": "pve-01",
        },
    )
    settings = _settings(
        tmp_path,
        pve_api_user="root@pam",
        pve_api_token_id="tk",
        pve_api_token_secret="sec",
        snapshot_retries=2,
        snapshot_retry_delay=1.5,
    )

    captured: Dict[str, Any] = {}

    def _fake_update(host, config, executor, **kw):
        captured.update(kw, host=host, config=config, executor=executor)
        from proxmox_fleet.flows.custom import CustomFlowOutcome

        return CustomFlowOutcome()

    executors: Dict[str, Any] = {}

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        executors[host] = ex
        return ex

    monkeypatch.setattr(driver_mod, "run_custom_update", _fake_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    run_custom_phase(settings=settings, inventory_path=inv, state_output_path=None)

    assert captured["api_params"] == {
        "api_host": "10.0.0.9",
        "api_user": "root@pam",
        "api_token_id": "tk",
        "api_token_secret": "sec",
    }
    assert captured["node_executor"] is executors["pve-01"]
    assert captured["snapshot_retries"] == 2
    assert captured["snapshot_retry_delay"] == 1.5


def _pve_custom_inventory_multi_cluster(tmp_path: Path) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\n"
        "pve-01 ansible_host=10.0.0.9\n"
        "pve-02 ansible_host=10.0.0.10 cluster=beta\n"
        "[custom_hosts]\n"
        "gitea ansible_host=10.0.0.1 custom_config=gitea\n"
    )
    return str(p)


def test_custom_phase_uses_beta_cluster_creds(tmp_path, monkeypatch):
    """Task 3: a config pinned to a beta-cluster node picks up that
    cluster's pve_clusters override instead of the global pve_api_* creds."""
    inv = _pve_custom_inventory_multi_cluster(tmp_path)
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "health_check": {"type": "none"},
            "pve_vmid": 105,
            "pve_node": "pve-02",
        },
    )
    settings = _settings(
        tmp_path,
        pve_api_user="root@pam",
        pve_api_token_id="tk",
        pve_api_token_secret="sec",
        pve_clusters={
            "beta": {
                "pve_api_user": "beta-user@pve",
                "pve_api_token_id": "beta-tok",
                "pve_api_token_secret": "beta-sec",
            }
        },
    )

    captured: Dict[str, Any] = {}

    def _fake_update(host, config, executor, **kw):
        captured.update(kw, host=host, config=config, executor=executor)
        from proxmox_fleet.flows.custom import CustomFlowOutcome

        return CustomFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_custom_update", _fake_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda host, **kw: ScriptedExecutor())

    run_custom_phase(settings=settings, inventory_path=inv, state_output_path=None)

    assert captured["api_params"] == {
        "api_host": "10.0.0.10",
        "api_user": "beta-user@pve",
        "api_token_id": "beta-tok",
        "api_token_secret": "beta-sec",
    }


def test_custom_phase_default_cluster_node_uses_globals_with_pve_clusters_set(tmp_path, monkeypatch):
    """A node with no cluster= (DEFAULT_CLUSTER) is unaffected by an unrelated
    beta override — back-compat within a mixed-cluster fleet."""
    inv = _pve_custom_inventory_multi_cluster(tmp_path)
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "health_check": {"type": "none"},
            "pve_vmid": 105,
            "pve_node": "pve-01",
        },
    )
    settings = _settings(
        tmp_path,
        pve_api_user="root@pam",
        pve_api_token_id="tk",
        pve_api_token_secret="sec",
        pve_clusters={
            "beta": {"pve_api_token_secret": "beta-sec"},
        },
    )

    captured: Dict[str, Any] = {}

    def _fake_update(host, config, executor, **kw):
        captured.update(kw, host=host, config=config, executor=executor)
        from proxmox_fleet.flows.custom import CustomFlowOutcome

        return CustomFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_custom_update", _fake_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda host, **kw: ScriptedExecutor())

    run_custom_phase(settings=settings, inventory_path=inv, state_output_path=None)

    assert captured["api_params"] == {
        "api_host": "10.0.0.9",
        "api_user": "root@pam",
        "api_token_id": "tk",
        "api_token_secret": "sec",
    }


def test_custom_phase_no_snapshot_wiring_without_pve_vmid(tmp_path, monkeypatch):
    inv = _pve_custom_inventory(tmp_path)
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "health_check": {"type": "none"},
        },
    )
    captured: Dict[str, Any] = {}

    def _fake_update(host, config, executor, **kw):
        captured.update(kw)
        from proxmox_fleet.flows.custom import CustomFlowOutcome

        return CustomFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_custom_update", _fake_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    run_custom_phase(settings=_settings(tmp_path), inventory_path=inv, state_output_path=None)

    assert captured["node_executor"] is None
    assert captured["api_params"] is None


def test_custom_phase_unknown_pve_node_fails_loud(tmp_path, monkeypatch):
    inv = _pve_custom_inventory(tmp_path)
    _write_config(
        tmp_path,
        "gitea",
        {
            "name": "Gitea",
            "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
            "health_check": {"type": "none"},
            "pve_vmid": "105",
            "pve_node": "pve-99",  # not in [proxmox_nodes]
        },
    )
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    with pytest.raises(SystemExit) as exc:
        run_custom_phase(settings=_settings(tmp_path), inventory_path=inv, state_output_path=None)
    assert exc.value.code == 1


# --- canary / staged rollout ----------------------------------------------------


def _remote_inventory(tmp_path: Path) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[remote_hosts]\n"
        "canary-01 ansible_host=10.0.2.1 canary=true\n"
        "web-01 ansible_host=10.0.2.2\n"
        "web-02 ansible_host=10.0.2.3\n"
    )
    return str(p)


def _patch_remote_flow(monkeypatch, *, fail_hosts=()):
    """Stub run_remote_update, recording the order hosts ran in."""
    ran: List[str] = []

    def _fake(name, ex, settings, **kw):
        ran.append(name)
        from proxmox_fleet.flows.remote import RemoteFlowOutcome
        from proxmox_fleet.models.state import ErrorEntry, RemoteRecord

        if name in fail_hosts:
            return RemoteFlowOutcome(
                record=RemoteRecord(host=name, status="FAILED"),
                failed=True,
                error=ErrorEntry(host=name, task="upgrade", error="boom"),
            )
        return RemoteFlowOutcome(record=RemoteRecord(host=name, status="Updated"), changed=True)

    monkeypatch.setattr(driver_mod, "run_remote_update", _fake)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())
    return ran


def test_remote_canary_runs_first_then_rest(tmp_path, monkeypatch):
    ran = _patch_remote_flow(monkeypatch)
    soaked = {}
    monkeypatch.setattr(
        driver_mod, "_soak_canaries", lambda settings, kuma_map, tokens, **kw: soaked.setdefault("tokens", list(tokens))
    )
    monkeypatch.setattr(
        driver_mod,
        "_soak_canaries",
        lambda settings, kuma_map, tokens, **kw: soaked.update(tokens=list(tokens)) or None,
    )

    state = driver_mod.run_remote_phase(
        settings=GlobalSettings(), inventory_path=_remote_inventory(tmp_path), state_output_path=None
    )

    assert ran[0] == "canary-01"  # canary wave first
    assert set(ran[1:]) == {"web-01", "web-02"}
    assert soaked["tokens"] == ["canary-01"]
    assert state.failed is False


def test_remote_canary_failure_skips_rest(tmp_path, monkeypatch):
    ran = _patch_remote_flow(monkeypatch, fail_hosts={"canary-01"})

    state = driver_mod.run_remote_phase(
        settings=GlobalSettings(), inventory_path=_remote_inventory(tmp_path), state_output_path=None
    )

    assert ran == ["canary-01"]  # rest never ran
    skipped = [r for r in state.remote if r.status == "SKIPPED (canary failed)"]
    assert sorted(r.host for r in skipped) == ["web-01", "web-02"]
    assert state.failed is True


def test_remote_canary_soak_failure_skips_rest(tmp_path, monkeypatch):
    ran = _patch_remote_flow(monkeypatch)
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: "canary canary-01 unhealthy after soak: down")

    state = driver_mod.run_remote_phase(
        settings=GlobalSettings(), inventory_path=_remote_inventory(tmp_path), state_output_path=None
    )

    assert ran == ["canary-01"]
    assert state.failed is True
    assert any("unhealthy after soak" in e.error for e in state.errors)
    assert sorted(r.host for r in state.remote if "SKIPPED" in r.status) == ["web-01", "web-02"]


def test_remote_no_canaries_single_wave(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text("[remote_hosts]\nweb-01 ansible_host=10.0.2.2\n")
    ran = _patch_remote_flow(monkeypatch)
    soak_called = {}
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: soak_called.setdefault("yes", True))

    driver_mod.run_remote_phase(settings=GlobalSettings(), inventory_path=str(p), state_output_path=None)

    assert ran == ["web-01"]
    assert soak_called == {}  # no staging → no soak


def test_remote_canary_via_settings_list(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text("[remote_hosts]\nweb-01 ansible_host=1.1.1.1\nweb-02 ansible_host=1.1.1.2\n")
    ran = _patch_remote_flow(monkeypatch)
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: None)

    driver_mod.run_remote_phase(
        settings=GlobalSettings(canary_hosts=["web-02"]), inventory_path=str(p), state_output_path=None
    )

    assert ran == ["web-02", "web-01"]


def test_remote_canary_dry_run_skips_soak(tmp_path, monkeypatch):
    ran = _patch_remote_flow(monkeypatch)
    monkeypatch.setattr(
        driver_mod, "_soak_canaries", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no soak in dry-run"))
    )

    state = driver_mod.run_remote_phase(
        settings=GlobalSettings(fleet_dry_run=True), inventory_path=_remote_inventory(tmp_path), state_output_path=None
    )

    assert len(ran) == 3  # both waves still ran
    assert state.failed is False


def test_vm_canary_failure_skips_rest_with_records(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\n"
        "[proxmox_vms]\n"
        "media-vm ansible_host=10.0.1.1 vmid=200 pve_node=pve-01 canary=true\n"
        "db-vm ansible_host=10.0.1.2 vmid=201 pve_node=pve-01\n"
    )
    ran: List[str] = []

    def _fake_vm_update(node_name, vmid, name, vm_ex, node_ex, settings, **kw):
        ran.append(name)
        from proxmox_fleet.flows.vm import VmFlowOutcome
        from proxmox_fleet.models.state import ErrorEntry, VmRecord

        return VmFlowOutcome(
            record=VmRecord(node=node_name, vmid=vmid, name=name, status="FAILED"),
            failed=True,
            error=ErrorEntry(host=name, task="upgrade", error="boom"),
        )

    monkeypatch.setattr(driver_mod, "run_vm_update", _fake_vm_update)
    monkeypatch.setattr(driver_mod, "_discover_vm_locations", lambda *a, **kw: {})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_vm_phase(settings=GlobalSettings(), inventory_path=str(p), state_output_path=None)

    assert ran == ["media-vm"]
    skipped = [r for r in state.vm if r.status == "SKIPPED (canary failed)"]
    assert [r.name for r in skipped] == ["db-vm"]
    assert skipped[0].node == "pve-01"  # pve_node hint used for the record


def test_vm_qualified_canary_token_stages_only_its_cluster(tmp_path, monkeypatch):
    """canary_hosts=["alpha/200"] stages only alpha's vmid-200 VM as a canary;
    beta's same-vmid VM runs in the rest wave (never staged, never skipped)."""
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "[proxmox_vms]\n"
        "alpha-vm ansible_host=10.0.1.1 vmid=200 pve_node=alpha-01\n"
        "beta-vm ansible_host=10.1.1.1 vmid=200 pve_node=beta-01\n"
    )
    ran: List[str] = []

    def _fake_vm_update(node_name, vmid, name, vm_ex, node_ex, settings, **kw):
        ran.append(name)
        from proxmox_fleet.flows.vm import VmFlowOutcome

        return VmFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_vm_update", _fake_vm_update)
    monkeypatch.setattr(driver_mod, "_discover_vm_locations", lambda *a, **kw: {})
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: None)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_vm_phase(
        settings=GlobalSettings(canary_hosts=["alpha/200"]), inventory_path=str(p), state_output_path=None
    )

    # alpha-vm is the (only) canary and runs first; beta-vm is the rest wave.
    assert ran == ["alpha-vm", "beta-vm"]
    assert not any("SKIPPED" in r.status for r in state.vm)
    assert state.failed is False


def test_vm_bare_canary_token_stages_vmid_in_every_cluster(tmp_path, monkeypatch):
    """A bare canary vmid keeps the historical behaviour: it stages that vmid
    in every cluster (back-compat)."""
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "[proxmox_vms]\n"
        "alpha-vm ansible_host=10.0.1.1 vmid=200 pve_node=alpha-01\n"
        "beta-vm ansible_host=10.1.1.1 vmid=200 pve_node=beta-01\n"
        "other-vm ansible_host=10.1.1.2 vmid=201 pve_node=beta-01\n"
    )
    ran: List[str] = []

    def _fake_vm_update(node_name, vmid, name, vm_ex, node_ex, settings, **kw):
        ran.append(name)
        from proxmox_fleet.flows.vm import VmFlowOutcome

        return VmFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_vm_update", _fake_vm_update)
    monkeypatch.setattr(driver_mod, "_discover_vm_locations", lambda *a, **kw: {})
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: None)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    driver_mod.run_vm_phase(
        settings=GlobalSettings(canary_hosts=["200"], vm_forks=1), inventory_path=str(p), state_output_path=None
    )

    # Both vmid-200 VMs form the canary wave; other-vm is the rest wave.
    assert ran == ["alpha-vm", "beta-vm", "other-vm"]


def test_lxc_canary_id_failure_skips_rest_across_nodes(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\npve-02 ansible_host=10.0.0.2\n")
    per_node_ids = {"pve-01": ["101", "102"], "pve-02": ["201"]}
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        return per_node_ids[ex.host]

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(lxc_id)
        from proxmox_fleet.flows.lxc import LxcFlowOutcome
        from proxmox_fleet.models.state import ErrorEntry, LxcRecord

        return LxcFlowOutcome(
            record=LxcRecord(node=node_name, name=f"ct{lxc_id}", id=lxc_id, app="FAILED"),
            failed=True,
            error=ErrorEntry(host=lxc_id, task="update", error="boom"),
        )

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    state = driver_mod.run_lxc_phase(
        settings=GlobalSettings(canary_hosts=["201"]), inventory_path=str(p), state_output_path=None
    )

    assert ran == ["201"]  # canary wave only
    skipped = [r for r in state.lxc if r.app == "SKIPPED (canary failed)"]
    assert sorted(r.id for r in skipped) == ["101", "102"]
    assert all(r.snap is False for r in skipped)


def test_lxc_canary_success_runs_rest(tmp_path, monkeypatch):
    p = tmp_path / "hosts.ini"
    p.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\n")
    ran: List[str] = []

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(lxc_id)
        from proxmox_fleet.flows.lxc import LxcFlowOutcome

        return LxcFlowOutcome()

    monkeypatch.setattr(driver_mod, "_discover_lxcs", lambda ex, s, **kw: ["101", "102", "103"])
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: None)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_lxc_phase(
        settings=GlobalSettings(canary_hosts=["102"]), inventory_path=str(p), state_output_path=None
    )

    assert ran == ["102", "101", "103"]  # canary first, then the rest
    assert state.failed is False


def test_lxc_discovery_failure_does_not_trip_canary_gate(tmp_path, monkeypatch):
    """A discovery error on one node must not abort the canary waves."""
    p = tmp_path / "hosts.ini"
    p.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\npve-bad ansible_host=10.0.0.9\n")
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        if ex.host == "pve-bad":
            raise RuntimeError("ssh down")
        return ["101", "102"]

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(lxc_id)
        from proxmox_fleet.flows.lxc import LxcFlowOutcome

        return LxcFlowOutcome()

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: None)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    state = driver_mod.run_lxc_phase(
        settings=GlobalSettings(canary_hosts=["101"]), inventory_path=str(p), state_output_path=None
    )

    assert state.failed is True  # discovery error recorded
    assert sorted(ran) == ["101", "102"]  # both waves still ran
    assert not any("SKIPPED" in r.app for r in state.lxc)


# ---------------------------------------------------------------------------
# Multi-cluster qualified ids (Task 1) — --limit and canary staging
# ---------------------------------------------------------------------------


def _two_cluster_inventory(tmp_path) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\nalpha-01 ansible_host=10.0.0.1 cluster=alpha\nbeta-01 ansible_host=10.1.0.1 cluster=beta\n"
    )
    return str(p)


def test_lxc_phase_qualified_limit_runs_only_that_cluster(tmp_path, monkeypatch):
    p = _two_cluster_inventory(tmp_path)
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        return ["101"]  # both clusters happen to have a container 101

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(f"{node_name}/{lxc_id}")
        from proxmox_fleet.flows.lxc import LxcFlowOutcome

        return LxcFlowOutcome()

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=p, state_output_path=None, limit={"alpha/101"})

    assert ran == ["alpha-01/101"]  # beta's container 101 never ran


def test_lxc_phase_bare_limit_runs_id_in_every_cluster(tmp_path, monkeypatch):
    p = _two_cluster_inventory(tmp_path)
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        return ["101"]

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(f"{node_name}/{lxc_id}")
        from proxmox_fleet.flows.lxc import LxcFlowOutcome

        return LxcFlowOutcome()

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=p, state_output_path=None, limit={"101"})

    assert sorted(ran) == ["alpha-01/101", "beta-01/101"]  # bare id → both clusters


def test_lxc_phase_qualified_canary_stages_only_its_cluster(tmp_path, monkeypatch):
    p = _two_cluster_inventory(tmp_path)
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        return ["101"]  # both clusters have a container 101

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(f"{node_name}/{lxc_id}")
        from proxmox_fleet.flows.lxc import LxcFlowOutcome

        return LxcFlowOutcome()

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr(driver_mod, "_soak_canaries", lambda *a, **kw: None)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    state = driver_mod.run_lxc_phase(
        settings=GlobalSettings(canary_hosts=["alpha/101"]), inventory_path=p, state_output_path=None
    )

    # Only alpha's 101 is a canary — it runs first; beta's 101 runs in the
    # (single) rest wave, never marked SKIPPED.
    assert ran[0] == "alpha-01/101"
    assert sorted(ran) == ["alpha-01/101", "beta-01/101"]
    assert not any("SKIPPED" in r.app for r in state.lxc)


def test_lxc_phase_qualified_canary_failure_skips_only_its_cluster(tmp_path, monkeypatch):
    p = _two_cluster_inventory(tmp_path)
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        return ["101"]

    def _fake_lxc_update(node_name, lxc_id, ex, settings, **kw):
        ran.append(f"{node_name}/{lxc_id}")
        from proxmox_fleet.flows.lxc import LxcFlowOutcome
        from proxmox_fleet.models.state import ErrorEntry, LxcRecord

        if node_name == "alpha-01":
            return LxcFlowOutcome(
                record=LxcRecord(node=node_name, name="ct101", id=lxc_id, app="FAILED"),
                failed=True,
                error=ErrorEntry(host=lxc_id, task="update", error="boom"),
            )
        return LxcFlowOutcome()

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "run_lxc_update", _fake_lxc_update)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    state = driver_mod.run_lxc_phase(
        settings=GlobalSettings(canary_hosts=["alpha/101"]), inventory_path=p, state_output_path=None
    )

    # alpha's canary failed; beta was never staged as a canary at all, so its
    # container is skipped along with the rest of the (single) canary wave.
    assert ran == ["alpha-01/101"]
    skipped = [r for r in state.lxc if r.app == "SKIPPED (canary failed)"]
    assert [(r.node, r.id) for r in skipped] == [("beta-01", "101")]


# --- _soak_canaries --------------------------------------------------------------


def test_soak_sleeps_then_checks(monkeypatch):
    slept: List[float] = []
    polled: List[str] = []

    def _fake_poll(fetch, pred, **kw):
        polled.append("x")
        return {"heartbeatList": {}}

    monkeypatch.setattr(driver_mod.http, "poll_until", _fake_poll)
    settings = GlobalSettings(canary_soak_minutes=2, kuma_url="http://kuma", kuma_slug="fleet")
    err = driver_mod._soak_canaries(settings, {"101": "5"}, ["101", "102"], _sleep=slept.append)
    assert err is None
    assert slept == [120.0]
    assert len(polled) == 1  # only the mapped canary is polled


def test_soak_returns_error_on_unhealthy(monkeypatch):
    def _boom(fetch, pred, **kw):
        raise TimeoutError("monitor down")

    monkeypatch.setattr(driver_mod.http, "poll_until", _boom)
    settings = GlobalSettings(kuma_url="http://kuma", kuma_slug="fleet")
    err = driver_mod._soak_canaries(settings, {"101": "5"}, ["101"], _sleep=lambda s: None)
    assert err is not None and "101" in err


def test_soak_no_kuma_url_is_noop():
    err = driver_mod._soak_canaries(GlobalSettings(), {"101": "5"}, ["101"], _sleep=lambda s: None)
    assert err is None


# ---------------------------------------------------------------------------
# _discover_vm_locations — multi-node fallback
# ---------------------------------------------------------------------------

_DISC_NODES = [
    {"name": "pve-01", "ansible_host": "10.0.0.1"},
    {"name": "pve-02", "ansible_host": "10.0.0.2"},
]
_DISC_RESOURCES = [{"vmid": 200, "node": "pve-02"}, {"vmid": 201, "node": "pve-03"}]


def _disc_executor(responses: Dict[str, PrimitiveResult], calls: List[str]):
    """Factory monkeypatched over driver.RunnerExecutor for discovery tests."""

    class _Exec:
        def __init__(self, host, **kw):
            calls.append(host)
            self._res = responses[host]

        def run_shell(self, cmd, **kw):
            return self._res

    return _Exec


def test_discover_vm_locations_falls_back_to_next_node(monkeypatch, capsys):
    calls: List[str] = []
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        _disc_executor(
            {
                "pve-01": PrimitiveResult(rc=4, failed=True, stderr="no route to host"),
                "pve-02": PrimitiveResult(rc=0, stdout=json.dumps(_DISC_RESOURCES)),
            },
            calls,
        ),
    )

    got = driver_mod._discover_vm_locations(_DISC_NODES, inventory_path="hosts.ini")

    assert got == {("default", "200"): ("pve-02", "10.0.0.2"), ("default", "201"): ("pve-03", "pve-03")}
    assert calls == ["pve-01", "pve-02"]
    assert "via pve-01 failed" in capsys.readouterr().err


def test_discover_vm_locations_first_node_ok_asks_no_others(monkeypatch):
    calls: List[str] = []
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        _disc_executor(
            {
                "pve-01": PrimitiveResult(rc=0, stdout=json.dumps(_DISC_RESOURCES)),
                "pve-02": PrimitiveResult(rc=0, stdout="[]"),
            },
            calls,
        ),
    )

    got = driver_mod._discover_vm_locations(_DISC_NODES, inventory_path="hosts.ini")

    assert got[("default", "200")] == ("pve-02", "10.0.0.2")
    assert calls == ["pve-01"]


def test_discover_vm_locations_all_nodes_down_returns_empty(monkeypatch, capsys):
    calls: List[str] = []
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        _disc_executor(
            {
                "pve-01": PrimitiveResult(rc=4, failed=True, stderr="no route to host"),
                "pve-02": PrimitiveResult(rc=0, stdout="not json"),
            },
            calls,
        ),
    )

    got = driver_mod._discover_vm_locations(_DISC_NODES, inventory_path="hosts.ini")

    assert got == {}
    assert calls == ["pve-01", "pve-02"]
    err = capsys.readouterr().err
    assert "via pve-01 failed" in err and "via pve-02 failed" in err


# ---------------------------------------------------------------------------
# Unreachable-node tolerance (quorum-gated skip instead of run failure)
# ---------------------------------------------------------------------------


def _two_node_inventory(tmp_path) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\npve-02 ansible_host=10.0.0.2\n")
    return str(p)


def test_cluster_quorate_asks_first_answering_node(monkeypatch):
    class _Exec:
        def __init__(self, host, **kw):
            self.host = host

        def run_shell(self, cmd, **kw):
            if self.host == "pve-01":
                return PrimitiveResult(rc=4, failed=True, unreachable=True)
            return PrimitiveResult(
                rc=0,
                stdout=json.dumps([{"type": "cluster", "quorate": 1}, {"type": "node", "name": "pve-02"}]),
            )

    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _Exec)
    nodes = [{"name": "pve-01", "ansible_host": "10.0.0.1"}, {"name": "pve-02", "ansible_host": "10.0.0.2"}]
    assert driver_mod._cluster_quorate(nodes, inventory_path="hosts.ini", skip={"pve-01"}) is True
    # Not-quorate cluster reports 0
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        lambda *a, **kw: type(
            "E",
            (),
            {
                "run_shell": lambda self, cmd, **kw2: PrimitiveResult(
                    rc=0, stdout=json.dumps([{"type": "cluster", "quorate": 0}])
                )
            },
        )(),
    )
    assert driver_mod._cluster_quorate(nodes, inventory_path="hosts.ini", skip=set()) is False


def test_cluster_quorate_standalone_and_silent(monkeypatch):
    # Standalone node: no cluster entry → quorum doesn't apply → True.
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        lambda *a, **kw: type(
            "E",
            (),
            {
                "run_shell": lambda self, cmd, **kw2: PrimitiveResult(
                    rc=0, stdout=json.dumps([{"type": "node", "name": "pve-01"}])
                )
            },
        )(),
    )
    nodes = [{"name": "pve-01", "ansible_host": "10.0.0.1"}]
    assert driver_mod._cluster_quorate(nodes, inventory_path="hosts.ini", skip=set()) is True
    # Nobody answers → None.
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        lambda *a, **kw: type(
            "E", (), {"run_shell": lambda self, cmd, **kw2: PrimitiveResult(rc=4, failed=True, unreachable=True)}
        )(),
    )
    assert driver_mod._cluster_quorate(nodes, inventory_path="hosts.ini", skip=set()) is None


def test_lxc_phase_unreachable_node_warns_when_quorate(tmp_path, monkeypatch):
    from proxmox_fleet.flows.lxc import LxcFlowOutcome
    from proxmox_fleet.runner import UnreachableHostError

    inv = _two_node_inventory(tmp_path)
    ran: List[str] = []

    def _fake_discover(ex, settings, **kw):
        if ex.host == "pve-01":
            raise UnreachableHostError("node unreachable: No route to host")
        return ["201"]

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "_cluster_quorate", lambda *a, **kw: True)
    monkeypatch.setattr(
        driver_mod,
        "run_lxc_update",
        lambda node, lxc_id, ex, settings, **kw: ran.append(f"{node}/{lxc_id}") or LxcFlowOutcome(),
    )
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    state = driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=inv, state_output_path=None)

    assert not state.failed
    assert state.errors == []
    assert len(state.warnings) == 1
    assert state.warnings[0].host == "pve-01"
    assert "unreachable" in state.warnings[0].warning
    assert ran == ["pve-02/201"]


def test_lxc_phase_unreachable_node_fails_without_quorum(tmp_path, monkeypatch):
    from proxmox_fleet.runner import UnreachableHostError

    inv = _two_node_inventory(tmp_path)

    def _fake_discover(ex, settings, **kw):
        if ex.host == "pve-01":
            raise UnreachableHostError("node unreachable: No route to host")
        return []

    def _fake_executor(host, **kw):
        ex = ScriptedExecutor()
        ex.host = host
        return ex

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr(driver_mod, "_cluster_quorate", lambda *a, **kw: False)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake_executor)

    state = driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=inv, state_output_path=None)

    assert state.failed
    assert state.warnings == []
    assert len(state.errors) == 1
    assert "NOT quorate" in state.errors[0].error


def test_node_phase_skips_unreachable_node_and_continues(tmp_path, monkeypatch):
    from proxmox_fleet.flows.node import NodeFlowOutcome
    from proxmox_fleet.models.state import ErrorEntry, NodeRecord as NR

    inv = _two_node_inventory(tmp_path)

    def _fake_node_update(node_name, executor, settings, **kw):
        if node_name == "pve-01":
            return NodeFlowOutcome(
                record=NR(node=node_name, status="FAILED"),
                failed=True,
                error=ErrorEntry(
                    host=node_name,
                    task="apt dist-upgrade",
                    error='Data could not be sent to remote host "10.0.0.1": No route to host',
                ),
            )
        return NodeFlowOutcome(record=NR(node=node_name, status="UPDATED"), changed=True)

    monkeypatch.setattr(driver_mod, "run_node_update", _fake_node_update)
    monkeypatch.setattr(driver_mod, "_cluster_quorate", lambda *a, **kw: True)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_node_phase(
        settings=GlobalSettings(), inventory_path=inv, state_output_path=None, include_manager=False
    )

    assert not state.failed
    assert [(r.node, r.status) for r in state.node] == [("pve-01", "SKIPPED (unreachable)"), ("pve-02", "UPDATED")]
    assert len(state.warnings) == 1
    assert state.warnings[0].host == "pve-01"
    assert state.errors == []


def test_node_phase_unreachable_without_quorum_still_aborts(tmp_path, monkeypatch):
    from proxmox_fleet.flows.node import NodeFlowOutcome
    from proxmox_fleet.models.state import ErrorEntry, NodeRecord as NR

    inv = _two_node_inventory(tmp_path)
    ran: List[str] = []

    def _fake_node_update(node_name, executor, settings, **kw):
        ran.append(node_name)
        return NodeFlowOutcome(
            record=NR(node=node_name, status="FAILED"),
            failed=True,
            error=ErrorEntry(host=node_name, task="apt dist-upgrade", error="No route to host"),
        )

    monkeypatch.setattr(driver_mod, "run_node_update", _fake_node_update)
    monkeypatch.setattr(driver_mod, "_cluster_quorate", lambda *a, **kw: False)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_node_phase(
        settings=GlobalSettings(), inventory_path=inv, state_output_path=None, include_manager=False
    )

    assert state.failed
    assert ran == ["pve-01"]  # aborted on first failure as before


def test_node_phase_real_failure_still_aborts(tmp_path, monkeypatch):
    from proxmox_fleet.flows.node import NodeFlowOutcome
    from proxmox_fleet.models.state import ErrorEntry, NodeRecord as NR

    inv = _two_node_inventory(tmp_path)
    ran: List[str] = []

    def _fake_node_update(node_name, executor, settings, **kw):
        ran.append(node_name)
        return NodeFlowOutcome(
            record=NR(node=node_name, status="FAILED"),
            failed=True,
            error=ErrorEntry(host=node_name, task="apt dist-upgrade", error="dpkg was interrupted"),
        )

    quorum_calls: List[str] = []
    monkeypatch.setattr(driver_mod, "run_node_update", _fake_node_update)
    monkeypatch.setattr(driver_mod, "_cluster_quorate", lambda *a, **kw: quorum_calls.append("x") or True)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_node_phase(
        settings=GlobalSettings(), inventory_path=inv, state_output_path=None, include_manager=False
    )

    assert state.failed
    assert ran == ["pve-01"]
    assert quorum_calls == []  # non-unreachable failures never probe quorum


# ---------------------------------------------------------------------------
# Quorum checks must stay inside the unreachable node's own cluster (Task 4)
# ---------------------------------------------------------------------------


def test_cluster_quorate_scopes_to_given_cluster(monkeypatch):
    """A sibling cluster's quorate answer must not leak into another cluster's verdict."""

    class _Exec:
        def __init__(self, host, **kw):
            self.host = host

        def run_shell(self, cmd, **kw):
            if self.host == "beta-01":
                return PrimitiveResult(rc=0, stdout=json.dumps([{"type": "cluster", "quorate": 1}]))
            # alpha nodes never answer.
            return PrimitiveResult(rc=4, failed=True, unreachable=True)

    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _Exec)
    nodes = [
        {"name": "beta-01", "ansible_host": "10.1.0.1", "cluster": "beta"},
        {"name": "alpha-01", "ansible_host": "10.0.0.1", "cluster": "alpha"},
        {"name": "alpha-02", "ansible_host": "10.0.0.2", "cluster": "alpha"},
    ]
    # Back-compat: cluster=None (default) considers every node in inventory
    # order — beta-01 answers first, so the (meaningless, cross-cluster) True
    # is what the old code returned.
    assert driver_mod._cluster_quorate(nodes, inventory_path="hosts.ini", skip=set()) is True
    # With cluster="alpha", beta-01 must be excluded from consideration — no
    # alpha node ever answers, so the verdict is None, not beta's True.
    assert driver_mod._cluster_quorate(nodes, inventory_path="hosts.ini", skip=set(), cluster="alpha") is None


def _mc_quorum_inventory(tmp_path: Path) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "alpha-02 ansible_host=10.0.0.2 cluster=alpha\n"
        "alpha-03 ansible_host=10.0.0.3 cluster=alpha\n"
    )
    return str(p)


def test_lxc_phase_cross_cluster_unreachable_not_tolerated_by_other_cluster_quorum(tmp_path, monkeypatch):
    """alpha-01/alpha-02 unreachable; beta-01 quorate; alpha-03 (still up) reports
    NOT quorate. Beta's healthy answer must never rescue alpha's nodes — this is
    the exact bug: with unscoped quorum, beta-01 (first in inventory order)
    answering quorate=True previously made alpha's unreachable nodes tolerated.
    """
    from proxmox_fleet.runner import UnreachableHostError

    inv = _mc_quorum_inventory(tmp_path)

    def _fake_discover(ex, settings, **kw):
        if ex.host in ("alpha-01", "alpha-02"):
            raise UnreachableHostError("node unreachable: No route to host")
        return []

    quorum_responses = {
        "beta-01": PrimitiveResult(rc=0, stdout=json.dumps([{"type": "cluster", "quorate": 1}])),
        "alpha-03": PrimitiveResult(rc=0, stdout=json.dumps([{"type": "cluster", "quorate": 0}])),
    }

    class _McExec:
        def __init__(self, host, **kw):
            self.host = host

        def run_shell(self, cmd, **kw):
            return quorum_responses.get(self.host, PrimitiveResult(rc=4, failed=True, unreachable=True))

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _McExec)

    state = driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=inv, state_output_path=None)

    assert state.failed
    assert state.warnings == []  # never tolerated on the strength of beta's quorum
    alpha_hosts = {e.host for e in state.errors}
    assert alpha_hosts == {"alpha-01", "alpha-02"}
    for e in state.errors:
        assert "NOT quorate" in e.error


def test_lxc_phase_single_cluster_unreachable_still_tolerated_when_quorate(tmp_path, monkeypatch):
    """Regression: an inventory with no cluster= vars (everything DEFAULT_CLUSTER)
    must behave exactly as before — unreachable node tolerated-as-skipped when
    the (whole, single) cluster is still quorate. Exercises the real
    (unmocked) _cluster_quorate to prove the cluster filter is a no-op here.
    """
    from proxmox_fleet.runner import UnreachableHostError

    inv = _two_node_inventory(tmp_path)  # pve-01, pve-02 — no cluster= vars

    def _fake_discover(ex, settings, **kw):
        if ex.host == "pve-01":
            raise UnreachableHostError("node unreachable: No route to host")
        return []

    class _Exec:
        def __init__(self, host, **kw):
            self.host = host

        def run_shell(self, cmd, **kw):
            if self.host == "pve-02":
                return PrimitiveResult(rc=0, stdout=json.dumps([{"type": "cluster", "quorate": 1}]))
            return PrimitiveResult(rc=4, failed=True, unreachable=True)

    monkeypatch.setattr(driver_mod, "_discover_lxcs", _fake_discover)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _Exec)

    state = driver_mod.run_lxc_phase(settings=GlobalSettings(), inventory_path=inv, state_output_path=None)

    assert not state.failed
    assert state.errors == []
    assert len(state.warnings) == 1
    assert state.warnings[0].host == "pve-01"
    assert "unreachable" in state.warnings[0].warning


def test_node_phase_quorum_check_scoped_to_failing_node_cluster(tmp_path, monkeypatch):
    """The node-phase serial-loop _cluster_quorate call must pass the failing
    node's own cluster, not fall back to the whole inventory."""
    from proxmox_fleet.flows.node import NodeFlowOutcome
    from proxmox_fleet.models.state import ErrorEntry, NodeRecord as NR

    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\nalpha-01 ansible_host=10.0.0.1 cluster=alpha\nbeta-01 ansible_host=10.1.0.1 cluster=beta\n"
    )
    inv = str(p)

    def _fake_node_update(node_name, executor, settings, **kw):
        return NodeFlowOutcome(
            record=NR(node=node_name, status="FAILED"),
            failed=True,
            error=ErrorEntry(host=node_name, task="apt dist-upgrade", error="No route to host"),
        )

    calls: List[Any] = []

    def _fake_quorate(nodes, *, inventory_path, skip, cluster=None):
        calls.append((skip, cluster))
        return True

    monkeypatch.setattr(driver_mod, "run_node_update", _fake_node_update)
    monkeypatch.setattr(driver_mod, "_cluster_quorate", _fake_quorate)
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_node_phase(
        settings=GlobalSettings(), inventory_path=inv, state_output_path=None, include_manager=False
    )

    assert not state.failed  # both tolerated as skipped (quorate stubbed True)
    assert calls == [({"alpha-01"}, "alpha"), ({"beta-01"}, "beta")]


# ---------------------------------------------------------------------------
# _discover_vm_locations — multi-cluster
# ---------------------------------------------------------------------------

_MC_NODES = [
    {"name": "alpha-01", "ansible_host": "10.0.0.1", "cluster": "alpha"},
    {"name": "alpha-02", "ansible_host": "10.0.0.2", "cluster": "alpha"},
    {"name": "beta-01", "ansible_host": "10.1.0.1", "cluster": "beta"},
]


def test_discover_vm_locations_queries_each_cluster(monkeypatch):
    """One pvesh walk per cluster; a shared vmid keeps both entries."""
    calls: List[str] = []
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        _disc_executor(
            {
                "alpha-01": PrimitiveResult(rc=0, stdout=json.dumps([{"vmid": 101, "node": "alpha-01"}])),
                "beta-01": PrimitiveResult(
                    rc=0, stdout=json.dumps([{"vmid": 101, "node": "beta-01"}, {"vmid": 300, "node": "beta-01"}])
                ),
            },
            calls,
        ),
    )

    got = driver_mod._discover_vm_locations(_MC_NODES, inventory_path="hosts.ini")

    assert calls == ["alpha-01", "beta-01"]  # one query per cluster
    assert got == {
        ("alpha", "101"): ("alpha-01", "10.0.0.1"),
        ("beta", "101"): ("beta-01", "10.1.0.1"),
        ("beta", "300"): ("beta-01", "10.1.0.1"),
    }


def test_discover_vm_locations_per_cluster_fallthrough(monkeypatch, capsys):
    """A dead first node blinds only its own cluster's first attempt."""
    calls: List[str] = []
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        _disc_executor(
            {
                "alpha-01": PrimitiveResult(rc=4, failed=True, stderr="no route to host"),
                "alpha-02": PrimitiveResult(rc=0, stdout=json.dumps([{"vmid": 200, "node": "alpha-02"}])),
                "beta-01": PrimitiveResult(rc=0, stdout=json.dumps([{"vmid": 200, "node": "beta-01"}])),
            },
            calls,
        ),
    )

    got = driver_mod._discover_vm_locations(_MC_NODES, inventory_path="hosts.ini")

    assert calls == ["alpha-01", "alpha-02", "beta-01"]
    assert got[("alpha", "200")] == ("alpha-02", "10.0.0.2")
    assert got[("beta", "200")] == ("beta-01", "10.1.0.1")
    assert "cluster 'alpha' via alpha-01 failed" in capsys.readouterr().err


def test_discover_vm_locations_one_cluster_down_other_survives(monkeypatch):
    calls: List[str] = []
    monkeypatch.setattr(
        "proxmox_fleet.driver.RunnerExecutor",
        _disc_executor(
            {
                "alpha-01": PrimitiveResult(rc=4, failed=True, stderr="down"),
                "alpha-02": PrimitiveResult(rc=4, failed=True, stderr="down"),
                "beta-01": PrimitiveResult(rc=0, stdout=json.dumps([{"vmid": 300, "node": "beta-01"}])),
            },
            calls,
        ),
    )

    got = driver_mod._discover_vm_locations(_MC_NODES, inventory_path="hosts.ini")

    assert got == {("beta", "300"): ("beta-01", "10.1.0.1")}


# ---------------------------------------------------------------------------
# _resolve_vm_cluster
# ---------------------------------------------------------------------------

_MC_NODE_CLUSTERS = {"alpha-01": "alpha", "alpha-02": "alpha", "beta-01": "beta"}


def _vm_spec(**kw):
    defaults = dict(name="vm-x", ansible_host="10.0.1.1", vmid="101", pve_node="")
    defaults.update(kw)
    return VmSpec(**defaults)


def test_resolve_vm_cluster_explicit_var_wins():
    locations = {("alpha", "101"): ("alpha-01", "10.0.0.1")}
    spec = _vm_spec(cluster="beta", pve_node="alpha-01")
    assert driver_mod._resolve_vm_cluster(spec, _MC_NODE_CLUSTERS, locations) == "beta"


def test_resolve_vm_cluster_from_pve_node():
    spec = _vm_spec(pve_node="beta-01")
    assert driver_mod._resolve_vm_cluster(spec, _MC_NODE_CLUSTERS, {}) == "beta"


def test_resolve_vm_cluster_single_discovery_hit():
    locations = {("alpha", "101"): ("alpha-01", "10.0.0.1"), ("beta", "300"): ("beta-01", "10.1.0.1")}
    assert driver_mod._resolve_vm_cluster(_vm_spec(), _MC_NODE_CLUSTERS, locations) == "alpha"


def test_resolve_vm_cluster_ambiguous_raises():
    locations = {("alpha", "101"): ("alpha-01", "10.0.0.1"), ("beta", "101"): ("beta-01", "10.1.0.1")}
    with pytest.raises(RuntimeError, match="alpha, beta"):
        driver_mod._resolve_vm_cluster(_vm_spec(), _MC_NODE_CLUSTERS, locations)


def test_resolve_vm_cluster_undiscovered_defaults():
    assert driver_mod._resolve_vm_cluster(_vm_spec(), _MC_NODE_CLUSTERS, {}) == "default"


def test_vm_phase_ambiguous_vmid_becomes_failed_record(tmp_path, monkeypatch):
    """An ambiguous shared vmid fails that VM loudly instead of guessing a node."""
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "[proxmox_vms]\n"
        "mystery-vm ansible_host=10.0.1.1 vmid=101\n"
    )
    monkeypatch.setattr(
        driver_mod,
        "_discover_vm_locations",
        lambda *a, **kw: {
            ("alpha", "101"): ("alpha-01", "10.0.0.1"),
            ("beta", "101"): ("beta-01", "10.1.0.1"),
        },
    )
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    state = driver_mod.run_vm_phase(settings=GlobalSettings(), inventory_path=str(p), state_output_path=None)

    assert state.failed is True
    assert any("set cluster= or pve_node=" in e.error for e in state.errors)


def test_vm_phase_shared_vmid_targets_own_cluster_node(tmp_path, monkeypatch):
    """Two clusters' vmid 101 must each be driven against their own node."""
    p = tmp_path / "hosts.ini"
    p.write_text(
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "[proxmox_vms]\n"
        "vm-a ansible_host=10.0.1.1 vmid=101 cluster=alpha\n"
        "vm-b ansible_host=10.1.1.1 vmid=101 cluster=beta\n"
    )
    monkeypatch.setattr(
        driver_mod,
        "_discover_vm_locations",
        lambda *a, **kw: {
            ("alpha", "101"): ("alpha-01", "10.0.0.1"),
            ("beta", "101"): ("beta-01", "10.1.0.1"),
        },
    )
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: ScriptedExecutor())

    targeted: List[tuple] = []

    def _fake_vm_update(node_name, vmid, name, vm_ex, node_ex, settings, **kw):
        targeted.append((name, node_name, kw.get("api_host"), kw.get("cluster")))
        return VmFlowOutcome()

    monkeypatch.setattr(driver_mod, "run_vm_update", _fake_vm_update)

    state = driver_mod.run_vm_phase(settings=GlobalSettings(), inventory_path=str(p), state_output_path=None)

    assert state.failed is False
    assert sorted(targeted) == [
        ("vm-a", "alpha-01", "10.0.0.1", "alpha"),
        ("vm-b", "beta-01", "10.1.0.1", "beta"),
    ]
