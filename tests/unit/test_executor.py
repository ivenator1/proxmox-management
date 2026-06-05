"""Tests for proxmox_fleet.executor — RunnerExecutor, _merge_facts, snapshot_with_retry."""
from __future__ import annotations


import proxmox_fleet.executor as executor_mod
from proxmox_fleet.executor import RunnerExecutor, _merge_facts, snapshot_with_retry
from proxmox_fleet.runner import PrimitiveResult


def _pr(**kwargs) -> PrimitiveResult:
    defaults = dict(rc=0, stdout="", stderr="", changed=False, failed=False, facts={})
    defaults.update(kwargs)
    return PrimitiveResult(**defaults)


# ---------------------------------------------------------------------------
# _merge_facts
# ---------------------------------------------------------------------------

def test_merge_facts_rc_overrides_from_facts():
    r = _pr(rc=0, facts={"rc": 2})
    result = _merge_facts(r)
    assert result.rc == 2


def test_merge_facts_stdout_overrides_from_facts():
    r = _pr(stdout="old", facts={"stdout": "new"})
    result = _merge_facts(r)
    assert result.stdout == "new"


def test_merge_facts_stderr_overrides_from_facts():
    r = _pr(stderr="old", facts={"stderr": "err"})
    result = _merge_facts(r)
    assert result.stderr == "err"


def test_merge_facts_changed_overrides_from_facts():
    r = _pr(changed=False, facts={"changed": True})
    result = _merge_facts(r)
    assert result.changed is True


def test_merge_facts_non_int_rc_ignored():
    r = _pr(rc=0, facts={"rc": "not-an-int"})
    result = _merge_facts(r)
    assert result.rc == 0  # unchanged


def test_merge_facts_failed_set_when_rc_nonzero():
    r = _pr(rc=1, failed=False, facts={})
    result = _merge_facts(r)
    assert result.failed is True


def test_merge_facts_empty_facts_leaves_result_unchanged():
    r = _pr(rc=0, stdout="hello", changed=True, facts={})
    result = _merge_facts(r)
    assert result.stdout == "hello"
    assert result.changed is True


# ---------------------------------------------------------------------------
# RunnerExecutor._shell extravars assembly
# ---------------------------------------------------------------------------

def _capture_extravars(monkeypatch, executor, command, **opts):
    """Call run_shell and return the extravars that were passed to invoke_primitive."""
    captured = {}

    def fake_invoke(primitive, *, inventory, host_pattern, extravars, check, **kw):
        captured["extravars"] = extravars
        captured["primitive"] = primitive
        captured["host_pattern"] = host_pattern
        return _pr()

    monkeypatch.setattr(executor_mod, "invoke_primitive", fake_invoke)
    executor.run_shell(command, **opts)
    return captured["extravars"], captured["primitive"], captured["host_pattern"]


def test_run_shell_basic_extravars(monkeypatch):
    ex = RunnerExecutor("node-01")
    ev, prim, hp = _capture_extravars(monkeypatch, ex, "echo hi")
    assert ev["shell_command"] == "echo hi"
    assert prim == "run_shell"
    assert hp == "node-01"


def test_run_shell_become_adds_flag(monkeypatch):
    ex = RunnerExecutor("node-01")
    ev, _, _ = _capture_extravars(monkeypatch, ex, "cmd", become=True)
    assert ev.get("shell_become") is True


def test_run_shell_chdir_adds_flag(monkeypatch):
    ex = RunnerExecutor("node-01")
    ev, _, _ = _capture_extravars(monkeypatch, ex, "cmd", chdir="/tmp")
    assert ev.get("shell_chdir") == "/tmp"


def test_run_shell_environment_adds_flag(monkeypatch):
    ex = RunnerExecutor("node-01")
    ev, _, _ = _capture_extravars(monkeypatch, ex, "cmd", environment={"KEY": "val"})
    assert ev.get("shell_environment") == {"KEY": "val"}


def test_run_shell_changed_when_none_excluded(monkeypatch):
    ex = RunnerExecutor("node-01")
    ev, _, _ = _capture_extravars(monkeypatch, ex, "cmd", changed_when=None)
    assert "shell_changed_when" not in ev


def test_run_shell_changed_when_false_included(monkeypatch):
    ex = RunnerExecutor("node-01")
    ev, _, _ = _capture_extravars(monkeypatch, ex, "cmd", changed_when=False)
    assert ev.get("shell_changed_when") is False


