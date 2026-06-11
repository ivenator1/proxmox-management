"""Tests for proxmox_fleet.history — the Python port of tasks/persist-history.yml.

Covers the run-summary count assembly (mirroring test_persist_history.py), the
file write/prune behaviour, and the new ``briefing`` field that records the exact
Discord message body.
"""
import json
import re

import pytest

from proxmox_fleet import briefing
from proxmox_fleet.history import (
    _ts_now,
    build_run_summary,
    history_summary,
    read_run,
    write_history,
)
from proxmox_fleet.models.state import FleetState


def _state(**kw) -> FleetState:
    return FleetState.from_raw(kw)


# --- count assembly (mirrors test_persist_history.py) ---------------------- #

def test_counts_all_empty():
    summary = build_run_summary(_state(), timestamp="T")
    assert summary["counts"] == {"lxc": 0, "vm": 0, "remote": 0, "node": 0,
                                 "custom": 0, "errors": 0, "warnings": 0}


def test_counts_populated():
    state = _state(
        fleet_lxc_data=[dict(node="n", name="a", id="1", app="OK"),
                        dict(node="n", name="b", id="2", app="OK")],
        fleet_custom_data=[dict(host="h", name="gitea", app="OK")],
        fleet_warning_log=[dict(host="h", task="snap", warning="failed")],
    )
    summary = build_run_summary(state, timestamp="T")
    assert summary["counts"]["lxc"] == 2
    assert summary["counts"]["custom"] == 1
    assert summary["counts"]["warnings"] == 1
    assert summary["counts"]["vm"] == 0


def test_summary_carries_flags_and_lists():
    state = _state(fleet_changed=True, fleet_failed=True,
                   fleet_vm_data=[dict(node="n", vmid="200", name="vm", status="UPDATED")])
    summary = build_run_summary(state, timestamp="2026-01-01")
    assert summary["timestamp"] == "2026-01-01"
    assert summary["changed"] is True
    assert summary["failed"] is True
    assert summary["vm"][0]["name"] == "vm"


# --- briefing field -------------------------------------------------------- #

def test_briefing_field_recorded():
    state = _state(fleet_node_data=[dict(node="pve-01", status="OK")],
                   fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101",
                                        app="Updated: v4.0 → v4.1", os="OK", snap=True)])
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
    run_file = write_history(state, history_dir=tmp_path, keep=30,
                             timestamp="20260101T000000Z", briefing=body)
    assert run_file == tmp_path / "run-20260101T000000Z.json"
    assert run_file.exists()
    latest = tmp_path / "latest.json"
    assert latest.exists()
    assert json.loads(run_file.read_text()) == json.loads(latest.read_text())
    assert json.loads(run_file.read_text())["briefing"] == body


def test_prune_keeps_newest(tmp_path):
    # Write 5 runs with keep=3; oldest two should be removed.
    for i in range(5):
        write_history(_state(), history_dir=tmp_path, keep=3,
                      timestamp=f"2026010{i}T000000Z")
    runs = sorted(p.name for p in tmp_path.glob("run-*.json"))
    assert runs == ["run-20260102T000000Z.json",
                    "run-20260103T000000Z.json",
                    "run-20260104T000000Z.json"]
    assert (tmp_path / "latest.json").exists()


def test_keep_zero_disables_prune(tmp_path):
    for i in range(3):
        write_history(_state(), history_dir=tmp_path, keep=0,
                      timestamp=f"2026010{i}T000000Z")
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
    write_history(_state(fleet_changed=True), history_dir=tmp_path, keep=0,
                  timestamp="20260102T000000000000Z")
    rows = history_summary(tmp_path)
    assert [r["timestamp"] for r in rows] == ["20260102T000000000000Z",
                                              "20260101T000000000000Z"]
    assert rows[0]["changed"] is True and rows[1]["changed"] is False


def test_history_summary_limit(tmp_path):
    for i in range(5):
        write_history(_state(), history_dir=tmp_path, keep=0,
                      timestamp=f"2026010{i}T000000000000Z")
    assert len(history_summary(tmp_path, limit=3)) == 3
    # limit <= 0 means "all runs".
    assert len(history_summary(tmp_path, limit=0)) == 5


def test_history_summary_carries_counts_and_failed(tmp_path):
    state = _state(fleet_failed=True,
                   fleet_lxc_data=[dict(node="n", name="a", id="1", app="FAILED")],
                   fleet_error_log=[dict(host="a", task="update", error="boom")])
    write_history(state, history_dir=tmp_path, keep=0, timestamp="20260101T000000000000Z")
    row = history_summary(tmp_path)[0]
    assert row["failed"] is True
    assert row["counts"]["lxc"] == 1
    assert row["counts"]["errors"] == 1


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
    write_history(state, history_dir=tmp_path, keep=0,
                  timestamp="20260101T000000000000Z", briefing=body)
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
