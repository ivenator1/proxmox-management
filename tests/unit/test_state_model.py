"""Tests for proxmox_fleet.models.state — the typed FleetState records."""
import json

from proxmox_fleet.models.state import FleetState, LxcRecord


def test_lxc_record_defaults():
    r = LxcRecord.model_validate({"node": "pve-01", "name": "sonarr", "id": "101", "app": "OK"})
    assert r.os == ""
    assert r.snap is True


def test_from_raw_accepts_fleet_names():
    raw = {
        "fleet_lxc_data": [{"node": "pve-01", "name": "x", "id": "1", "app": "Updated: 1 → 2",
                            "os": "OK", "snap": True}],
        "fleet_custom_data": [{"host": "nas", "name": "Gitea", "app": "Updated: 1.0 → 1.1"}],
        "fleet_changed": True,
        "fleet_failed": False,
    }
    state = FleetState.from_raw(raw)
    assert state.changed is True
    assert state.lxc[0].name == "x"
    assert state.custom[0].name == "Gitea"
    assert state.vm == []


def test_from_raw_accepts_short_names():
    state = FleetState.from_raw({"node": [{"node": "pve-01", "status": "OK"}], "failed": True})
    assert state.failed is True
    assert state.node[0].status == "OK"


def test_round_trip_file(tmp_path):
    state = FleetState.from_raw({
        "fleet_lxc_data": [{"node": "pve-01", "name": "x", "id": "1", "app": "OK"}],
        "fleet_changed": True,
    })
    p = tmp_path / "latest.json"
    state.dump(p)
    reloaded = FleetState.load(p)
    assert reloaded.changed is True
    assert reloaded.lxc[0].id == "1"
    # file is valid JSON
    assert json.loads(p.read_text())["changed"] is True
