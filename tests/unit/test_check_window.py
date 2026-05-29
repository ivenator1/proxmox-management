"""
Unit tests for the maintenance-window time/day logic in tasks/check-window.yml.
"""
from tests.conftest import make_native_env

DAY_OK = """\
{{ (maintenance_window.days is not defined)
   or (_mw_day | lower in (maintenance_window.days | map('lower') | list)) }}"""

TIME_OK = """\
{{ (_mw_time >= _mw_start and _mw_time <= _mw_end) if (_mw_start <= _mw_end)
   else (_mw_time >= _mw_start or _mw_time <= _mw_end) }}"""

IN_WINDOW = """\
{{ true if (force_window | default(false) | bool)
   else ((_mw_day_ok | bool) and (_mw_time_ok | bool)) }}"""


def rn(expr, **ctx):
    return make_native_env().from_string(expr).render(**ctx)


# --- day matching ---

def test_day_ok_when_days_omitted():
    assert rn(DAY_OK, maintenance_window={}, _mw_day='Wed') is True


def test_day_ok_case_insensitive_match():
    assert rn(DAY_OK, maintenance_window={'days': ['Sat', 'Sun']}, _mw_day='sat') is True


def test_day_not_matched():
    assert rn(DAY_OK, maintenance_window={'days': ['Sat', 'Sun']}, _mw_day='Mon') is False


# --- time matching (normal window) ---

def test_time_inside_normal_window():
    assert rn(TIME_OK, _mw_time='03:00', _mw_start='02:00', _mw_end='04:00') is True


def test_time_outside_normal_window():
    assert rn(TIME_OK, _mw_time='05:00', _mw_start='02:00', _mw_end='04:00') is False


def test_time_on_boundary_inclusive():
    assert rn(TIME_OK, _mw_time='02:00', _mw_start='02:00', _mw_end='04:00') is True


# --- time matching (window wrapping past midnight) ---

def test_time_wrap_after_start():
    assert rn(TIME_OK, _mw_time='23:30', _mw_start='22:00', _mw_end='02:00') is True


def test_time_wrap_before_end():
    assert rn(TIME_OK, _mw_time='01:00', _mw_start='22:00', _mw_end='02:00') is True


def test_time_wrap_outside():
    assert rn(TIME_OK, _mw_time='12:00', _mw_start='22:00', _mw_end='02:00') is False


# --- combined / force ---

def test_in_window_true_when_both_ok():
    assert rn(IN_WINDOW, _mw_day_ok=True, _mw_time_ok=True) is True


def test_in_window_false_when_time_bad():
    assert rn(IN_WINDOW, _mw_day_ok=True, _mw_time_ok=False) is False


def test_force_window_bypasses():
    assert rn(IN_WINDOW, force_window=True, _mw_day_ok=False, _mw_time_ok=False) is True
