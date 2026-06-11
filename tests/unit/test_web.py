"""Tests for proxmox_fleet.web — the fleet dashboard (FastAPI app + RunManager).

Read-only pages are exercised with TestClient against fixture history/pending
dirs (written by the real history/scan writers); the run trigger is tested
with a stub subprocess command standing in for ``python -m proxmox_fleet.cli``.
Skipped wholesale when the ``[web]`` extra is not installed.
"""
import json
import os
import signal
import sys
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from proxmox_fleet import briefing  # noqa: E402
from proxmox_fleet import scan as scan_mod  # noqa: E402
from proxmox_fleet.history import write_history  # noqa: E402
from proxmox_fleet.lock import acquire_run_lock  # noqa: E402
from proxmox_fleet.models.settings import GlobalSettings  # noqa: E402
from proxmox_fleet.models.state import FleetState  # noqa: E402
from proxmox_fleet.web.app import build_run_args, create_app  # noqa: E402
from proxmox_fleet.web.runs import RunActive, RunManager  # noqa: E402


# --- fixtures --------------------------------------------------------------- #

def _state(**kw) -> FleetState:
    return FleetState.from_raw(kw)


def _seed_history(history_dir):
    """Two runs (older clean, newer failed) + one pending snapshot."""
    older = _state(fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101",
                                        app="Updated: v4.0 → v4.1", os="OK")],
                   fleet_changed=True)
    write_history(older, history_dir=history_dir, keep=0,
                  timestamp="20260101T000000000000Z",
                  briefing=briefing.prepare_body(older))
    newer = _state(fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101",
                                        app="FAILED + ROLLED BACK", os="OK")],
                   fleet_error_log=[dict(host="sonarr", task="app update", error="boom")],
                   fleet_changed=True, fleet_failed=True)
    write_history(newer, history_dir=history_dir, keep=0,
                  timestamp="20260102T000000000000Z",
                  briefing=briefing.prepare_body(newer))
    scan_mod.write_pending({
        "timestamp": "20260103T000000000000Z",
        "hosts": {"web-01": {"kind": "remote", "pkg_mgr": "apt",
                             "pending_count": 2, "pending": ["curl", "openssl"],
                             "error": None}},
        "lxc": {"101": {"node": "pve-01", "name": "sonarr", "skipped": None,
                        "os_pending_count": 1, "os_pending": ["libssl3"],
                        "app": {"script": "sonarr", "current": "4.0.17",
                                "latest": "4.0.18", "outdated": True},
                        "error": None}},
    }, history_dir=history_dir, keep=0)


@pytest.fixture
def history_dir(tmp_path):
    d = tmp_path / "history"
    _seed_history(d)
    return d


def _settings(history_dir, **kw) -> GlobalSettings:
    return GlobalSettings(fleet_history_dir=str(history_dir), **kw)


def _client(history_dir, *, run_manager=None, **kw) -> TestClient:
    app = create_app(_settings(history_dir, **kw), run_manager=run_manager)
    return TestClient(app)


