"""Tests for proxmox_fleet.scan_notifications — manual-mapping scan notifications.

The pure selection core (fingerprint, state machine, body render) runs with no
I/O; the one-call integration (load → decide → dispatch → persist) uses a
recording dispatch fake and tmp_path as the history dir, so no network or
Ansible is touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

import pytest

from proxmox_fleet import scan_notifications as scan_notif
from proxmox_fleet.scan_notifications import (
    decide_manual_notifications,
    fingerprint,
    load_state,
    render_body,
    run_manual_notifications,
    save_state,
    scan_color,
    scan_ntfy_title,
    scan_title,
)

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _entry(**kw: Any) -> Dict[str, Any]:
    """A normalized manual mapping entry, overridable per test."""
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


def _state_entry(entry: Mapping[str, Any], last_notified: datetime) -> Dict[str, str]:
    return {"fingerprint": _fp(entry), "last_notified": last_notified.astimezone(timezone.utc).isoformat()}


class _RecordingDispatch:
    """Records dispatch attempts instead of sending anything."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, notifiers_list, **kwargs):
        self.calls.append({"notifiers": list(notifiers_list), **kwargs})


# --------------------------------------------------------------------------
# Fingerprint semantics
# --------------------------------------------------------------------------

def test_fingerprint_stable_across_transient_formatting():
    a = _entry(details="Security  and\nbugfix   updates", summary="3  packages")
    b = _entry(details="Security and bugfix updates", summary="3 packages")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_ignores_extra_transient_keys():
    base = _entry()
    noisy = dict(base, ts="2026-08-01T12:00:00Z", checked_at="irrelevant")
    assert fingerprint(noisy) == fingerprint(base)


def test_fingerprint_excludes_display_only_fields():
    a = _entry(apply_hint="GUI → Upgrade", unreachable=True)
    b = _entry(apply_hint="another hint", unreachable=False)
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_sensitive_to_semantics():
    base = _entry()
    assert fingerprint(base) != fingerprint(_entry(latest="2.0.0"))
    assert fingerprint(base) != fingerprint(_entry(update_available=False))
    assert fingerprint(base) != fingerprint(_entry(reboot_required=True))
    assert fingerprint(base) != fingerprint(_entry(current="1.2.4"))
    assert fingerprint(base) != fingerprint(_entry(summary="1 package pending"))
    assert fingerprint(base) != fingerprint(_entry(error="boom"))
    assert fingerprint(base) != fingerprint(_entry(adapter="opnsense"))


def test_hosts_using_same_adapter_keep_independent_state():
    first = _entry(host="nas-a")
    second = _entry(host="nas-b")
    decision = decide_manual_notifications([first, second], {}, now=NOW)
    assert decision.selected == ["nas-a", "nas-b"]
    assert set(decision.new_state) == {"nas-a", "nas-b"}


# --------------------------------------------------------------------------
# Selection — pending update/reboot
# --------------------------------------------------------------------------

def test_first_pending_notifies_and_advances_state():
    entry = _entry()
    decision = decide_manual_notifications([entry], {}, now=NOW)

    assert decision.notify is True
    assert decision.failed is False
    assert decision.selected == ["web-01"]
    assert decision.reasons == {"web-01": "first"}
    assert decision.pending_count == 1
    assert decision.error_count == 0

    stored = decision.new_state["web-01"]
    assert stored["fingerprint"] == _fp(entry)
    assert stored["last_notified"] == NOW.astimezone(timezone.utc).isoformat()


def test_changed_fingerprint_re_notifies():
    old = _entry(latest="1.4.0")
    state = {"web-01": _state_entry(old, NOW - timedelta(hours=1))}
    changed = _entry(latest="2.0.0")

    decision = decide_manual_notifications([changed], state, now=NOW)

    assert decision.notify is True
    assert decision.reasons == {"web-01": "change"}
    assert decision.new_state["web-01"]["fingerprint"] == _fp(changed)


def test_under_reminder_suppressed_and_state_untouched():
    entry = _entry()
    last = NOW - timedelta(hours=23)
    state = {"web-01": _state_entry(entry, last)}

    decision = decide_manual_notifications([entry], state, now=NOW, reminder_hours=24)

    assert decision.notify is False
    assert decision.selected == []
    assert decision.new_state == state  # no advance, no wipe


def test_exact_reminder_notifies():
    entry = _entry()
    last = NOW - timedelta(hours=24)  # exact boundary
    state = {"web-01": _state_entry(entry, last)}

    decision = decide_manual_notifications([entry], state, now=NOW, reminder_hours=24)

    assert decision.notify is True
    assert decision.reasons == {"web-01": "reminder"}
    assert decision.new_state["web-01"]["last_notified"] == NOW.astimezone(timezone.utc).isoformat()