def test_run_shell_ignore_errors_adds_flag(monkeypatch):
    ex = RunnerExecutor("node-01")
    ev, _, _ = _capture_extravars(monkeypatch, ex, "cmd", ignore_errors=True)
    assert ev.get("shell_ignore_errors") is True


def test_run_local_targets_localhost(monkeypatch):
    ex = RunnerExecutor("node-01")
    captured = {}

    def fake_invoke(primitive, *, inventory, host_pattern, extravars, check, **kw):
        captured["host_pattern"] = host_pattern
        return _pr()

    monkeypatch.setattr(executor_mod, "invoke_primitive", fake_invoke)
    ex.run_local("local-cmd")
    assert captured["host_pattern"] == "localhost"


# ---------------------------------------------------------------------------
# RunnerExecutor.snapshot — fact merge
# ---------------------------------------------------------------------------

def test_snapshot_fact_merge_changed(monkeypatch):
    def fake_invoke(primitive, *, extravars, **kw):
        return _pr(facts={"changed": True, "failed": False})

    monkeypatch.setattr(executor_mod, "invoke_primitive", fake_invoke)
    ex = RunnerExecutor("node-01")
    result = ex.snapshot("101", snap_state="present", api_host="1.2.3.4",
                         api_user="root@pam", api_token_id="tk", api_token_secret="sec")
    assert result.changed is True
    assert result.failed is False


def test_snapshot_fact_merge_failed(monkeypatch):
    def fake_invoke(primitive, *, extravars, **kw):
        return _pr(facts={"changed": False, "failed": True})

    monkeypatch.setattr(executor_mod, "invoke_primitive", fake_invoke)
    ex = RunnerExecutor("node-01")
    result = ex.snapshot("101", snap_state="present", api_host="1.2.3.4",
                         api_user="root@pam", api_token_id="tk", api_token_secret="sec")
    assert result.failed is True


# ---------------------------------------------------------------------------
# snapshot_with_retry
# ---------------------------------------------------------------------------

class _FakeExecutor:
    host = "node-01"

    def __init__(self, results):
        self._results = list(results)
        self._calls = 0

    def snapshot(self, vmid, *, snap_state, **kw):
        self._calls += 1
        return self._results.pop(0)

    def run_shell(self, *a, **kw):
        return _pr()

    def run_local(self, *a, **kw):
        return _pr()

    def reboot(self, **kw):
        return _pr()


def test_snapshot_with_retry_present_succeeds_on_second_attempt():
    sleeps = []
    ex = _FakeExecutor([
        _pr(changed=False, failed=True),
        _pr(changed=True, failed=False),
    ])
    result = snapshot_with_retry(ex, "101", snap_state="present",
                                 retries=3, delay=1.0,
                                 _sleep=sleeps.append,
                                 api_host="1.2.3.4", api_user="u",
                                 api_token_id="t", api_token_secret="s")
    assert result.changed is True
    assert ex._calls == 2
    assert sleeps == [1.0]  # slept once between attempts


def test_snapshot_with_retry_absent_succeeds_when_not_failed():
    ex = _FakeExecutor([
        _pr(failed=True),
        _pr(failed=False),
    ])
    result = snapshot_with_retry(ex, "101", snap_state="absent",
                                 retries=3, delay=0.0,
                                 _sleep=lambda _: None,
                                 api_host="1.2.3.4", api_user="u",
                                 api_token_id="t", api_token_secret="s")
    assert result.failed is False


def test_snapshot_with_retry_exhausted_returns_failed_result():
    ex = _FakeExecutor([
        _pr(changed=False, failed=True),
        _pr(changed=False, failed=True),
        _pr(changed=False, failed=True),
        _pr(changed=False, failed=True),
    ])
    result = snapshot_with_retry(ex, "101", snap_state="present",
                                 retries=3, delay=0.0,
                                 _sleep=lambda _: None,
                                 api_host="1.2.3.4", api_user="u",
                                 api_token_id="t", api_token_secret="s")
    assert result.failed is True


def test_snapshot_with_retry_injectable_sleep_called():
    sleeps = []
    ex = _FakeExecutor([
        _pr(changed=False, failed=True),
        _pr(changed=True, failed=False),
    ])
    snapshot_with_retry(ex, "101", snap_state="present",
                        retries=3, delay=7.5,
                        _sleep=sleeps.append,
                        api_host="1.2.3.4", api_user="u",
                        api_token_id="t", api_token_secret="s")
    assert sleeps == [7.5]
