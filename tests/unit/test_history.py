"""Tests for proxmox_fleet.history — the Python port of tasks/persist-history.yml.

Covers the run-summary count assembly (mirroring test_persist_history.py), the
file write/prune behaviour, and the new ``briefing`` field that records the exact
Discord message body.
"""

import json
from pathlib import Path
import re
from typing import Dict

import pytest

from proxmox_fleet import briefing
from proxmox_fleet.history import (
    _ts_now,
    build_run_summary,
    count_packages,
    count_updates,
    history_summary,
    read_run,
    read_totals,
    write_history,
)
from proxmox_fleet.models.state import FleetState


def _state(**kw) -> FleetState:
    return FleetState.from_raw(kw)


# --- count assembly (mirrors test_persist_history.py) ---------------------- #


def test_counts_all_empty():
    summary = build_run_summary(_state(), timestamp="T")
    assert summary["counts"] == {"lxc": 0, "vm": 0, "remote": 0, "node": 0, "custom": 0, "errors": 0, "warnings": 0}


def test_counts_populated():
    state = _state(
        fleet_lxc_data=[dict(node="n", name="a", id="1", app="OK"), dict(node="n", name="b", id="2", app="OK")],
        fleet_custom_data=[dict(host="h", name="gitea", app="OK")],
        fleet_warning_log=[dict(host="h", task="snap", warning="failed")],
    )
    summary = build_run_summary(state, timestamp="T")
    assert summary["counts"]["lxc"] == 2
    assert summary["counts"]["custom"] == 1
    assert summary["counts"]["warnings"] == 1
    assert summary["counts"]["vm"] == 0


def test_summary_carries_flags_and_lists():
    state = _state(
        fleet_changed=True, fleet_failed=True, fleet_vm_data=[dict(node="n", vmid="200", name="vm", status="UPDATED")]
    )
    summary = build_run_summary(state, timestamp="2026-01-01")
    assert summary["timestamp"] == "2026-01-01"
    assert summary["changed"] is True
    assert summary["failed"] is True
    assert summary["vm"][0]["name"] == "vm"


# --- briefing field -------------------------------------------------------- #


def test_briefing_field_recorded():
    state = _state(
        fleet_node_data=[dict(node="pve-01", status="OK")],
        fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", app="Updated: v4.0 → v4.1", os="OK", snap=True)],
    )
    body = briefing.prepare_body(state)
    summary = build_run_summary(state, timestamp="T", briefing=body)
    assert summary["briefing"] == body
    assert "sonarr" in summary["briefing"]


def test_briefing_field_absent_when_not_passed():
    summary = build_run_summary(_state(), timestamp="T")
    assert "briefing" not in summary


# --- file write / prune ---------------------------------------------------- #


def test_write_creates_run_and_latest(tmp_path):
    state = _state(fleet_changed=True)
    body = briefing.prepare_body(state)
    run_file = write_history(state, history_dir=tmp_path, keep=30, timestamp="20260101T000000Z", briefing=body)
    assert run_file == tmp_path / "run-20260101T000000Z.json"
    assert run_file.exists()
    latest = tmp_path / "latest.json"
    assert latest.exists()
    assert json.loads(run_file.read_text()) == json.loads(latest.read_text())
    assert json.loads(run_file.read_text())["briefing"] == body


def test_prune_keeps_newest(tmp_path):
    # Write 5 runs with keep=3; oldest two should be removed.
    for i in range(5):
        write_history(_state(), history_dir=tmp_path, keep=3, timestamp=f"2026010{i}T000000Z")
    runs = sorted(p.name for p in tmp_path.glob("run-*.json"))
    assert runs == ["run-20260102T000000Z.json", "run-20260103T000000Z.json", "run-20260104T000000Z.json"]
    assert (tmp_path / "latest.json").exists()


def test_keep_zero_disables_prune(tmp_path):
    for i in range(3):
        write_history(_state(), history_dir=tmp_path, keep=0, timestamp=f"2026010{i}T000000Z")
    assert len(list(tmp_path.glob("run-*.json"))) == 3


def test_default_timestamp_used(tmp_path):
    run_file = write_history(_state(), history_dir=tmp_path, keep=30)
    assert run_file.name.startswith("run-") and run_file.name.endswith("Z.json")
    data = json.loads(run_file.read_text())
    assert data["timestamp"].endswith("Z")


def test_ts_now_includes_microseconds():
    ts = _ts_now()
    # Format: YYYYMMDDTHHMMSSffffffZ (6 microsecond digits between seconds and Z)
    assert re.match(r"^\d{8}T\d{6}\d{6}Z$", ts), f"unexpected format: {ts}"


# --- history_summary (read-back) -------------------------------------------- #


