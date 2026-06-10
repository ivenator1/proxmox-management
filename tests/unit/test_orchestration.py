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


def test_run_concurrent_falls_back_to_serial_when_workers_lte_one():
    out = run_concurrent([1, 2, 3], lambda x: x * 2, max_workers=1)
    assert [r[1] for r in out] == [2, 4, 6]


def test_retry_with_empty_exceptions_does_not_catch():
    def fn():
        raise ValueError("uncaught")

    with pytest.raises(ValueError):
        retry(fn, retries=3, delay=0, exceptions=(), sleep=lambda _: None)


def test_retry_total_attempts_is_retries_plus_one():
    calls = []

    def fn():
        calls.append(1)
        return 42

    retry(fn, retries=0, delay=0, sleep=lambda _: None)
    assert len(calls) == 1


def test_run_concurrent_timeout_does_not_block_on_hung_worker():
    """A worker that outlives the timeout must not block run_concurrent's return:
    shutdown(wait=True) — what the context manager does — would hang here."""
    import threading
    import time as _time
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    release = threading.Event()

    def fn(x):
        if x == "hung":
            release.wait(timeout=30)  # simulates a hung SSH session
            return "late"
        return "ok"

    start = _time.monotonic()
    results = run_concurrent(["fast", "hung"], fn, max_workers=2, timeout=0.2)
    elapsed = _time.monotonic() - start
    release.set()  # let the leaked worker thread finish

    assert elapsed < 5, f"run_concurrent blocked on the hung worker ({elapsed:.1f}s)"
    assert results[0][1] == "ok"
    assert isinstance(results[1][2], FuturesTimeoutError)
