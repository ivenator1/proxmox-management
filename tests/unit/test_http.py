"""Tests for proxmox_fleet.http — manager-local IO helpers (no real network)."""
import socket
import ssl
import urllib.request

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


class _FakeResponse:
    """Minimal urlopen return value: enough to satisfy http.request."""

    def __init__(self, status=200, body="{}"):
        self.status = status
        self.body = body
        self.headers = {"content-type": "application/json"}

    def read(self):
        return self.body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_urlopen(monkeypatch, calls):
    """Replace urllib.request.urlopen with a recorder; return the fake response."""
    fake = _FakeResponse()

    def fake_urlopen(req, timeout=None, context=None):
        calls.append({"req": req, "timeout": timeout, "context": context})
        return fake

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return fake


def test_request_verify_default_uses_verified_context(monkeypatch):
    calls = []
    _capture_urlopen(monkeypatch, calls)

    http.request("https://example.test")

    assert len(calls) == 1
    ctx = calls[0]["context"]
    # Default behavior must keep verifying: either no context (urllib's
    # default) or an explicitly created context that still verifies.
    assert ctx is None or ctx.verify_mode != ssl.CERT_NONE


def test_request_verify_false_uses_unverified_context(monkeypatch):
    calls = []
    _capture_urlopen(monkeypatch, calls)

    http.request("https://example.test", verify=False)

    assert len(calls) == 1
    ctx = calls[0]["context"]
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_get_json_verify_false_propagates(monkeypatch):
    calls = []
    _capture_urlopen(monkeypatch, calls)

    http.get_json("https://example.test", verify=False)

    assert len(calls) == 1
    assert calls[0]["context"].verify_mode == ssl.CERT_NONE
    assert calls[0]["context"].check_hostname is False


def test_post_json_verify_false_propagates(monkeypatch):
    calls = []
    _capture_urlopen(monkeypatch, calls)

    http.post_json("https://example.test", {}, verify=False)

    assert len(calls) == 1
    assert calls[0]["context"].verify_mode == ssl.CERT_NONE
    assert calls[0]["context"].check_hostname is False


def test_get_json_default_still_verifies(monkeypatch):
    calls = []
    _capture_urlopen(monkeypatch, calls)

    http.get_json("https://example.test")

    assert len(calls) == 1
    ctx = calls[0]["context"]
    assert ctx is None or ctx.verify_mode != ssl.CERT_NONE


def test_post_json_default_still_verifies(monkeypatch):
    calls = []
    _capture_urlopen(monkeypatch, calls)

    http.post_json("https://example.test", {})

    assert len(calls) == 1
    ctx = calls[0]["context"]
    assert ctx is None or ctx.verify_mode != ssl.CERT_NONE