def test_history_summary_newest_first(tmp_path):
    write_history(_state(), history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    write_history(_state(fleet_changed=True), history_dir=tmp_path, keep=0, timestamp="20260102T000000000000Z")
    rows = history_summary(tmp_path)
    assert [r["timestamp"] for r in rows] == ["20260102T000000000000Z", "20260101T000000000000Z"]
    assert rows[0]["changed"] is True and rows[1]["changed"] is False


def test_history_summary_limit(tmp_path):
    for i in range(5):
        write_history(_state(), history_dir=tmp_path, keep=0, timestamp=f"2026010{i}T000000000000Z")
    assert len(history_summary(tmp_path, limit=3)) == 3
    # limit <= 0 means "all runs".
    assert len(history_summary(tmp_path, limit=0)) == 5


def test_history_summary_carries_counts_and_failed(tmp_path):
    state = _state(
        fleet_failed=True,
        fleet_lxc_data=[dict(node="n", name="a", id="1", app="FAILED")],
        fleet_error_log=[dict(host="a", task="update", error="boom")],
    )
    write_history(state, history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    row = history_summary(tmp_path)[0]
    assert row["failed"] is True
    assert row["counts"]["lxc"] == 1
    assert row["counts"]["errors"] == 1


def test_count_updates_os_and_app():
    summary = build_run_summary(
        _state(
            fleet_lxc_data=[
                # app updated, OS updated → counts in both
                dict(node="n", name="a", id="1", app="Updated: v4.0 → v4.1", os="Updated (3 upgraded)"),
                # rescue statuses count in neither
                dict(node="n", name="b", id="2", app="FAILED + ROLLED BACK", os="OK"),
                # OS-only update ("& Rebooted" variant), no script
                dict(node="n", name="c", id="3", app="NO SCRIPT", os="Updated (1 upgraded) & Rebooted"),
            ],
            fleet_vm_data=[
                dict(node="n", vmid="200", name="vm", status="UPDATED & REBOOTED"),
                # dry-run "WOULD UPDATE" is not an applied update
                dict(node="n", vmid="201", name="vm2", status="WOULD UPDATE"),
            ],
            fleet_remote_data=[dict(host="web", status="UPDATED")],
            fleet_node_data=[dict(node="pve", status="UPDATED (MANUAL REBOOT REQ)"), dict(node="pve2", status="OK")],
        ),
        timestamp="T",
    )
    assert count_updates(summary) == {"os": 5, "app": 1}


def test_count_updates_empty_run():
    assert count_updates(build_run_summary(_state(), timestamp="T")) == {"os": 0, "app": 0}


def test_count_updates_excludes_dry_run_node_records():
    """Node/manager status strings have no simulation variant (a dry run
    reports plain 'UPDATED'), so the record's dry_run flag is the only signal
    count_updates has — dry_run=true records must be excluded while identical
    real statuses still count."""
    summary = build_run_summary(
        _state(
            fleet_node_data=[
                # simulated run: same status string, flagged dry_run → not counted
                {"node": "pve-01", "status": "UPDATED", "dry_run": True},
                # real run: same status string, no flag → counted
                {"node": "pve-02", "status": "UPDATED"},
                # the manager self-update follows the same rule
                {"node": "Ansible-Manager", "status": "UPDATED", "dry_run": True},
                {"node": "Ansible-Manager", "status": "UPDATED"},
            ],
        ),
        timestamp="T",
    )
    assert count_updates(summary) == {"os": 2, "app": 0}


# --- package counts & all-time totals -------------------------------------- #


def test_count_packages_from_records():
    run = {
        "lxc": [{"os": "Updated (12 upgraded) & Rebooted"}, {"os": "Updated (3 upgraded)"}, {"os": "OK"}],
        # VM records carry pkg_count directly; status parsing is the fallback
        "vm": [{"status": "UPDATED", "pkg_count": 5}, {"status": "Updated (2 upgraded)"}],
        "remote": [{"status": "UPDATED"}],  # no count anywhere → contributes 0
    }
    assert count_packages(run) == 22


def test_count_packages_empty_run():
    assert count_packages({}) == 0
    assert count_packages(build_run_summary(_state(), timestamp="T")) == 0


def _lxc_run(pkg=0, app_updated=False):
    return _state(
        fleet_lxc_data=[
            dict(
                node="n",
                name="a",
                id="1",
                app="Updated: v1 → v2" if app_updated else "OK",
                os=f"Updated ({pkg} upgraded)" if pkg else "OK",
            )
        ]
    )


def test_write_history_accumulates_totals(tmp_path):
    write_history(_lxc_run(pkg=4, app_updated=True), history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    write_history(_lxc_run(pkg=2), history_dir=tmp_path, keep=0, timestamp="20260102T000000000000Z")
    totals = json.loads((tmp_path / "totals.json").read_text())
    assert totals["packages"] == 6
    assert totals["app_updates"] == 1
    assert totals["runs"] == 2
    assert totals["since"] == "20260101T000000000000Z"


def test_totals_survive_prune(tmp_path):
    # keep=1 deletes older run files, but totals.json keeps counting them
    for i, pkg in enumerate((5, 3, 1)):
        write_history(_lxc_run(pkg=pkg), history_dir=tmp_path, keep=1, timestamp=f"2026010{i + 1}T000000000000Z")
    assert len(list(tmp_path.glob("run-*.json"))) == 1
    totals = read_totals(tmp_path)
    assert totals["packages"] == 9
    assert totals["runs"] == 3
    assert totals["since"] == "20260101T000000000000Z"


def test_totals_reseed_from_retained_when_missing(tmp_path):
    write_history(_lxc_run(pkg=5), history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    (tmp_path / "totals.json").unlink()
    # next write finds no totals — reseeds from the files still on disk
    write_history(_lxc_run(pkg=2), history_dir=tmp_path, keep=0, timestamp="20260102T000000000000Z")
    totals = read_totals(tmp_path)
    assert totals["packages"] == 7
    assert totals["runs"] == 2


def test_read_totals_fallback_sums_run_files(tmp_path):
    # a dir written before the accumulator existed: run files, no totals.json
    write_history(_lxc_run(pkg=3, app_updated=True), history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    (tmp_path / "totals.json").unlink()
    totals = read_totals(tmp_path)
    assert totals == {"packages": 3, "app_updates": 1, "runs": 1, "since": "20260101T000000000000Z"}


def test_read_totals_empty_dir(tmp_path):
    assert read_totals(tmp_path / "missing") == {"packages": 0, "app_updates": 0, "runs": 0, "since": None}


def test_history_summary_carries_update_counts(tmp_path):
    state = _state(
        fleet_lxc_data=[dict(node="n", name="a", id="1", app="UPDATED", os="OK")],
        fleet_remote_data=[dict(host="web", status="UPDATED & REBOOTED")],
    )
    write_history(state, history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    row = history_summary(tmp_path)[0]
    assert row["updates"] == {"os": 1, "app": 1}


def test_history_summary_skips_corrupt_files(tmp_path):
    write_history(_state(), history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    (tmp_path / "run-20260102T000000000000Z.json").write_text("{truncated", encoding="utf-8")
    rows = history_summary(tmp_path)
    assert [r["timestamp"] for r in rows] == ["20260101T000000000000Z"]


def test_history_summary_empty_dir(tmp_path):
    assert history_summary(tmp_path) == []
    assert history_summary(tmp_path / "missing") == []


# --- read_run ---------------------------------------------------------------- #


def test_read_run_latest(tmp_path):
    state = _state(fleet_changed=True)
    body = briefing.prepare_body(state)
    write_history(state, history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z", briefing=body)
    run = read_run(tmp_path, "latest")
    assert run["timestamp"] == "20260101T000000000000Z"
    assert run["briefing"] == body


def test_read_run_ref_forms_equivalent(tmp_path):
    write_history(_state(), history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    bare = read_run(tmp_path, "20260101T000000000000Z")
    prefixed = read_run(tmp_path, "run-20260101T000000000000Z")
    filename = read_run(tmp_path, "run-20260101T000000000000Z.json")
    assert bare == prefixed == filename


def test_read_run_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_run(tmp_path, "latest")
    with pytest.raises(FileNotFoundError):
        read_run(tmp_path, "20990101T000000000000Z")


def test_read_run_non_object_payload_raises(tmp_path):
    (tmp_path / "latest.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError):
        read_run(tmp_path, "latest")


# --- package-detail stripping (PR1) ----------------------------------------- #

from proxmox_fleet.history import _strip_package_detail  # noqa: E402


def _run_with_packages():
    """A run whose four record buckets all carry a `packages` list (PR1)."""
    return _state(
        fleet_lxc_data=[
            dict(
                node="pve-01",
                name="sonarr",
                id="101",
                app="OK",
                os="Updated (2 upgraded)",
                snap=True,
                packages=[{"name": "libssl3", "from": "a", "to": "b"}],
            )
        ],
        fleet_vm_data=[
            dict(
                node="pve-01",
                vmid="200",
                name="my-vm",
                status="UPDATED",
                pkg_count=1,
                packages=[{"name": "curl", "from": "1", "to": "2"}],
            )
        ],
        fleet_remote_data=[
            dict(host="web", status="UPDATED", pkg_count=1, packages=[{"name": "nginx", "from": "x", "to": "y"}])
        ],
        fleet_node_data=[
            dict(node="pve-02", status="UPDATED", pkg_count=1, packages=[{"name": "kernel", "from": "1", "to": "2"}])
        ],
    )


def _bucket_packages(path) -> Dict[str, bool]:
    """Which buckets still carry a `packages` key in one run file."""
    data = json.loads(Path(path).read_text())
    return {b: "packages" in (data.get(b) or [{}])[0] for b in ("lxc", "vm", "remote", "node")}


def test_strip_package_detail_removes_old_keeps_newest(tmp_path):
    """keep_detail=1 → the newest run keeps packages, older runs lose the key in
    every bucket; latest.json and totals.json are untouched."""
    runs = []
    for i in range(3):
        runs.append(
            write_history(
                _run_with_packages(), history_dir=tmp_path, keep=0, keep_detail=1, timestamp=f"2026010{i}T000000000000Z"
            )
        )
    assert _bucket_packages(runs[2]) == {"lxc": True, "vm": True, "remote": True, "node": True}
    assert _bucket_packages(runs[1]) == {"lxc": False, "vm": False, "remote": False, "node": False}
    assert _bucket_packages(runs[0]) == {"lxc": False, "vm": False, "remote": False, "node": False}
    # latest.json mirrors the newest run — never stripped
    latest = json.loads((tmp_path / "latest.json").read_text())
    assert "packages" in (latest.get("vm") or [{}])[0]
    # totals are status-string / pkg_count based — unaffected by the strip
    totals = read_totals(tmp_path)
    assert totals["packages"] == 15  # (2 + 1 + 1 + 1) per run × 3 runs
    assert totals["runs"] == 3


def test_strip_package_detail_is_idempotent(tmp_path):
    for i in range(3):
        write_history(
            _run_with_packages(), history_dir=tmp_path, keep=0, keep_detail=1, timestamp=f"2026010{i}T000000000000Z"
        )
    older = tmp_path / "run-20260100T000000000000Z.json"
    assert "packages" not in older.read_text()
    before = older.read_text()
    # a second pass rewrites nothing — the file is byte-identical
    _strip_package_detail(tmp_path, 1)
    assert older.read_text() == before


def test_strip_package_detail_zero_disables(tmp_path):
    """keep_detail <= 0 → nothing is ever stripped."""
    for i in range(3):
        write_history(
            _run_with_packages(), history_dir=tmp_path, keep=0, keep_detail=0, timestamp=f"2026010{i}T000000000000Z"
        )
    for run_file in tmp_path.glob("run-*.json"):
        assert "packages" in run_file.read_text()


def test_strip_package_detail_never_touches_latest(tmp_path):
    """_strip_package_detail only rewrites run-*.json — latest.json survives even
    when a hypothetical keep would target the newest run."""
    for i in range(2):
        write_history(
            _run_with_packages(), history_dir=tmp_path, keep=0, keep_detail=0, timestamp=f"2026010{i}T000000000000Z"
        )
    latest_before = (tmp_path / "latest.json").read_text()
    _strip_package_detail(tmp_path, 1)
    assert (tmp_path / "latest.json").read_text() == latest_before


# --- PR3: per-host ledger wiring -------------------------------------------- #


def test_write_history_observes_ledger(tmp_path):
    """write_history folds every record into hosts.json (after totals)."""
    state = _state(
        fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", app="OK", os="Updated (2 upgraded)", snap=True)],
        fleet_remote_data=[dict(host="web-01", status="OK")],
    )
    write_history(state, history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    hosts = json.loads((tmp_path / "hosts.json").read_text())["hosts"]
    # LXC identity is node/id, multi-cluster-safe — not the container name.
    assert "pve-01/101" in hosts
    assert "sonarr" not in hosts
    assert hosts["pve-01/101"]["last_changed_ts"] == "20260101T000000000000Z"
    assert hosts["pve-01/101"]["last_status"] == "Updated (2 upgraded)"
    assert hosts["web-01"]["last_run_ts"] == "20260101T000000000000Z"
    assert "last_changed_ts" not in hosts["web-01"]


def test_write_history_corrupt_ledger_does_not_fail(tmp_path):
    """A corrupt hosts.json must never fail a run — write_history recovers."""
    (tmp_path / "hosts.json").write_text("{broken", encoding="utf-8")
    state = _state(fleet_remote_data=[dict(host="web-01", status="UPDATED")])
    write_history(state, history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    data = json.loads((tmp_path / "hosts.json").read_text())
    assert data["hosts"]["web-01"]["last_changed_ts"] == "20260101T000000000000Z"
