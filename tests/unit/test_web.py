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
from proxmox_fleet.web import auth  # noqa: E402
from proxmox_fleet.web.app import (  # noqa: E402
    _activity_weeks,
    _endpoint_counts,
    _health_score,
    build_run_args,
    create_app,
    spark_points,
    ts_human,
    ts_span,
    ts_iso,
)
from proxmox_fleet.web.runs import RunActive, RunManager  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_auth_state():
    """auth holds module-level engine/secret state — isolate per test."""
    auth.reset_for_tests()
    yield
    auth.reset_for_tests()


def _logged_in(app):
    """Bypass the session check — these tests cover the pages, not the login
    flow (test_web_auth.py exercises the real cookie auth)."""
    app.dependency_overrides[auth.current_active_user] = lambda: None
    return app


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


def _client(history_dir, *, run_manager=None, login=True, **kw) -> TestClient:
    app = create_app(_settings(history_dir, **kw), run_manager=run_manager)
    if login:
        _logged_in(app)
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


_INVENTORY = """\
[proxmox_nodes]
pve-01
pve-02

[proxmox_vms]
vm-01 vmid=200

[remote_hosts]
web-01

[custom_hosts]
"""


def test_endpoint_counts_from_inventory_and_scan(tmp_path):
    inv = tmp_path / "hosts.ini"
    inv.write_text(_INVENTORY, encoding="utf-8")
    pending = {"lxc": {"101": {}, "102": {}}, "hosts": {}}
    eps = {e["label"]: e["value"] for e in _endpoint_counts(str(inv), pending)}
    assert eps == {"LXCs": 2, "VMs": 1, "PVE hosts": 2,
                   "remote hosts": 1, "custom systems": 0}


def test_endpoint_counts_missing_inventory_and_scan(tmp_path):
    eps = _endpoint_counts(str(tmp_path / "nope.ini"), None)
    assert {e["label"] for e in eps} == {"LXCs", "VMs", "PVE hosts",
                                         "remote hosts", "custom systems"}
    assert all(e["value"] == 0 for e in eps)


def test_index_shows_endpoints_and_update_trends(history_dir, tmp_path):
    inv = tmp_path / "hosts.ini"
    inv.write_text(_INVENTORY, encoding="utf-8")
    app = create_app(_settings(history_dir), inventory_path=str(inv))
    _logged_in(app)
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "Update endpoints" in resp.text
    assert "PVE hosts" in resp.text and "custom systems" in resp.text
    # the error-trend card is gone; the update-trends card replaces it
    assert "Error trend" not in resp.text
    assert "Update trends" in resp.text
    # all-time counters (seeded history: one app update, no pkg counts)
    assert "LXC app updates · all time" in resp.text
    assert "pkgs upgraded · all time" in resp.text
    assert "runs recorded" in resp.text
    # one combined chart with a JSON data island for the hover layer
    assert "trend-chart" in resp.text
    assert 'id="trend-data"' in resp.text
    assert "chart-legend" in resp.text
    # timestamps carry data-utc so dashboard.js can localize them
    assert 'class="ts" data-utc="' in resp.text


def test_static_assets_must_revalidate(history_dir):
    # no-cache stops browsers heuristically serving stale CSS/JS after a
    # deploy; the ETag keeps revalidation a cheap 304
    resp = _client(history_dir).get("/static/dashboard.css")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert "etag" in resp.headers


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


def test_trigger_requires_login(history_dir):
    fake = FakeManager()
    client = _client(history_dir, run_manager=fake, login=False)
    assert client.post("/runs", data={}).status_code == 401
    assert fake.started == []


