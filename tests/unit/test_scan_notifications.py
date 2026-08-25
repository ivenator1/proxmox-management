"""Tests for manual scan state and fleet-briefing notification selection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

import pytest

from proxmox_fleet.scan_notifications import (
    decide_stored_notifications,
    fingerprint,
    load_state,
    record_manual_results,
    render_body,
    save_state,
)

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _entry(**kw: Any) -> Dict[str, Any]:
    base = {
        "host": "web-01",
        "adapter": "truenas_scale",
        "current": "1.2.3",
        "latest": "1.4.0",
        "update_available": True,
        "reboot_required": False,
        "summary": "3 packages pending",
        "details": "Security and bugfix updates",
        "apply_hint": "Proxmox UI → node → Updates → Upgrade",
        "unreachable": False,
        "error": "",
    }
    base.update(kw)
    return base


def _error_entry(**kw: Any) -> Dict[str, Any]:
    entry = _entry(host="db-01", adapter="opnsense", update_available=False, **kw)
    if "error" not in kw:
        entry["error"] = "connection refused"
    return entry


def _fp(entry: Mapping[str, Any]) -> str:
    return fingerprint(entry)


# Fingerprint semantics

def test_fingerprint_stable_across_transient_formatting():
    a = _entry(details="Security  and\nbugfix   updates", summary="3  packages")
    b = _entry(details="Security and bugfix updates", summary="3 packages")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_ignores_transient_and_display_only_fields():
    base = _entry()
    noisy = dict(
        base,
        ts="2026-08-01T12:00:00Z",
        checked_at="irrelevant",
        apply_hint="different hint",
        unreachable=True,
    )
    assert fingerprint(noisy) == fingerprint(base)


@pytest.mark.parametrize(
    "change",
    [
        {"latest": "2.0.0"},
        {"update_available": False},
        {"reboot_required": True},
        {"current": "1.2.4"},
        {"summary": "1 package pending"},
        {"error": "boom"},
        {"adapter": "opnsense"},
    ],
)
def test_fingerprint_sensitive_to_semantics(change):
    assert fingerprint(_entry()) != fingerprint(_entry(**change))


# Node-like body rendering

def test_body_sections_are_nested_and_have_no_trailing_newline():
    body = render_body([_entry()], [_error_entry()])

    assert body.startswith("**Manual Systems: (ATTENTION REQUIRED)**")
    assert "- Updates / reboots" in body
    assert "  - **web-01** — 1.2.3 → 1.4.0" in body
    assert "    - Security and bugfix updates" in body
    assert "    - GUI apply: Proxmox UI → node → Updates → Upgrade" in body
    assert "- Check errors" in body
    assert "  - **db-01** — connection refused" in body
    assert body.index("- Updates / reboots") < body.index("- Check errors")
    assert body == body.rstrip()


def test_render_body_indents_every_multiline_detail():
    body = render_body(
        [_entry(details="Checking upgrades...\nVersion 1.4.0 available")],
        [],
    )

    assert "    - Checking upgrades..." in body
    assert "    - Version 1.4.0 available" in body
    assert "\nVersion 1.4.0" not in body


def test_render_body_reboot_only_entry():
    body = render_body(
        [_entry(update_available=False, reboot_required=True, current="", latest="")],
        [],
    )
    assert "update available (reboot required)" in body


def test_render_body_truncates_at_limit():
    body = render_body([_entry(details="x" * 5000)], [])
    assert len(body) <= 4000
    assert body.endswith("...")


def test_render_body_empty_with_no_entries():
    assert render_body([], []) == ""


def test_render_body_omits_absent_details_and_hint():
    body = render_body([_entry(details="", apply_hint="")], [])
    assert body == (
        "**Manual Systems: (ATTENTION REQUIRED)**\n"
        "- Updates / reboots\n"
        "  - **web-01** — 1.2.3 → 1.4.0"
    )


# Persistence

def test_state_roundtrip_is_version_two_and_atomic(tmp_path: Path):
    state = {"web-01": {"fingerprint": "abc", "entry": _entry()}}

    assert save_state(tmp_path, state) is True
    assert load_state(tmp_path) == state
    on_disk = json.loads((tmp_path / "manual-notify-state.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == 2
    assert on_disk["hosts"] == state
    assert not (tmp_path / "manual-notify-state.json.tmp").exists()


def test_load_state_is_defensive(tmp_path: Path):
    assert load_state(tmp_path) == {}
    path = tmp_path / "manual-notify-state.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_state(tmp_path) == {}
    path.write_text(json.dumps({"hosts": "nope"}), encoding="utf-8")
    assert load_state(tmp_path) == {}


def test_load_state_drops_fingerprintless_entries(tmp_path: Path):
    (tmp_path / "manual-notify-state.json").write_text(
        json.dumps(
            {
                "version": 2,
                "hosts": {
                    "ok": {"fingerprint": "x"},
                    "bad": {"last_notified": "t"},
                },
            }
        ),
        encoding="utf-8",
    )
    assert load_state(tmp_path) == {"ok": {"fingerprint": "x"}}


def test_state_write_failure_is_nonfatal(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    assert record_manual_results([_entry()], history_dir=blocker) is False


# Scan recording and fleet-run selection

def test_scan_records_actionable_result_without_marking_it_notified(tmp_path: Path):
    assert record_manual_results([_entry()], history_dir=tmp_path) is True

    stored = load_state(tmp_path)["web-01"]
    assert stored["fingerprint"] == _fp(_entry())
    assert stored["entry"]["latest"] == "1.4.0"
    assert "last_notified" not in stored
    assert "notified_fingerprint" not in stored

    decision = decide_stored_notifications(load_state(tmp_path), now=NOW)
    assert decision.notify is True
    assert decision.failed is False
    assert decision.reasons == {"web-01": "first"}


def test_hosts_using_same_adapter_keep_independent_sorted_state(tmp_path: Path):
    record_manual_results(
        [_entry(host="nas-b"), _entry(host="nas-a")],
        history_dir=tmp_path,
    )
    decision = decide_stored_notifications(load_state(tmp_path), now=NOW)
    assert decision.selected == ["nas-a", "nas-b"]


def test_fleet_commit_then_daily_reminder(tmp_path: Path):
    record_manual_results([_entry()], history_dir=tmp_path)
    first = decide_stored_notifications(load_state(tmp_path), now=NOW)
    save_state(tmp_path, first.new_state)

    under_window = decide_stored_notifications(
        load_state(tmp_path), now=NOW + timedelta(hours=23), reminder_hours=24
    )
    assert under_window.notify is False

    reminder = decide_stored_notifications(
        load_state(tmp_path), now=NOW + timedelta(hours=24), reminder_hours=24
    )
    assert reminder.notify is True
    assert reminder.reasons == {"web-01": "reminder"}


def test_changed_scan_result_waits_for_fleet_briefing(tmp_path: Path):
    record_manual_results([_entry()], history_dir=tmp_path)
    first = decide_stored_notifications(load_state(tmp_path), now=NOW)
    save_state(tmp_path, first.new_state)

    changed = _entry(latest="2.0.0")
    record_manual_results([changed], history_dir=tmp_path)
    stored = load_state(tmp_path)["web-01"]
    assert stored["fingerprint"] == _fp(changed)
    assert stored["notified_fingerprint"] == _fp(_entry())

    decision = decide_stored_notifications(
        load_state(tmp_path), now=NOW + timedelta(hours=1)
    )
    assert decision.reasons == {"web-01": "change"}
    assert "2.0.0" in decision.body


def test_scan_clean_clears_unreachable_and_absent_hosts_stay(tmp_path: Path):
    record_manual_results(
        [_entry(), _error_entry()],
        history_dir=tmp_path,
    )
    record_manual_results([_entry(unreachable=True)], history_dir=tmp_path)
    assert set(load_state(tmp_path)) == {"web-01", "db-01"}

    clean = _entry(update_available=False, reboot_required=False)
    record_manual_results([clean], history_dir=tmp_path)
    assert set(load_state(tmp_path)) == {"db-01"}


def test_error_is_manual_attention_and_force_overrides_window(tmp_path: Path):
    record_manual_results([_error_entry()], history_dir=tmp_path)
    first = decide_stored_notifications(load_state(tmp_path), now=NOW)
    assert first.failed is True
    assert first.error_count == 1
    assert "- Check errors" in first.body
    save_state(tmp_path, first.new_state)

    forced = decide_stored_notifications(
        load_state(tmp_path), now=NOW + timedelta(hours=1), force=True
    )
    assert forced.notify is True
    assert forced.reasons == {"db-01": "force"}


def test_naive_now_is_persisted_as_utc(tmp_path: Path):
    record_manual_results([_entry()], history_dir=tmp_path)
    decision = decide_stored_notifications(
        load_state(tmp_path), now=datetime(2026, 8, 1, 12, 0, 0)
    )
    assert decision.new_state["web-01"]["last_notified"] == "2026-08-01T12:00:00+00:00"


def test_version_one_state_migrates_on_next_scan(tmp_path: Path):
    old = {
        "web-01": {
            "fingerprint": _fp(_entry()),
            "last_notified": NOW.isoformat(),
        }
    }
    save_state(tmp_path, old)

    record_manual_results([_entry()], history_dir=tmp_path)

    stored = load_state(tmp_path)["web-01"]
    assert stored["notified_fingerprint"] == _fp(_entry())
    assert stored["entry"]["host"] == "web-01"
    assert decide_stored_notifications(
        load_state(tmp_path), now=NOW + timedelta(hours=1)
    ).notify is False
