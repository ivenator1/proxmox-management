"""Tests for proxmox_fleet.orchestration — the stdlib forks/serial/retry helpers."""
import pytest

from proxmox_fleet.orchestration import retry, run_concurrent, run_serial


def test_run_serial_collects_results_and_errors():
    def fn(x):
        if x == 2:
            raise ValueError("boom")
        return x * 10

    out = run_serial([1, 2, 3], fn)
    assert [r[1] for r in out] == [10, None, 30]
    assert isinstance(out[1][2], ValueError)


def test_run_serial_abort_on_error_stops():
    seen = []

    def fn(x):
        seen.append(x)
        if x == 2:
            raise RuntimeError("stop")
        return x

    out = run_serial([1, 2, 3], fn, abort_on_error=True)
    assert seen == [1, 2]  # 3 never ran
    assert len(out) == 2


def test_run_concurrent_preserves_order():
    out = run_concurrent([1, 2, 3, 4], lambda x: x * x, max_workers=3)
    assert [r[1] for r in out] == [1, 4, 9, 16]


def test_run_concurrent_captures_exceptions_per_item():
    def fn(x):
        if x == 3:
            raise ValueError("nope")
        return x

    out = run_concurrent([1, 2, 3], fn, max_workers=2)
    assert out[2][1] is None
    assert isinstance(out[2][2], ValueError)
    assert out[0][1] == 1


def test_retry_succeeds_first_try():
    calls = []
    assert retry(lambda: calls.append(1) or "ok", retries=3, delay=0, sleep=lambda _: None) == "ok"
    assert len(calls) == 1


def test_retry_until_condition():
    state = {"n": 0}

    def fn():
        state["n"] += 1
        return state["n"]

    result = retry(fn, retries=5, delay=0, until=lambda r: r >= 3, sleep=lambda _: None)
    assert result == 3


def test_retry_reraises_last_exception():
    def fn():
        raise ValueError("always")

    with pytest.raises(ValueError):
        retry(fn, retries=2, delay=0, sleep=lambda _: None)


def test_retry_raises_when_condition_never_holds():
    with pytest.raises(RuntimeError):
        retry(lambda: 1, retries=2, delay=0, until=lambda r: False, sleep=lambda _: None)