def test_trigger_starts_run_when_logged_in(history_dir):
    fake = FakeManager()
    client = _client(history_dir, run_manager=fake)
    resp = client.post("/runs", data={"dry_run": "on"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/runs/RUNID"
    assert fake.started == [["--check"]]


def test_trigger_scan_when_logged_in(history_dir):
    fake = FakeManager()
    client = _client(history_dir, run_manager=fake)
    resp = client.post("/runs", data={"scan": "on"}, follow_redirects=False)
    assert resp.status_code == 303
    assert fake.started == [["--scan"]]


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


def test_runmanager_astream_matches_stream(tmp_path):
    """The async tail (SSE endpoint) must yield the same events as stream()."""
    import asyncio

    mgr = _manager(tmp_path, "print('one'); print('two')")
    run_id = mgr.start([])
    _wait_finished(mgr, run_id)

    async def _collect():
        return [event async for event in mgr.astream(run_id, poll=0.01)]

    events = asyncio.run(_collect())
    assert events == list(mgr.stream(run_id, sleep=lambda s: None))
    assert events[-1] == {"event": "done", "data": "0"}


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


# --- inventory enrollment + settings + ssh ------------------------------------ #

import subprocess  # noqa: E402

from proxmox_fleet.web import sshsetup  # noqa: E402
from proxmox_fleet.web.sshsetup import SshSetupError, push_key  # noqa: E402
from proxmox_fleet.web.sshsetup import test_key as ssh_test_key  # noqa: E402

INI = """\
[proxmox_nodes]
pve-01 ansible_host=10.0.0.10

[proxmox_vms]

[remote_hosts]

[custom_hosts]
"""

VARS = """\
# hand-written comment that must survive
lxc_forks: 20
canary_hosts: []
"""


@pytest.fixture
def project(tmp_path):
    """Temp project dir: hosts.ini + vars.yml + seeded history."""
    (tmp_path / "hosts.ini").write_text(INI)
    (tmp_path / "vars.yml").write_text(VARS)
    _seed_history(tmp_path / "history")
    return tmp_path


def _project_client(project, *, login=True, **kw) -> TestClient:
    settings = GlobalSettings(fleet_history_dir=str(project / "history"),
                              host_vars_dir=str(project / "host_vars"), **kw)
    app = create_app(settings, vars_path=str(project / "vars.yml"),
                     inventory_path=str(project / "hosts.ini"))
    if login:
        _logged_in(app)
    return TestClient(app)


def test_inventory_page_lists_groups(project):
    resp = _project_client(project).get("/inventory")
    assert resp.status_code == 200
    assert "proxmox_nodes" in resp.text
    assert "pve-01" in resp.text
    assert "Enroll a host" in resp.text


def test_inventory_mutations_require_login(project):
    client = _project_client(project, login=False)
    assert client.post("/inventory/add", data={"group": "remote_hosts",
                                               "name": "x"}).status_code == 401
    assert client.post("/inventory/remove", data={}).status_code == 401
    assert client.post("/settings", data={}).status_code == 401
    assert client.post("/settings/raw", data={}).status_code == 401
    assert client.post("/ssh/push", data={}).status_code == 401


def test_enroll_vm_writes_ini_and_vars(project):
    client = _project_client(project)
    resp = client.post("/inventory/add", data={
        "group": "proxmox_vms", "name": "media-vm",
        "ansible_host": "10.0.0.50", "vmid": "200", "pve_node": "pve-01",
        "canary": "true", "kuma_id": "7",
    }, follow_redirects=False)
    assert resp.status_code == 303
    ini = (project / "hosts.ini").read_text()
    assert "media-vm" in ini and "vmid=200" in ini and "canary=true" in ini
    vars_text = (project / "vars.yml").read_text()
    assert "# hand-written comment that must survive" in vars_text
    assert "vm_kuma_map" in vars_text and "'200': '7'" in vars_text.replace('"', "'")
    # and the page reflects it
    page = client.get("/inventory").text
    assert "media-vm" in page and "canary" in page


def test_enroll_with_maintenance_window_writes_host_vars(project):
    client = _project_client(project)
    resp = client.post("/inventory/add", data={
        "group": "remote_hosts", "name": "web-01", "ansible_host": "10.0.0.5",
        "pre_update_cmd": "systemctl stop app",
        "mw_days": "Sat,Sun", "mw_start": "02:00", "mw_end": "05:00",
    }, follow_redirects=False)
    assert resp.status_code == 303
    import yaml as _yaml
    hv = _yaml.safe_load((project / "host_vars" / "web-01.yml").read_text())
    assert hv["pre_update_cmd"] == "systemctl stop app"
    assert hv["maintenance_window"] == {"days": ["Sat", "Sun"],
                                        "start": "02:00", "end": "05:00"}


def test_enroll_validation_errors_400(project):
    client = _project_client(project)
    # custom host without custom_config
    assert client.post("/inventory/add", data={
        "group": "custom_hosts", "name": "db-01", "ansible_host": "10.0.0.9",
    }).status_code == 400
    # vm without vmid
    assert client.post("/inventory/add", data={
        "group": "proxmox_vms", "name": "v", "ansible_host": "10.0.0.9",
        "pve_node": "pve-01",
    }).status_code == 400
    # bad maintenance window key time format is accepted as str, but bad day
    # set is not validated here — bad host name is:
    assert client.post("/inventory/add", data={
        "group": "remote_hosts", "name": "bad name", "ansible_host": "10.0.0.9",
    }).status_code == 400
    # duplicate
    assert client.post("/inventory/add", data={
        "group": "proxmox_nodes", "name": "pve-01", "ansible_host": "10.0.0.9",
    }).status_code == 400


def test_remove_with_cleanup(project):
    client = _project_client(project)
    client.post("/inventory/add", data={
        "group": "proxmox_vms", "name": "media-vm", "ansible_host": "10.0.0.50",
        "vmid": "200", "pve_node": "pve-01", "kuma_id": "7",
        "mw_days": "Sat",
    }, follow_redirects=False)
    resp = client.post("/inventory/remove", data={
        "group": "proxmox_vms", "name": "media-vm", "cleanup": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "media-vm" not in (project / "hosts.ini").read_text()
    import yaml as _yaml
    data = _yaml.safe_load((project / "vars.yml").read_text())
    assert data.get("vm_kuma_map", {}) == {}
    assert not (project / "host_vars" / "media-vm.yml").exists()


def test_remove_missing_400(project):
    assert _project_client(project).post("/inventory/remove", data={
        "group": "remote_hosts", "name": "ghost"}).status_code == 400


def test_inventory_bootstrap_when_missing(project):
    (project / "hosts.ini").unlink()
    client = _project_client(project)
    page = client.get("/inventory").text
    assert "Create hosts.ini" in page
    assert client.post("/inventory/create", data={},
                       follow_redirects=False).status_code == 303
    assert "[proxmox_vms]" in (project / "hosts.ini").read_text()


def test_settings_page_and_diffed_save(project):
    client = _project_client(project)
    page = client.get("/settings").text
    assert "lxc_forks" in page and "discord_webhook" in page
    resp = client.post("/settings", data={
        "lxc_forks": "5",            # changed
        "vm_forks": "2",             # unchanged (default)
        "lxc_dry_run": "false",      # unchanged
        "discord_webhook": "",       # secret left blank → keep
        "canary_hosts": "105\nmedia-vm",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "lxc_forks" in resp.headers["location"]
    assert "vm_forks" not in resp.headers["location"]
    import yaml as _yaml
    data = _yaml.safe_load((project / "vars.yml").read_text())
    assert data["lxc_forks"] == 5
    assert data["canary_hosts"] == ["105", "media-vm"]
    assert "vm_forks" not in data            # untouched fields never written
    assert "discord_webhook" not in data     # blank secret kept
    text = (project / "vars.yml").read_text()
    assert "# hand-written comment that must survive" in text


def test_settings_invalid_value_400(project):
    assert _project_client(project).post("/settings", data={
        "lxc_forks": "banana"}).status_code == 400


def test_settings_raw_replace_and_reject(project):
    client = _project_client(project)
    assert client.post("/settings/raw", data={
        "content": "key: [unclosed"}).status_code == 400
    assert client.post("/settings/raw", data={
        "content": "lxc_forks: nope"}).status_code == 400
    resp = client.post("/settings/raw", data={
        "content": "# replaced\nlxc_forks: 9\n"}, follow_redirects=False)
    assert resp.status_code == 303
    assert (project / "vars.yml").read_text() == "# replaced\nlxc_forks: 9\n"
    backups = list(project.glob("vars.yml.bak-*"))
    assert backups and "hand-written comment" in backups[-1].read_text()


class _FakeRun:
    def __init__(self, rc=0, out="ok\n"):
        self.rc, self.out, self.calls = rc, out, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.rc, stdout=self.out, stderr="")


def test_push_key_password_via_env_never_argv(tmp_path):
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAA fleet-manager\n")
    fake = _FakeRun()
    ok, out = push_key("10.0.0.5", "root", "s3cret!", port=2222,
                       pubkey_path=str(pub), run=fake)
    assert ok and out == "ok\n"
    argv, kw = fake.calls[0]
    assert argv[0] == "ssh-copy-id"
    assert "root@10.0.0.5" in argv and "2222" in argv
    assert all("s3cret!" not in a for a in argv)         # never on the cmdline
    assert kw["env"]["FLEET_SSH_PW"] == "s3cret!"        # only in the env
    assert kw["env"]["SSH_ASKPASS_REQUIRE"] == "force"
    assert kw["start_new_session"] is True


def test_push_key_validation():
    with pytest.raises(SshSetupError):
        push_key("bad host;rm", "root", "pw", run=_FakeRun())
    with pytest.raises(SshSetupError):
        push_key("10.0.0.5", "root;x", "pw", run=_FakeRun())
    with pytest.raises(SshSetupError, match="password"):
        push_key("10.0.0.5", "root", "", run=_FakeRun())
    with pytest.raises(SshSetupError, match="port"):
        push_key("10.0.0.5", "root", "pw", port="99999", run=_FakeRun())


def test_test_key_batchmode(tmp_path):
    fake = _FakeRun(rc=0, out="")
    ok, out = ssh_test_key("10.0.0.5", "root", key_path=str(tmp_path / "nokey"), run=fake)
    assert ok and "OK" in out
    argv, _ = fake.calls[0]
    assert "BatchMode=yes" in argv and argv[-1] == "true"
    fake_fail = _FakeRun(rc=255, out="Permission denied")
    ok, out = ssh_test_key("10.0.0.5", "root", run=fake_fail)
    assert not ok and "Permission denied" in out


def test_ssh_routes_render_result(project, monkeypatch):
    monkeypatch.setattr(sshsetup, "push_key",
                        lambda *a, **kw: (True, "key installed"))
    monkeypatch.setattr(sshsetup, "test_key",
                        lambda *a, **kw: (False, "Permission denied"))
    client = _project_client(project)
    resp = client.post("/ssh/push", data={"host": "10.0.0.5", "user": "root",
                                          "password": "pw"})
    assert resp.status_code == 200
    assert "key installed" in resp.text and "push succeeded" in resp.text
    resp = client.post("/ssh/test", data={"host": "10.0.0.5"})
    assert resp.status_code == 200
    assert "test failed" in resp.text and "Permission denied" in resp.text


def test_ssh_push_invalid_host_400(project):
    assert _project_client(project).post("/ssh/push", data={
        "host": "bad;host", "password": "pw"}).status_code == 400


# --- template helpers (filters + flare) ---------------------------------------- #

def test_ts_human_and_iso():
    assert ts_human("20260102T153045123456Z") == "2026-01-02 15:30 UTC"
    assert ts_iso("20260102T153045123456Z") == "2026-01-02T15:30:45Z"
    # malformed input falls back loudly-visible but never raises
    assert ts_human("not-a-ts") == "not-a-ts"
    assert ts_iso("not-a-ts") == ""


def test_ts_span_wraps_for_client_localization():
    html = str(ts_span("20260102T153045123456Z"))
    assert html == ('<span class="ts" data-utc="2026-01-02T15:30:45Z">'
                    "2026-01-02 15:30 UTC</span>")
    # malformed input degrades to plain escaped text, no span
    assert str(ts_span("<not-a-ts>")) == "&lt;not-a-ts&gt;"


def test_spark_points():
    pts = spark_points([0, 5, 10], 100, 20)
    pairs = [tuple(float(n) for n in p.split(",")) for p in pts.split()]
    assert len(pairs) == 3
    assert pairs[0][0] < pairs[1][0] < pairs[2][0]   # x advances
    assert pairs[0][1] > pairs[2][1]                 # larger value plots higher
    assert spark_points([]) == ""
    assert len(spark_points([7]).split()) == 2       # single value → flat line
    assert spark_points(["a", "b"]) == ""            # non-numeric → empty


def test_health_score():
    assert _health_score(None, None) == 100
    failed = {"failed": True, "counts": {"errors": 2, "warnings": 1}}
    assert _health_score(failed, None) == 100 - 25 - 10 - 2
    assert _health_score(None, {"outdated_apps": 3}) == 97
    floor = {"failed": True, "counts": {"errors": 50, "warnings": 0}}
    assert _health_score(floor, None) == 0   # clamped


def test_activity_weeks_grid_shape_and_levels():
    from datetime import date
    rows = [
        {"timestamp": "20260601T000000000000Z", "failed": False},
        {"timestamp": "20260601T120000000000Z", "failed": True},
        {"timestamp": "20260603T000000000000Z", "failed": False},
        {"timestamp": "bogus", "failed": False},   # ignored, never raises
    ]
    grid = _activity_weeks(rows, weeks=4, today=date(2026, 6, 11))
    assert len(grid) == 4 and all(len(week) == 7 for week in grid)
    by_date = {day["date"]: day for week in grid for day in week}
    assert by_date["2026-06-01"]["count"] == 2
    assert by_date["2026-06-01"]["failed"] is True
    assert by_date["2026-06-01"]["level"] == 2
    assert by_date["2026-06-03"]["level"] == 1
    assert by_date["2026-06-02"]["count"] == 0
    assert by_date["2026-06-12"]["future"] is True   # day after `today`
    # last column contains today
    assert any(day["date"] == "2026-06-11" for day in grid[-1])


def test_index_renders_health_gauge_and_pulse_strip(history_dir):
    resp = _client(history_dir).get("/")
    assert resp.status_code == 200
    assert "Fleet health" in resp.text
    assert "pulse-strip" in resp.text
    assert "palette-data" in resp.text   # command-palette blob on every page


def test_history_renders_activity_heatmap(history_dir):
    resp = _client(history_dir).get("/history")
    assert resp.status_code == 200
    assert "Activity" in resp.text
    assert "heatmap" in resp.text
