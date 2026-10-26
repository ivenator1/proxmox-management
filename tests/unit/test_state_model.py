"""Tests for proxmox_fleet.models.state — the typed FleetState records."""

import json

import pytest
from pydantic import ValidationError

from proxmox_fleet.models.state import (
    DiskUsageEntry,
    DiskUsageInsight,
    FleetState,
    HousekeepingSummary,
    LxcRecord,
    NodeRecord,
    RemoteRecord,
    VmRecord,
    WarningEntry,
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


def test_alloy_status_is_optional_and_legacy_safe():
    legacy_lxc = LxcRecord(node="pve-01", name="sonarr", id="101", app="OK")
    legacy_vm = VmRecord(node="pve-01", vmid="200", name="vm", status="OK")
    assert "alloy" not in legacy_lxc.model_dump()
    assert "alloy" not in legacy_vm.model_dump()
    assert LxcRecord(
        node="pve-01", name="sonarr", id="101", app="OK", alloy="Configured"
    ).model_dump()["alloy"] == "Configured"
    assert VmRecord(
        node="pve-01", vmid="200", name="vm", status="", alloy="Installed"
    ).model_dump()["alloy"] == "Installed"


def test_warning_notification_marker_is_optional():
    old = WarningEntry(host="h", task="t", warning="w")
    alloy = WarningEntry(host="h", task="Alloy compliance", warning="drift", notifying=True)
    assert "notifying" not in old.model_dump()
    assert alloy.model_dump()["notifying"] is True


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


# --- housekeeping summary (step 1) ------------------------------------------ #


def test_housekeeping_summary_defaults():
    s = HousekeepingSummary(status="Cleaned")
    assert s.bytes_reclaimed == 0
    assert s.bytes_archived == 0
    assert s.files_pruned == 0


@pytest.mark.parametrize(
    "status",
    ["Configured", "Cleaned", "Backfill pending", "Blocked", "Audit"],
)
def test_housekeeping_summary_accepts_exact_statuses(status):
    assert HousekeepingSummary(status=status).status == status


def test_housekeeping_summary_rejects_unknown_status():
    with pytest.raises(ValidationError):
        HousekeepingSummary(status="Done")


@pytest.mark.parametrize("field", ["bytes_reclaimed", "bytes_archived", "files_pruned"])
def test_housekeeping_summary_rejects_negative_counts(field):
    with pytest.raises(ValidationError):
        HousekeepingSummary(status="Cleaned", **{field: -1})


def test_lxc_housekeeping_omitted_when_none():
    """A guest that did nothing relevant stays key-free (legacy byte shape)."""
    r = LxcRecord(node="pve-01", name="sonarr", id="101", app="OK")
    assert r.housekeeping is None
    assert "housekeeping" not in r.model_dump()


def test_lxc_housekeeping_present_when_set():
    r = LxcRecord(
        node="pve-01",
        name="npm",
        id="123",
        app="OK",
        housekeeping=HousekeepingSummary(
            status="Cleaned", bytes_reclaimed=1024, bytes_archived=2048, files_pruned=3
        ),
    )
    assert r.model_dump()["housekeeping"] == {
        "status": "Cleaned",
        "bytes_reclaimed": 1024,
        "bytes_archived": 2048,
        "files_pruned": 3,
    }


def test_lxc_housekeeping_accepts_dict_and_round_trips(tmp_path):
    state = FleetState(
        lxc=[
            LxcRecord(
                node="pve-01",
                name="pbs",
                id="120",
                app="OK",
                housekeeping={
                    "status": "Backfill pending",
                    "bytes_archived": 4096,
                },
            )
        ]
    )
    assert state.lxc[0].housekeeping is not None
    assert state.lxc[0].housekeeping.status == "Backfill pending"

    p = tmp_path / "state.json"
    state.dump(p)
    loaded = FleetState.load(p)
    assert loaded.lxc[0].housekeeping.status == "Backfill pending"
    assert loaded.lxc[0].housekeeping.bytes_archived == 4096
    assert loaded.lxc[0].housekeeping.bytes_reclaimed == 0


def test_legacy_lxc_record_serialization_unchanged():
    """Adding the optional summary must not add or shift legacy keys."""
    legacy = LxcRecord(node="pve-01", name="sonarr", id="101", app="OK", os="OK", snap=True)
    assert legacy.model_dump() == {
        "node": "pve-01",
        "name": "sonarr",
        "id": "101",
        "app": "OK",
        "os": "OK",
        "snap": True,
    }


# --- disk-usage insight (step 5) -------------------------------------------- #


def _disk_entry(**overrides):
    values = dict(
        path="/var/log",
        allocated_bytes=14 * 1024**3,
        share_percent=70.0,
        max_expected_share_percent=20.0,
    )
    values.update(overrides)
    return DiskUsageEntry(**values)


def _disk_insight(**overrides):
    values = dict(
        status="complete",
        used_bytes=20 * 1024**3,
        entries=[_disk_entry()],
    )
    values.update(overrides)
    return DiskUsageInsight(**values)


def test_disk_usage_entry_defaults_and_metadata():
    entry = _disk_entry()
    assert entry.kind == "directory"
    assert entry.model_dump() == {
        "path": "/var/log",
        "allocated_bytes": 14 * 1024**3,
        "share_percent": 70.0,
        "max_expected_share_percent": 20.0,
        "kind": "directory",
    }


@pytest.mark.parametrize("kind", ["directory", "directory-other", "root-files"])
def test_disk_usage_entry_accepts_exact_kinds(kind):
    assert _disk_entry(kind=kind).kind == kind


def test_disk_usage_entry_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        _disk_entry(kind="file")


@pytest.mark.parametrize(
    "overrides",
    [
        {"allocated_bytes": -1},
        {"allocated_bytes": True},
        {"share_percent": -0.1},
        {"share_percent": 100.1},
        {"share_percent": float("nan")},
        {"share_percent": float("inf")},
        {"share_percent": True},
        {"max_expected_share_percent": -1.0},
        {"max_expected_share_percent": 101.0},
        {"max_expected_share_percent": float("nan")},
        {"max_expected_share_percent": False},
        {"allocated_bytes": 1.5},
    ],
)
def test_disk_usage_entry_rejects_invalid_numbers(overrides):
    with pytest.raises(ValidationError):
        _disk_entry(**overrides)


def test_disk_usage_entry_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        _disk_entry(detail="not an entry field")


def test_disk_usage_entry_accepts_zero_and_boundary_percentages():
    entry = _disk_entry(allocated_bytes=0, share_percent=0, max_expected_share_percent=100)
    assert entry.allocated_bytes == 0
    assert entry.share_percent == 0.0
    assert entry.max_expected_share_percent == 100.0


def test_disk_usage_insight_defaults():
    insight = _disk_insight()
    assert insight.omitted == 0
    assert insight.detail == ""
    assert insight.status == "complete"
    assert insight.used_bytes == 20 * 1024**3
    assert insight.entries[0].path == "/var/log"


@pytest.mark.parametrize("status", ["complete", "partial", "unavailable"])
def test_disk_usage_insight_accepts_exact_statuses(status):
    assert _disk_insight(status=status).status == status


def test_disk_usage_insight_rejects_unknown_status():
    with pytest.raises(ValidationError):
        _disk_insight(status="done")


def test_disk_usage_insight_allows_unknown_used_bytes():
    insight = _disk_insight(status="unavailable", used_bytes=None, entries=[])
    assert insight.used_bytes is None


def test_unavailable_insight_defaults_used_bytes_none_for_exclude_none_roundtrip(tmp_path):
    """History/dashboard serialization may drop None keys; reload must still work."""
    insight = DiskUsageInsight(
        status="unavailable", entries=[], detail="mount table missing"
    )
    assert insight.used_bytes is None
    state = FleetState(
        warnings=[
            WarningEntry(
                host="pve-01/135",
                task="disk space",
                warning="rootfs 77% full",
                disk_usage=insight,
            )
        ]
    )

    dumped = state.model_dump(exclude_none=True)
    dumped_insight = dumped["warnings"][0]["disk_usage"]
    assert "used_bytes" not in dumped_insight
    assert dumped_insight["status"] == "unavailable"

    p = tmp_path / "state.json"
    p.write_text(json.dumps(dumped), encoding="utf-8")
    loaded = FleetState.load(p)
    loaded_insight = loaded.warnings[0].disk_usage
    assert loaded_insight is not None
    assert loaded_insight.status == "unavailable"
    assert loaded_insight.used_bytes is None
    assert loaded_insight.entries == []
    assert loaded_insight.detail == "mount table missing"

    # Legacy warnings stay key-free even under exclude_none.
    legacy = WarningEntry(host="h", task="t", warning="w")
    assert "disk_usage" not in legacy.model_dump(exclude_none=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"used_bytes": -1},
        {"used_bytes": True},
        {"omitted": -1},
        {"omitted": True},
        {"entries": "not-a-list"},
    ],
)
def test_disk_usage_insight_rejects_invalid_counts(overrides):
    with pytest.raises(ValidationError):
        _disk_insight(**overrides)