def _wait_finished(manager, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = manager.read_meta(run_id)
        if meta["finished"] is not None:
            return meta
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


# --- build_run_args --------------------------------------------------------- #

def test_build_args_full_run_default():
    assert build_run_args({}) == []


def test_build_args_flags():
    args = build_run_args({"dry_run": "on", "force_notify": "on", "force_window": "on"})
    assert args == ["--check", "-e", "force_notify=true", "-e", "force_window=true"]


def test_build_args_limit_and_phases():
    args = build_run_args({"limit": "pve-01, 105", "phases": "lxc,vm"})
    assert args == ["--phases", "lxc,vm", "--limit", "pve-01,105"]


def test_build_args_scan_keeps_only_limit():
    args = build_run_args({"scan": "on", "dry_run": "on", "force_notify": "on",
                           "phases": "lxc", "limit": "105"})
    assert args == ["--scan", "--limit", "105"]


def test_build_args_rejects_shell_metacharacters():
    with pytest.raises(ValueError, match="invalid token"):
        build_run_args({"limit": "pve-01;rm -rf /"})


def test_build_args_rejects_unknown_phase():
    with pytest.raises(ValueError, match="unknown phase"):
        build_run_args({"phases": "lxc,bogus"})


# --- read-only pages --------------------------------------------------------- #

def test_index_shows_latest_run_and_pending(history_dir):
    resp = _client(history_dir).get("/")
    assert resp.status_code == 200
    assert "20260102T000000000000Z" in resp.text   # latest run
    assert "20260103T000000000000Z" in resp.text   # latest pending scan
    assert "FAILED" in resp.text


def test_index_renders_empty_dirs(tmp_path):
    resp = _client(tmp_path / "empty").get("/")
    assert resp.status_code == 200
    assert "No run history yet" in resp.text


def test_history_list_with_delta(history_dir):
    resp = _client(history_dir).get("/history")
    assert resp.status_code == 200
    assert "20260101T000000000000Z" in resp.text
    assert "20260102T000000000000Z" in resp.text
    assert "errors +1" in resp.text   # newer run vs older run


def test_history_detail_shows_records_and_briefing(history_dir):
    resp = _client(history_dir).get("/history/20260102T000000000000Z")
    assert resp.status_code == 200
    assert "FAILED + ROLLED BACK" in resp.text
    assert "boom" in resp.text
    assert "Briefing" in resp.text


def test_history_detail_unknown_404(history_dir):
    assert _client(history_dir).get("/history/nope").status_code == 404


def test_pending_page(history_dir):
    resp = _client(history_dir).get("/pending")
    assert resp.status_code == 200
    assert "web-01" in resp.text
    assert "curl" in resp.text
    assert "4.0.17" in resp.text and "4.0.18" in resp.text
    assert "outdated" in resp.text


def test_pending_unknown_ref_404(history_dir):
    assert _client(history_dir).get("/pending", params={"ref": "nope"}).status_code == 404


def test_host_drilldown_across_runs(history_dir):
    resp = _client(history_dir).get("/hosts/sonarr")
    assert resp.status_code == 200
    assert "Updated: v4.0 → v4.1" in resp.text       # older run record
    assert "FAILED + ROLLED BACK" in resp.text        # newer run record
    assert "boom" in resp.text                        # error record matches host


def test_host_drilldown_unknown_host_is_empty(history_dir):
    resp = _client(history_dir).get("/hosts/ghost")
    assert resp.status_code == 200
    assert "No records" in resp.text


def test_trigger_page(history_dir):
    resp = _client(history_dir).get("/trigger")
    assert resp.status_code == 200
    assert "--scan" in resp.text


# --- run trigger auth + argv composition ------------------------------------- #

class FakeManager:
    """Records start() args; canned list/meta for page rendering."""

    def __init__(self, fail_active=False):
        self.started = []
        self.fail_active = fail_active

    def start(self, args):
        if self.fail_active:
            raise RunActive("dashboard run X is still in flight")
        self.started.append(list(args))
        return "RUNID"

    def active_run(self):
        return None

    def list_runs(self, limit=20):
        return []

    def read_meta(self, run_id):
        return {"id": run_id, "argv": [], "args": [], "started": run_id,
                "pid": 0, "finished": run_id, "rc": 0}


def test_trigger_requires_token_when_configured(history_dir):
    client = _client(history_dir, run_manager=FakeManager(), dashboard_token="sekrit")
    assert client.post("/runs", data={}).status_code == 401
    assert client.post("/runs", data={"token": "wrong"}).status_code == 401


def test_trigger_accepts_bearer_header(history_dir):
    fake = FakeManager()
    client = _client(history_dir, run_manager=fake, dashboard_token="sekrit")
    resp = client.post("/runs", data={"dry_run": "on"},
                       headers={"Authorization": "Bearer sekrit"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/runs/RUNID"
    assert fake.started == [["--check"]]


def test_trigger_accepts_form_token(history_dir):
    fake = FakeManager()
    client = _client(history_dir, run_manager=fake, dashboard_token="sekrit")
    resp = client.post("/runs", data={"token": "sekrit", "scan": "on"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert fake.started == [["--scan"]]


def test_trigger_open_when_no_token_configured(history_dir):
    fake = FakeManager()
    client = _client(history_dir, run_manager=fake)
    assert client.post("/runs", data={}, follow_redirects=False).status_code == 303


def test_trigger_invalid_args_400(history_dir):
    client = _client(history_dir, run_manager=FakeManager())
    assert client.post("/runs", data={"phases": "bogus"}).status_code == 400


def test_trigger_conflict_409_when_run_active(history_dir):
    client = _client(history_dir, run_manager=FakeManager(fail_active=True))
    assert client.post("/runs", data={}).status_code == 409


# --- RunManager --------------------------------------------------------------- #

def _manager(tmp_path, code):
    """A RunManager whose 'CLI' is an inline python script (args are ignored)."""
    return RunManager(tmp_path, command=[sys.executable, "-c", code])


def test_runmanager_runs_and_records_rc(tmp_path):
    mgr = _manager(tmp_path, "print('alpha'); print('beta')")
    run_id = mgr.start(["--check"])
    meta = _wait_finished(mgr, run_id)
    assert meta["rc"] == 0
    assert meta["args"] == ["--check"]
    log = mgr.read_log(run_id)
    assert "alpha" in log and "beta" in log


def test_runmanager_records_nonzero_rc(tmp_path):
    mgr = _manager(tmp_path, "import sys; sys.exit(3)")
    run_id = mgr.start([])
    assert _wait_finished(mgr, run_id)["rc"] == 3


def test_runmanager_refuses_overlapping_child(tmp_path):
    mgr = _manager(tmp_path, "import time; time.sleep(30)")
    run_id = mgr.start([])
    try:
        with pytest.raises(RunActive, match="in flight"):
            mgr.start([])
    finally:
        os.kill(mgr.read_meta(run_id)["pid"], signal.SIGTERM)
        _wait_finished(mgr, run_id)


def test_runmanager_refuses_when_fleet_lock_held(tmp_path):
    mgr = _manager(tmp_path, "print('hi')")
    with acquire_run_lock(tmp_path):
        with pytest.raises(RunActive, match="lock held"):
            mgr.start([])
    # lock released → start succeeds
    _wait_finished(mgr, mgr.start([]))


def test_runmanager_finalizes_orphaned_meta(tmp_path):
    """A meta whose pid is dead but never finalized (dashboard restarted
    mid-run) is closed out on read: finished set, rc stays None."""
    mgr = _manager(tmp_path, "print('hi')")
    mgr.runs_dir.mkdir(parents=True)
    (mgr.runs_dir / "ORPHan.json").write_text(json.dumps({
        "id": "ORPHan", "argv": [], "args": [], "started": "T",
        "pid": 2 ** 22 + 12345, "finished": None, "rc": None,
    }), encoding="utf-8")
    meta = mgr.read_meta("ORPHan")
    assert meta["finished"] is not None
    assert meta["rc"] is None
    assert mgr.active_run() is None


def test_runmanager_stream_replays_lines_then_done(tmp_path):
    mgr = _manager(tmp_path, "print('one'); print('two')")
    run_id = mgr.start([])
    _wait_finished(mgr, run_id)
    events = list(mgr.stream(run_id, sleep=lambda s: None))
    lines = [e["data"] for e in events if e["event"] == "line"]
    assert lines == ["one", "two"]
    assert events[-1] == {"event": "done", "data": "0"}


def test_runmanager_stream_unknown_id_raises(tmp_path):
    mgr = _manager(tmp_path, "print('hi')")
    with pytest.raises(FileNotFoundError):
        list(mgr.stream("nope"))


# --- end-to-end: trigger → console → SSE -------------------------------------- #

def test_trigger_to_sse_roundtrip(history_dir, tmp_path):
    mgr = RunManager(tmp_path / "runs-home",
                     command=[sys.executable, "-c", "print('fleet says hello')"])
    client = _client(history_dir, run_manager=mgr)

    resp = client.post("/runs", data={"dry_run": "on"}, follow_redirects=False)
    assert resp.status_code == 303
    run_id = resp.headers["location"].rsplit("/", 1)[1]
    _wait_finished(mgr, run_id)

    console = client.get(f"/runs/{run_id}")
    assert console.status_code == 200
    assert f"/runs/{run_id}/stream" in console.text

    stream = client.get(f"/runs/{run_id}/stream")
    assert stream.status_code == 200
    assert "data: fleet says hello" in stream.text
    assert "event: done" in stream.text
    assert "data: 0" in stream.text

    log = client.get(f"/runs/{run_id}/log")
    assert log.status_code == 200
    assert "fleet says hello" in log.text


def test_console_unknown_run_404(history_dir):
    client = _client(history_dir)   # default RunManager over the history dir
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope/stream").status_code == 404
    assert client.get("/runs/nope/log").status_code == 404
