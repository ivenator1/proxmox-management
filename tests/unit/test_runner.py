"""Tests for proxmox_fleet.runner._harvest — event-parsing in isolation.

Creates a minimal fake ansible-runner object (no ansible_runner import needed).
"""
from proxmox_fleet.runner import _harvest


class _FakeRunner:
    def __init__(self, rc=0, status="successful", events=None):
        self.rc = rc
        self.status = status
        self.events = events or []


def _ok_event(stdout="", changed=False, stats_data=None):
    res = {"changed": changed}
    if stdout:
        res["stdout"] = stdout
    if stats_data is not None:
        res["ansible_stats"] = {"data": stats_data}
    return {"event": "runner_on_ok", "event_data": {"res": res}}


def _failed_event(stdout="", stderr="", msg=""):
    res = {}
    if stdout:
        res["stdout"] = stdout
    if stderr:
        res["stderr"] = stderr
    if msg:
        res["msg"] = msg
    return {"event": "runner_on_failed", "event_data": {"res": res}}


def _unreachable_event():
    return {"event": "runner_on_unreachable", "event_data": {"res": {}}}


# --- happy path ---

def test_harvest_ok_event_with_set_stats():
    runner = _FakeRunner(events=[
        _ok_event(stdout="hello", changed=True, stats_data={"rc": 0, "stdout": "hello"})
    ])
    result = _harvest(runner)
    assert result.failed is False
    assert result.changed is True
    assert result.facts == {"rc": 0, "stdout": "hello"}
    assert result.rc == 0


def test_harvest_stdout_from_event():
    runner = _FakeRunner(events=[_ok_event(stdout="output line")])
    result = _harvest(runner)
    assert "output line" in result.stdout


# --- failure cases ---

def test_harvest_failed_event_sets_failed():
    runner = _FakeRunner(events=[_failed_event(stderr="boom")])
    result = _harvest(runner)
    assert result.failed is True
    assert "boom" in result.stderr


def test_harvest_failed_event_captures_msg():
    runner = _FakeRunner(events=[_failed_event(msg="Connection refused")])
    result = _harvest(runner)
    assert result.failed is True
    assert "Connection refused" in result.stderr


def test_harvest_unreachable_event_sets_failed():
    runner = _FakeRunner(events=[_unreachable_event()])
    result = _harvest(runner)
    assert result.failed is True


def test_harvest_runner_status_failed_sets_failed():
    runner = _FakeRunner(rc=0, status="failed", events=[])
    result = _harvest(runner)
    assert result.failed is True


def test_harvest_nonzero_rc_sets_failed():
    runner = _FakeRunner(rc=1, events=[])
    result = _harvest(runner)
    assert result.failed is True
    assert result.rc == 1


# --- facts accumulation ---

def test_harvest_facts_accumulate_across_events():
    runner = _FakeRunner(events=[
        _ok_event(stats_data={"key1": "a"}),
        _ok_event(stats_data={"key2": "b"}),
    ])
    result = _harvest(runner)
    assert result.facts["key1"] == "a"
    assert result.facts["key2"] == "b"


def test_harvest_empty_events_uses_runner_rc():
    runner = _FakeRunner(rc=0, events=[])
    result = _harvest(runner)
    assert result.rc == 0
    assert result.failed is False


# --- edge cases ---

def test_harvest_non_int_rc_in_stats_does_not_crash():
    runner = _FakeRunner(events=[
        _ok_event(stats_data={"rc": "not-an-int"})
    ])
    result = _harvest(runner)
    # facts are still stored
    assert result.facts["rc"] == "not-an-int"


def test_harvest_changed_accumulates_true():
    runner = _FakeRunner(events=[
        _ok_event(changed=False),
        _ok_event(changed=True),
    ])
    result = _harvest(runner)
    assert result.changed is True


def test_harvest_result_has_events_list():
    runner = _FakeRunner(events=[_ok_event()])
    result = _harvest(runner)
    assert len(result.events) == 1