def test_disk_usage_insight_rejects_more_than_twenty_entries():
    entries = [_disk_entry(path=f"/var/log/{index}") for index in range(21)]
    with pytest.raises(ValidationError):
        _disk_insight(entries=entries)


def test_disk_usage_insight_accepts_twenty_entries():
    entries = [_disk_entry(path=f"/var/log/{index}") for index in range(20)]
    assert len(_disk_insight(entries=entries).entries) == 20


def test_disk_usage_insight_detail_is_bounded():
    assert _disk_insight(detail="x" * 160).detail == "x" * 160
    with pytest.raises(ValidationError):
        _disk_insight(detail="x" * 161)


@pytest.mark.parametrize("detail", ["line\nbreak", "nul\x00byte", "zero\u200bwidth"])
def test_disk_usage_insight_detail_rejects_control_characters(detail):
    with pytest.raises(ValidationError):
        _disk_insight(detail=detail)


def test_disk_usage_insight_with_detail_revalidates_bound():
    insight = _disk_insight(detail="original")
    updated = insight.with_detail("generic disk thresholds")
    assert updated.detail == "generic disk thresholds"
    assert updated.entries[0].path == "/var/log"
    assert insight.detail == "original"
    with pytest.raises(ValidationError):
        insight.with_detail("x" * 161)


