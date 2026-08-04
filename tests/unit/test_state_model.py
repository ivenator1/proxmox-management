"""Tests for proxmox_fleet.models.state — the typed FleetState records."""

import json

from proxmox_fleet.models.state import (
    FleetState,
    LxcRecord,
    NodeRecord,
    RemoteRecord,
    VmRecord,
)


def test_lxc_record_defaults():
    r = LxcRecord.model_validate({"node": "pve-01", "name": "sonarr", "id": "101", "app": "OK"})
    assert r.os == ""
    assert r.snap is True


# --- packages detail (PR1) -------------------------------------------------- #


def test_packages_omitted_when_none():
    """PR1: a record without package detail serializes key-free — the exact
    legacy shape, so idle/dry-run/old records gain no `packages` key."""
    r = LxcRecord.model_validate({"node": "pve-01", "name": "sonarr", "id": "101", "app": "OK"})
    assert r.packages is None
    assert "packages" not in r.model_dump()


def test_packages_present_when_set():
    """PR1: a real-run success record keeps its exact [{"name", "from", "to"}] list."""
    r = LxcRecord.model_validate(
        {
            "node": "pve-01",
            "name": "sonarr",
            "id": "101",
            "app": "Updated: 1 → 2",
            "packages": [{"name": "libssl3", "from": "1.0", "to": "1.1"}],
        }
    )
    dumped = r.model_dump()
    assert dumped["packages"] == [{"name": "libssl3", "from": "1.0", "to": "1.1"}]


def test_packages_key_free_in_every_bucket_when_none():
    """PR1: vm/remote/node records behave like lxc — no `packages` key until
    detail is set (a bare status record stays byte-compatible with pre-PR1)."""
    vm = VmRecord(node="pve-01", vmid="200", name="my-vm", status="OK")
    remote = RemoteRecord(host="web", status="OK")
    node = NodeRecord(node="pve-02", status="OK")
    for record in (vm, remote, node):
        assert "packages" not in record.model_dump()


def test_packages_round_trip_through_dump_and_load(tmp_path):
    """PR1: a record with detail survives dump → load unchanged, and a legacy
    key-free file loads with packages None (never a crash or an invented key)."""
    state = FleetState.from_raw(
        {
            "fleet_lxc_data": [
                {
                    "node": "pve-01",
                    "name": "sonarr",
                    "id": "101",
                    "app": "Updated: 1 → 2",
                    "packages": [{"name": "libssl3", "from": "1.0", "to": "1.1"}],
                }
            ],
        }
    )
    p = tmp_path / "state.json"
    state.dump(p)
    reloaded = FleetState.load(p)
    assert reloaded.lxc[0].packages == [{"name": "libssl3", "from": "1.0", "to": "1.1"}]
    legacy = FleetState.from_raw(
        {
            "fleet_lxc_data": [{"node": "pve-01", "name": "sonarr", "id": "101", "app": "OK"}],
        }
    )
    assert legacy.lxc[0].packages is None


def test_unrelated_optional_serialization_unchanged():
    """PR1 only adds the `packages` field — it must not disturb legacy optional
    serialization: `os`/`snap` defaults stay, and `pkg_count` (no exclude_if)
    still serializes as null, exactly as before."""
    lxc = LxcRecord.model_validate({"node": "pve-01", "name": "sonarr", "id": "101", "app": "OK"})
    dumped = lxc.model_dump()
    assert dumped["os"] == ""
    assert dumped["snap"] is True
    vm = VmRecord(node="pve-01", vmid="200", name="my-vm", status="OK")
    assert vm.model_dump()["pkg_count"] is None
    remote = RemoteRecord(host="web", status="OK")
    assert "pkg_count" in remote.model_dump()
    assert remote.model_dump()["pkg_count"] is None
    node = NodeRecord(node="pve-02", status="OK")
    assert node.model_dump()["pkg_count"] is None


# --- dry-run marker (PR3) -------------------------------------------------- #


def test_node_dry_run_serialization():
    """PR3: the NodeRecord dry-run marker serializes dry_run=true when set
    (node/manager status strings have no simulation variant, so the flag is
    the ledger's only signal) and is omitted entirely when None — real runs
    keep the exact legacy byte shape."""
    dry = NodeRecord(node="pve-02", status="UPDATED", dry_run=True)
    assert dry.model_dump()["dry_run"] is True
    real = NodeRecord(node="pve-02", status="UPDATED")
    assert "dry_run" not in real.model_dump()


def test_node_diagnostics_are_optional_and_omitted_for_legacy_records():
    legacy = NodeRecord(node="pve-01", status="OK")
    dumped = legacy.model_dump()
    assert "reboot_reasons" not in dumped
    assert "checks" not in dumped

    record = NodeRecord(
        node="pve-01",
        status="UPDATED (MANUAL REBOOT REQ)",
        reboot_reasons=["NVIDIA module mismatch: loaded 1, installed 2"],
        checks={"running_kernel": "6.8.12-8-pve", "nvidia_dkms_ready": True},
    )
    assert record.model_dump()["reboot_reasons"] == [
        "NVIDIA module mismatch: loaded 1, installed 2"
    ]
    assert record.model_dump()["checks"]["nvidia_dkms_ready"] is True


def test_node_diagnostics_round_trip_through_state(tmp_path):
    state = FleetState(
        node=[
            NodeRecord(
                node="pve-01",
                status="UPDATED & REBOOTED",
                reboot_reasons=["kernel update: old → new"],
                checks={"pre_reboot": {"nvidia_loaded": "old"}},
            )
        ]
    )
    path = tmp_path / "state.json"
    state.dump(path)
    loaded = FleetState.load(path)
    assert loaded.node[0].reboot_reasons == ["kernel update: old → new"]
    assert loaded.node[0].checks == {"pre_reboot": {"nvidia_loaded": "old"}}


def test_node_dry_run_round_trips_through_dump_and_load(tmp_path):
    """PR3: a dry_run=true record survives dump → load; a legacy key-free
    file loads with dry_run None, never a crash or an invented flag."""
    state = FleetState.from_raw(
        {
            "fleet_node_data": [{"node": "pve-01", "status": "UPDATED", "pkg_count": None, "dry_run": True}],
        }
    )
    p = tmp_path / "state.json"
    state.dump(p)
    reloaded = FleetState.load(p)
    assert reloaded.node[0].dry_run is True
    legacy = FleetState.from_raw(
        {
            "fleet_node_data": [{"node": "pve-01", "status": "UPDATED"}],
        }
    )
    assert legacy.node[0].dry_run is None


def test_from_raw_accepts_fleet_names():
    raw = {
        "fleet_lxc_data": [
            {"node": "pve-01", "name": "x", "id": "1", "app": "Updated: 1 → 2", "os": "OK", "snap": True}
        ],
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
    state = FleetState.from_raw(
        {
            "fleet_lxc_data": [{"node": "pve-01", "name": "x", "id": "1", "app": "OK"}],
            "fleet_changed": True,
        }
    )
    p = tmp_path / "latest.json"
    state.dump(p)
    reloaded = FleetState.load(p)
    assert reloaded.changed is True
    assert reloaded.lxc[0].id == "1"
    # file is valid JSON
    assert json.loads(p.read_text())["changed"] is True