def test_past_reminder_notifies():
    entry = _entry()
    last = NOW - timedelta(hours=25)
    state = {"web-01": _state_entry(entry, last)}

    decision = decide_manual_notifications([entry], state, now=NOW, reminder_hours=24)

    assert decision.notify is True
    assert decision.reasons == {"web-01": "reminder"}


def test_reboot_only_entry_is_pending():
    entry = _entry(update_available=False, reboot_required=True, latest="", current="")
    decision = decide_manual_notifications([entry], {}, now=NOW)

    assert decision.notify is True
    assert decision.pending_count == 1
    assert "(reboot required)" in decision.body


def test_clean_host_clears_its_own_state():
    state = {"web-01": _state_entry(_entry(), NOW - timedelta(hours=5))}
    clean = _entry(update_available=False, reboot_required=False, latest="1.2.3", current="1.2.3")

    decision = decide_manual_notifications([clean], state, now=NOW)

    assert decision.notify is False
    assert "web-01" not in decision.new_state


def test_absent_hosts_keep_state_in_limited_scan():
    state = {
        "web-01": _state_entry(_entry(), NOW - timedelta(hours=5)),
        "db-01": _state_entry(_error_entry(), NOW - timedelta(hours=5)),
    }
    # Limited scan: only web-01 is observed, and it is now clean.
    clean = _entry(update_available=False, reboot_required=False)
    decision = decide_manual_notifications([clean], state, now=NOW)

    assert "web-01" not in decision.new_state  # observed clean → cleared
    assert "db-01" in decision.new_state  # unobserved → preserved


def test_force_overrides_reminder_window():
    entry = _entry()
    state = {"web-01": _state_entry(entry, NOW - timedelta(hours=1))}

    decision = decide_manual_notifications([entry], state, now=NOW, reminder_hours=24, force=True)

    assert decision.notify is True
    assert decision.reasons == {"web-01": "force"}


def test_naive_now_treated_as_utc():
    entry = _entry()
    naive = datetime(2026, 8, 1, 12, 0, 0)

    decision = decide_manual_notifications([entry], {}, now=naive)

    assert decision.new_state["web-01"]["last_notified"] == "2026-08-01T12:00:00+00:00"


# --------------------------------------------------------------------------
# Selection — genuine check errors
# --------------------------------------------------------------------------

def test_error_first_notifies_with_failure_severity():
    entry = _error_entry()
    decision = decide_manual_notifications([entry], {}, now=NOW)

    assert decision.notify is True
    assert decision.failed is True
    assert decision.error_count == 1
    assert decision.pending_count == 0
    assert decision.reasons == {"db-01": "first"}
    assert "**Manual check errors**" in decision.body
    assert "**db-01** — connection refused" in decision.body


def test_changed_error_re_notifies():
    old = _error_entry(error="timeout")
    state = {"db-01": _state_entry(old, NOW - timedelta(hours=1))}
    changed = _error_entry(error="connection refused")

    decision = decide_manual_notifications([changed], state, now=NOW)

    assert decision.notify is True
    assert decision.failed is True
    assert decision.reasons == {"db-01": "change"}


def test_error_under_reminder_suppressed():
    entry = _error_entry()
    state = {"db-01": _state_entry(entry, NOW - timedelta(hours=1))}

    decision = decide_manual_notifications([entry], state, now=NOW, reminder_hours=24)

    assert decision.notify is False
    assert decision.failed is False  # nothing selected → nothing failed


def test_error_wins_over_pending_in_one_entry():
    entry = _entry(update_available=True, error="check aborted")
    decision = decide_manual_notifications([entry], {}, now=NOW)

    assert decision.notify is True
    assert decision.failed is True
    assert decision.error_count == 1
    assert decision.pending_count == 0


# --------------------------------------------------------------------------
# Unreachable hosts
# --------------------------------------------------------------------------

def test_unreachable_never_notifies_or_fails():
    entry = _error_entry(unreachable=True)
    decision = decide_manual_notifications([entry], {}, now=NOW)

    assert decision.notify is False
    assert decision.failed is False
    assert decision.selected == []
    assert decision.body == ""


def test_unreachable_keeps_existing_state():
    state = {"db-01": _state_entry(_error_entry(), NOW - timedelta(hours=5))}
    entry = _error_entry(unreachable=True)

    decision = decide_manual_notifications([entry], state, now=NOW)

    assert decision.new_state == state  # untouched, even though observed