def test_disk_usage_insight_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        _disk_insight(root="/")


def test_warning_disk_usage_is_optional_and_legacy_key_free():
    legacy = WarningEntry(host="h", task="t", warning="w")
    assert legacy.disk_usage is None
    assert legacy.model_dump() == {"host": "h", "task": "t", "warning": "w"}


def test_warning_disk_usage_retains_full_metadata_paths(tmp_path):
    long_path = "/var/lib/" + "a" * 200
    insight = _disk_insight(
        entries=[
            _disk_entry(path=long_path, kind="directory-other"),
            _disk_entry(path="/", allocated_bytes=1024, share_percent=1.0, kind="root-files"),
        ],
        omitted=2,
        detail="partial",
    )
    state = FleetState(
        warnings=[
            WarningEntry(
                host="pve-01/135",
                task="disk space",
                warning="Disk space low",
                disk_usage=insight,
            )
        ]
    )
    dumped = state.model_dump()["warnings"][0]
    assert dumped["disk_usage"]["status"] == "complete"
    assert dumped["disk_usage"]["entries"][0]["path"] == long_path
    assert dumped["disk_usage"]["omitted"] == 2
    assert dumped["disk_usage"]["detail"] == "partial"

    p = tmp_path / "state.json"
    state.dump(p)
    loaded = FleetState.load(p)
    loaded_insight = loaded.warnings[0].disk_usage
    assert loaded_insight is not None
    assert loaded_insight.entries[0].path == long_path
    assert loaded_insight.entries[1].kind == "root-files"
    assert loaded_insight.used_bytes == 20 * 1024**3
