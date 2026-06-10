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
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import FleetState
from proxmox_fleet.runner import PrimitiveResult


def _ok(stdout: str = "", changed: bool = True) -> PrimitiveResult:
    return PrimitiveResult(rc=0, changed=changed, stdout=stdout)


def _fail(rc: int = 1) -> PrimitiveResult:
    return PrimitiveResult(rc=rc, failed=True, stderr="boom")


class ScriptedExecutor:
    """Fake executor injected by tests."""

    def __init__(
        self,
        script: Optional[Dict[str, List[PrimitiveResult]]] = None,
        default: Optional[PrimitiveResult] = None,
    ) -> None:
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
        "app-01 ansible_host=10.0.0.1 custom_config=app\n"
        "db-01 ansible_host=10.0.0.2 custom_config=db",
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
    _write_config(tmp_path, "gitea", {
        "name": "Gitea",
        "version_command": "ver",
        "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
        "changed_when": {"type": "version"},
        "health_check": {"type": "none"},
    })
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
    _write_config(tmp_path, "gitea", {
        "name": "Gitea",
        "version_command": "ver",
        "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
        "changed_when": {"type": "version"},
        "health_check": {"type": "none"},
    })
    settings = _settings(tmp_path, fleet_dry_run=True)

    executor = ScriptedExecutor({"ver": [_ok("1.0")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv,
                             state_output_path=str(tmp_path / "out.json"))
    # dry-run: only version command ran, update step did not.
    assert "do-upgrade" not in executor.commands
    assert "dry-run" in state.custom[0].app


def test_extra_vars_fleet_dry_run_propagated(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(tmp_path, "gitea", {
        "name": "Gitea",
        "version_command": "ver",
        "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
        "changed_when": {"type": "version"},
        "health_check": {"type": "none"},
    })
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
    _write_config(tmp_path, "gitea", {
        "name": "Gitea",
        "version_command": "ver",
        "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
        "changed_when": {"type": "version"},
        "health_check": {"type": "none"},
    })
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
    _write_config(tmp_path, "gitea", {
        "name": "Gitea",
        "version_command": "ver",
        "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
        "changed_when": {"type": "version"},
        "health_check": {"type": "none"},
    })
    settings = _settings(tmp_path)

    executor = ScriptedExecutor({"ver": [_ok("1.0")]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv, check=True,
                             state_output_path=str(tmp_path / "out.json"))
    assert "do-upgrade" not in executor.commands
    assert "dry-run" in state.custom[0].app


def test_failed_host_recorded_in_state(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(tmp_path, "gitea", {
        "name": "Gitea",
        "version_command": "ver",
        "update_steps": [{"name": "fail-step", "command": "bad-cmd"}],
        "changed_when": {"type": "version"},
        "health_check": {"type": "none"},
    })
    settings = _settings(tmp_path)

    executor = ScriptedExecutor({
        "ver": [_ok("1.0")],
        "bad-cmd": [_fail()],
    })
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv,
                             state_output_path=str(tmp_path / "out.json"))
    assert state.failed is True
    assert state.custom[0].app == "FAILED"
    assert len(state.errors) == 1


def test_window_skip_outside_window(tmp_path, monkeypatch):
    inv = _write_inventory(tmp_path, "gitea ansible_host=10.0.0.1 custom_config=gitea")
    _write_config(tmp_path, "gitea", {
        "name": "Gitea",
        "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
        "health_check": {"type": "none"},
    })
    # Set maintenance_window in host_vars.
    hv = tmp_path / "host_vars"
    hv.mkdir()
    (hv / "gitea.yml").write_text(
        "maintenance_window:\n  days: [Sat]\n  start: '02:00'\n  end: '04:00'\n  tz: UTC\n"
    )
    settings = GlobalSettings(
        configs_dir=str(tmp_path / "configs"),
        host_vars_dir=str(hv),
    )

    executor = ScriptedExecutor()
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    # Use a fixed "now" that is outside the Saturday window (it's a Monday).
    from datetime import datetime, timezone
    monday_noon = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # Monday
    monkeypatch.setattr("proxmox_fleet.window.datetime", type("FakeDT", (), {
        "now": staticmethod(lambda **kw: monday_noon),
    }))

    state = run_custom_phase(settings=settings, inventory_path=inv,
                             state_output_path=str(tmp_path / "out.json"))
    # Host was skipped → no record, no commands run.
    assert state.custom == []
    assert executor.commands == []


# --- run_node_phase helpers ---------------------------------------------------

APT_UPGRADED = "3 upgraded, 0 newly installed, 0 to remove.\n"
APT_NOOP = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
NOT_MANAGER = _ok(stdout="1\n", changed=False)   # pct list grep → not manager
NO_REBOOT = _ok(stdout="ok\n", changed=False)


def _node_inventory(tmp_path: Path, node_lines: str) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(f"[proxmox_nodes]\n{node_lines}\n")
    return str(p)


def _node_settings(**kw) -> GlobalSettings:
    return GlobalSettings(manager_lxc_id="121", **kw)


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
    inv = _node_inventory(tmp_path,
        "pve-01 ansible_host=10.0.0.1\n"
        "pve-02 ansible_host=10.0.0.2",
    )
    mgr_executor = ScriptedNodeExecutor({
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "reboot-required": [_ok(stdout="ok\n", changed=False)],
    })

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
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor",
                        lambda *a, **kw: _make_node_executor())

    state_path = tmp_path / "out.json"
    run_node_phase(settings=_node_settings(), inventory_path=inv,
                   check=False, state_output_path=str(state_path))

    raw = json.loads(state_path.read_text())
    for key in ("fleet_node_data", "fleet_changed", "fleet_failed",
                "fleet_error_log", "fleet_warning_log"):
        assert key in raw, f"missing key: {key}"


def test_node_phase_dry_run_propagated(tmp_path, monkeypatch):
    """fleet_dry_run=True sends apt-get -s (not -y) to nodes."""
    inv = _node_inventory(tmp_path, "pve-01 ansible_host=10.0.0.1")
    executor = _make_node_executor()
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor",
                        lambda *a, **kw: executor)

    run_node_phase(settings=_node_settings(fleet_dry_run=True), inventory_path=inv,
                   check=False, state_output_path=str(tmp_path / "out.json"))

    apt_cmds = [c for c in executor.commands if "dist-upgrade" in c]
    assert apt_cmds, "expected an apt command"
    assert all("-s" in c for c in apt_cmds), "dry-run should use apt-get -s"


# --- run_node_phase — failure / abort ----------------------------------------

def test_node_phase_failure_aborts_remaining_nodes(tmp_path, monkeypatch):
    """First node fails → second node is not processed; manager still runs."""
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip retry delays

    inv = _node_inventory(tmp_path,
        "pve-01 ansible_host=10.0.0.1\n"
        "pve-02 ansible_host=10.0.0.2",
    )
    # default=_fail() so every apt attempt fails, exhausting all retries.
    fail_executor = ScriptedNodeExecutor({"pct list": [NOT_MANAGER]}, default=_fail())
    pve02_executor = _make_node_executor()
    mgr_executor = ScriptedNodeExecutor({
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "reboot-required": [_ok(stdout="ok\n", changed=False)],
    })

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
    assert "pve-02" not in node_names        # aborted
    assert "Ansible-Manager" in node_names   # always runs

    # pve-02 executor was never called.
    assert pve02_executor.commands == []


def test_node_phase_failure_recorded_in_state(tmp_path, monkeypatch):
    """A failed node produces a FAILED record and sets fleet_failed."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    inv = _node_inventory(tmp_path, "pve-01 ansible_host=10.0.0.1")
    fail_executor = ScriptedNodeExecutor({"pct list": [NOT_MANAGER]}, default=_fail())
    mgr_executor = ScriptedNodeExecutor({
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "reboot-required": [_ok(stdout="ok\n", changed=False)],
    })
    dispatch = {"pve-01": fail_executor, "localhost": mgr_executor}

    def _fake(host, **kw):
        ex = dispatch.get(host, _make_node_executor())
        ex.host = host
        return ex

    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", _fake)

    state_path = tmp_path / "out.json"
    state = run_node_phase(settings=_node_settings(), inventory_path=inv,
                           check=False, state_output_path=str(state_path))

    assert state.failed is True
    failed = [r for r in state.node if r.node == "pve-01"]
    assert len(failed) == 1
    assert failed[0].status == "FAILED"

    raw = json.loads(state_path.read_text())
    assert raw["fleet_failed"] is True


def test_node_phase_empty_inventory_still_runs_manager(tmp_path, monkeypatch):
    """No [proxmox_nodes] entries → only manager record in state."""
    inv = _node_inventory(tmp_path, "")  # empty group
    mgr_executor = ScriptedNodeExecutor({
        "dist-upgrade": [_ok(stdout=APT_NOOP)],
        "reboot-required": [_ok(stdout="ok\n", changed=False)],
    })
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor",
                        lambda *a, **kw: mgr_executor)

    state = run_node_phase(settings=_node_settings(), inventory_path=inv,
                           check=False, state_output_path=str(tmp_path / "out.json"))

    assert state.failed is False
    assert len(state.node) == 1
    assert state.node[0].node == "Ansible-Manager"


# --- run_custom_phase (existing) -----------------------------------------------

def test_dep_failed_propagates_to_next_host(tmp_path, monkeypatch):
    inv = _write_inventory(
        tmp_path,
        "db-01 ansible_host=10.0.0.1 custom_config=db\n"
        "app-01 ansible_host=10.0.0.2 custom_config=app",
    )
    # db fails; app depends_on db.
    hv = tmp_path / "host_vars"
    hv.mkdir()
    (hv / "app-01.yml").write_text("depends_on:\n  - db-01\n")

    _write_config(tmp_path, "db", {
        "name": "DB",
        "update_steps": [{"name": "fail-step", "command": "bad-db"}],
        "health_check": {"type": "none"},
    })
    _write_config(tmp_path, "app", {
        "name": "App",
        "update_steps": [{"name": "upgrade", "command": "do-app"}],
        "health_check": {"type": "none"},
    })

    settings = GlobalSettings(
        configs_dir=str(tmp_path / "configs"),
        host_vars_dir=str(hv),
    )

    executor = ScriptedExecutor({"bad-db": [_fail()]})
    monkeypatch.setattr("proxmox_fleet.driver.RunnerExecutor", lambda *a, **kw: executor)

    state = run_custom_phase(settings=settings, inventory_path=inv,
                             state_output_path=str(tmp_path / "out.json"))
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
        fleet_lxc_data=[{"node": "pve-01", "name": "sonarr", "id": "101",
                         "app": "Updated: v4.0 → v4.1", "os": "OK", "snap": True}],
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
    settings = GlobalSettings(discord_webhook="https://d/hook",
                              fleet_history_dir=str(tmp_path), force_notify=True)
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


# --- _merge_state -------------------------------------------------------------

def test_merge_state_concatenates_and_or_joins():
    dst = FleetState.from_raw({
        "fleet_lxc_data": [{"node": "n1", "name": "a", "id": "1", "app": "OK"}],
        "fleet_changed": True,
        "fleet_failed": False,
        "fleet_error_log": [{"host": "h1", "task": "t", "error": "e"}],
    })
    src = FleetState.from_raw({
        "fleet_lxc_data": [{"node": "n2", "name": "b", "id": "2", "app": "OK"}],
        "fleet_vm_data": [{"node": "n2", "vmid": "9", "name": "vm", "status": "OK"}],
        "fleet_changed": False,
        "fleet_failed": True,
        "fleet_warning_log": [{"host": "h2", "task": "u", "warning": "w"}],
    })
    _merge_state(dst, src)
    assert [r.id for r in dst.lxc] == ["1", "2"]
    assert [r.vmid for r in dst.vm] == ["9"]
    assert dst.changed is True   # True or False
    assert dst.failed is True    # False or True
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
        run_fleet(settings=GlobalSettings(apt_proxy_ip="10.0.0.1", apt_proxy_port=3142),
                  inventory_path="x")