# --------------------------------------------------------------------------
# Body rendering
# --------------------------------------------------------------------------

def test_body_sections_no_trailing_newline():
    pending = _entry()
    error = _error_entry()
    decision = decide_manual_notifications([pending, error], {}, now=NOW)

    body = decision.body
    assert body.startswith("**Manual updates required**")
    assert "**Manual check errors**" in body
    assert "**web-01** — 1.2.3 → 1.4.0" in body
    assert "Security and bugfix updates" in body
    assert "GUI apply: Proxmox UI → node → Updates → Upgrade" in body
    assert body.index("**Manual updates required**") < body.index("**Manual check errors**")
    assert not body.endswith("\n")
    assert body == body.rstrip()


def test_render_body_truncates_at_limit():
    entry = _entry(details="x" * 5000)
    body = render_body([entry], [])

    assert len(body) <= 4000
    assert body.endswith("...")
    assert not body.endswith("\n")


def test_render_body_empty_with_no_entries():
    assert render_body([], []) == ""


def test_render_body_error_section_only():
    body = render_body([], [_error_entry()])
    assert body.startswith("**Manual check errors**")
    assert "**db-01** — connection refused" in body


def test_render_body_no_details_or_hint_lines_when_absent():
    body = render_body([_entry(details="", apply_hint="")], [])
    assert "GUI apply" not in body
    assert body == "**Manual updates required**\n- **web-01** — 1.2.3 → 1.4.0"


# --------------------------------------------------------------------------
# Title/colour metadata
# --------------------------------------------------------------------------

def test_severity_metadata_drives_title_and_colour():
    assert scan_title(True) == "❌ Scan: Manual Check Errors"
    assert scan_title(False) == "⚠️ Scan: Manual Updates Available"
    assert scan_ntfy_title(True) == "Fleet Scan: Manual Check Errors"
    assert scan_ntfy_title(False) == "Fleet Scan: Manual Updates Available"
    assert scan_color(True) != scan_color(False)
    assert scan_color(True) == scan_notif._COLOR_FAILED
    assert scan_color(False) == scan_notif._COLOR_WARNING


def test_decision_exposes_selection_metadata():
    decision = decide_manual_notifications([_entry(), _error_entry()], {}, now=NOW)
    assert decision.notify is True
    assert decision.failed is True
    assert decision.selected == ["web-01", "db-01"]
    assert decision.pending_count == 1
    assert decision.error_count == 1


# --------------------------------------------------------------------------
# State persistence
# --------------------------------------------------------------------------

