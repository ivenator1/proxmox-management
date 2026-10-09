"""Runner event parsing and transient artifact ownership tests."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from proxmox_fleet.runner import _harvest, invoke_primitive


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


def test_harvest_unreachable_flag():
    runner = _FakeRunner(events=[_unreachable_event()], rc=4, status="failed")
    result = _harvest(runner)
    assert result.failed is True
    assert result.unreachable is True


def test_harvest_plain_failure_is_not_unreachable():
    runner = _FakeRunner(events=[_failed_event(stderr="boom")], rc=2, status="failed")
    result = _harvest(runner)
    assert result.failed is True
    assert result.unreachable is False


@pytest.mark.parametrize("failed", [False, True])
def test_primitive_removes_automatic_artifacts_after_harvesting(monkeypatch, tmp_path, failed):
    import ansible_runner

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    directories = []

    def run(**kwargs):
        directory = Path(kwargs["private_data_dir"] or tempfile.mkdtemp())
        directories.append(directory)
        event_file = directory / "result.json"
        event_file.write_text(json.dumps(_failed_event(msg="execution failed") if failed else _ok_event()))

        class DiskRunner:
            rc = 2 if failed else 0
            status = "failed" if failed else "successful"

            @property
            def events(self):
                yield json.loads(event_file.read_text())

        return DiskRunner()

    monkeypatch.setattr(ansible_runner, "run", run)
    result = invoke_primitive("fixture", inventory=str(tmp_path / "inventory"))

    assert result.failed is failed
    if failed:
        assert "execution failed" in result.stderr
    assert all(not directory.exists() for directory in directories)


def test_primitive_preparation_failure_removes_automatic_artifacts(monkeypatch, tmp_path):
    import ansible_runner

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    directories = []

    def run(**kwargs):
        directory = Path(kwargs["private_data_dir"] or tempfile.mkdtemp())
        directories.append(directory)
        (directory / "partial-event").write_text("incomplete")
        raise RuntimeError("preparation failed")

    monkeypatch.setattr(ansible_runner, "run", run)
    with pytest.raises(RuntimeError, match="preparation failed"):
        invoke_primitive("fixture", inventory=str(tmp_path / "inventory"))
    assert all(not directory.exists() for directory in directories)


def test_primitive_keeps_caller_owned_artifacts_after_failure(monkeypatch, tmp_path):
    import ansible_runner

    directory = tmp_path / "caller-owned"
    directory.mkdir()
    original = directory / "caller-data"
    original.write_text("preserve")

    def run(**kwargs):
        (Path(kwargs["private_data_dir"]) / "diagnostic").write_text("failure detail")
        return SimpleNamespace(rc=2, status="failed", events=[_failed_event(msg="execution failed")])

    monkeypatch.setattr(ansible_runner, "run", run)
    result = invoke_primitive("fixture", private_data_dir=str(directory))

    assert result.failed
    assert original.read_text() == "preserve"
    assert (directory / "diagnostic").read_text() == "failure detail"


def test_primitive_reports_aborted_runner_despite_complete_task_output(monkeypatch, tmp_path):
    import ansible_runner

    runner = _FakeRunner(
        rc=1, status="failed",
        events=[_ok_event(stdout=json.dumps({"profiles": ["pbs"], "files": ["completed metadata"]}))],
    )
    monkeypatch.setattr(ansible_runner, "run", lambda **kwargs: runner)

    result = invoke_primitive("fixture", private_data_dir=str(tmp_path))

    assert result.failed
    assert "failed" in result.stderr
    assert "rc=1" in result.stderr


def test_primitive_preserves_callback_error_without_failed_task_event(monkeypatch, tmp_path):
    import ansible_runner

    runner = _FakeRunner(
        rc=1, status="failed",
        events=[
            _ok_event(stdout=json.dumps({"profiles": ["pbs"]})),
            {"event": "error", "stdout": "[ERROR]: A worker was found in a dead state",
             "event_data": {"error": True, "task": "Return housekeeping probe facts"}},
        ],
    )
    monkeypatch.setattr(ansible_runner, "run", lambda **kwargs: runner)

    result = invoke_primitive("fixture", private_data_dir=str(tmp_path))

    assert result.failed
    assert "worker" in result.stderr and "dead state" in result.stderr
