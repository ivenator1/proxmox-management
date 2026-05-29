"""Stdlib replacements for Ansible's orchestration features.

| Ansible            | here                                  |
|--------------------|---------------------------------------|
| forks / parallel   | run_concurrent (ThreadPoolExecutor)   |
| serial: N          | run_concurrent(max_workers=N)         |
| serial: 1          | run_serial                            |
| any_errors_fatal   | run_serial(abort_on_error=True)       |
| retries / until    | retry                                 |

Work here is IO-bound (SSH, HTTP), so threads are the right tool.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, Type, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_serial(
    items: Iterable[T],
    fn: Callable[[T], R],
    *,
    abort_on_error: bool = False,
) -> List[Tuple[T, Optional[R], Optional[BaseException]]]:
    """Run ``fn`` over ``items`` one at a time (serial: 1).

    Returns ``(item, result, exception)`` triples. With ``abort_on_error`` the
    loop stops at the first exception (the Ansible ``any_errors_fatal`` analogue);
    remaining items are not run.
    """
    out: List[Tuple[T, Optional[R], Optional[BaseException]]] = []
    for item in items:
        try:
            out.append((item, fn(item), None))
        except BaseException as exc:  # noqa: BLE001 - mirror Ansible's catch-all rescue
            out.append((item, None, exc))
            if abort_on_error:
                break
    return out


def run_concurrent(
    items: Sequence[T],
    fn: Callable[[T], R],
    *,
    max_workers: int = 1,
) -> List[Tuple[T, Optional[R], Optional[BaseException]]]:
    """Run ``fn`` over ``items`` with bounded concurrency (forks / serial: N).

    Preserves input order in the returned triples. Exceptions are captured per
    item, never raised, so one failing host does not abort the others.
    """
    if max_workers <= 1:
        return run_serial(items, fn, abort_on_error=False)

    results: List[Tuple[T, Optional[R], Optional[BaseException]]] = [
        (item, None, None) for item in items
    ]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {pool.submit(fn, item): idx for idx, item in enumerate(items)}
        for future, idx in future_to_idx.items():
            item = items[idx]
            try:
                results[idx] = (item, future.result(), None)
            except BaseException as exc:  # noqa: BLE001
                results[idx] = (item, None, exc)
    return results


def retry(
    fn: Callable[[], R],
    *,
    retries: int = 5,
    delay: float = 30.0,
    until: Optional[Callable[[R], bool]] = None,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> R:
    """Call ``fn`` until it succeeds and ``until(result)`` is truthy.

    Mirrors Ansible ``register`` + ``until`` + ``retries``/``delay``. ``retries``
    is the number of *additional* attempts after the first (so total attempts =
    retries + 1), matching Ansible semantics. Re-raises the last exception, or
    raises ``RuntimeError`` if ``until`` never holds.
    """
    last_exc: Optional[BaseException] = None
    last_result: Optional[R] = None
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            last_result = fn()
            if until is None or until(last_result):
                return last_result
        except exceptions as exc:
            last_exc = exc
        if attempt < attempts - 1:
            sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry: condition never satisfied within %d attempts" % attempts)