def test_state_roundtrip(tmp_path: Path):
    state = {"web-01": {"fingerprint": "abc", "last_notified": "2026-08-01T12:00:00+00:00"}}

    assert save_state(tmp_path, state) is True
    assert load_state(tmp_path) == state
    on_disk = json.loads((tmp_path / "manual-notify-state.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == 1
    assert on_disk["hosts"] == state


def test_load_state_defensive():
    history = Path("/nonexistent-history-dir")
    assert load_state(history) == {}

    with pytest.raises(OSError):
        history.read_text()  # sanity: the directory really does not exist


def test_load_state_corrupt_file(tmp_path: Path):
    (tmp_path / "manual-notify-state.json").write_text("{not json", encoding="utf-8")
    assert load_state(tmp_path) == {}


def test_load_state_wrong_shape(tmp_path: Path):
    (tmp_path / "manual-notify-state.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_state(tmp_path) == {}
    (tmp_path / "manual-notify-state.json").write_text(json.dumps({"hosts": "nope"}), encoding="utf-8")
    assert load_state(tmp_path) == {}


def test_load_state_drops_fingerprintless_entries(tmp_path: Path):
    (tmp_path / "manual-notify-state.json").write_text(
        json.dumps({"version": 1, "hosts": {"ok": {"fingerprint": "x"}, "bad": {"last_notified": "t"}}}),
        encoding="utf-8",
    )
    assert load_state(tmp_path) == {"ok": {"fingerprint": "x"}}


# --------------------------------------------------------------------------
# One-call integration
# --------------------------------------------------------------------------

def test_run_dispatches_and_persists_advanced_state(tmp_path: Path, monkeypatch):
    dispatch = _RecordingDispatch()
    notifier_list = [{"type": "discord", "enabled": True, "webhook": "https://discord.test/hook"}]

    report = run_manual_notifications(
        [_entry()],
        history_dir=tmp_path,
        notifiers_list=notifier_list,
        now=NOW,
        dispatch_fn=dispatch,
    )

    assert report.notify is True
    assert report.dispatched is True
    assert report.failed is False
    assert report.title == "⚠️ Scan: Manual Updates Available"
    assert report.state_saved is True
    assert report.state_path == tmp_path / "manual-notify-state.json"

    assert len(dispatch.calls) == 1
    call = dispatch.calls[0]
    assert call["notifiers"] == notifier_list
    assert call["title"] == "⚠️ Scan: Manual Updates Available"
    assert call["ntfy_title"] == "Fleet Scan: Manual Updates Available"
    assert call["color"] == scan_notif._COLOR_WARNING
    assert call["failed"] is False
    assert "web-01" in call["body"]

    # Post-attempt state persisted with last_notified advanced.
    persisted = load_state(tmp_path)
    assert persisted["web-01"]["fingerprint"] == _fp(_entry())
    assert persisted["web-01"]["last_notified"] == NOW.astimezone(timezone.utc).isoformat()


def test_run_advances_last_notified_after_attempt(tmp_path: Path):
    dispatch = _RecordingDispatch()
    # First run: first notification, last_notified advances to NOW.
    run_manual_notifications([_entry()], history_dir=tmp_path, now=NOW, dispatch_fn=dispatch)
    assert load_state(tmp_path)["web-01"]["last_notified"] == NOW.astimezone(timezone.utc).isoformat()

    # Second run one hour later, same fingerprint: under the 24h reminder
    # window → no dispatch, state untouched (last_notified NOT re-advanced).
    later = NOW + timedelta(hours=1)
    report = run_manual_notifications([_entry()], history_dir=tmp_path, now=later, dispatch_fn=dispatch)

    assert report.notify is False
    assert report.dispatched is False
    assert len(dispatch.calls) == 1
    assert load_state(tmp_path)["web-01"]["last_notified"] == NOW.astimezone(timezone.utc).isoformat()

    # Third run after the window: reminder fires again and advances.
    after_window = NOW + timedelta(hours=25)
    report = run_manual_notifications([_entry()], history_dir=tmp_path, now=after_window, dispatch_fn=dispatch)
    assert report.notify is True
    assert report.reasons == {"web-01": "reminder"}
    assert len(dispatch.calls) == 2
    assert load_state(tmp_path)["web-01"]["last_notified"] == after_window.astimezone(timezone.utc).isoformat()


def test_run_clears_clean_hosts_on_disk(tmp_path: Path):
    dispatch = _RecordingDispatch()
    state = {"web-01": _state_entry(_entry(), NOW - timedelta(hours=5))}
    save_state(tmp_path, state)

    clean = _entry(update_available=False, reboot_required=False)
    report = run_manual_notifications([clean], history_dir=tmp_path, now=NOW, dispatch_fn=dispatch)

    assert report.notify is False
    assert report.state_saved is True
    assert "web-01" not in load_state(tmp_path)


def test_run_without_notifiers_still_attempts_and_advances(tmp_path: Path):
    dispatch = _RecordingDispatch()

    report = run_manual_notifications([_entry()], history_dir=tmp_path, now=NOW, dispatch_fn=dispatch)

    assert report.dispatched is True
    assert dispatch.calls[0]["notifiers"] == []
    assert "web-01" in load_state(tmp_path)  # state advanced regardless


def test_run_noop_when_nothing_to_do(tmp_path: Path):
    dispatch = _RecordingDispatch()
    report = run_manual_notifications([], history_dir=tmp_path, now=NOW, dispatch_fn=dispatch)

    assert report.notify is False
    assert report.dispatched is False
    assert report.body == ""
    assert dispatch.calls == []
    assert not (tmp_path / "manual-notify-state.json").exists()


def test_state_write_failure_does_not_fail_scan(tmp_path: Path):
    dispatch = _RecordingDispatch()
    # history_dir is a regular file: load is defensive (→ {}), save fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    report = run_manual_notifications([_entry()], history_dir=blocker, now=NOW, dispatch_fn=dispatch)

    assert report.notify is True  # decision + dispatch still happened
    assert report.state_saved is False  # write failed, but scan did not abort


def test_run_error_report_severity(tmp_path: Path):
    dispatch = _RecordingDispatch()
    report = run_manual_notifications([_error_entry()], history_dir=tmp_path, now=NOW, dispatch_fn=dispatch)

    assert report.notify is True
    assert report.failed is True
    assert report.title == "❌ Scan: Manual Check Errors"
    assert report.color == scan_notif._COLOR_FAILED
    assert "**Manual check errors**" in report.body
