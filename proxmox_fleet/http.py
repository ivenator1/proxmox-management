"""Manager-local IO — runs on the manager where the brain lives, so it never
round-trips through ansible-runner.

Covers what used to be ``delegate_to: localhost`` ``uri``/``wait_for`` tasks:
Kuma polling, GitHub latest-release lookup, HTTP health-check, Discord/ntfy
webhook POST, dead-man ping, and the apt-proxy TCP check. Stdlib only
(``urllib``/``socket``) — no new runtime dependency.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from proxmox_fleet.orchestration import retry


@dataclass
class HttpResponse:
    status: int
    body: str
    headers: Dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


def request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: float = 30.0,
    verify: bool = True,
) -> HttpResponse:
    """Single HTTP request. Raises urllib.error.HTTPError on 4xx/5xx.

    Sets a browser-like ``User-Agent`` by default — urllib's stock
    ``Python-urllib/x.y`` is blocked by Cloudflare-fronted hosts (e.g. Discord
    webhooks return 403 Forbidden) unless overridden via ``headers``.

    ``verify=False`` disables TLS certificate verification (for appliances
    with self-signed certs); the default keeps urllib's verified context.
    """
    req_headers = {"User-Agent": "proxmox-fleet/1.0", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    if verify:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # nosec B310 - operator-controlled URLs
            raw = resp.read().decode("utf-8", errors="replace")
            return HttpResponse(status=resp.status, body=raw, headers=dict(resp.headers.items()))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310  # nosec B310 - operator-controlled URLs
        raw = resp.read().decode("utf-8", errors="replace")
        return HttpResponse(status=resp.status, body=raw, headers=dict(resp.headers.items()))


def get_json(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    retries: int = 0,
    delay: float = 10.0,
    timeout: float = 30.0,
    verify: bool = True,
) -> Any:
    """GET a URL and parse JSON, with optional retry on transient errors."""

    def _once() -> Any:
        return request(url, method="GET", headers=headers, timeout=timeout, verify=verify).json()

    return retry(_once, retries=retries, delay=delay, exceptions=(urllib.error.URLError, OSError))


def post_json(
    url: str,
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    retries: int = 0,
    delay: float = 10.0,
    ok_statuses: tuple = (200, 201, 202, 204),
    timeout: float = 30.0,
    verify: bool = True,
) -> HttpResponse:
    """POST a JSON body; retry until status is in ``ok_statuses``."""
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **(headers or {})}

    def _once() -> HttpResponse:
        try:
            return request(url, method="POST", headers=hdrs, data=body, timeout=timeout, verify=verify)
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read().decode("utf-8", "replace"))

    return retry(
        _once,
        retries=retries,
        delay=delay,
        until=lambda r: r.status in ok_statuses,
        exceptions=(urllib.error.URLError, OSError),
    )


def poll_until(
    fetch: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    retries: int = 5,
    delay: float = 30.0,
) -> Any:
    """Poll ``fetch`` until ``predicate`` holds — the Kuma/HTTP health-check loop."""
    return retry(fetch, retries=retries, delay=delay, until=predicate,
                 exceptions=(urllib.error.URLError, OSError))


def wait_for_port(
    host: str,
    port: int,
    *,
    timeout: float = 300.0,
    interval: float = 3.0,
    _sleep: Callable[[float], None] = time.sleep,
    _now: Callable[[], float] = time.monotonic,
) -> bool:
    """Block until a TCP port accepts connections or ``timeout`` elapses.

    The apt-proxy / post-reboot ``wait_for`` analogue. Returns True on success,
    raises TimeoutError otherwise.
    """
    deadline = _now() + timeout
    while _now() < deadline:
        try:
            with socket.create_connection((host, port), timeout=interval):
                return True
        except OSError:
            _sleep(interval)
    raise TimeoutError(f"port {host}:{port} not reachable within {timeout}s")
