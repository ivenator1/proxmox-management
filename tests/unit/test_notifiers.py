"""Tests for proxmox_fleet.notifiers — the Python port of tasks/notify.yml + the
Phase-4 notifier-resolution shim.

HTTP is monkeypatched at ``proxmox_fleet.http`` so no network is touched.
"""
import pytest

from proxmox_fleet import http, notifiers
from proxmox_fleet.models.settings import GlobalSettings


class _Resp:
    def __init__(self, status=200):
        self.status = status


@pytest.fixture
def captured(monkeypatch):
    calls = {"post_json": [], "request": []}

    def fake_post_json(url, payload, **kw):
        calls["post_json"].append({"url": url, "payload": payload, "kw": kw})
        return _Resp(204)

    def fake_request(url, **kw):
        calls["request"].append({"url": url, "kw": kw})
        return _Resp(200)

    monkeypatch.setattr(http, "post_json", fake_post_json)
    monkeypatch.setattr(http, "request", fake_request)
    return calls


# --- resolve_notifiers (back-compat shim) ---------------------------------- #

def test_explicit_notifiers_passed_through():
    n = [{"type": "ntfy", "enabled": True, "url": "https://ntfy.sh/x"}]
    settings = GlobalSettings(notifiers=n, discord_webhook="ignored")
    assert notifiers.resolve_notifiers(settings) == n


def test_shim_synthesizes_discord_from_webhook():
    settings = GlobalSettings(discord_webhook="https://discord.test/hook")
    assert notifiers.resolve_notifiers(settings) == [
        {"type": "discord", "enabled": True, "webhook": "https://discord.test/hook"}
    ]


def test_shim_empty_when_no_webhook_and_no_notifiers():
    assert notifiers.resolve_notifiers(GlobalSettings()) == []


def test_explicit_empty_list_wins_over_webhook():
    # An explicit [] means "no notifiers" — it must not fall back to the webhook.
    settings = GlobalSettings(notifiers=[], discord_webhook="https://discord.test/hook")
    assert notifiers.resolve_notifiers(settings) == []


# --- dispatch: discord ----------------------------------------------------- #

def test_dispatch_discord_payload(captured):
    notifiers.dispatch(
        [{"type": "discord", "enabled": True, "webhook": "https://d/hook"}],
        title="T", ntfy_title="NT", body="BODY", color=3066993, failed=False,
    )
    assert len(captured["post_json"]) == 1
    call = captured["post_json"][0]
    assert call["url"] == "https://d/hook"
    assert call["payload"] == {"embeds": [{"title": "T", "description": "BODY", "color": 3066993}]}


def test_dispatch_skips_disabled(captured):
    notifiers.dispatch(
        [{"type": "discord", "enabled": False, "webhook": "https://d/hook"}],
        title="T", ntfy_title="NT", body="B", color=1, failed=False,
    )
    assert captured["post_json"] == []


# --- dispatch: ntfy headers ------------------------------------------------ #

def test_dispatch_ntfy_headers_on_failure(captured):
    notifiers.dispatch(
        [{"type": "ntfy", "enabled": True, "url": "https://ntfy/x"}],
        title="T", ntfy_title="Fleet Update: Failures Detected", body="B",
        color=1, failed=True,
    )
    call = captured["request"][0]
    h = call["kw"]["headers"]
    assert call["url"] == "https://ntfy/x"
    assert call["kw"]["method"] == "POST"
    assert call["kw"]["data"] == b"B"
    assert h["Title"] == "Fleet Update: Failures Detected"
    assert h["Priority"] == "5"
    assert h["Tags"] == "warning"
    assert h["Markdown"] == "yes"
    assert "Authorization" not in h


def test_dispatch_ntfy_headers_on_success(captured):
    notifiers.dispatch(
        [{"type": "ntfy", "enabled": True, "url": "https://ntfy/x"}],
        title="T", ntfy_title="NT", body="B", color=1, failed=False,
    )
    h = captured["request"][0]["kw"]["headers"]
    assert h["Priority"] == "3"
    assert h["Tags"] == "white_check_mark"


def test_dispatch_ntfy_explicit_overrides_and_token(captured):
    notifiers.dispatch(
        [{"type": "ntfy", "enabled": True, "url": "https://ntfy/x",
          "priority": 4, "tags": "package", "token": "tk_abc"}],
        title="T", ntfy_title="NT", body="B", color=1, failed=False,
    )
    h = captured["request"][0]["kw"]["headers"]
    assert h["Priority"] == "4"
    assert h["Tags"] == "package"
    assert h["Authorization"] == "Bearer tk_abc"


def test_dispatch_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(http, "post_json", boom)
    # Must not raise.
    notifiers.dispatch(
        [{"type": "discord", "enabled": True, "webhook": "https://d/hook"}],
        title="T", ntfy_title="NT", body="B", color=1, failed=False,
    )


# --- ping_deadmans --------------------------------------------------------- #

def test_ping_deadmans_ok_no_fail_suffix(captured):
    notifiers.ping_deadmans("https://hc.io/abc", failed=False)
    assert captured["request"][0]["url"] == "https://hc.io/abc"
    assert captured["request"][0]["kw"]["method"] == "GET"


def test_ping_deadmans_appends_fail(captured):
    notifiers.ping_deadmans("https://hc.io/abc", failed=True)
    assert captured["request"][0]["url"] == "https://hc.io/abc/fail"


def test_ping_deadmans_noop_when_empty(captured):
    notifiers.ping_deadmans("", failed=False)
    notifiers.ping_deadmans("   ", failed=True)
    assert captured["request"] == []


def test_ping_deadmans_swallows_errors(monkeypatch):
    # Patch retry to run the fn once (no 5x10s sleep) so the raised error
    # surfaces immediately and ping_deadmans' try/except must swallow it.
    monkeypatch.setattr(notifiers, "retry", lambda fn, **kw: fn())
    monkeypatch.setattr(http, "request", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    notifiers.ping_deadmans("https://hc.io/abc", failed=False)  # must not raise
