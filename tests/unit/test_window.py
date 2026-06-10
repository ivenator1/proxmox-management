"""Tests for proxmox_fleet.window.in_window — mirrors test_check_window.py
case-for-case using plain Python (no Jinja shim).
"""
from datetime import datetime, timedelta, timezone

import pytest

from proxmox_fleet.window import in_window

# Helper: build a datetime with a fixed HH:MM on a specific weekday.
# weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun

_UTC = timezone.utc


def _dt(weekday: int, hhmm: str) -> datetime:
    """Return a UTC datetime on ISO weekday *weekday* (0=Mon) at HH:MM."""
    h, m = map(int, hhmm.split(":"))
    # 2024-01-01 is a Monday (weekday 0).
    base = datetime(2024, 1, 1, tzinfo=_UTC)
    return base + timedelta(days=weekday, hours=h, minutes=m)


# --- day matching ---

def test_day_ok_when_days_omitted():
    now = _dt(2, "03:00")  # Wednesday
    assert in_window({}, now=now) is True


def test_day_ok_titlecase_match():
    now = _dt(5, "03:00")  # Saturday
    assert in_window({"days": ["Sat", "Sun"]}, now=now) is True


def test_day_match_is_case_insensitive():
    """Parity with the retired check-window.yml, which lowercased both sides —
    `days: [sat, sun]` in host_vars must not silently skip the host forever."""
    now = _dt(5, "03:00")  # Saturday
    assert in_window({"days": ["sat", "sun"]}, now=now) is True
    assert in_window({"days": ["SAT"]}, now=now) is True
    assert in_window({"days": ["mon"]}, now=now) is False


def test_day_not_matched():
    now = _dt(0, "03:00")  # Monday
    assert in_window({"days": ["Sat", "Sun"]}, now=now) is False


@pytest.mark.parametrize("weekday,abbrev", [
    (0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"),
    (4, "Fri"), (5, "Sat"), (6, "Sun"),
])
def test_weekday_abbreviation_is_locale_independent(weekday, abbrev):
    """Each ISO weekday matches its fixed English abbreviation regardless of the
    process locale (we no longer rely on locale-sensitive strftime('%a'))."""
    now = _dt(weekday, "03:00")
    assert in_window({"days": [abbrev], "start": "00:00", "end": "23:59"}, now=now) is True
    # And a different day's abbreviation does not match.
    other = "Mon" if abbrev != "Mon" else "Tue"
    assert in_window({"days": [other], "start": "00:00", "end": "23:59"}, now=now) is False


# --- time matching (normal window) ---

def test_time_inside_normal_window():
    now = _dt(0, "03:00")
    assert in_window({"start": "02:00", "end": "04:00"}, now=now) is True


def test_time_outside_normal_window():
    now = _dt(0, "05:00")
    assert in_window({"start": "02:00", "end": "04:00"}, now=now) is False


def test_time_on_boundary_inclusive():
    now = _dt(0, "02:00")
    assert in_window({"start": "02:00", "end": "04:00"}, now=now) is True


# --- time matching (window wrapping past midnight) ---

def test_time_wrap_after_start():
    now = _dt(0, "23:30")
    assert in_window({"start": "22:00", "end": "02:00"}, now=now) is True


def test_time_wrap_before_end():
    now = _dt(0, "01:00")
    assert in_window({"start": "22:00", "end": "02:00"}, now=now) is True


def test_time_wrap_outside():
    now = _dt(0, "12:00")
    assert in_window({"start": "22:00", "end": "02:00"}, now=now) is False


# --- combined ---

def test_in_window_true_when_day_and_time_ok():
    now = _dt(5, "03:00")  # Saturday
    assert in_window({"days": ["Sat", "Sun"], "start": "02:00", "end": "04:00"}, now=now) is True


def test_in_window_false_when_time_outside():
    now = _dt(5, "06:00")  # Saturday but outside window
    assert in_window({"days": ["Sat", "Sun"], "start": "02:00", "end": "04:00"}, now=now) is False


def test_in_window_false_when_day_outside():
    now = _dt(0, "03:00")  # Monday — wrong day
    assert in_window({"days": ["Sat", "Sun"], "start": "02:00", "end": "04:00"}, now=now) is False


# --- force ---

def test_force_bypasses_day_and_time():
    now = _dt(0, "12:00")  # Monday, midday — outside any reasonable window
    assert in_window({"days": ["Sat"], "start": "02:00", "end": "04:00"}, now=now, force=True) is True


def test_force_false_without_kwarg():
    now = _dt(0, "12:00")
    assert in_window({"days": ["Sat"], "start": "02:00", "end": "04:00"}, now=now) is False


# --- tz handling ---

def test_tz_defaults_to_utc():
    # No tz key → UTC, same as explicit UTC.
    now = _dt(5, "03:00")
    result_no_tz = in_window({"days": ["Sat"], "start": "02:00", "end": "04:00"}, now=now)
    result_utc = in_window({"days": ["Sat"], "start": "02:00", "end": "04:00", "tz": "UTC"}, now=now)
    assert result_no_tz == result_utc is True


def test_tz_aware_now_in_different_tz_is_converted():
    # UTC midnight (00:00 UTC) = 20:00 US Eastern (America/New_York, UTC-4 in summer).
    # A window of 19:00-21:00 America/New_York: inside when converted, outside in raw UTC.
    now_utc_midnight = datetime(2024, 7, 20, 0, 0, 0, tzinfo=timezone.utc)  # Sat 00:00 UTC = Fri 20:00 ET
    assert in_window(
        {"start": "19:00", "end": "21:00", "tz": "America/New_York"},
        now=now_utc_midnight,
    ) is True  # 20:00 ET is inside 19:00-21:00
    assert in_window(
        {"start": "19:00", "end": "21:00", "tz": "UTC"},
        now=now_utc_midnight,
    ) is False  # 00:00 UTC is outside 19:00-21:00


def test_maintenance_window_model_accepted_by_in_window():
    from proxmox_fleet.models.config import MaintenanceWindow
    mw = MaintenanceWindow(days=["Sat"], start="02:00", end="04:00", tz="UTC")
    now = _dt(5, "03:00")  # Saturday 03:00 UTC — inside the window
    assert in_window(mw, now=now) is True
    now_outside = _dt(5, "06:00")  # Saturday 06:00 UTC — outside
    assert in_window(mw, now=now_outside) is False


def test_invalid_tz_falls_back_to_utc():
    now = _dt(5, "03:00")
    assert in_window({"days": ["Sat"], "start": "02:00", "end": "04:00", "tz": "Not/A/Zone"}, now=now) is True
