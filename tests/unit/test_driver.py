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

from proxmox_fleet.driver import _deep_merge, run_custom_phase
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.models.state import FleetState
from proxmox_fleet.runner import PrimitiveResult


def _ok(stdout: str = "", changed: bool = True) -> PrimitiveResult:
    return PrimitiveResult(rc=0, changed=changed, stdout=stdout)


def _fail(rc: int = 1) -> PrimitiveResult:
    return PrimitiveResult(rc=rc, failed=True, stderr="boom")


class ScriptedExecutor:
    """Fake executor injected by tests."""

    def __init__(self, script: Optional[Dict[str, List[PrimitiveResult]]] = None) -> None:
        self.script: Dict[str, List[PrimitiveResult]] = {k: list(v) for k, v in (script or {}).items()}
        self.commands: List[str] = []
        self.reboots = 0

    def _resp(self, command: str) -> PrimitiveResult:
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return _ok()

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
