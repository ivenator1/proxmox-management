"""Tests for proxmox_fleet.http — manager-local IO helpers (no real network)."""
import socket

import pytest

from proxmox_fleet import http
from proxmox_fleet.http import HttpResponse, poll_until, wait_for_port


def test_poll_until_kuma_style():
    # Simulate a Kuma heartbeat that goes healthy on the 3rd poll.
    beats = iter([0, 0, 1])

    def fetch():
        return {"status": next(beats)}

    result = poll_until(fetch, lambda r: r["status"] == 1, retries=5, delay=0)
    assert result["status"] == 1


def test_http_response_json():
    assert HttpResponse(status=200, body='{"tag_name": "v1.2"}').json() == {"tag_name": "v1.2"}
    assert HttpResponse(status=204, body="").json() is None


def test_wait_for_port_success():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert wait_for_port("127.0.0.1", port, timeout=2, interval=0.1) is True
    finally:
        srv.close()


def test_wait_for_port_timeout():
    # Pick a port nothing is listening on; fake the clock so the test is instant.
    times = iter([0.0, 0.0, 1.0, 99.0])

    with pytest.raises(TimeoutError):
        wait_for_port(
            "127.0.0.1", 1,
            timeout=5, interval=0.01,
            _sleep=lambda _: None,
            _now=lambda: next(times),
        )


def test_get_json_uses_request(monkeypatch):
    monkeypatch.setattr(http, "request",
                        lambda url, **kw: HttpResponse(status=200, body='{"tag_name": "v9"}'))
    assert http.get_json("http://x")["tag_name"] == "v9"


def test_post_json_retries_until_ok(monkeypatch):
    seq = iter([HttpResponse(status=500, body=""), HttpResponse(status=204, body="")])
    monkeypatch.setattr(http, "request", lambda url, **kw: next(seq))
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    resp = http.post_json("http://x", {"a": 1}, retries=3, delay=0)
    assert resp.status == 204
